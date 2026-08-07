import json
import re
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

STATUS_PENDING_EVAL = "待测评"
STATUS_EVAL_FAILED = "测评但未通过"
STATUS_EVAL_PASSED = "测评且通过"
STATUS_EVAL_ERROR = "测评协议失败"
MAX_REPAIR_ROUNDS = 3


def repo_status_path(output_dir):
    return os.path.join(output_dir, "repo_status.json")


def eval_feedback_path(output_dir):
    return os.path.join(output_dir, "eval_feedback.json")


def load_json_file(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def save_json_file(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = os.path.join(
        directory,
        f".{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex}.tmp",
    )
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def write_repo_status(output_dir, status, **kwargs):
    status_data = {
        "status": status,
        "updated_at": get_now_str(),
        **kwargs,
    }
    save_json_file(repo_status_path(output_dir), status_data)
    return status_data


def parse_eval_rationale(rationale):
    if isinstance(rationale, list):
        return rationale
    if isinstance(rationale, dict):
        return [rationale]
    if not isinstance(rationale, str):
        return []

    text = rationale.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except Exception:
        pass

    return [{"file_name": "repository", "severity_level": "unknown", "critique": text}]


def normalize_severity(value):
    return str(value or "").strip().lower()


def summarize_eval_feedback(rationales):
    findings = []
    findings_by_file = {}
    has_high_severity = False

    for rationale in rationales:
        for item in parse_eval_rationale(rationale):
            file_name = str(item.get("file_name") or "repository")
            func_name = str(item.get("func_name") or "")
            severity = normalize_severity(item.get("severity_level"))
            critique = str(item.get("critique") or "").strip()
            if not critique:
                continue

            finding = {
                "file_name": file_name,
                "func_name": func_name,
                "severity_level": severity or "unknown",
                "critique": critique,
            }
            findings.append(finding)
            findings_by_file.setdefault(file_name, []).append(finding)
            if severity == "high":
                has_high_severity = True

    files_to_repair = []
    for file_name in findings_by_file:
        if file_name == "repository":
            continue
        # Preserve the evaluator's exact path. The repair stage validates it
        # against TaskManifest instead of silently removing traversal or
        # normalizing backslashes into a different file.
        files_to_repair.append(file_name)

    files_to_repair = sorted(set(files_to_repair))

    summary_lines = []
    for file_name, items in findings_by_file.items():
        severity_order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
        sorted_items = sorted(
            items,
            key=lambda item: severity_order.get(item["severity_level"], 3),
        )
        for item in sorted_items:
            func = f"::{item['func_name']}" if item.get("func_name") else ""
            summary_lines.append(
                f"[{item['severity_level']}] {file_name}{func}: {item['critique']}"
            )

    return {
        "summary": "\n".join(summary_lines),
        "findings": findings,
        "findings_by_file": findings_by_file,
        "files_to_repair": files_to_repair,
        "has_high_severity": has_high_severity,
    }

def make_openai_client(provider_id, model_id):
    """Create a client bound to one explicit registry provider/model pair."""
    try:
        from provider_registry import create_registered_client
    except ModuleNotFoundError:
        from codes.provider_registry import create_registered_client

    return create_registered_client(provider_id, model_id)


def normalize_completion(completion, model_name):
    """Convert provider responses to the dict shape used by PaperAlgo logs."""
    if hasattr(completion, "model_dump_json"):
        return json.loads(completion.model_dump_json())
    if isinstance(completion, dict):
        return completion
    if isinstance(completion, str):
        if completion.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise ValueError(
                "The provider returned an HTML page instead of an API response. "
                "Check that the base_url points to an OpenAI-compatible API endpoint, "
                "not the provider website."
            )
        return {
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": completion,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
    raise TypeError(f"Unsupported completion response type: {type(completion).__name__}")


def get_completion_message(completion_json):
    message = completion_json["choices"][0]["message"]
    return {
        "role": message.get("role", "assistant"),
        "content": message.get("content", ""),
    }


def load_paper_content(paper_format, json_path=None, latex_path=None, markdown_path=None):
    paper_format = paper_format.lower()

    if paper_format == "json":
        if not json_path:
            raise ValueError("--pdf_json_path is required when --paper_format JSON")
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)

    if paper_format == "latex":
        if not latex_path:
            raise ValueError("--pdf_latex_path is required when --paper_format LaTeX")
        with open(latex_path, encoding="utf-8") as f:
            return f.read()

    if paper_format == "markdown":
        if not markdown_path:
            raise ValueError("--pdf_markdown_path is required when --paper_format Markdown")
        with open(markdown_path, encoding="utf-8") as f:
            return f.read()

    raise ValueError(
        "Invalid paper format. Please select 'JSON', 'LaTeX', or 'Markdown'."
    )


def format_cost(cost, currency):
    if cost is None:
        return "unavailable"
    if currency == "USD":
        return f"${cost:.8f}"
    return f"{currency} {cost:.8f}"


def extract_planning(trajectories_json_file_path):
    with open(trajectories_json_file_path, encoding="utf-8") as f:
        traj = json.load(f)

    context_lst = []
    for turn in traj:
        if turn['role'] == 'assistant':
            # context_lst.append(turn['content'])
            content = turn['content']
            if "</think>" in content:
                content = content.split("</think>")[-1].strip()
            context_lst.append(content)


    context_lst = context_lst[:3] 

    return context_lst



def content_to_json(data):
    clean_data = re.sub(r'\[CONTENT\]|\[/CONTENT\]', '', data).strip()

    clean_data = re.sub(r'(".*?"),\s*#.*', r'\1,', clean_data)

    clean_data = re.sub(r',\s*\]', ']', clean_data)

    clean_data = re.sub(r'\n\s*', '', clean_data)


    # JSON parsing
    try:
        json_data = json.loads(clean_data)
        return json_data
    except json.JSONDecodeError as e:
        # print(e)
        return content_to_json2(data)
        
    
def content_to_json2(data):
    # remove [CONTENT][/CONTENT]
    clean_data = re.sub(r'\[CONTENT\]|\[/CONTENT\]', '', data).strip()

    # "~~~~", #comment -> "~~~~",
    clean_data = re.sub(r'(".*?"),\s*#.*', r'\1,', clean_data)

    # "~~~~" #comment → "~~~~"
    clean_data = re.sub(r'(".*?")\s*#.*', r'\1', clean_data)


    # ("~~~~",] -> "~~~~"])
    clean_data = re.sub(r',\s*\]', ']', clean_data)

    clean_data = re.sub(r'\n\s*', '', clean_data)

    # JSON parsing
    try:
        json_data = json.loads(clean_data)
        return json_data
    
    except json.JSONDecodeError as e:
        # print("Json parsing error", e)
        return content_to_json3(data)

def content_to_json3(data):
    # remove [CONTENT] [/CONTENT]
    clean_data = re.sub(r'\[CONTENT\]|\[/CONTENT\]', '', data).strip()

    # "~~~~", #comment -> "~~~~",
    clean_data = re.sub(r'(".*?"),\s*#.*', r'\1,', clean_data)

    # "~~~~" #comment → "~~~~"
    clean_data = re.sub(r'(".*?")\s*#.*', r'\1', clean_data)

    # remove ("~~~~",] -> "~~~~"])
    clean_data = re.sub(r',\s*\]', ']', clean_data)

    clean_data = re.sub(r'\n\s*', '', clean_data) 
    clean_data = re.sub(r'"""', '"', clean_data)  # Replace triple double quotes
    clean_data = re.sub(r"'''", "'", clean_data)  # Replace triple single quotes
    clean_data = re.sub(r"\\", "'", clean_data)  # Replace \ 

    # JSON parsing
    try:
        json_data = json.loads(f"""{clean_data}""")
        return json_data
    
    except json.JSONDecodeError as e:
        # print(e)
        
        # print(f"[DEBUG] utils.py > content_to_json3 ")
        # return None 
        return content_to_json4(data)
    
def content_to_json4(data):
    # 1. Extract Logic Analysis, Task list
    pattern = r'"Logic Analysis":\s*(\[[\s\S]*?\])\s*,\s*"Task list":\s*(\[[\s\S]*?\])'
    match = re.search(pattern, data)

    if match:
        logic_analysis = json.loads(match.group(1))
        task_list = json.loads(match.group(2))

        result = {
            "Logic Analysis": logic_analysis,
            "Task list": task_list
        }
    else:
        result = {}

    # print(json.dumps(result, indent=2))
    return result

def extract_code_from_content(content):
    """Extract source code from an LLM response and remove markdown wrappers."""
    if not content:
        return ""

    text = content.strip()
    pattern = r"```(?:[A-Za-z0-9_.+-]+)?\s*\n(.*?)\n?```"
    code_blocks = re.findall(pattern, text, re.DOTALL)
    if code_blocks:
        text = code_blocks[0].strip()

    text = re.sub(r"^\s*```[A-Za-z0-9_.+-]*\s*", "", text).strip()
    text = re.sub(r"\s*```\s*$", "", text).strip()

    lines = text.splitlines()
    if lines:
        header_pattern = (
            r"^\s*(?:##|#|//)\s*(?:Code:|File name:)?\s*"
            r"[\w./\\-]+\.(?:py|r|R|yaml|yml)\s*$"
        )
        if re.match(header_pattern, lines[0]):
            lines = lines[1:]

    return "\n".join(lines).strip()
    
def extract_code_from_content2(content):
    pattern = r'```python\s*(.*?)```'
    result = re.search(pattern, content, re.DOTALL)

    if result:
        extracted_code = result.group(1).strip()
    else:
        extracted_code = ""
        print("[WARNING] No Python code found.")
    return extracted_code

def format_json_data(data):
    formatted_text = ""
    for key, value in data.items():
        formatted_text += "-" * 40 + "\n"
        formatted_text += "[" + key + "]\n"
        if isinstance(value, list):
            for item in value:
                formatted_text += f"- {item}\n"
        else:
            formatted_text += str(value) + "\n"
        formatted_text += "\n"
    return formatted_text


def cal_cost(response_json, model_name, provider_id=None):
    """Calculate cost only from dated prices in the explicit registry contract."""
    usage = response_json.get("usage")
    if not isinstance(usage, Mapping) or not usage:
        return {
            'model_name': model_name,
            'actual_input_tokens': None,
            'input_cost': None,
            'cached_tokens': None,
            'cached_input_cost': None,
            'output_tokens': None,
            'output_cost': None,
            'total_cost': None,
            'currency': None,
            'prompt_tokens': None,
        }
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    prompt_token_details = usage.get("prompt_tokens_details") or {}
    cached_tokens = int(
        usage.get(
            "cached_tokens",
            prompt_token_details.get(
                "cached_tokens",
                usage.get("prompt_cache_hit_tokens", 0),
            ),
        )
        or 0
    )
    cached_tokens = min(max(cached_tokens, 0), prompt_tokens)

    # input token = (prompt_tokens - cached_tokens)
    actual_input_tokens = prompt_tokens - cached_tokens
    output_tokens = completion_tokens

    pricing = None
    if provider_id:
        try:
            from provider_registry import get_provider_registry
        except ModuleNotFoundError:
            from codes.provider_registry import get_provider_registry

        candidate = get_provider_registry().get(provider_id, model_name).pricing
        if candidate is not None and candidate.status == "configured":
            pricing = candidate

    if pricing is None:
        return {
            'model_name': model_name,
            'actual_input_tokens': actual_input_tokens,
            'input_cost': None,
            'cached_tokens': cached_tokens,
            'cached_input_cost': None,
            'output_tokens': output_tokens,
            'output_cost': None,
            'total_cost': None,
            'currency': None,
            'prompt_tokens': prompt_tokens,
        }

    input_cost = (actual_input_tokens / 1_000_000) * pricing.input_per_million
    cached_input_cost = (
        0
        if pricing.cached_input_per_million is None
        else (cached_tokens / 1_000_000) * pricing.cached_input_per_million
    )
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_million

    total_cost = input_cost + cached_input_cost + output_cost

    return {
        'model_name': model_name,
        'actual_input_tokens': actual_input_tokens,
        'input_cost': input_cost,
        'cached_tokens': cached_tokens,
        'cached_input_cost': cached_input_cost,
        'output_tokens': output_tokens,
        'output_cost': output_cost,
        'total_cost': total_cost,
        'currency': pricing.currency,
        'prompt_tokens': prompt_tokens,
    }

def load_accumulated_cost(accumulated_cost_file):
    if os.path.exists(accumulated_cost_file):
        with open(accumulated_cost_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("total_cost", 0.0)
    else:
        return 0.0

def save_accumulated_cost(accumulated_cost_file, cost):
    with open(accumulated_cost_file, "w", encoding="utf-8") as f:
        json.dump({"total_cost": cost}, f, ensure_ascii=False)

def print_response(completion_json, is_llm=False):
    print("============================================")
    if is_llm:
        print(completion_json['text'])
    else:
        print(completion_json['choices'][0]['message']['content'])
    print("============================================\n")

def _legacy_print_log_cost(
    completion_json,
    gpt_version,
    current_stage,
    output_dir,
    total_accumulated_cost,
    provider_id=None,
):
    usage_info = cal_cost(completion_json, gpt_version, provider_id)

    current_cost = usage_info['total_cost']
    currency = usage_info.get('currency') or 'USD'
    if current_cost is not None:
        total_accumulated_cost += current_cost

    output_lines = []
    output_lines.append("🌟 Usage Summary 🌟")
    output_lines.append(f"{current_stage}")
    output_lines.append(f"🛠️ Model: {usage_info['model_name']}")
    if current_cost is None:
        output_lines.append(f"📥 Input tokens: {usage_info['actual_input_tokens']} (Cost: unavailable)")
        output_lines.append(f"📦 Cached input tokens: {usage_info['cached_tokens']} (Cost: unavailable)")
        output_lines.append(f"📤 Output tokens: {usage_info['output_tokens']} (Cost: unavailable)")
        output_lines.append("💵 Current total cost: unavailable for this model")
    else:
        output_lines.append(f"📥 Input tokens: {usage_info['actual_input_tokens']} (Cost: ${usage_info['input_cost']:.8f})")
        output_lines.append(f"📦 Cached input tokens: {usage_info['cached_tokens']} (Cost: ${usage_info['cached_input_cost']:.8f})")
        output_lines.append(f"📤 Output tokens: {usage_info['output_tokens']} (Cost: ${usage_info['output_cost']:.8f})")
        output_lines.append(f"💵 Current total cost: ${current_cost:.8f}")
    output_lines.append(f"🪙 Accumulated total cost so far: ${total_accumulated_cost:.8f}")
    output_lines.append("============================================\n")

    output_text = "\n".join(output_lines)
    
    print(output_text)

    with open(f"{output_dir}/cost_info.log", "a", encoding="utf-8") as f:
        f.write(output_text + "\n")
    
    return total_accumulated_cost


def print_log_cost(
    completion_json,
    gpt_version,
    current_stage,
    output_dir,
    total_accumulated_cost,
    provider_id=None,
):
    usage_info = cal_cost(completion_json, gpt_version, provider_id)

    current_cost = usage_info['total_cost']
    currency = usage_info.get('currency') or 'USD'
    if current_cost is not None:
        total_accumulated_cost += current_cost

    output_lines = [
        "Usage Summary",
        f"{current_stage}",
        f"Model: {usage_info['model_name']}",
    ]

    usage_unknown = usage_info['prompt_tokens'] is None
    if usage_unknown:
        output_lines.extend([
            "Token usage: unavailable",
            "Current total cost: unavailable for this model",
        ])
    elif current_cost is None:
        output_lines.extend([
            f"Input cache miss tokens: {usage_info['actual_input_tokens']} (Cost: unavailable)",
            f"Input cache hit tokens: {usage_info['cached_tokens']} (Cost: unavailable)",
            f"Output tokens: {usage_info['output_tokens']} (Cost: unavailable)",
            "Current total cost: unavailable for this model",
        ])
    else:
        output_lines.extend([
            f"Input cache miss tokens: {usage_info['actual_input_tokens']} (Cost: {format_cost(usage_info['input_cost'], currency)})",
            f"Input cache hit tokens: {usage_info['cached_tokens']} (Cost: {format_cost(usage_info['cached_input_cost'], currency)})",
            f"Output tokens: {usage_info['output_tokens']} (Cost: {format_cost(usage_info['output_cost'], currency)})",
            f"Current total cost: {format_cost(current_cost, currency)}",
        ])

    output_lines.append(
        f"Accumulated total cost so far: {format_cost(total_accumulated_cost, currency)}"
    )
    output_lines.append("============================================\n")

    output_text = "\n".join(output_lines)
    print(output_text)

    with open(f"{output_dir}/cost_info.log", "a", encoding="utf-8") as f:
        f.write(output_text + "\n")

    return total_accumulated_cost


def num_tokens_from_messages(messages, model="gpt-4o-2024-08-06"):
    import tiktoken
    
    """Return the number of tokens used by a list of messages."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        print("Warning: model not found. Using o200k_base encoding.")
        encoding = tiktoken.get_encoding("o200k_base")
    if model in {
        "gpt-3.5-turbo-0125",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
        "gpt-4o-mini-2024-07-18",
        "gpt-4o-2024-08-06"
        }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif "gpt-3.5-turbo" in model:
        print("Warning: gpt-3.5-turbo may update over time. Returning num tokens assuming gpt-3.5-turbo-0125.")
        return num_tokens_from_messages(messages, model="gpt-3.5-turbo-0125")
    elif "gpt-4o-mini" in model:
        print("Warning: gpt-4o-mini may update over time. Returning num tokens assuming gpt-4o-mini-2024-07-18.")
        return num_tokens_from_messages(messages, model="gpt-4o-mini-2024-07-18")
    elif "gpt-4o" in model:
        print("Warning: gpt-4o and gpt-4o-mini may update over time. Returning num tokens assuming gpt-4o-2024-08-06.")
        return num_tokens_from_messages(messages, model="gpt-4o-2024-08-06")

    elif "gpt-4" in model:
        print("Warning: gpt-4 may update over time. Returning num tokens assuming gpt-4-0613.")
        return num_tokens_from_messages(messages, model="gpt-4-0613")
    else:
        raise NotImplementedError(
            f"""num_tokens_from_messages() is not implemented for model {model}."""
        )
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            # num_tokens += len(encoding.encode(value) 
            num_tokens += len(encoding.encode(value, allowed_special={"<|endoftext|>"},disallowed_special=()))
            
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens



def read_all_files(directory, allowed_ext, is_print=True): 
    """Recursively read all .py files in the specified directory and return their contents."""
    all_files_content = {}
    
    for root, _, files in os.walk(directory):  # Recursively traverse directories
        for filename in files:
            relative_path = os.path.relpath(os.path.join(root, filename), directory)  # Preserve directory structure

            # print(f"fn: {filename}\tdirectory: {directory}")
            _file_name, ext = os.path.splitext(filename)
            
            is_skip = False
            if len(directory) < len(root):
                root2 = root[len(directory)+1:]
                for dirname in root2.split("/"):
                    if dirname.startswith("."):
                        is_skip = True
                        break
            
            if filename.startswith(".") or "requirements.txt" in filename or ext == "" or is_skip:
                if is_print and ext == "":
                    print(f"[SKIP] {os.path.join(root, filename)}")
                continue
                
            if ext not in allowed_ext:
                if _file_name.lower() != "readme": 
                    if is_print:
                        print(f"[SKIP] {os.path.join(root, filename)}")
                    continue

            try:
                filepath = os.path.join(root, filename)
                file_size = os.path.getsize(filepath) # bytes
                
                if file_size > 204800: # > 200KB 
                    print(f"[BIG] {filepath} {file_size}")

                try:
                    with open(filepath, "r", encoding="utf-8") as file:
                        all_files_content[relative_path] = file.read()
                except UnicodeDecodeError:
                    with open(filepath, "r", encoding="utf-8-sig") as file:
                        all_files_content[relative_path] = file.read()
            except Exception as e:
                print(e)
                print(f"[SKIP] {os.path.join(root, filename)}")
    
    
    return all_files_content

def read_python_files(directory):
    """Recursively read all .py files in the specified directory and return their contents."""
    python_files_content = {}
    
    for root, _, files in os.walk(directory):  # Recursively traverse directories
        for filename in files:
            if filename.endswith(".py"):  # Check if file has .py extension
                relative_path = os.path.relpath(os.path.join(root, filename), directory)  # Preserve directory structure
                with open(os.path.join(root, filename), "r", encoding="utf-8") as file:
                    python_files_content[relative_path] = file.read()
    
    return python_files_content
  

def extract_json_from_string(text):
    # Extract content inside ```yaml\n...\n```
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)

    if match:
        yaml_content = match.group(1)
        return yaml_content
    else:
        print("No JSON content found.")
        return ""


def get_now_str():
    now = datetime.now()
    now = str(now)
    now = now.split(".")[0]
    now = now.replace("-","").replace(" ","_").replace(":","")
    return now # now - "20250427_205124"
