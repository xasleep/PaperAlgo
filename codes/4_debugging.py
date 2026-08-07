import os
import json
import argparse
import re

from utils import make_openai_client
from task_manifest import (
    InvalidTaskPathError,
    TaskManifest,
    load_task_manifest,
    read_manifest_text_files,
    safe_join,
    safe_write_text,
    task_artifact_key,
    validate_task_path,
)


def parse_and_apply_changes(responses, debug_dir, manifest: TaskManifest, save_num=1):
    """Apply SEARCH / REPLACE edits produced by the LLM to files in debug_dir."""
    for response in responses:
        # Split into blocks per file
        file_blocks = re.split(r"Filename:\s*([^\n]+)", response)
        # Example: ['', 'file1.py', '...file1 content...', 'file2.py', '...file2 content...', ...]

        if len(file_blocks) < 3:
            print(f"❌ No filename patterns found in response:\n{response[:200]}...\n")
            continue

        # Process blocks per file (odd indices: filename, even indices: diff content)
        for i in range(1, len(file_blocks), 2):
            filename = file_blocks[i]
            file_content_block = file_blocks[i + 1]
            requested_file = validate_task_path(filename)
            task_file = manifest.find(requested_file)
            if task_file is None:
                raise InvalidTaskPathError(
                    f"Rejected debug path {filename!r}: path is not present in the TaskManifest."
                )
            filepath = safe_join(debug_dir, task_file)

            # SEARCH/REPLACE pattern
            search_replace_pattern = (
                r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
            )
            matches = re.findall(search_replace_pattern, file_content_block, re.DOTALL)

            if not matches:
                print(f"❌ No SEARCH/REPLACE patterns found for file: {filename}\n")
                continue

            # Check file existence
            if not os.path.exists(filepath):
                print(f"❌ File does not exist: {filepath}\n")
                continue

            # Read file. Disk and decoding failures must fail the stage.
            with open(filepath, "r", encoding="utf-8") as f:
                file_content = f.read()
            original_file_content = file_content

            modified = False

            # Apply SEARCH/REPLACE
            for idx, (search_text, replace_text) in enumerate(matches, 1):
                search_text = search_text.strip()
                replace_text = replace_text.strip()

                if search_text in file_content:
                    file_content = file_content.replace(search_text, replace_text)
                    modified = True
                    print(f"✅ {filename}: Modification {idx} applied")
                else:
                    print(
                        f"❌ {filename}: Search text for modification {idx} not found:\n"
                        f"{search_text[:200]}...\n"
                    )

            # If modified, create backup and save
            if modified:
                backup_name = (
                    f".{task_artifact_key(task_file)}.{save_num:03d}.bak.txt"
                )
                backup_relative_path = "/".join(
                    (*task_file.parts[:-1], backup_name)
                )
                backup_file = validate_task_path(backup_relative_path)
                backup_path = safe_write_text(
                    debug_dir,
                    backup_file,
                    original_file_content,
                )
                safe_write_text(debug_dir, task_file, file_content)
                print(f"💾 {filename}: File saved. Backup: {backup_path}\n")
            else:
                print(f"ℹ️ {filename}: No modifications applied\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Debug a generated repository given an error log and planning artifacts."
    )
    parser.add_argument(
        "--error_file_name",
        type=str,
        required=True,
        help="Path to a text file containing the execution error message.",
    )

    # Either provide output_dir directly, or let the script construct it from the dataset style
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help=(
            "Root output directory that contains planning_trajectories.json and the debug directory."
        ),
    )
    parser.add_argument(
        "--paper_name",
        type=str,
        required=True,
        help="Paper name for output_dir.",
    )
    parser.add_argument(
        "--output_repo_dir",
        type=str,
        required=True,
        help="Generated repository root whose files may be debugged.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        required=True,
        help="Explicit provider registry ID.",
    )
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Registered model ID used for debugging.",
    )
    parser.add_argument(
        "--save_num",
        type=int,
        default=1,
        required=True,
        help="Backup index included in the validated backup artifact name.",
    )
    return parser.parse_args()


