import json
import os
from tqdm import tqdm
import re
import sys
import copy
from utils import (
    extract_planning,
    extract_code_from_content,
    print_response,
    print_log_cost,
    load_accumulated_cost,
    save_accumulated_cost,
    make_openai_client,
    normalize_completion,
    get_completion_message,
    load_paper_content,
    MAX_REPAIR_ROUNDS,
    STATUS_PENDING_EVAL,
    eval_feedback_path as default_eval_feedback_path,
    load_json_file,
    repo_status_path,
    write_repo_status,
)
from evaluation_contract import decide_repair_action, resolve_files_to_fix
from task_manifest import (
    load_task_manifest,
    safe_join,
    safe_write_text,
    task_artifact_key,
    validate_task_path,
)
import argparse

parser = argparse.ArgumentParser()

parser.add_argument('--paper_name',type=str)
parser.add_argument('--provider',type=str, required=True)
parser.add_argument('--gpt_version',type=str, required=True)
parser.add_argument('--paper_format',type=str, default="JSON", choices=["JSON", "LaTeX", "Markdown"])
parser.add_argument('--pdf_json_path', type=str) # json format
parser.add_argument('--pdf_latex_path', type=str) # latex format
parser.add_argument('--pdf_markdown_path', type=str) # markdown format
parser.add_argument('--domain', type=str, default="general", choices=["general", "statistics"])
parser.add_argument('--output_dir',type=str, default="")
parser.add_argument('--output_repo_dir',type=str, default="")
parser.add_argument('--repair_from_eval', action="store_true")
parser.add_argument('--eval_feedback_path', type=str, default="")
parser.add_argument('--max_repair_rounds', type=int, default=MAX_REPAIR_ROUNDS)

args    = parser.parse_args()
paper_name = args.paper_name
gpt_version = args.gpt_version
provider_id = args.provider
paper_format = args.paper_format
pdf_json_path = args.pdf_json_path
pdf_latex_path = args.pdf_latex_path
pdf_markdown_path = args.pdf_markdown_path
domain = args.domain
output_dir = args.output_dir
output_repo_dir = args.output_repo_dir
repair_from_eval = args.repair_from_eval
eval_feedback_file = args.eval_feedback_path or default_eval_feedback_path(output_dir)
max_repair_rounds = args.max_repair_rounds
client = make_openai_client(provider_id, gpt_version)

paper_content = load_paper_content(
    paper_format,
    json_path=pdf_json_path,
    latex_path=pdf_latex_path,
    markdown_path=pdf_markdown_path,
)

with open(f'{output_dir}/planning_config.yaml', encoding="utf-8") as f:
    config_yaml = f.read()

context_lst = extract_planning(f'{output_dir}/planning_trajectories.json')
# 0: overview, 1: detailed, 2: PRD
task_manifest = load_task_manifest(output_dir)
todo_file_lst = task_manifest.paths
task_file_by_path = {task.relative_path: task for task in task_manifest.files}
done_file_lst = ['config.yaml']
done_file_dict = {}

repair_feedback = None
repair_files = set()
repo_status = load_json_file(repo_status_path(output_dir), default={}) or {}
current_repair_round = int(repo_status.get("repair_round", 0) or 0)

if repair_from_eval:
    repair_feedback = load_json_file(eval_feedback_file, default=None)
    if repair_feedback is None:
        raise FileNotFoundError(
            f"Evaluation feedback file not found: {eval_feedback_file}"
        )
    if repair_feedback.get("passed"):
        print("[INFO] Evaluation already passed. No repair is needed.")
        sys.exit(0)
    feedback_repair_round = int(repair_feedback.get("repair_round", 0) or 0)
    current_repair_round = max(current_repair_round, feedback_repair_round)
    feedback_for_decision = {
        **repair_feedback,
        "repair_round": current_repair_round,
        "max_repair_rounds": max_repair_rounds,
    }
    repair_decision = decide_repair_action(feedback_for_decision)

    if repair_decision["status"] == "skipped":
        print(
            "[INFO] Repair skipped: "
            f"{repair_decision['reason']}."
        )
        sys.exit(0)
    if repair_decision["status"] != "ready":
        raise RuntimeError(
            "Repair is not allowed by the evaluation contract: "
            f"{repair_decision['reason']}."
        )

    repair_files = set(
        resolve_files_to_fix(
            repair_decision["files_to_fix"],
            task_manifest,
            repo_root=output_repo_dir,
        )
    )

    print(
        f"[INFO] Repair mode enabled. Repair round "
        f"{current_repair_round + 1}/{max_repair_rounds}."
    )
    print(f"[INFO] Files to repair: {', '.join(sorted(repair_files))}")

    for todo_file_name in todo_file_lst:
        if todo_file_name.endswith((".yaml", ".yml")):
            continue
        file_path = safe_join(output_repo_dir, task_file_by_path[todo_file_name])
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                done_file_dict[todo_file_name] = f.read()
            done_file_lst.append(todo_file_name)

