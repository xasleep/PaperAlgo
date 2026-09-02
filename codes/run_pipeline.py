import argparse
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    from checkpoint_protocol import (
        CHECKPOINT_STAGES,
        completed_checkpoint,
        failed_checkpoint,
        list_checkpoints,
        read_checkpoint,
        running_checkpoint,
        write_checkpoint,
    )
    from evaluation_contract import decide_repair_action
    from provider_registry import REGISTRY_PATH_ENV, get_provider_registry
    from task_manifest import (
        load_task_manifest,
        safe_join,
        safe_write_text,
        validate_task_path,
    )
    from utils import (
        MAX_REPAIR_ROUNDS,
        STATUS_EVAL_FAILED,
        STATUS_EVAL_PASSED,
        load_json_file,
        repo_status_path,
        save_json_file,
    )
except ModuleNotFoundError:
    from codes.checkpoint_protocol import (
        CHECKPOINT_STAGES,
        completed_checkpoint,
        failed_checkpoint,
        list_checkpoints,
        read_checkpoint,
        running_checkpoint,
        write_checkpoint,
    )
    from codes.evaluation_contract import decide_repair_action
    from codes.provider_registry import REGISTRY_PATH_ENV, get_provider_registry
    from codes.task_manifest import (
        load_task_manifest,
        safe_join,
        safe_write_text,
        validate_task_path,
    )
    from codes.utils import (
        MAX_REPAIR_ROUNDS,
        STATUS_EVAL_FAILED,
        STATUS_EVAL_PASSED,
        load_json_file,
        repo_status_path,
        save_json_file,
    )


PROVIDER_REGISTRY = get_provider_registry()

SYSTEM_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PAPER2CODE_COST_LEDGER_DB_PATH",
    "PAPER2CODE_PROVIDER_REGISTRY_PATH",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}
PROVIDER_ENV_NAMES = PROVIDER_REGISTRY.environment_names() | {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
}
ROLE_ENV_NAMES = {
    "EVAL_API_KEY",
    "EVAL_BASE_URL",
    "REPRODUCE_API_KEY",
    "REPRODUCE_BASE_URL",
}
MIN_GENERATED_N = 1
MAX_GENERATED_N = 32
MAX_REPAIR_ROUNDS_LIMIT = 10


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compact_now_str():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def get_repo_root(script_dir):
    return os.path.abspath(os.path.join(script_dir, ".."))


def get_workspace_root(script_dir):
    return os.path.abspath(os.path.join(script_dir, "..", ".."))


def build_clean_env():
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in SYSTEM_ENV_ALLOWLIST
        and name.upper() not in PROVIDER_ENV_NAMES
        and name.upper() not in ROLE_ENV_NAMES
    }
    registry_path = env.get(REGISTRY_PATH_ENV)
    if registry_path and not os.path.isabs(registry_path):
        env[REGISTRY_PATH_ENV] = os.path.abspath(registry_path)
    return env


def get_role_env(provider, model_id, role_prefix, additional_model_ids=()):
    env = build_clean_env()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    contracts = [
        PROVIDER_REGISTRY.get(provider, candidate_model_id)
        for candidate_model_id in (model_id, *additional_model_ids)
    ]

    role_api_key = os.environ.get(f"{role_prefix}_API_KEY")
    role_base_url = os.environ.get(f"{role_prefix}_BASE_URL")

    for contract in contracts:
        api_key = role_api_key or os.environ.get(contract.api_key_env)
        base_url = (
            role_base_url
            or os.environ.get(contract.base_url_env)
            or contract.base_url
        )
        if api_key:
            env[contract.api_key_env] = api_key
        if base_url:
            env[contract.base_url_env] = base_url

    return env


def validate_provider_env(provider, model_id, env):
    PROVIDER_REGISTRY.resolve(provider, model_id, environ=env)


def update_status(status_path, **updates):
    current = load_json_file(status_path, default={}) or {}
    current.update(updates)
    current["updated_at"] = now_str()
    save_json_file(status_path, current)
    return current


def write_summary(summary_path, **updates):
    current = load_json_file(summary_path, default={}) or {}
    current.update(updates)
    current["updated_at"] = now_str()
    save_json_file(summary_path, current)
    return current


