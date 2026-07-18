import json
import re
import os
import argparse
from utils import extract_planning, content_to_json, format_json_data
from task_manifest import safe_write_text, validate_task_path

parser = argparse.ArgumentParser()

parser.add_argument('--paper_name',type=str)
parser.add_argument('--output_dir',type=str, default="")

args    = parser.parse_args()

output_dir = args.output_dir

with open(f'{output_dir}/planning_trajectories.json', encoding='utf8') as f:
    traj = json.load(f)

yaml_raw_content = ""
for turn in reversed(traj):
    if turn.get("role") != "assistant":
        continue

    content = turn.get("content", "")
    if "```yaml" in content or "## config.yaml" in content:
        yaml_raw_content = content
        break

if "</think>" in yaml_raw_content:
    yaml_raw_content = yaml_raw_content.split("</think>")[-1]

match = re.search(r"```yaml\n(.*?)\n```", yaml_raw_content, re.DOTALL)
if match:
    yaml_content = match.group(1)
else:
    # print("No YAML content found.")
    match2 = re.search(r"```yaml\\n(.*?)\\n```", yaml_raw_content, re.DOTALL)
    if match2:
        yaml_content = match2.group(1)
    else:
        raise ValueError("Planning response did not contain config.yaml YAML content.")

safe_write_text(
    output_dir,
    validate_task_path("planning_config.yaml"),
    yaml_content,
)

# ---------------------------------------

artifact_output_dir=f"{output_dir}/planning_artifacts"

os.makedirs(artifact_output_dir, exist_ok=True)

context_lst = extract_planning(f'{output_dir}/planning_trajectories.json')

arch_design = content_to_json(context_lst[1])
logic_design = content_to_json(context_lst[2])

formatted_arch_design = format_json_data(arch_design)
formatted_logic_design = format_json_data(logic_design)

safe_write_text(
    artifact_output_dir,
    validate_task_path("1.1_overall_plan.txt"),
    context_lst[0],
)
safe_write_text(
    artifact_output_dir,
    validate_task_path("1.2_arch_design.txt"),
    formatted_arch_design,
)
safe_write_text(
    artifact_output_dir,
    validate_task_path("1.3_logic_design.txt"),
    formatted_logic_design,
)
safe_write_text(
    artifact_output_dir,
    validate_task_path("1.4_config.yaml"),
    yaml_content,
)
