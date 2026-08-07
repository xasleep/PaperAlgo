import json
import os
import argparse
from collections.abc import Mapping
from evaluation_contract import (
    build_evaluation_error_result,
    build_quality_result,
    classify_evaluation_exception,
    legacy_status_from_result,
    resolve_files_to_fix,
)
from task_manifest import TaskManifestError, load_task_manifest, read_manifest_text_files
from openai import BadRequestError, PermissionDeniedError
from provider_registry import get_provider_registry
from utils import (
    num_tokens_from_messages,
    read_all_files,
    extract_json_from_string,
    get_now_str,
    print_log_cost,
    make_openai_client,
    load_paper_content,
    MAX_REPAIR_ROUNDS,
    eval_feedback_path,
    load_json_file,
    repo_status_path,
    save_json_file,
    summarize_eval_feedback,
    write_repo_status,
)

client = None
MIN_GENERATED_N = 1
MAX_GENERATED_N = 32
MAX_REPAIR_ROUNDS_LIMIT = 10
FALLBACK_REASON_QUOTA = "quota_like_error"


class EvaluationProviderResponseError(RuntimeError):
    """Stable evaluation error for malformed provider response cardinality."""

    code = "provider_response_choice_count_mismatch"

    def __init__(self, provider_id, model_id, requested_n, returned_n):
        super().__init__(
            "Provider/model returned fewer choices than requested "
            f"(provider_id={provider_id}, model_id={model_id}, "
            f"requested_n={requested_n}, returned_n={returned_n})."
        )
        self.provider_id = provider_id
        self.model_id = model_id
        self.requested_n = requested_n
        self.returned_n = returned_n


class EvaluationProviderUsageError(RuntimeError):
    """Stable evaluation error for malformed provider usage metadata."""

    code = "provider_response_usage_invalid"

    def __init__(self, provider_id, model_id):
        super().__init__(
            "Provider/model returned invalid usage metadata "
            f"(provider_id={provider_id}, model_id={model_id})."
        )
        self.provider_id = provider_id
        self.model_id = model_id


def api_call(request_json):
    if client is None:
        raise RuntimeError("API client has not been initialized.")
    completion = client.chat.completions.create(**request_json)
    return completion


def model_max_choices_per_request(provider_id, model_name):
    return get_provider_registry().get(provider_id, model_name).max_n


def build_request_json(provider_id, gpt_version, msg, generated_n):
    contract = get_provider_registry().get(provider_id, gpt_version)
    request_json = {
        "model": gpt_version,
        "messages": msg,
        **contract.request_options,
    }
    if generated_n != 1:
        request_json["n"] = generated_n
    return request_json


def default_fallback_models(provider_id, model_name):
    contract = get_provider_registry().get(provider_id, model_name)
    return list(contract.fallback_model_ids)


def parse_fallback_models(value):
    if value == "":
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return value.split(",")
    raise ValueError("fallback_gpt_versions must be a string or list.")


def validate_eval_args(args):
    if args.generated_n < MIN_GENERATED_N or args.generated_n > MAX_GENERATED_N:
        raise ValueError(
            f"generated_n must be between {MIN_GENERATED_N} and {MAX_GENERATED_N}."
        )
    if args.max_repair_rounds < 0 or args.max_repair_rounds > MAX_REPAIR_ROUNDS_LIMIT:
        raise ValueError(
            f"max_repair_rounds must be between 0 and {MAX_REPAIR_ROUNDS_LIMIT}."
        )


def is_quota_fallback_error(error):
    message = str(error).lower()
    status_code = getattr(error, "status_code", None)
    if "allocationquota.freetieronly" in message:
        return True
    if status_code == 403 and any(
        marker in message
        for marker in ["free quota", "free tier", "quota", "insufficient"]
    ):
        return True
    return False