class PipelineCheckpointAdapter:
    """Optional filesystem adapter used only by the SQLite Worker runtime."""

    def __init__(self, args, run_dir, job_id):
        self.enabled = args.checkpoint_mode == "sqlite"
        self.run_dir = Path(run_dir)
        self.job_id = job_id
        self.resume_from_stage = args.resume_from_stage or None
        self.resume_sequence = int(args.resume_stage_sequence or 0)
        self.resume_attempt = int(args.resume_stage_attempt or 0)
        self.recovery_count = int(args.checkpoint_recovery_count or 0)

    def should_run(self, stage_sequence):
        return not self.enabled or not self.resume_sequence or stage_sequence >= self.resume_sequence

    def attempt_for(self, stage_sequence):
        if self.resume_sequence == stage_sequence and self.resume_attempt:
            return self.resume_attempt
        return 1

    def cost_context(self, stage_name, stage_sequence, *, repair_attempt=None):
        if not self.enabled:
            return {}
        context = {
            "PAPER2CODE_COST_JOB_ID": self.job_id,
            "PAPER2CODE_COST_STAGE": stage_name,
            "PAPER2CODE_COST_STAGE_ATTEMPT": str(self.attempt_for(stage_sequence)),
            "PAPER2CODE_COST_RECOVERY_ATTEMPT": str(self.recovery_count),
        }
        if repair_attempt is not None:
            context["PAPER2CODE_COST_REPAIR_ATTEMPT"] = str(repair_attempt)
        return context

    @contextmanager
    def stage(
        self,
        stage_name,
        stage_sequence,
        *,
        artifact_paths,
        resume_from_stage,
    ):
        if not self.enabled:
            yield
            return
        stage_attempt = self.attempt_for(stage_sequence)
        started_at = now_str()
        write_checkpoint(
            self.run_dir,
            running_checkpoint(
                job_id=self.job_id,
                stage_name=stage_name,
                stage_sequence=stage_sequence,
                stage_attempt=stage_attempt,
                started_at=started_at,
            ),
        )
        try:
            yield
            paths = artifact_paths() if callable(artifact_paths) else artifact_paths
            write_checkpoint(
                self.run_dir,
                completed_checkpoint(
                    self.run_dir,
                    job_id=self.job_id,
                    stage_name=stage_name,
                    stage_sequence=stage_sequence,
                    stage_attempt=stage_attempt,
                    started_at=started_at,
                    completed_at=now_str(),
                    artifact_paths=paths,
                    resume_from_stage=(
                        resume_from_stage()
                        if callable(resume_from_stage)
                        else resume_from_stage
                    ),
                ),
            )
        except Exception:
            write_checkpoint(
                self.run_dir,
                failed_checkpoint(
                    job_id=self.job_id,
                    stage_name=stage_name,
                    stage_sequence=stage_sequence,
                    stage_attempt=stage_attempt,
                    started_at=started_at,
                    completed_at=now_str(),
                    error_code="stage_execution_failed",
                ),
            )
            raise


def relative_run_path(run_dir, path):
    return Path(path).relative_to(Path(run_dir)).as_posix()


def existing_artifact_paths(run_dir, paths):
    return [relative_run_path(run_dir, path) for path in paths if Path(path).is_file()]


def glob_artifact_paths(run_dir, *patterns):
    root = Path(run_dir)
    paths = []
    for pattern in patterns:
        paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted({relative_run_path(root, path) for path in paths})


def checkpoint_state_artifact_paths(run_dir, stage_name):
    """Return the complete current state needed beyond a completed boundary."""

    ordered_stages = [
        "mineru_parse",
        "mineru_skipped",
        "planning",
        "extract_config",
        "analyzing",
        "coding",
        "evaluation",
        "repair",
        "completed",
    ]
    if stage_name not in CHECKPOINT_STAGES:
        raise ValueError("Checkpoint stage is not allowed.")
    if stage_name in {"mineru_parse", "mineru_skipped"}:
        rank = 0
    else:
        rank = ordered_stages.index(stage_name)
    patterns = ["input/source_markdown.md", "input/source_markdown.markdown"]
    if rank >= ordered_stages.index("planning"):
        patterns.extend(
            [
                "output/task_manifest.json",
                "output/planning_response.json",
                "output/planning_trajectories.json",
            ]
        )
    if rank >= ordered_stages.index("extract_config"):
        patterns.extend(
            ["output/planning_config.yaml", "output/planning_artifacts/**/*"]
        )
    if rank >= ordered_stages.index("analyzing"):
        patterns.extend(
            [
                "output/analyzing_artifacts/**/*",
                "output/*_simple_analysis_response.json",
                "output/*_simple_analysis_trajectories.json",
            ]
        )
    if rank >= ordered_stages.index("coding"):
        patterns.append("repo/**/*")
    if stage_name in {"evaluation", "repair", "completed"}:
        patterns.extend(["output/repo_status.json", "output/eval_feedback.json"])
    if stage_name == "completed":
        patterns.extend(["run_status.json", "run_summary.json"])
    return glob_artifact_paths(run_dir, *patterns)


