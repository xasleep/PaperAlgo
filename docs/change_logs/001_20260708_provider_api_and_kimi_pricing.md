# 001 Provider API And Kimi Pricing

- 修改时间�?026-07-08 19:33
- 简要总结：为 Paper2Code 增加�?Provider OpenAI-compatible API 路由，并补充 Kimi / DeepSeek �?token 成本统计能力�?
## 1. 修改文件

- `codes/utils.py`
- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`

## 2. 修改内容

### 2.1 `codes/utils.py`

新增 `make_openai_client(model_name=None)`，根据模型名前缀选择环境变量�?`base_url`�?
- `claude-*` 使用 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
- `kimi-*` 使用 `MOONSHOT_API_KEY` �?`KIMI_API_KEY`
- `deepseek-*` 使用 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`
- 其他模型�?OpenAI-compatible fallback

新增 `normalize_completion()` �?`get_completion_message()`，统一处理 OpenAI SDK object、dict、string response，避免不同网关返回结构不一致导致脚本崩溃�?
新增 Kimi �?DeepSeek CNY token 价格表：

- `KIMI_MODEL_COST`
- `DEEPSEEK_MODEL_COST`
- `CNY_MODEL_COST`

并修�?`cal_cost()` / `print_log_cost()`，支持：

- cache hit token
- cache miss token
- CNY 计费输出
- 未知模型价格时显�?`unavailable`

### 2.2 调用脚本

`1_planning.py`、`2_analyzing.py`、`3_coding.py`、`eval.py` 改为通过 `make_openai_client(gpt_version)` 创建客户端，避免只支�?`OPENAI_API_KEY`�?
## 3. 修改原因

原项目默认按 OpenAI API 使用，无法稳定接�?DeepSeek、Kimi、Claude、Qwen 或第三方 OpenAI-compatible 网关。加�?provider-aware client 后，可以通过 `--gpt_version` 和环境变量切换模型�?
## 4. 使用方式

示例，Kimi�?
```powershell
$env:MOONSHOT_API_KEY="你的 Kimi API Key"
$env:MOONSHOT_BASE_URL="https://api.moonshot.cn/v1"
```

示例，DeepSeek�?
```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek API Key"
$env:DEEPSEEK_BASE_URL="你的 DeepSeek Base URL"
```

## 5. 验证方式

运行�?
```powershell
python -m py_compile codes\utils.py codes\1_planning.py codes\2_analyzing.py codes\3_coding.py codes\eval.py
```
