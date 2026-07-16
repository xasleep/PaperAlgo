# 005 Qwen Eval And Encoding Compatibility

- 修改时间：2026-07-08 19:33
- 简要总结：修复 Qwen 评测请求限制、Windows 控制台编码错误，以及 R 文件 UTF-8 读取问题。

## 1. 修改文件

- `codes/eval.py`
- `codes/utils.py`
- `codes/run_pipeline.py`

## 2. 关键改动

- `eval.py` 新增 provider 单次请求 `n` 限制，Kimi、DeepSeek、Qwen 按 `n=1` 拆分多次请求。
- 避免 Qwen 在 thinking 模式下因 `n > 1` 报错。
- `read_all_files()` 优先使用 `utf-8` 和 `utf-8-sig` 读取 `.R` 文件，避免 Windows 默认 GBK 导致读取失败。
- `utils.py` 尝试将 `stdout/stderr` 配置为 UTF-8，并使用 `errors="replace"`。
- `run_pipeline.py` 启动子进程时设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。

## 3. 修改原因

Windows PowerShell 默认编码和不同 provider 的请求限制会让 pipeline 中断。该改动提升了 Qwen 评测和 Windows 本地运行的稳定性。

## 4. 验证方式

```powershell
python .\eval.py ... --generated_n 8 --gpt_version qwen3.7-max
```

预期会按 provider 限制拆分请求，而不是直接向 Qwen 发送不支持的 `n`。