code_msg = [
    {"role": "system", "content": f"""You are an expert researcher and software engineer with a deep understanding of experimental design and reproducibility in scientific research.
You will receive a research paper in {paper_format} format, an overview of the plan, a Design in JSON format consisting of "Implementation approach", "File list", "Data structures and interfaces", and "Program call flow", followed by a Task in JSON format that includes "Required packages", "Required other language third-party packages", "Logic Analysis", and "Task list", along with a configuration file named "config.yaml". 
Your task is to write code to reproduce the experiments and methodologies described in the paper. 

The code you write must be elegant, modular, and maintainable. For Python repositories, adhere to Google-style guidelines. For R repositories, write idiomatic, concise R code with clear functions and explicit arguments.
The code must strictly align with the paper's methodology, experimental setup, and evaluation metrics. 
Write code with triple quoto."""}]

if domain == "statistics":
    code_msg.append({
        "role": "system",
        "content": """Statistics domain coding constraints:
- Use R as the implementation language. Implement only the .R files in the task list plus config.yaml.
- Focus on simulation.R, estimators.R, experiments.R, main.R, and config.yaml when they are in the task list.
- Do not create or assume model.R, metrics.R, utils.R, Python files, trainer.py, dataset_loader.py, neural-network training loops, epochs, mini-batches, or dataloaders unless explicitly required by the paper and already present in the task list.
- estimators.R must implement every estimation method from the paper with the required objective/log-likelihood/moment functions, parameter checks, safe log/probability calculations, initialization, constraints, optimization details, convergence diagnostics, and standard errors/confidence intervals when reported.
- simulation.R must implement the paper's DGP/scenarios, including true parameters, sample sizes, noise distributions, dependence structures, censoring/missingness, or any other mechanism described in the paper.
- experiments.R must define the evaluation metrics before the main experiment body. Use short comments to state each metric name and formula, then run Monte Carlo replications or empirical-data fitting workflows, record the seeds used, aggregate results, and produce table-ready outputs.
- main.R must begin with two separated numbered comment sections. The headings must be exactly `### 1. Paper details requiring assumptions:` and `### 2. Output locations:`. Then create result directories, save seed records and result tables, and call experiments.R. Put directory creation and table saving here.
- Prefer compact, paper-faithful functions over large class-like abstractions. Do not restate the paper in long comments.
- Output raw R source code only for .R files: no Markdown fences, no "## Code:" header, no explanatory prose.
"""
    })

def get_language_label(file_name):
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".r":
        return "r"
    if ext in [".yaml", ".yml"]:
        return "yaml"
    if ext == ".py":
        return "python"
    return ""