args = parse_args()
client = make_openai_client(args.provider, args.model)

if not os.path.exists(args.error_file_name):
    raise FileNotFoundError(f"Error file not found: {args.error_file_name}")

with open(args.error_file_name, "r", encoding="utf-8") as f:
    execution_error_msg = f.read()

# --------------------------------------------------
# Resolve output_dir and debug_dir
# --------------------------------------------------
output_dir = os.path.abspath(args.output_dir)
debug_dir = os.path.abspath(args.output_repo_dir)

# --------------------------------------------------
# Load the Planning-validated task manifest
# --------------------------------------------------
task_manifest = load_task_manifest(output_dir)
todo_file_lst = task_manifest.paths

# --------------------------------------------------
# Load repo files and configuration files
# --------------------------------------------------
python_dict = read_manifest_text_files(
    debug_dir,
    task_manifest,
    allowed_extensions={".py"},
)

codes = ""
for todo_file in todo_file_lst:
    if todo_file.endswith(".yaml"):
        continue
    if todo_file not in python_dict:
        print(f"⚠️ {todo_file} not found in python_dict. Skipping.")
        continue
    codes += f"```python\n## File name: {todo_file}\n{python_dict[todo_file]}\n```\n\n"

config_path = safe_join(debug_dir, validate_task_path("config.yaml"))
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config_yaml = f.read()
    codes += f"```yaml\n## File name: config.yaml\n{config_yaml}\n```\n\n"
        
reproduce_path = safe_join(debug_dir, validate_task_path("reproduce.sh"))
if os.path.exists(reproduce_path):
    with open(reproduce_path, "r", encoding="utf-8") as f:
        reproduce_sh = f.read()
    codes += f"```bash\n## File name: reproduce.sh\n{reproduce_sh}\n```\n\n"

# --------------------------------------------------
# Build debugging prompt
# --------------------------------------------------
msg = [
    {
        "role": "system",
        "content": """You are a highly capable code assistant specializing in debugging real-world code repositories. You will be provided with:
(1) a code repository (in part or in full), and
(2) one or more execution error messages generated during the execution of the repository.

Your objective is to debug the code so that it executes successfully.
This may involve identifying the root causes of the errors, modifying faulty logic or syntax, handling missing dependencies, or making other appropriate corrections.

Guidelines:
- Provide the exact lines or file changes needed to resolve the issue.
- When necessary, suggest best practices or improvements to prevent similar issues.
- Show only the modified lines using a unified diff format:

<<<<<<< SEARCH  
    original line  
=======  
    corrected line  
>>>>>>> REPLACE  

- If multiple fixes are needed, provide them sequentially with clear separation.
- If external dependencies or environment setups are required (for example, packages, versions, file paths), specify them explicitly.

Constraints:
- Do not make speculative edits without justification.
- Do not assume access to an internet connection for installation or retrieval unless explicitly stated.
- Prioritize minimal and effective fixes that preserve the original intent of the code.
- Maintain the coding style and structure used in the original repository unless refactoring is necessary for correctness.
""",
    },
    {
        "role": "user",
        "content": f"""
### Code Repository
{codes}

--

### Execution Error Messages
{execution_error_msg}

--

## Instruction
Now, you need to debug the above code so that it runs without errors. Identify the cause of the execution error and modify the code appropriately. Your output must follow the exact format as shown in the example below.

--

## Format Example
Filename: train.py
<<<<<<< SEARCH
result = model.predict(input_data)
=======
result = model(input_data)
>>>>>>> REPLACE

--

## Answer
""",
    },
]
response = client.chat.completions.create(
    model=args.model,
    messages=msg,
    **client.contract.request_options,
)

answer = response.choices[0].message.content
# print("===== RAW MODEL ANSWER =====")
# print(answer)

# Use the direct API response as input to the patch applier
responses = [answer]
parse_and_apply_changes(responses, debug_dir, task_manifest, save_num=args.save_num)