def resume_markdown_path(run_dir, job_id):
    entries = list_checkpoints(Path(run_dir), expected_job_id=job_id)
    input_entries = [
        (checkpoint, path)
        for checkpoint, path in entries
        if checkpoint["stage_sequence"] == 1 and checkpoint["status"] == "completed"
    ]
    if not input_entries:
        raise RuntimeError("A completed input checkpoint is required for recovery.")
    checkpoint, relative_checkpoint = max(
        input_entries, key=lambda item: int(item[0]["stage_attempt"])
    )
    checkpoint = read_checkpoint(
        Path(run_dir) / Path(*relative_checkpoint.split("/")),
        run_dir=Path(run_dir),
        expected_job_id=job_id,
    )
    markdown_artifacts = [
        artifact
        for artifact in checkpoint["artifacts"]
        if str(artifact["path"]).lower().endswith((".md", ".markdown"))
    ]
    if len(markdown_artifacts) != 1:
        raise RuntimeError("The input checkpoint must identify one Markdown artifact.")
    return str(Path(run_dir) / Path(*str(markdown_artifacts[0]["path"]).split("/")))


def should_echo_line(line, console_output):
    if console_output == "full":
        return True
    if console_output == "quiet":
        return False

    stripped = line.strip()
    if not stripped:
        return False

    progress_prefixes = (
        "[INFO]",
        "[WARNING]",
        "[ERROR]",
        "[AUTO-REFINE]",
        "[PIPELINE]",
        "Usage Summary",
        "Evaluation Summary",
    )
    if stripped.startswith(progress_prefixes):
        return True

    progress_fragments = (
        "Evaluation request",
        "Current status",
        "Status:",
        "Score:",
        "Repair round:",
        "Model:",
        "Current total cost:",
        "Accumulated total cost",
    )
    return any(fragment in stripped for fragment in progress_fragments)


def run_command(
    label,
    cmd,
    cwd,
    log_path,
    status_path,
    env=None,
    console_output="progress",
    cost_context=None,
):
    if env is None:
        env = build_clean_env()
    else:
        env = dict(env)
    if cost_context:
        env.update(cost_context)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    update_status(
        status_path,
        status="running",
        stage=label,
        message=f"Running {label}",
        current_command=[str(part) for part in cmd],
        current_log=log_path,
    )

    print("=" * 80)
    print(f"[PIPELINE] {label}")
    print(f"Log: {log_path}")
    if console_output == "full":
        print(" ".join(str(part) for part in cmd))
    print("=" * 80)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log_file.write(line)
            if should_echo_line(line, console_output):
                print(line, end="")
        return_code = proc.wait()

    if return_code != 0:
        update_status(
            status_path,
            status="failed",
            stage=label,
            message=f"{label} failed with exit code {return_code}",
            failed_command=[str(part) for part in cmd],
            failed_log=log_path,
        )
        raise subprocess.CalledProcessError(return_code, cmd)


def discover_markdown(mineru_dir, paper_name):
    candidates = []
    for root, _, files in os.walk(mineru_dir):
        for file_name in files:
            if file_name.lower().endswith(".md"):
                full_path = os.path.join(root, file_name)
                score = 0
                if file_name.lower() == f"{paper_name.lower()}.md":
                    score += 10
                if os.path.basename(root).lower() == "auto":
                    score += 5
                candidates.append((score, os.path.getmtime(full_path), full_path))

    if not candidates:
        raise FileNotFoundError(
            f"MinerU did not produce a Markdown file under {mineru_dir}"
        )

    candidates.sort(reverse=True)
    return candidates[0][2]


def default_mineru_executable(script_dir):
    workspace_root = get_workspace_root(script_dir)
    candidate = os.path.join(workspace_root, "mineru_env", "Scripts", "mineru.exe")
    if os.path.exists(candidate):
        return candidate
    return "mineru"


def add_optional_arg(cmd, flag, value):
    if value:
        cmd.extend([flag, value])


def parse_bool(value):
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def validate_runtime_args(args):
    if args.generated_n < MIN_GENERATED_N or args.generated_n > MAX_GENERATED_N:
        raise ValueError(
            f"generated_n must be between {MIN_GENERATED_N} and {MAX_GENERATED_N}."
        )
    if args.max_repair_rounds < 0 or args.max_repair_rounds > MAX_REPAIR_ROUNDS_LIMIT:
        raise ValueError(
            f"max_repair_rounds must be between 0 and {MAX_REPAIR_ROUNDS_LIMIT}."
        )
    eval_fallback_models = parse_eval_fallback_versions(args.eval_fallback_gpt_versions)
    PROVIDER_REGISTRY.get(args.reproduce_provider, args.reproduce_gpt_version)
    PROVIDER_REGISTRY.validate_fallback_chain(
        args.eval_provider,
        args.eval_gpt_version,
        eval_fallback_models,
    )
    checkpoint_mode = getattr(args, "checkpoint_mode", "off")
    resume_from_stage = getattr(args, "resume_from_stage", "")
    resume_stage_sequence = int(getattr(args, "resume_stage_sequence", 0) or 0)
    resume_stage_attempt = int(getattr(args, "resume_stage_attempt", 0) or 0)
    recovery_count = int(getattr(args, "checkpoint_recovery_count", 0) or 0)
    if checkpoint_mode == "off":
        if (
            resume_from_stage
            or resume_stage_sequence
            or resume_stage_attempt
            or recovery_count
        ):
            raise ValueError("Checkpoint recovery arguments require --checkpoint_mode sqlite.")
        return
    if checkpoint_mode != "sqlite":
        raise ValueError("checkpoint_mode must be 'off' or 'sqlite'.")
    if not getattr(args, "job_id", ""):
        raise ValueError("--job_id is required when checkpoint mode is enabled.")
    if resume_from_stage:
        if resume_from_stage not in CHECKPOINT_STAGES:
            raise ValueError("--resume_from_stage is not an allowed pipeline stage.")
        if resume_stage_sequence < 1 or resume_stage_attempt < 1:
            raise ValueError("Resume sequence and attempt must be positive.")
    elif resume_stage_sequence or resume_stage_attempt:
        raise ValueError("Resume stage, sequence, and attempt must be provided together.")
    if recovery_count < 0:
        raise ValueError("--checkpoint_recovery_count must not be negative.")