def get_write_msg(todo_file_name, detailed_logic_analysis, done_file_lst): 
    code_files = ""
    for done_file in done_file_lst:
        if done_file.endswith(".yaml"): continue
        language_label = get_language_label(done_file)
        code_files += f"""
```{language_label}
{done_file_dict[done_file]}
```

"""

    if domain == "statistics":
        format_example = f"""# Format example
Return only valid R source code for {todo_file_name}.
Do not wrap the answer in markdown code fences.
Do not include "## Code:", "## {todo_file_name}", or any prose outside the code."""
        language_instruction = f"""Based on the paper, plan, design, task and configuration file(config.yaml) specified previously, write the R code.

We have {done_file_lst}.
Next, you must write only the "{todo_file_name}".
1. Only One file: implement THIS ONLY ONE FILE.
2. Pure R source: return only executable R code, with no markdown fences and no file-name header.
3. Concise implementation: prefer small functions, explicit arguments, and simple lists/data frames. Avoid long narrative comments and large class-like wrappers unless essential.
4. Mathematical fidelity: implement formulas, distributions, likelihoods, constraints, estimators, simulation DGP, and metrics exactly as described in the paper.
5. R dependencies: use base R and the R packages listed in the task/config when needed. Use `source("...")` for sibling project files when the current file depends on them.
6. Configuration: use values from config.yaml. Do not fabricate configuration values. If a paper value is unclear, expose it as a named argument or read it from config.
7. Complete code: no TODO, no pseudocode, no placeholder functions.
8. Keep helper logic local: put parameter validation and safe log/probability calculations directly in estimators.R; put result directory creation and table saving directly in main.R.
9. experiments.R requirements: before the numerical simulation or empirical fitting body, define the paper metrics and add short comments with each metric formula. If seeds are needed, create and preserve a seed record before running replications.
10. main.R requirements: the file must begin with the following two comment sections, using exactly three # characters, one space, and numbered headings:

### 1. Paper details requiring assumptions:
# - List each item from Anything UNCLEAR or write "None identified from planning." if there are no unclear details.

### 2. Output locations:
# - Raw estimates: results/raw_estimates.csv
# - Summary tables: results/summary_metrics.csv
# - Seed records: results/seeds.csv
# - Logs: results/run_log.txt

{detailed_logic_analysis}"""
    else:
        format_example = f"""# Format example
## Code: {todo_file_name}
```python
## {todo_file_name}
...
```"""
        language_instruction = f"""Based on the paper, plan, design, task and configuration file(config.yaml) specified previously, follow "Format example", write the code.

We have {done_file_lst}.
Next, you must write only the "{todo_file_name}".
1. Only One file: do your best to implement THIS ONLY ONE FILE.
2. COMPLETE CODE: Your code will be part of the entire project, so please implement complete, reliable, reusable code snippets.
3. Set default value: If there is any setting, ALWAYS SET A DEFAULT VALUE, ALWAYS USE STRONG TYPE AND EXPLICIT VARIABLE. AVOID circular import.
4. Follow design: YOU MUST FOLLOW "Data structures and interfaces". DONT CHANGE ANY DESIGN. Do not use public member functions that do not exist in your design.
5. CAREFULLY CHECK THAT YOU DONT MISS ANY NECESSARY CLASS/FUNCTION IN THIS FILE.
6. Before using a external variable/module, make sure you import it first.
7. Write out EVERY CODE DETAIL, DON'T LEAVE TODO.
8. REFER TO CONFIGURATION: you must use configuration from "config.yaml". DO NOT FABRICATE any configuration values.

{detailed_logic_analysis}

## Code: {todo_file_name}"""

    write_msg=[
{'role': 'user', "content": f"""# Context
## Paper
{paper_content}

-----

## Overview of the plan
{context_lst[0]}

-----

## Design
{context_lst[1]}

-----

## Task
{context_lst[2]}

-----

## Configuration file
```yaml
{config_yaml}
```
-----

## Code Files
{code_files}

-----

{format_example}

-----

# Instruction
{language_instruction}"""}]
    return write_msg


def get_file_feedback(todo_file_name):
    if not repair_feedback:
        return ""

    findings = repair_feedback.get("findings") or []
    relevant = []
    for finding in findings:
        file_name = str(finding.get("file_name") or "")
        if todo_file_name in file_name or os.path.basename(todo_file_name) in file_name:
            relevant.append(finding)

    if not relevant:
        return repair_feedback.get("summary", "")

    lines = []
    for finding in relevant:
        func = finding.get("func_name") or ""
        func_part = f"::{func}" if func else ""
        lines.append(
            f"[{finding.get('severity_level', 'unknown')}] "
            f"{finding.get('file_name', todo_file_name)}{func_part}: "
            f"{finding.get('critique', '')}"
        )
    return "\n".join(lines)


def get_repair_msg(todo_file_name, detailed_logic_analysis):
    current_code = done_file_dict.get(todo_file_name, "")
    file_feedback = get_file_feedback(todo_file_name)
    all_feedback = repair_feedback.get("summary", "") if repair_feedback else ""
    language_label = get_language_label(todo_file_name)

    return [{
        "role": "user",
        "content": f"""# Repair Context
## Paper
{paper_content}

-----

## Overview of the plan
{context_lst[0]}

-----

## Design
{context_lst[1]}

-----

## Task
{context_lst[2]}

-----

## Configuration file
```yaml
{config_yaml}
```

-----

## Current file to repair: {todo_file_name}
```{language_label}
{current_code}
```

-----

## Evaluation feedback for this file
{file_feedback}

-----

## Full evaluation feedback summary
{all_feedback}

-----

## Previous logic analysis for this file
{detailed_logic_analysis}

-----

# Instruction
Repair only "{todo_file_name}" according to the evaluation feedback.
Preserve correct existing behavior that is not criticized.
Do not rewrite unrelated files.
Do not add TODOs or placeholders.
For R files, output raw executable R source code only: no Markdown fences, no "## Code:" header, and no prose outside the code.
For Python files, output only the full corrected source code.
"""}
    ]


def api_call(msg):
    request = {"model": gpt_version, "messages": msg, **client.contract.request_options}
    return client.chat.completions.create(**request)


