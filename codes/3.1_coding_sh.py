import json
import os
from tqdm import tqdm
import sys
import copy
from utils import extract_code_from_content, print_response, print_log_cost, load_accumulated_cost, save_accumulated_cost, make_openai_client
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
parser.add_argument('--gpt_version',type=str, default="o3-mini")
parser.add_argument('--paper_format',type=str, default="JSON", choices=["JSON", "LaTeX"])
parser.add_argument('--pdf_json_path', type=str) # json format
parser.add_argument('--pdf_latex_path', type=str) # latex format
parser.add_argument('--output_dir',type=str, default="")
parser.add_argument('--output_repo_dir',type=str, default="")

args    = parser.parse_args()
client = make_openai_client()

paper_name = args.paper_name
gpt_version = args.gpt_version
paper_format = args.paper_format
pdf_json_path = args.pdf_json_path
pdf_latex_path = args.pdf_latex_path
output_dir = args.output_dir
output_repo_dir = args.output_repo_dir

if paper_format == "JSON":
    with open(f'{pdf_json_path}', encoding="utf-8") as f:
        paper_content = json.load(f)
elif paper_format == "LaTeX":
    with open(f'{pdf_latex_path}', encoding="utf-8") as f:
        paper_content = f.read()
else:
    print(f"[ERROR] Invalid paper format. Please select either 'JSON' or 'LaTeX.")
    sys.exit(0)

with open(f'{output_dir}/planning_config.yaml', encoding="utf-8") as f:
    config_yaml = f.read()

task_manifest = load_task_manifest(output_dir)
todo_file_lst = task_manifest.paths
done_file_lst = ['config.yaml']
done_file_dict = {}

code_msg = [
    {"role": "system", "content": f"""You are an expert researcher and software engineer with a deep understanding of experimental design and reproducibility in scientific research.
You will receive configuration file named "config.yaml", and implmented code repository. 
Your task is to write a Bash script that can run the given repository from scratch. The script should create and activate the required environment, install all dependencies, and include the commands needed to execute the main file or entry point. Make sure the script is self-contained and can be executed without any manual setup.
     
Write code with triple quoto."""}]

def get_write_msg(todo_file_name, done_file_lst): 
    code_files = ""
    for done_file in done_file_lst:
        if done_file.endswith(".yaml"): continue
        code_files += f"""
```python
{done_file_dict[done_file]}
```

"""

    write_msg=[
{'role': 'user', "content": f"""# Context

## Configuration file
```yaml
{config_yaml}
```
-----

## Code Files
{code_files}

-----

# Format example
## Code: {todo_file_name}
```python
## {todo_file_name}
...
```

-----

# Instruction
Based on the code files, follow "Format example", write the code. 

We have {done_file_lst}.
Next, you must write only the "{todo_file_name}".

## Code: {todo_file_name}"""}]
    return write_msg


def api_call(msg):
    if "o3-mini" in gpt_version or "o4-mini" in gpt_version:
        completion = client.chat.completions.create(
            model=gpt_version, 
            reasoning_effort="high",
            messages=msg
        )
    else:
        completion = client.chat.completions.create(
            model=gpt_version, 
            messages=msg
        )
    return completion
    

artifact_output_dir=f'{output_dir}/coding_artifacts'
os.makedirs(artifact_output_dir, exist_ok=True)

python_dict = {}
for task_file in task_manifest.files:
    if task_file.relative_path.endswith((".yaml", ".yml")):
        continue
    task_path = safe_join(output_repo_dir, task_file)
    if task_path.exists():
        with open(task_path, "r", encoding="utf-8") as task_stream:
            python_dict[task_file.relative_path] = task_stream.read()

for todo_idx, todo_file_name in enumerate(tqdm(todo_file_lst)):
    if todo_file_name == "config.yaml":
        continue
    
    done_file_dict[todo_file_name] = python_dict[todo_file_name]
    done_file_lst.append(todo_file_name)


total_accumulated_cost = load_accumulated_cost(f"{output_dir}/accumulated_cost.json")
reproduce_task = validate_task_path("reproduce.sh")
for todo_idx, todo_file_name in enumerate([reproduce_task.relative_path]):
    responses = []
    trajectories = copy.deepcopy(code_msg)

    current_stage = f"[CODING] {todo_file_name}"
    print(current_stage)

    if todo_file_name == "config.yaml":
        continue

    instruction_msg = get_write_msg(todo_file_name, done_file_lst)
    trajectories.extend(instruction_msg)

    completion = api_call(trajectories)
    # print(completion.choices[0].message)
    
    # response
    completion_json = json.loads(completion.model_dump_json())
    responses.append(completion_json)

    # trajectories
    message = completion.choices[0].message
    trajectories.append({'role': message.role, 'content': message.content})

    done_file_lst.append(todo_file_name)

    # save
    # save_dir_name = f"{paper_name}_repo"
    # print and logging
    print_response(completion_json)
    temp_total_accumulated_cost = print_log_cost(completion_json, gpt_version, current_stage, output_dir, total_accumulated_cost)
    total_accumulated_cost = temp_total_accumulated_cost

    # save artifacts
    safe_write_text(
        artifact_output_dir,
        validate_task_path(f"{task_artifact_key(reproduce_task)}_coding.txt"),
        completion_json['choices'][0]['message']['content'],
    )


    # extract code save 
    code = extract_code_from_content(message.content)
    if len(code) == 0:
        code = message.content 

    done_file_dict[todo_file_name] = code
    safe_write_text(output_repo_dir, reproduce_task, code)

save_accumulated_cost(f"{output_dir}/accumulated_cost.json", total_accumulated_cost)