def build_domain_eval_instruction(domain):
    if domain != "statistics":
        return ""

    return """

---

Domain-Specific Evaluation Instructions for Statistics/Econometrics Papers:

Evaluate the repository as a statistics/econometrics paper reproduction, not as a deep-learning training project. Do not penalize the repository for missing trainer.py, dataset_loader.py, neural-network epochs, mini-batch training, or dataset-loader abstractions unless the paper explicitly requires them.

Focus correctness assessment on:
1. Numerical simulation / DGP: Does simulation.R generate data according to the exact data-generating process and scenarios in the paper, including true parameters, sample sizes, burn-in, noise/distribution assumptions, dependence structures, censoring/missingness, and replication counts?
2. Parameter estimation methods: Does estimators.R correctly implement every estimator described in the paper, such as MLE, CLS, GMM, EM, Bayesian estimation, method of moments, penalized estimation, or custom estimating equations? Check objective functions, initialization, constraints, optimization, parameter checks, safe likelihood/probability calculations, standard errors, confidence intervals, and convergence diagnostics.
3. Metrics and reported tables: Does experiments.R define and compute the paper's reported metrics, such as empirical mean, bias, variance, RMSE, MAE, coverage probability, confidence interval length, selection accuracy, convergence rate, or custom statistics? It should briefly comment the metric formulas before the main simulation or empirical-analysis body.
4. Experiment orchestration: Does experiments.R reproduce the Monte Carlo design or empirical-data fitting workflow, estimator comparisons, scenario grids, seed records, aggregation logic, and result-table outputs described by the paper?
5. Main entry point: Does main.R begin with two separated numbered comment sections headed exactly '### 1. Paper details requiring assumptions:' and '### 2. Output locations:'? Does it clearly state raw estimates, summary tables, seed records, and logs paths, create result directories, save seed records, run experiments, and save csv/rds outputs?
6. Formula consistency: For formula-heavy papers, prioritize exact mathematical consistency over software architecture style. Penalize incorrect formulas, wrong parameter constraints, wrong likelihood/objective functions, missing estimators, or incorrect simulation mechanisms as high severity.

Severity calibration for statistics/econometrics:
- High: Incorrect model equations, likelihood/pmf/density/objective, parameter constraints, DGP, estimator formula, optimization target, or missing a main estimator/simulation design from the paper.
- Medium: Incorrect or incomplete standard errors, confidence intervals, convergence criteria, tuning constants, scenario grids, burn-in, aggregation logic, or reported metric definitions.
- Low: Minor output formatting differences, extra helper abstractions, non-critical defaults, plotting/table formatting issues, or implementation choices that do not alter the statistical method.
"""


def get_code_language(file_name):
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".r":
        return "r"
    if ext in [".yaml", ".yml"]:
        return "yaml"
    if ext == ".py":
        return "python"
    return ""


def add_usage(total_usage, usage):
    if not isinstance(usage, Mapping):
        raise TypeError("usage must be a mapping")
    for key, value in usage.items():
        if isinstance(value, Mapping):
            if not isinstance(total_usage.get(key), dict):
                total_usage[key] = {}
            target = total_usage[key]
            add_usage(target, value)
        elif isinstance(value, (int, float)):
            total_usage[key] = total_usage.get(key, 0) + value
        elif key not in total_usage or total_usage[key] is None:
            total_usage[key] = value


def response_usage_mapping(provider_id, model_id, completion_json):
    if "usage" not in completion_json or completion_json["usage"] is None:
        return None
    usage = completion_json["usage"]
    if not isinstance(usage, Mapping):
        raise EvaluationProviderUsageError(provider_id, model_id)
    return usage


