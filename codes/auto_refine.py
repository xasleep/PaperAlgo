import argparse
import os
import subprocess
import sys

from utils import (
    MAX_REPAIR_ROUNDS,
    STATUS_EVAL_FAILED,
    STATUS_EVAL_PASSED,
    STATUS_PENDING_EVAL,
    load_json_file,
    repo_status_path,
)


def add_optional_arg(cmd, flag, value):
    if value:
        cmd.extend([flag, value])


def run_command(label, cmd, cwd):
    print("=" * 80)
    print(label)
    print(" ".join(cmd))
    print("=" * 80)
    subprocess.run(cmd, cwd=cwd, check=True)


def build_eval_cmd(args, script_dir):
    cmd = [
        sys.executable,
        os.path.join(script_dir, "eval.py"),
        "--paper_name",
        args.paper_name,
        "--paper_format",
        args.paper_format,
        "--domain",
        args.domain,
        "--data_dir",
        args.data_dir,
        "--output_dir",
        args.output_dir,
        "--target_repo_dir",
        args.target_repo_dir,
        "--eval_result_dir",
        args.eval_result_dir,
        "--eval_type",
        args.eval_type,
        "--generated_n",
        str(args.generated_n),
        "--gpt_version",
        args.eval_gpt_version,
    ]
    add_optional_arg(cmd, "--pdf_json_path", args.pdf_json_path)
    add_optional_arg(cmd, "--pdf_latex_path", args.pdf_latex_path)
    add_optional_arg(cmd, "--pdf_markdown_path", args.pdf_markdown_path)
    add_optional_arg(cmd, "--gold_repo_dir", args.gold_repo_dir)
    add_optional_arg(cmd, "--selected_file_path", args.selected_file_path)
    if args.papercoder:
        cmd.append("--papercoder")
    return cmd


def build_repair_cmd(args, script_dir):
    cmd = [
        sys.executable,
        os.path.join(script_dir, "3_coding.py"),
        "--paper_name",
        args.paper_name,
        "--paper_format",
        args.paper_format,
        "--domain",
        args.domain,
        "--gpt_version",
        args.repair_gpt_version,
        "--output_dir",
        args.output_dir,
        "--output_repo_dir",
        args.target_repo_dir,
        "--repair_from_eval",
        "--max_repair_rounds",
        str(args.max_repair_rounds),
    ]
    add_optional_arg(cmd, "--pdf_json_path", args.pdf_json_path)
    add_optional_arg(cmd, "--pdf_latex_path", args.pdf_latex_path)
    add_optional_arg(cmd, "--pdf_markdown_path", args.pdf_markdown_path)
    add_optional_arg(cmd, "--eval_feedback_path", args.eval_feedback_path)
    return cmd


def main(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    status_output_dir = args.output_dir
    if not os.path.isabs(status_output_dir):
        status_output_dir = os.path.abspath(os.path.join(script_dir, status_output_dir))
    status_file = repo_status_path(status_output_dir)

    while True:
        eval_cmd = build_eval_cmd(args, script_dir)
        run_command("[AUTO-REFINE] Evaluation", eval_cmd, script_dir)

        status = load_json_file(status_file, default={}) or {}
        current_status = status.get("status")
        repair_round = int(status.get("repair_round", 0) or 0)
        score = status.get("eval_score")
        has_high = status.get("has_high_severity")

        print("=" * 80)
        print("[AUTO-REFINE] Current status")
        print(f"Status: {current_status}")
        print(f"Score: {score}")
        print(f"Has high severity: {has_high}")
        print(f"Repair round: {repair_round}/{args.max_repair_rounds}")
        print("=" * 80)

        if current_status == STATUS_EVAL_PASSED:
            print("[AUTO-REFINE] Repository passed evaluation.")
            return

        if current_status != STATUS_EVAL_FAILED:
            raise RuntimeError(
                "Evaluation did not produce a recognized failed/passed status. "
                f"Found status: {current_status!r}"
            )

        if repair_round >= args.max_repair_rounds:
            raise RuntimeError(
                f"Evaluation still failed after {repair_round} repair rounds. "
                "Stopping to avoid an infinite repair loop."
            )

        repair_cmd = build_repair_cmd(args, script_dir)
        run_command("[AUTO-REFINE] Repair", repair_cmd, script_dir)

        repaired_status = load_json_file(status_file, default={}) or {}
        if repaired_status.get("status") != STATUS_PENDING_EVAL:
            raise RuntimeError(
                "Repair did not return the repository to pending-evaluation status. "
                f"Found status: {repaired_status.get('status')!r}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper_name", type=str, required=True)
    parser.add_argument(
        "--paper_format",
        type=str,
        default="JSON",
        choices=["JSON", "LaTeX", "Markdown"],
    )
    parser.add_argument(
        "--domain",
        type=str,
        default="general",
        choices=["general", "statistics"],
    )
    parser.add_argument("--pdf_json_path", type=str)
    parser.add_argument("--pdf_latex_path", type=str)
    parser.add_argument("--pdf_markdown_path", type=str)
    parser.add_argument("--data_dir", type=str, default="../data")
    parser.add_argument("--output_dir", type=str, required=True)
    parser.add_argument("--target_repo_dir", type=str, required=True)
    parser.add_argument("--eval_result_dir", type=str, required=True)
    parser.add_argument(
        "--eval_type",
        type=str,
        default="ref_free",
        choices=["ref_free", "ref_based"],
    )
    parser.add_argument("--generated_n", type=int, default=8)
    parser.add_argument("--eval_gpt_version", type=str, required=True)
    parser.add_argument("--repair_gpt_version", type=str, required=True)
    parser.add_argument("--gold_repo_dir", type=str, default="")
    parser.add_argument("--selected_file_path", type=str, default="")
    parser.add_argument("--eval_feedback_path", type=str, default="")
    parser.add_argument("--max_repair_rounds", type=int, default=MAX_REPAIR_ROUNDS)
    parser.add_argument("--papercoder", action="store_true")

    main(parser.parse_args())