def build_python_cmd(script_dir, script_name):
    return [sys.executable, os.path.join(script_dir, script_name)]


def is_path_under(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def validate_markdown_path(markdown_path, runs_dir):
    if not markdown_path:
        raise ValueError("--pdf_markdown_path is required when --skip_mineru is set.")

    resolved_path = os.path.abspath(markdown_path)
    if not os.path.isfile(resolved_path):
        raise FileNotFoundError(resolved_path)
    if os.path.splitext(resolved_path)[1].lower() not in {".md", ".markdown"}:
        raise ValueError("--pdf_markdown_path must use a Markdown extension.")

    resolved_runs_dir = os.path.abspath(runs_dir)
    if not is_path_under(resolved_path, resolved_runs_dir):
        raise ValueError("--pdf_markdown_path must be under the runs directory.")

    return resolved_path


def parse_eval_fallback_versions(value):
    if value == "":
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return value.split(",")
    raise ValueError("eval_fallback_gpt_versions must be a string or list.")


def format_eval_fallback_versions(models):
    return ",".join(models)


def effective_eval_gpt_version(args):
    return getattr(args, "active_eval_gpt_version", "") or args.eval_gpt_version


def current_eval_fallback_models(args):
    if effective_eval_gpt_version(args) != args.eval_gpt_version:
        return parse_eval_fallback_versions(
            getattr(args, "active_eval_fallback_gpt_versions", "")
        )
    return parse_eval_fallback_versions(args.eval_fallback_gpt_versions)


def eval_fallback_versions_for_command(args):
    return format_eval_fallback_versions(current_eval_fallback_models(args))


def remaining_fallback_models_after(repo_status, fallback_model):
    explicit_remaining = repo_status.get("fallback_remaining_models")
    if explicit_remaining is not None:
        return parse_eval_fallback_versions(explicit_remaining)

    model_chain = repo_status.get("fallback_model_chain")
    if not isinstance(model_chain, list):
        return []

    normalized_chain = parse_eval_fallback_versions(model_chain)
    try:
        model_index = normalized_chain.index(fallback_model)
    except ValueError:
        return []
    return normalized_chain[model_index + 1 :]


def fallback_model_chain_from_status(repo_status):
    model_chain = repo_status.get("fallback_model_chain")
    if not isinstance(model_chain, list):
        return []
    return parse_eval_fallback_versions(model_chain)


def remember_fallback_eval_model(args, repo_status, status_path=None):
    fallback_model = repo_status.get("fallback_eval_model") or repo_status.get("eval_model")
    fallback_used = bool(repo_status.get("fallback_used"))
    if not fallback_used or not fallback_model:
        return False
    if fallback_model == args.eval_gpt_version:
        return False

    remaining_fallback_models = remaining_fallback_models_after(repo_status, fallback_model)
    args.active_eval_gpt_version = fallback_model
    args.active_eval_fallback_gpt_versions = format_eval_fallback_versions(
        remaining_fallback_models
    )
    if status_path:
        update_status(
            status_path,
            requested_eval_model=args.eval_gpt_version,
            effective_eval_model=fallback_model,
            fallback_eval_model=fallback_model,
            fallback_from_model=repo_status.get("fallback_from_model") or "",
            fallback_reason=repo_status.get("fallback_reason") or "",
            eval_fallback_active=True,
            remaining_eval_fallback_models=remaining_fallback_models,
            eval_fallback_model_chain=fallback_model_chain_from_status(repo_status),
            message=(
                f"Using fallback evaluation model {fallback_model} for subsequent "
                "auto-refine rounds."
            ),
        )
    return True


def repair_limit_message(repair_round, max_repair_rounds):
    if max_repair_rounds == 0:
        return (
            "Evaluation failed and max_repair_rounds=0, so no repair "
            "was attempted."
        )
    return f"Evaluation still failed after {repair_round} repair rounds."


def build_eval_cmd(args, script_dir, paper_name, markdown_path, output_dir, repo_dir, results_dir):
    eval_model = effective_eval_gpt_version(args)
    cmd = build_python_cmd(script_dir, "eval.py") + [
        "--paper_name",
        paper_name,
        "--paper_format",
        "Markdown",
        "--pdf_markdown_path",
        markdown_path,
        "--domain",
        args.domain,
        "--data_dir",
        args.data_dir,
        "--output_dir",
        output_dir,
        "--target_repo_dir",
        repo_dir,
        "--eval_result_dir",
        results_dir,
        "--eval_type",
        args.eval_type,
        "--generated_n",
        str(args.generated_n),
        "--provider",
        args.eval_provider,
        "--gpt_version",
        eval_model,
        "--max_repair_rounds",
        str(args.max_repair_rounds),
        "--papercoder",
    ]
    add_optional_arg(cmd, "--fallback_gpt_versions", eval_fallback_versions_for_command(args))
    return cmd


def build_repair_cmd(args, script_dir, paper_name, markdown_path, output_dir, repo_dir):
    return build_python_cmd(script_dir, "3_coding.py") + [
        "--paper_name",
        paper_name,
        "--paper_format",
        "Markdown",
        "--pdf_markdown_path",
        markdown_path,
        "--domain",
        args.domain,
        "--provider",
        args.reproduce_provider,
        "--gpt_version",
        args.reproduce_gpt_version,
        "--output_dir",
        output_dir,
        "--output_repo_dir",
        repo_dir,
        "--repair_from_eval",
        "--max_repair_rounds",
        str(args.max_repair_rounds),
    ]


def main(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = get_repo_root(script_dir)
    validate_runtime_args(args)

    reproduce_env = get_role_env(
        args.reproduce_provider, args.reproduce_gpt_version, "REPRODUCE"
    )
    eval_fallback_models = parse_eval_fallback_versions(
        args.eval_fallback_gpt_versions
    )
    eval_env = get_role_env(
        args.eval_provider,
        args.eval_gpt_version,
        "EVAL",
        eval_fallback_models,
    )

    validate_provider_env(
        args.reproduce_provider, args.reproduce_gpt_version, reproduce_env
    )
    validate_provider_env(args.eval_provider, args.eval_gpt_version, eval_env)
    for fallback_model in eval_fallback_models:
        validate_provider_env(args.eval_provider, fallback_model, eval_env)

    paper_pdf_path = os.path.abspath(args.paper_pdf_path)
    if not os.path.exists(paper_pdf_path):
        raise FileNotFoundError(paper_pdf_path)

    paper_name = args.paper_name or os.path.splitext(os.path.basename(paper_pdf_path))[0]
    job_id = args.job_id or f"{compact_now_str()}_{paper_name}_{uuid.uuid4().hex[:8]}"

    runs_dir = args.runs_dir
    if not os.path.isabs(runs_dir):
        runs_dir = os.path.abspath(os.path.join(repo_root, runs_dir))

    run_dir = os.path.join(runs_dir, job_id)
    input_dir = os.path.join(run_dir, "input")
    mineru_dir = os.path.join(run_dir, "mineru")
    output_dir = os.path.join(run_dir, "output")
    repo_dir = os.path.join(run_dir, "repo")
    results_dir = os.path.join(run_dir, "results")
    logs_dir = os.path.join(run_dir, "logs")
    status_path = os.path.join(run_dir, "run_status.json")
    summary_path = os.path.join(run_dir, "run_summary.json")

    for directory in [input_dir, mineru_dir, output_dir, repo_dir, results_dir, logs_dir]:
        os.makedirs(directory, exist_ok=True)

    checkpoint_adapter = PipelineCheckpointAdapter(args, run_dir, job_id)

    copied_pdf_path = os.path.join(input_dir, os.path.basename(paper_pdf_path))
    shutil.copy2(paper_pdf_path, copied_pdf_path)

    update_status(
        status_path,
        status="running",
        stage="initializing",
        message="Pipeline initialized",
        job_id=job_id,
        paper_name=paper_name,
        run_dir=run_dir,
        started_at=now_str(),
        reproduce_provider=args.reproduce_provider,
        reproduce_model=args.reproduce_gpt_version,
        eval_provider=args.eval_provider,
        eval_model=args.eval_gpt_version,
        requested_eval_model=args.eval_gpt_version,
        effective_eval_model=effective_eval_gpt_version(args),
        eval_fallback_active=False,
        remaining_eval_fallback_models=current_eval_fallback_models(args),
        auto_refine=args.auto_refine,
        max_repair_rounds=args.max_repair_rounds,
        repair_policy=(
            "single_evaluation"
            if not args.auto_refine
            else "evaluate_only"
            if args.max_repair_rounds == 0
            else "auto_refine"
        ),
        checkpoint_mode=args.checkpoint_mode,
        recovery_count=args.checkpoint_recovery_count,
        resume_from_stage=args.resume_from_stage or None,
    )
    write_summary(
        summary_path,
        job_id=job_id,
        paper_name=paper_name,
        pdf_path=copied_pdf_path,
        run_dir=run_dir,
        mineru_dir=mineru_dir,
        output_dir=output_dir,
        repo_dir=repo_dir,
        results_dir=results_dir,
        status="running",
    )

    if checkpoint_adapter.should_run(1):
        input_stage = "mineru_skipped" if args.skip_mineru else "mineru_parse"
        with checkpoint_adapter.stage(
            input_stage,
            1,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, input_stage
            ),
            resume_from_stage="planning",
        ):
            if args.skip_mineru:
                markdown_path = validate_markdown_path(args.pdf_markdown_path, runs_dir)
                update_status(
                    status_path,
                    status="running",
                    stage="mineru_skipped",
                    message="Skipped MinerU and reused existing Markdown.",
                    markdown_path=markdown_path,
                )
                print("=" * 80)
                print("[PIPELINE] mineru_skipped")
                print(f"Markdown: {markdown_path}")
                print("=" * 80)
            else:
                mineru_executable = args.mineru_executable or default_mineru_executable(script_dir)
                mineru_cmd = [
                    mineru_executable,
                    "-p",
                    copied_pdf_path,
                    "-o",
                    mineru_dir,
                    "-b",
                    args.mineru_backend,
                    "-f",
                    "true" if args.mineru_formula else "false",
                    "-t",
                    "true" if args.mineru_table else "false",
                ]
                run_command(
                    "mineru_parse",
                    mineru_cmd,
                    script_dir,
                    os.path.join(logs_dir, "01_mineru_parse.log"),
                    status_path,
                    console_output=args.console_output,
                )
                markdown_path = discover_markdown(mineru_dir, paper_name)
            if checkpoint_adapter.enabled:
                suffix = Path(markdown_path).suffix.lower()
                checkpoint_markdown = os.path.join(
                    input_dir, f"source_markdown{suffix}"
                )
                if os.path.abspath(markdown_path) != os.path.abspath(checkpoint_markdown):
                    shutil.copy2(markdown_path, checkpoint_markdown)
                markdown_path = checkpoint_markdown
    else:
        markdown_path = resume_markdown_path(run_dir, job_id)
    write_summary(summary_path, markdown_path=markdown_path)

    planning_cmd = build_python_cmd(script_dir, "1_planning.py") + [
        "--paper_name",
        paper_name,
        "--paper_format",
        "Markdown",
        "--pdf_markdown_path",
        markdown_path,
        "--domain",
        args.domain,
        "--provider",
        args.reproduce_provider,
        "--gpt_version",
        args.reproduce_gpt_version,
        "--output_dir",
        output_dir,
    ]
    if checkpoint_adapter.should_run(2):
        with checkpoint_adapter.stage(
            "planning",
            2,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, "planning"
            ),
            resume_from_stage="extract_config",
        ):
            run_command(
                "planning",
                planning_cmd,
                script_dir,
                os.path.join(logs_dir, "02_planning.log"),
                status_path,
                env=reproduce_env,
                console_output=args.console_output,
                cost_context=checkpoint_adapter.cost_context("planning", 2),
            )
            load_task_manifest(output_dir)
    else:
        load_task_manifest(output_dir)

    extract_config_cmd = build_python_cmd(script_dir, "1.1_extract_config.py") + [
        "--paper_name",
        paper_name,
        "--output_dir",
        output_dir,
    ]
    if checkpoint_adapter.should_run(3):
        with checkpoint_adapter.stage(
            "extract_config",
            3,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, "extract_config"
            ),
            resume_from_stage="analyzing",
        ):
            run_command(
                "extract_config",
                extract_config_cmd,
                script_dir,
                os.path.join(logs_dir, "03_extract_config.log"),
                status_path,
                console_output=args.console_output,
            )

    analyzing_cmd = build_python_cmd(script_dir, "2_analyzing.py") + [
        "--paper_name",
        paper_name,
        "--paper_format",
        "Markdown",
        "--pdf_markdown_path",
        markdown_path,
        "--domain",
        args.domain,
        "--provider",
        args.reproduce_provider,
        "--gpt_version",
        args.reproduce_gpt_version,
        "--output_dir",
        output_dir,
    ]
    if checkpoint_adapter.should_run(4):
        with checkpoint_adapter.stage(
            "analyzing",
            4,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, "analyzing"
            ),
            resume_from_stage="coding",
        ):
            run_command(
                "analyzing",
                analyzing_cmd,
                script_dir,
                os.path.join(logs_dir, "04_analyzing.log"),
                status_path,
                env=reproduce_env,
                console_output=args.console_output,
                cost_context=checkpoint_adapter.cost_context("analyzing", 4),
            )

    planning_config_file = validate_task_path("planning_config.yaml")
    planning_config = safe_join(output_dir, planning_config_file)
    if planning_config.exists():
        with open(planning_config, "r", encoding="utf-8") as stream:
            safe_write_text(
                repo_dir,
                validate_task_path("config.yaml"),
                stream.read(),
            )

    coding_cmd = build_python_cmd(script_dir, "3_coding.py") + [
        "--paper_name",
        paper_name,
        "--paper_format",
        "Markdown",
        "--pdf_markdown_path",
        markdown_path,
        "--domain",
        args.domain,
        "--provider",
        args.reproduce_provider,
        "--gpt_version",
        args.reproduce_gpt_version,
        "--output_dir",
        output_dir,
        "--output_repo_dir",
        repo_dir,
    ]
    if checkpoint_adapter.should_run(5):
        with checkpoint_adapter.stage(
            "coding",
            5,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, "coding"
            ),
            resume_from_stage="evaluation",
        ):
            run_command(
                "coding",
                coding_cmd,
                script_dir,
                os.path.join(logs_dir, "05_coding.log"),
                status_path,
                env=reproduce_env,
                console_output=args.console_output,
                cost_context=checkpoint_adapter.cost_context("coding", 5),
            )

    if checkpoint_adapter.enabled and checkpoint_adapter.resume_sequence > 6:
        previous_repo_status = load_json_file(
            repo_status_path(output_dir), default={}
        ) or {}
        remember_fallback_eval_model(
            args, previous_repo_status, status_path
        )

    resume_completed = (
        checkpoint_adapter.enabled
        and checkpoint_adapter.resume_from_stage == "completed"
    )
    last_stage_sequence = 5
    if args.auto_refine and not resume_completed:
        stage_sequence = (
            checkpoint_adapter.resume_sequence
            if checkpoint_adapter.resume_sequence > 5
            else 6
        )
        while True:
            if stage_sequence % 2 == 1:
                repair_round = (stage_sequence - 5) // 2
                repair_cmd = build_repair_cmd(
                    args,
                    script_dir,
                    paper_name,
                    markdown_path,
                    output_dir,
                    repo_dir,
                )
                with checkpoint_adapter.stage(
                    "repair",
                    stage_sequence,
                    artifact_paths=lambda: checkpoint_state_artifact_paths(
                        run_dir, "repair"
                    ),
                    resume_from_stage="evaluation",
                ):
                    run_command(
                        f"repair_round_{repair_round}",
                        repair_cmd,
                        script_dir,
                        os.path.join(logs_dir, f"07_repair_round_{repair_round}.log"),
                        status_path,
                        env=reproduce_env,
                        console_output=args.console_output,
                        cost_context=checkpoint_adapter.cost_context(
                            "repair",
                            stage_sequence,
                            repair_attempt=repair_round,
                        ),
                    )
                last_stage_sequence = stage_sequence
                stage_sequence += 1

            eval_round = (stage_sequence - 6) // 2
            eval_cmd = build_eval_cmd(
                args,
                script_dir,
                paper_name,
                markdown_path,
                output_dir,
                repo_dir,
                results_dir,
            )
            evaluation_next = {"stage": None}
            evaluation_limit_message = {"message": None}
            with checkpoint_adapter.stage(
                "evaluation",
                stage_sequence,
                artifact_paths=lambda: checkpoint_state_artifact_paths(
                    run_dir, "evaluation"
                ),
                resume_from_stage=lambda: evaluation_next["stage"],
            ):
                run_command(
                    f"evaluation_round_{eval_round}",
                    eval_cmd,
                    script_dir,
                    os.path.join(logs_dir, f"06_eval_round_{eval_round}.log"),
                    status_path,
                    env=eval_env,
                    console_output=args.console_output,
                    cost_context=checkpoint_adapter.cost_context(
                        "evaluation",
                        stage_sequence,
                    ),
                )
                repo_status = load_json_file(repo_status_path(output_dir), default={}) or {}
                remember_fallback_eval_model(args, repo_status, status_path)
                repair_decision = decide_repair_action(repo_status)
                if repo_status.get("status") == STATUS_EVAL_PASSED:
                    evaluation_next["stage"] = "completed"
                elif (
                    repo_status.get("status") == STATUS_EVAL_FAILED
                    and repair_decision["status"] == "ready"
                ):
                    evaluation_next["stage"] = "repair"
                elif repo_status.get("status") == STATUS_EVAL_FAILED:
                    evaluation_limit_message["message"] = (
                        repair_limit_message(
                            int(repo_status.get("repair_round", 0) or 0),
                            args.max_repair_rounds,
                        )
                        if repair_decision["reason"] == "repair_limit_reached"
                        else f"Evaluation failed without an allowed repair: {repair_decision['reason']}."
                    )
                else:
                    raise RuntimeError(
                        "Evaluation did not produce a repairable quality failure. "
                        f"Reason: {repair_decision['reason']}. "
                        f"Found status: {repo_status.get('status')!r}"
                    )
            last_stage_sequence = stage_sequence
            if evaluation_next["stage"] == "completed":
                break
            if evaluation_limit_message["message"]:
                message = evaluation_limit_message["message"]
                if args.max_repair_rounds == 0:
                    update_status(
                        status_path,
                        status="failed",
                        stage="evaluation_no_repair",
                        message=message,
                        repo_status=repo_status,
                    )
                raise RuntimeError(message)
            stage_sequence += 1
    elif not args.auto_refine and not resume_completed:
        stage_sequence = 6
        eval_cmd = build_eval_cmd(
            args,
            script_dir,
            paper_name,
            markdown_path,
            output_dir,
            repo_dir,
            results_dir,
        )
        with checkpoint_adapter.stage(
            "evaluation",
            stage_sequence,
            artifact_paths=lambda: checkpoint_state_artifact_paths(
                run_dir, "evaluation"
            ),
            resume_from_stage="completed",
        ):
            run_command(
                "evaluation",
                eval_cmd,
                script_dir,
                os.path.join(logs_dir, "06_evaluation.log"),
                status_path,
                env=eval_env,
                console_output=args.console_output,
                cost_context=checkpoint_adapter.cost_context(
                    "evaluation",
                    stage_sequence,
                ),
            )
            repo_status = load_json_file(repo_status_path(output_dir), default={}) or {}
            remember_fallback_eval_model(args, repo_status, status_path)
        last_stage_sequence = stage_sequence

    completion_sequence = (
        checkpoint_adapter.resume_sequence
        if resume_completed
        else last_stage_sequence + 1
    )

    with checkpoint_adapter.stage(
        "completed",
        completion_sequence,
        artifact_paths=lambda: checkpoint_state_artifact_paths(
            run_dir, "completed"
        ),
        resume_from_stage=None,
    ):
        repo_status = load_json_file(repo_status_path(output_dir), default={}) or {}
        final_status = repo_status.get("status", "unknown")
        update_status(
            status_path,
            status="completed",
            stage="completed",
            message=f"Pipeline completed with repository status: {final_status}",
            repo_status=repo_status,
            completed_at=now_str(),
        )
        write_summary(
            summary_path,
            status=final_status,
            repo_status=repo_status,
            eval_score=repo_status.get("eval_score"),
            repair_round=repo_status.get("repair_round"),
            latest_eval_result=repo_status.get("eval_result_file"),
            feedback_file=repo_status.get("feedback_file"),
            files=sorted(
                [
                    file_name
                    for file_name in os.listdir(repo_dir)
                    if os.path.isfile(os.path.join(repo_dir, file_name))
                ]
            ),
        )

    print("=" * 80)
    print("[PIPELINE] Completed")
    print(f"Run directory: {run_dir}")
    print(f"Repository: {repo_dir}")
    print(f"Status: {final_status}")
    print(f"Summary: {summary_path}")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper_pdf_path", type=str, required=True)
    parser.add_argument("--skip_mineru", action="store_true")
    parser.add_argument("--pdf_markdown_path", type=str, default="")
    parser.add_argument("--paper_name", type=str, default="")
    parser.add_argument(
        "--domain",
        type=str,
        default="statistics",
        choices=["general", "statistics"],
    )
    parser.add_argument(
        "--reproduce_provider",
        type=str,
        required=True,
        choices=PROVIDER_REGISTRY.provider_ids,
    )
    parser.add_argument("--reproduce_gpt_version", type=str, required=True)
    parser.add_argument(
        "--eval_provider",
        type=str,
        required=True,
        choices=PROVIDER_REGISTRY.provider_ids,
    )
    parser.add_argument("--eval_gpt_version", type=str, required=True)
    parser.add_argument("--eval_fallback_gpt_versions", type=str, default="")
    parser.add_argument("--runs_dir", type=str, default="runs")
    parser.add_argument("--job_id", type=str, default="")
    parser.add_argument(
        "--checkpoint_mode",
        type=str,
        default="off",
        choices=["off", "sqlite"],
    )
    parser.add_argument("--resume_from_stage", type=str, default="")
    parser.add_argument("--resume_stage_sequence", type=int, default=0)
    parser.add_argument("--resume_stage_attempt", type=int, default=0)
    parser.add_argument("--checkpoint_recovery_count", type=int, default=0)
    parser.add_argument("--mineru_executable", type=str, default="")
    parser.add_argument("--mineru_backend", type=str, default="pipeline")
    parser.add_argument("--mineru_formula", type=parse_bool, default=True)
    parser.add_argument("--mineru_table", type=parse_bool, default=True)
    parser.add_argument("--data_dir", type=str, default="../data")
    parser.add_argument(
        "--eval_type",
        type=str,
        default="ref_free",
        choices=["ref_free", "ref_based"],
    )
    parser.add_argument("--generated_n", type=int, default=8)
    parser.add_argument("--auto_refine", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max_repair_rounds", type=int, default=MAX_REPAIR_ROUNDS)
    parser.add_argument(
        "--console_output",
        type=str,
        default="progress",
        choices=["progress", "full", "quiet"],
    )

    main(parser.parse_args())