def run_completion_requests(provider_id, gpt_version, msg, generated_n, input_tokens=None):
    max_choices_per_request = model_max_choices_per_request(provider_id, gpt_version)

    if max_choices_per_request is not None and generated_n > max_choices_per_request:
        print(
            f"[INFO] {gpt_version} supports at most n={max_choices_per_request} "
            f"per request. Running multiple evaluation requests for n={generated_n}."
        )
        per_request_n = max_choices_per_request
        request_count = (generated_n + per_request_n - 1) // per_request_n
    else:
        per_request_n = generated_n
        request_count = 1

    completion_json_lst = []
    choices = []
    usage = {}
    usage_seen = False

    for request_idx in range(request_count):
        remaining_n = generated_n - len(choices)
        current_n = min(per_request_n, remaining_n)
        request_json = build_request_json(provider_id, gpt_version, msg, current_n)

        if request_count > 1:
            print(f"[INFO] Evaluation request {request_idx + 1}/{request_count}")

        transport_request = dict(request_json)
        if input_tokens is not None:
            transport_request["_input_token_count"] = input_tokens
        completion = api_call(transport_request)
        completion_json = json.loads(completion.model_dump_json())
        completion_json_lst.append(completion_json)

        response_choices = completion_json.get("choices")
        if not isinstance(response_choices, list) or len(response_choices) < current_n:
            returned_n = len(response_choices) if isinstance(response_choices, list) else 0
            raise EvaluationProviderResponseError(
                provider_id,
                gpt_version,
                current_n,
                returned_n,
            )

        for choice in response_choices:
            indexed_choice = dict(choice)
            indexed_choice["index"] = len(choices)
            choices.append(indexed_choice)

        response_usage = response_usage_mapping(provider_id, gpt_version, completion_json)
        if response_usage is not None:
            add_usage(usage, response_usage)
            usage_seen = True

    aggregate_completion_json = {
        "object": "paper2code.multi_completion",
        "model": gpt_version,
        "choices": choices,
        "usage": usage if usage_seen else None,
        "responses": completion_json_lst,
    }

    final_request_json = build_request_json(provider_id, gpt_version, msg, per_request_n)

    return final_request_json, aggregate_completion_json, generated_n


def run_completion_requests_with_fallback(
    provider_id,
    gpt_version,
    msg,
    generated_n,
    fallback_models,
    input_tokens=None,
):
    global client

    fallback_models = list(
        get_provider_registry().validate_fallback_chain(
            provider_id,
            gpt_version,
            fallback_models,
        )
    )
    model_chain = [gpt_version] + fallback_models
    last_error = None
    fallback_reason = ""
    fallback_from_model = ""

    for model_idx, model_name in enumerate(model_chain):
        if model_idx > 0:
            print(
                f"[WARNING] Falling back evaluation model from "
                f"{model_chain[model_idx - 1]} to {model_name}."
            )
        if input_tokens is not None:
            get_provider_registry().validate_context(
                provider_id,
                model_name,
                input_tokens,
            )
        client = make_openai_client(provider_id, model_name)

        try:
            request_json, completion_json, generated_n = run_completion_requests(
                provider_id,
                model_name,
                msg,
                generated_n,
                input_tokens,
            )
            fallback_info = {
                "fallback_used": model_idx > 0,
                "fallback_reason": fallback_reason if model_idx > 0 else "",
                "fallback_from_model": fallback_from_model if model_idx > 0 else "",
                "fallback_eval_model": model_name if model_idx > 0 else "",
                "fallback_model_chain": model_chain,
                "fallback_remaining_models": model_chain[model_idx + 1 :],
            }
            return request_json, completion_json, generated_n, model_name, fallback_info
        except (PermissionDeniedError, BadRequestError) as error:
            last_error = error
            if model_idx < len(model_chain) - 1 and is_quota_fallback_error(error):
                fallback_reason = FALLBACK_REASON_QUOTA
                fallback_from_model = model_name
                print(
                    f"[WARNING] Evaluation model {model_name} hit a quota-like "
                    f"error. Trying fallback model."
                )
                continue
            raise

    raise last_error


def _safe_model_name(model_name):
    return str(model_name or "unknown").replace("/", "_").replace("\\", "_")


def persist_evaluation_result(
    *,
    output_dir,
    eval_result_dir,
    paper_name,
    eval_type,
    result,
    summary="",
    cost_completion_json=None,
):
    now_str = get_now_str()
    os.makedirs(eval_result_dir, exist_ok=True)
    eval_result_file = (
        f"{eval_result_dir}/{paper_name}_eval_{eval_type}_"
        f"{_safe_model_name(result.get('eval_model') or result.get('requested_eval_model'))}_"
        f"{now_str}.json"
    )
    result_payload = dict(result)
    save_json_file(eval_result_file, result_payload)

    errors = result.get("errors") or []
    error_summary = ""
    if errors:
        first_error = errors[0]
        error_summary = str(first_error.get("message") or first_error.get("code") or "")

    feedback_json = {
        **result_payload,
        "requested_eval_model": result.get("requested_eval_model"),
        "eval_model": result.get("eval_model"),
        "score": result.get("quality_score"),
        "valid_n": result.get("valid_n"),
        "score_lst": result.get("score_lst"),
        "passed": result.get("quality_verdict") == "passed",
        "summary": summary or error_summary,
        "files_to_repair": result.get("files_to_fix", []),
        "eval_result_file": eval_result_file,
        "updated_at": get_now_str(),
    }
    save_json_file(eval_feedback_path(output_dir), feedback_json)

    write_repo_status(
        output_dir,
        legacy_status_from_result(result),
        **result_payload,
        files_to_repair=result.get("files_to_fix", []),
        eval_score=result.get("quality_score"),
        feedback_file=eval_feedback_path(output_dir),
        eval_result_file=eval_result_file,
    )

    if cost_completion_json is not None:
        print_log_cost(
            cost_completion_json,
            result.get("eval_model") or result.get("requested_eval_model"),
            f"[Evaluation] {paper_name} - {eval_type}",
            output_dir,
            0,
            result.get("provider_id"),
        )

    return eval_result_file


