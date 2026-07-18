import json
import os
import sys
import argparse
from task_manifest import load_task_manifest, read_manifest_text_files
from openai import BadRequestError, PermissionDeniedError
from utils import (
    num_tokens_from_messages,
    read_all_files,
    extract_json_from_string,
    get_now_str,
    print_log_cost,
    make_openai_client,
    load_paper_content,
    MAX_REPAIR_ROUNDS,
    STATUS_EVAL_FAILED,
    STATUS_EVAL_PASSED,
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


def api_call(request_json):
    if client is None:
        raise RuntimeError("API client has not been initialized.")
    completion = client.chat.completions.create(**request_json)
    return completion


def model_max_choices_per_request(model_name):
    model_name = model_name.lower()
    if model_name.startswith("kimi-") or model_name.startswith("deepseek-"):
        return 1
    if model_name.startswith("qwen"):
        return 1
    return None


def build_request_json(gpt_version, msg, generated_n):
    if "o3-mini" in gpt_version:
        return {
            "model": gpt_version,
            "messages": msg,
            "reasoning_effort": "high",
            "n": generated_n,
        }

    return {
        "model": gpt_version,
        "messages": msg,
        "temperature": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "stop": None,
        "n": generated_n,
    }


def default_fallback_models(model_name):
    model_name = model_name.lower()
    if model_name == "qwen3.7-max":
        return ["qwen3.7-plus"]
    if model_name == "qwen-3.7-max":
        return ["qwen-3.7-plus"]
    return []


def parse_fallback_models(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


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
    for key, value in usage.items():
        if isinstance(value, dict):
            if not isinstance(total_usage.get(key), dict):
                total_usage[key] = {}
            target = total_usage[key]
            add_usage(target, value)
        elif isinstance(value, (int, float)):
            total_usage[key] = total_usage.get(key, 0) + value
        elif key not in total_usage or total_usage[key] is None:
            total_usage[key] = value


def run_completion_requests(gpt_version, msg, generated_n):
    if "o3-mini" in gpt_version and generated_n > 8:
        print("[WARNING] o3-mini does not support n > 8. Setting generated_n to 8.")
        generated_n = 8

    max_choices_per_request = model_max_choices_per_request(gpt_version)

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

    for request_idx in range(request_count):
        remaining_n = generated_n - len(choices)
        current_n = min(per_request_n, remaining_n)
        request_json = build_request_json(gpt_version, msg, current_n)

        if request_count > 1:
            print(f"[INFO] Evaluation request {request_idx + 1}/{request_count}")

        completion = api_call(request_json)
        completion_json = json.loads(completion.model_dump_json())
        completion_json_lst.append(completion_json)

        for choice in completion_json.get("choices", []):
            choice["index"] = len(choices)
            choices.append(choice)

        add_usage(usage, completion_json.get("usage", {}))

    aggregate_completion_json = {
        "object": "paper2code.multi_completion",
        "model": gpt_version,
        "choices": choices,
        "usage": usage,
        "responses": completion_json_lst,
    }

    final_request_json = build_request_json(gpt_version, msg, per_request_n)

    return final_request_json, aggregate_completion_json, generated_n


def run_completion_requests_with_fallback(gpt_version, msg, generated_n, fallback_models):
    global client

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
        client = make_openai_client(model_name)

        try:
            request_json, completion_json, generated_n = run_completion_requests(
                model_name,
                msg,
                generated_n,
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
    fallback_models = parse_fallback_models(args.fallback_gpt_versions)
    if not fallback_models:
        fallback_models = default_fallback_models(gpt_version)
    generated_n = args.generated_n
    max_repair_rounds = args.max_repair_rounds
    data_dir = args.data_dir
    eval_type = args.eval_type
    is_papercoder = True if args.papercoder else False

    gold_repo_dir = args.gold_repo_dir

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
    except Exception as e:
        print(
            f"[WARNING] An exception was raised while counting tokens "
            f"for the target repository of {args.paper_name}."
        )
        print(e)
        print("-" * 40)
        num_tokens = 0

    if num_tokens > 128000:
        print(f"[ERROR] {args.paper_name} more than 128k")
        sys.exit(0)

    (
        request_json,
        completion_json,
        generated_n,
        actual_gpt_version,
        fallback_info,
    ) = run_completion_requests_with_fallback(
        gpt_version,
        msg,
        generated_n,
        fallback_models,
    )

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

    if len(all_scores) == 0:
        print("[ERROR] No valid evaluation responses were parsed.")
        avg_score = 0
    else:
        avg_score = sum(all_scores) / len(all_scores)

    feedback = summarize_eval_feedback(rationales)
    is_passed = avg_score >= 4.0 and not feedback["has_high_severity"]
    repo_status = STATUS_EVAL_PASSED if is_passed else STATUS_EVAL_FAILED

    existing_status = load_json_file(repo_status_path(output_dir), default={}) or {}
    repair_round = int(existing_status.get("repair_round", 0) or 0)

    output_json = {
        "paper_name": paper_name,
        "target_repo_dir": target_repo_dir,
        "eval_type": eval_type,
        "gold_repo_dir": gold_repo_dir,
        "generated_n": generated_n,
        "requested_gpt_version": gpt_version,
        "actual_gpt_version": actual_gpt_version,
        "fallback_gpt_versions": fallback_models,
        **fallback_info,
        "request_json": request_json,
        "completion_json": completion_json,
        "eval_result": {
            "score": avg_score,
            "valid_n": len(all_scores),
            "score_lst": all_scores,
            "rationale_lst": rationales,
            "has_high_severity": feedback["has_high_severity"],
            "passed": is_passed,
        },
    }

    now_str = get_now_str()
    os.makedirs(eval_result_dir, exist_ok=True)

    with open(
        f"{eval_result_dir}/{paper_name}_eval_{eval_type}_{actual_gpt_version}_{now_str}.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(output_json, f, ensure_ascii=False, indent=2)

    feedback_json = {
        "paper_name": paper_name,
        "target_repo_dir": target_repo_dir,
        "eval_type": eval_type,
        "requested_eval_model": gpt_version,
        "eval_model": actual_gpt_version,
        "fallback_gpt_versions": fallback_models,
        **fallback_info,
        "score": avg_score,
        "valid_n": len(all_scores),
        "score_lst": all_scores,
        "passed": is_passed,
        "pass_rule": "score >= 4.0 and no high severity findings",
        "has_high_severity": feedback["has_high_severity"],
        "repair_round": repair_round,
        "max_repair_rounds": max_repair_rounds,
        "summary": feedback["summary"],
        "findings": feedback["findings"],
        "findings_by_file": feedback["findings_by_file"],
        "files_to_repair": feedback["files_to_repair"],
        "eval_result_file": f"{eval_result_dir}/{paper_name}_eval_{eval_type}_{actual_gpt_version}_{now_str}.json",
        "updated_at": get_now_str(),
    }
    save_json_file(eval_feedback_path(output_dir), feedback_json)

    write_repo_status(
        output_dir,
        repo_status,
        paper_name=paper_name,
        target_repo_dir=target_repo_dir,
        eval_type=eval_type,
        requested_eval_model=gpt_version,
        eval_model=actual_gpt_version,
        **fallback_info,
        eval_score=avg_score,
        valid_n=len(all_scores),
        has_high_severity=feedback["has_high_severity"],
        pass_rule="score >= 4.0 and no high severity findings",
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
        feedback_file=eval_feedback_path(output_dir),
        eval_result_file=feedback_json["eval_result_file"],
    )

    # ---------------
    print()
    print("=" * 40)
    print("🌟 Evaluation Summary 🌟")
    print(f"📄 Paper name: {paper_name}")
    print(f"🧪 Evaluation type: {eval_type}")
    print(f"📁 Target repo directory: {target_repo_dir}")
    print("📊 Evaluation result:")
    print(f"\t📈 Score: {avg_score:.4f}")
    print(f"\t✅ Valid: {output_json['eval_result']['valid_n']}/{generated_n}")
    print(f"\t🚦 Status: {repo_status}")
    print(f"\t🧯 High severity: {feedback['has_high_severity']}")
    print("=" * 40)

    print_log_cost(
        completion_json,
        actual_gpt_version,
        f"[Evaluation] {paper_name} - {eval_type}",
        output_dir,
        0,
    )
    # ---------------


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
    argparser.add_argument("--gpt_version", type=str, default="deepseek-v4-pro")
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