def analysis_artifact_path(task_file, suffix):
    artifact_file = validate_task_path(f"{task_artifact_key(task_file)}{suffix}")
    artifact_path = safe_join(output_dir, artifact_file)
    if artifact_path.exists():
        return artifact_path

    # Read-only compatibility for artifacts produced before TaskManifest v1.
    legacy_key = task_file.relative_path.replace("/", "_")
    legacy_file = validate_task_path(f"{legacy_key}{suffix}")
    legacy_matches = 0
    for manifest_file in task_manifest.files:
        manifest_legacy_key = manifest_file.relative_path.replace("/", "_")
        manifest_legacy_file = validate_task_path(f"{manifest_legacy_key}{suffix}")
        if manifest_legacy_file.canonical_key == legacy_file.canonical_key:
            legacy_matches += 1
    if legacy_matches != 1:
        return artifact_path
    legacy_path = safe_join(output_dir, legacy_file)
    return legacy_path if legacy_path.exists() else artifact_path
    

# testing for checking
detailed_logic_analysis_dict = {}
retrieved_section_dict = {}
for todo_file_name in todo_file_lst:
    # simple analysis
    if todo_file_name == "config.yaml":
        continue

    task_file = task_file_by_path[todo_file_name]
    analysis_path = analysis_artifact_path(
        task_file,
        "_simple_analysis_response.json",
    )
    with open(analysis_path, encoding="utf-8") as f:
        detailed_logic_analysis_response = json.load(f)
    detailed_logic_analysis_dict[todo_file_name] = detailed_logic_analysis_response[0]['choices'][0]['message']['content']

artifact_output_dir=f'{output_dir}/coding_artifacts'
os.makedirs(artifact_output_dir, exist_ok=True)

total_accumulated_cost = load_accumulated_cost(f"{output_dir}/accumulated_cost.json")
for todo_idx, todo_file_name in enumerate(tqdm(todo_file_lst)):
    task_file = task_file_by_path[todo_file_name]
    responses = []
    trajectories = copy.deepcopy(code_msg)

    current_stage = f"[REPAIR] {todo_file_name}" if repair_from_eval else f"[CODING] {todo_file_name}"
    print(current_stage)

    if todo_file_name == "config.yaml":
        continue

    if repair_from_eval and todo_file_name not in repair_files:
        print(f"[SKIP] {todo_file_name} has no evaluation feedback.")
        continue

    if repair_from_eval:
        instruction_msg = get_repair_msg(
            todo_file_name,
            detailed_logic_analysis_dict[todo_file_name],
        )
    else:
        instruction_msg = get_write_msg(
            todo_file_name,
            detailed_logic_analysis_dict[todo_file_name],
            done_file_lst,
        )
    trajectories.extend(instruction_msg)

    completion = api_call(trajectories)
    # print(completion.choices[0].message)
    
    # response
    completion_json = normalize_completion(completion, gpt_version)
    responses.append(completion_json)

    # trajectories
    message = get_completion_message(completion_json)
    trajectories.append({'role': message["role"], 'content': message["content"]})

    if todo_file_name not in done_file_lst:
        done_file_lst.append(todo_file_name)

    # save
    # save_dir_name = f"{paper_name}_repo"
    # print and logging
    print_response(completion_json)
    temp_total_accumulated_cost = print_log_cost(completion_json, gpt_version, current_stage, output_dir, total_accumulated_cost, provider_id)
    total_accumulated_cost = temp_total_accumulated_cost

    # save artifacts
    safe_write_text(
        artifact_output_dir,
        validate_task_path(f"{task_artifact_key(task_file)}_coding.txt"),
        completion_json['choices'][0]['message']['content'],
    )


    # extract code save 
    code = extract_code_from_content(message["content"])
    if len(code) == 0:
        code = message["content"]

    done_file_dict[todo_file_name] = code
    safe_write_text(output_repo_dir, task_file, code)

save_accumulated_cost(f"{output_dir}/accumulated_cost.json", total_accumulated_cost)

new_repair_round = current_repair_round + 1 if repair_from_eval else current_repair_round
completed_repair_attempts = list(repo_status.get("completed_repair_attempts") or [])
if repair_from_eval:
    completed_repair_attempts.append(
        {
            "attempt": new_repair_round,
            "status": "completed",
            "result": "pending_evaluation",
            "reason": repair_decision["reason"],
            "files_to_fix": sorted(repair_files),
        }
    )
write_repo_status(
    output_dir,
    STATUS_PENDING_EVAL,
    paper_name=paper_name,
    target_repo_dir=output_repo_dir,
    eval_status_before_repair=repo_status.get("status"),
    repair_round=new_repair_round,
    max_repair_rounds=max_repair_rounds,
    repair_status="completed" if repair_from_eval else "not_applicable",
    repair_attempt=new_repair_round if repair_from_eval else 0,
    repair_reason=repair_decision["reason"] if repair_from_eval else "",
    repaired_from_feedback=repair_from_eval,
    repaired_files=sorted(repair_files) if repair_from_eval else [],
    files_to_fix=sorted(repair_files) if repair_from_eval else [],
    completed_repair_attempts=completed_repair_attempts,
    feedback_file=eval_feedback_file if repair_from_eval else "",
)
