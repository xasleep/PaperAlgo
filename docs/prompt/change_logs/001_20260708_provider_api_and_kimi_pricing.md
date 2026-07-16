# 001 Provider API And Kimi Pricing

- 修改时间：2026-07-08 19:33
- 简要总结：为 Paper2Code 增加多 Provider OpenAI-compatible API 路由，并补充 Kimi / DeepSeek 的 token 成本统计能力。

## 1. 修改文件

- `codes/utils.py`
- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`

## 2. 关键改动

- 在 `codes/utils.py` 中新增 provider-aware 的 OpenAI-compatible client 创建逻辑。
- 根据模型名前缀选择对应的 API key 和 base URL，例如 Kimi、DeepSeek、Claude、Qwen 或通用 OpenAI-compatible 网关。
- 统一处理 OpenAI SDK object、dict 和 string response，降低不同网关返回结构差异导致的脚本错误。
- 增加 Kimi / DeepSeek 的人民币 token 价格统计表，并在成本日志中输出可读成本信息。
- 将 planning、analyzing、coding、evaluation 脚本改为通过统一 helper 创建 client。

## 3. 修改原因

原项目默认按 OpenAI API 使用，难以稳定接入 DeepSeek、Kimi、Claude、Qwen 或第三方 OpenAI-compatible 网关。引入 provider-aware client 后，可以通过模型名和环境变量切换后端模型。

## 4. 验证方式

```powershell
python -m py_compile codes\utils.py codes\1_planning.py codes\2_analyzing.py codes\3_coding.py codes\eval.py
```