def main(args):
    global client
    validate_eval_args(args)

    paper_name = args.paper_name
    paper_format = args.paper_format
    domain = args.domain
    pdf_json_path = args.pdf_json_path
    pdf_latex_path = args.pdf_latex_path
    pdf_markdown_path = args.pdf_markdown_path
    output_dir = args.output_dir
    target_repo_dir = args.target_repo_dir
    eval_result_dir = args.eval_result_dir
    gpt_version = args.gpt_version
    provider_id = args.provider
    fallback_models = parse_fallback_models(args.fallback_gpt_versions)
    if not fallback_models:
        fallback_models = default_fallback_models(provider_id, gpt_version)
    generated_n = args.generated_n
    max_repair_rounds = args.max_repair_rounds
    data_dir = args.data_dir
    eval_type = args.eval_type
    is_papercoder = True if args.papercoder else False

    gold_repo_dir = args.gold_repo_dir

    existing_status = load_json_file(repo_status_path(output_dir), default={}) or {}
    repair_round = int(existing_status.get("repair_round", 0) or 0)
    task_manifest = None

    paper_content = load_paper_content(
        paper_format,
        json_path=pdf_json_path,
        latex_path=pdf_latex_path,
        markdown_path=pdf_markdown_path,
    )

    codes = ""

    if is_papercoder:
        # configuration
        with open(f"{output_dir}/planning_config.yaml", "r", encoding="utf-8") as f:
            config_yaml = f.read()

        task_manifest = load_task_manifest(output_dir)
        todo_file_lst = task_manifest.paths
        allowed_extensions = {".r"} if domain == "statistics" else {".py"}
        target_files_dict = read_manifest_text_files(
            target_repo_dir,
            task_manifest,
            allowed_extensions=allowed_extensions,
        )

        for todo_file in todo_file_lst:
            if todo_file.endswith(".yaml"):
                continue

            if todo_file not in target_files_dict:
                print(f"[WARNING] {todo_file} not found in target repo; skipping it.")
                continue

            language = get_code_language(todo_file)
            codes += (
                f"```{language}\n"
                f"## File name: {todo_file}\n"
                f"{target_files_dict[todo_file]}\n"
                f"```\n\n"
            )

        codes += (
            f"```yaml\n"
            f"## File name: config.yaml\n"
            f"{config_yaml}\n"
            f"```\n\n"
        )

    else:
        target_files_dict = read_all_files(
            target_repo_dir,
            allowed_ext=[".py", ".R", ".r", ".yaml", ".yml", ".md", ".sh", ".bash"],
            is_print=False,
        )

        for file_name, code in target_files_dict.items():
            codes += (
                f"```## File name: {file_name}\n"
                f"{code}\n"
                f"```\n\n"
            )

    with open(f"{data_dir}/prompts/{eval_type}.txt", "r", encoding="utf-8") as f:
        prompt = f.read()

    cur_prompt = prompt.replace("{{Paper}}", f"{paper_content}").replace("{{Code}}", codes)
    cur_prompt += build_domain_eval_instruction(domain)

    # reference-based
    if "ref_based" == eval_type and len(gold_repo_dir) > 0:
        all_files_dict = read_all_files(
            gold_repo_dir,
            allowed_ext=[".py", ".R", ".r", ".yaml", ".yml", ".md", ".sh", ".bash"],
            is_print=False,
        )

        goldcodes = ""
        gold_cnt = 0

        if len(args.selected_file_path) > 0:
            selected_file_lst = []

            with open(args.selected_file_path, "r", encoding="utf-8") as f:
                selected_file_lst = f.readlines()

            for s_idx in range(len(selected_file_lst)):
                selected_file_lst[s_idx] = selected_file_lst[s_idx].strip()

            for all_file, all_file_code in all_files_dict.items():
                if all_file not in selected_file_lst:
                    continue

                goldcodes += (
                    f"```## File name: {all_file}\n"
                    f"{all_file_code}\n"
                    f"```\n\n"
                )

                gold_cnt += 1

        else:
            for all_file, all_file_code in all_files_dict.items():
                goldcodes += (
                    f"```## File name: {all_file}\n"
                    f"{all_file_code}\n"
                    f"```\n\n"
                )

                gold_cnt += 1

        cur_prompt = cur_prompt.replace("{{GoldCode}}", f"{goldcodes}")

    msg = [{"role": "system", "content": cur_prompt}]

    try:
        num_tokens = num_tokens_from_messages(msg)
    except Exception as exc:
        raise RuntimeError(
            "Unable to determine request size for provider context validation."
        ) from exc

    try:
        (
            request_json,
            completion_json,
            generated_n,
            actual_gpt_version,
            fallback_info,
        ) = run_completion_requests_with_fallback(
            provider_id,
            gpt_version,
            msg,
            generated_n,
            fallback_models,
            num_tokens,
        )
    except Exception as exc:
        classified = classify_evaluation_exception(exc)
        result = build_evaluation_error_result(
            paper_name=paper_name,
            target_repo_dir=target_repo_dir,
            eval_type=eval_type,
            requested_eval_model=gpt_version,
            provider_id=provider_id,
            error_code=classified["error_code"],
            error_message=classified["message"],
            generated_n=generated_n,
            repair_round=repair_round,
            max_repair_rounds=max_repair_rounds,
        )
        persist_evaluation_result(
            output_dir=output_dir,
            eval_result_dir=eval_result_dir,
            paper_name=paper_name,
            eval_type=eval_type,
            result=result,
            summary=classified["message"],
        )
        print(f"[ERROR] Evaluation failed before quality assessment: {classified['error_code']}")
        return

    score_key = "score"
    rationale_key = "critique_list"

    all_scores = []
    rationales = []

    for n in range(generated_n):
        choice = completion_json["choices"][n]
        output = choice["message"]["content"].strip()

        try:
            output_json2 = json.loads(output)
            score = int(output_json2[score_key])

            if isinstance(output_json2[rationale_key], str):
                rationale = output_json2[rationale_key]
            else:
                rationale = json.dumps(output_json2[rationale_key], ensure_ascii=False)

        except Exception:
            try:
                output_json2 = json.loads(extract_json_from_string(output))
                score = int(output_json2[score_key])

                if isinstance(output_json2[rationale_key], str):
                    rationale = output_json2[rationale_key]
                else:
                    rationale = json.dumps(output_json2[rationale_key], ensure_ascii=False)

            except Exception as e2:
                print("[WARNING] Invalid response: parsing error")
                print(e2)
                print("-" * 40)
                continue

        # score
        if score < 1 or score > 5:
            print(
                f"[WARNING] Invalid response: score {score}, "
                f"Score must be in the range of 1–5."
            )
            continue

        all_scores.append(int(score))
        rationales.append(rationale)

    feedback = summarize_eval_feedback(rationales)
    try:
        if task_manifest is None and feedback["files_to_repair"]:
            raise TaskManifestError(
                "Evaluator selected repair files without a TaskManifest boundary."
            )
        files_to_fix = (
            list(resolve_files_to_fix(feedback["files_to_repair"], task_manifest))
            if task_manifest is not None
            else []
        )
        result = build_quality_result(
            paper_name=paper_name,
            target_repo_dir=target_repo_dir,
            eval_type=eval_type,
            requested_eval_model=gpt_version,
            eval_model=actual_gpt_version,
            provider_id=provider_id,
            generated_n=generated_n,
            scores=all_scores,
            findings=feedback["findings"],
            files_to_fix=files_to_fix,
            repair_round=repair_round,
            max_repair_rounds=max_repair_rounds,
            **fallback_info,
        )
        summary = feedback["summary"]
    except Exception as exc:
        classified = classify_evaluation_exception(exc)
        result = build_evaluation_error_result(
            paper_name=paper_name,
            target_repo_dir=target_repo_dir,
            eval_type=eval_type,
            requested_eval_model=gpt_version,
            eval_model=actual_gpt_version,
            provider_id=provider_id,
            error_code="malformed_evaluator_response"
            if classified["error_code"] == "provider_protocol_error"
            else classified["error_code"],
            error_message=classified["message"],
            generated_n=generated_n,
            repair_round=repair_round,
            max_repair_rounds=max_repair_rounds,
            **fallback_info,
        )
        summary = classified["message"]

    persist_evaluation_result(
        output_dir=output_dir,
        eval_result_dir=eval_result_dir,
        paper_name=paper_name,
        eval_type=eval_type,
        result=result,
        summary=summary,
        cost_completion_json=completion_json,
    )

    print()
    print("=" * 40)
    print("🌟 Evaluation Summary 🌟")
    print(f"📄 Paper name: {paper_name}")
    print(f"🧪 Evaluation type: {eval_type}")
    print(f"📁 Target repo directory: {target_repo_dir}")
    print("📊 Evaluation result:")
    if result["quality_score"] is None:
        print("\t📈 Score: not assessed")
    else:
        print(f"\t📈 Score: {result['quality_score']:.4f}")
    print(f"\t✅ Valid: {result['valid_n']}/{generated_n}")
    print(f"\t🧪 Evaluation status: {result['evaluation_status']}")
    print(f"\t🚦 Quality verdict: {result['quality_verdict']}")
    print(f"\t🧯 Repair status: {result['repair_status']}")
    print("=" * 40)


