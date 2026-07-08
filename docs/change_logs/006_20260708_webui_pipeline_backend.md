# 006 WebUI Pipeline Backend

- 修改时间�?026-07-08 19:33
- 简要总结：新增面�?WebUI 后端调用的统一 pipeline 脚本，支�?PDF 输入、MinerU 解析、代码复现、评测和自动修复闭环�?
## 1. 修改文件

- `codes/run_pipeline.py`
- `codes/auto_refine.py`
- `codes/eval.py`
- `codes/3_coding.py`
- `codes/utils.py`

## 2. 修改内容

### 2.1 `run_pipeline.py`

新增统一入口�?
```text
PDF -> MinerU -> planning -> extract_config -> analyzing -> coding -> eval/repair
```

支持参数�?
```text
--paper_pdf_path
--paper_name
--domain
--reproduce_provider
--reproduce_gpt_version
--eval_provider
--eval_gpt_version
--auto_refine / --no-auto_refine
--max_repair_rounds
```

### 2.2 Job ID 目录结构

每次任务输出到：

```text
runs/<job_id>/
  input/
  mineru/
  output/
  repo/
  results/
  logs/
  run_status.json
  run_summary.json
```

避免 WebUI 多用户、多任务时互相覆盖�?
### 2.3 状态与摘要

`run_status.json` 用于前端轮询当前阶段�?
```json
{
  "status": "running",
  "stage": "planning",
  "message": "Running planning"
}
```

`run_summary.json` 用于前端展示最终结果：

```json
{
  "paper_name": "...",
  "markdown_path": "...",
  "repo_dir": "...",
  "status": "测评且通过",
  "eval_score": 4.25,
  "repair_round": 1
}
```

### 2.4 API Key 安全策略

不通过命令行传 API key�?
支持 WebUI 后端设置�?
```text
REPRODUCE_API_KEY
REPRODUCE_BASE_URL
EVAL_API_KEY
EVAL_BASE_URL
```

也兼容已�?provider 环境变量�?
```text
DEEPSEEK_API_KEY
MOONSHOT_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
```

### 2.5 Provider 隔离

`run_pipeline.py` 在不同子进程中使用不同环境：

- planning/analyzing/coding/repair 使用 reproduction env
- eval 使用 evaluation env

避免复现模型和评测模型都�?OpenAI-compatible 时发�?key 覆盖�?
## 3. 修改原因

未来 WebUI 不应该直接串联多个底层脚本。统一 pipeline 能降低前端复杂度，并通过 job id、状态文件、日志文件提供稳定的后端协议�?
## 4. 使用方式

```powershell
python .\run_pipeline.py `
  --paper_pdf_path "...\2010TINAR.pdf" `
  --paper_name 2010TINAR `
  --domain statistics `
  --reproduce_provider deepseek `
  --reproduce_gpt_version deepseek-v4-pro `
  --eval_provider qwen `
  --eval_gpt_version qwen3.7-max `
  --generated_n 8 `
  --auto_refine `
  --max_repair_rounds 3
```