if __name__ == "__main__":
    argparser = argparse.ArgumentParser()

    argparser.add_argument("--paper_name", type=str)
    argparser.add_argument(
        "--paper_format",
        type=str,
        default="JSON",
        choices=["JSON", "LaTeX", "Markdown"],
    )
    argparser.add_argument(
        "--domain",
        type=str,
        default="general",
        choices=["general", "statistics"],
    )
    argparser.add_argument("--pdf_json_path", type=str)
    argparser.add_argument("--pdf_latex_path", type=str)
    argparser.add_argument("--pdf_markdown_path", type=str)
    argparser.add_argument("--data_dir", type=str, default="../data")

    argparser.add_argument("--output_dir", type=str)

    argparser.add_argument("--target_repo_dir", type=str)
    argparser.add_argument("--gold_repo_dir", type=str, default="")
    argparser.add_argument("--eval_result_dir", type=str)

    argparser.add_argument(
        "--eval_type",
        type=str,
        default="ref_free",
        choices=["ref_free", "ref_based"],
    )

    argparser.add_argument("--generated_n", type=int, default=8)
    argparser.add_argument("--provider", type=str, required=True)
    argparser.add_argument("--gpt_version", type=str, required=True)
    argparser.add_argument("--fallback_gpt_versions", type=str, default="")
    argparser.add_argument("--max_repair_rounds", type=int, default=MAX_REPAIR_ROUNDS)

    argparser.add_argument("--selected_file_path", type=str, default="")
    argparser.add_argument("--papercoder", action="store_true")

    args = argparser.parse_args()
    main(args)


# ref-free
# python eval.py \
#     --paper_name Transformer \
#     --pdf_json_path ../examples/Transformer_cleaned.json \
#     --data_dir ../data \
#     --output_dir ../outputs/Transformer \
#     --target_repo_dir ../outputs/Transformer_repo \
#     --eval_result_dir ../results \
#     --eval_type ref_free \
#     --generated_n 8 \
#     --papercoder

# ref-based
# python eval.py \
#     --paper_name Transformer \
#     --pdf_json_path ../examples/Transformer_cleaned.json \
#     --data_dir ../data \
#     --output_dir ../outputs/Transformer \
#     --target_repo_dir ../outputs/Transformer_repo \
#     --gold_repo_dir ../examples/Transformer_gold_repo \
#     --eval_result_dir ../results \
#     --eval_type ref_based \
#     --generated_n 8 \
#     --papercoder
