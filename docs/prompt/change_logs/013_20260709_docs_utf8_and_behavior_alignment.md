# 013 Docs UTF-8 And Behavior Alignment

- 修改时间：2026-07-09
- 简要总结：修复历史 change log 编码混乱和旧路径引用，并将仓库模块说明对齐到当前 FastAPI、settings、runs、安全边界和 eval fallback 行为。

## 1. 修改文件

- `docs/info/全仓目录结构与模块说明.md`
- `docs/prompt/change_logs/001_20260708_provider_api_and_kimi_pricing.md`
- `docs/prompt/change_logs/002_20260708_mineru_markdown_and_statistics_domain.md`
- `docs/prompt/change_logs/003_20260708_statistics_r_generation.md`
- `docs/prompt/change_logs/004_20260708_eval_status_and_repair_loop.md`
- `docs/prompt/change_logs/005_20260708_qwen_eval_and_encoding_compatibility.md`
- `docs/prompt/change_logs/006_20260708_webui_pipeline_backend.md`
- `docs/prompt/change_logs/007_20260708_change_log_system.md`
- `docs/prompt/change_logs/008_20260708_pipeline_console_output_control.md`
- `docs/prompt/change_logs/README.md`
- `tests/test_docs_utf8.py`

## 2. 关键改动

- 将历史 `001` 到 `008` change log 重写为可读 UTF-8 中文，移除乱码文本。
- 将历史文档中的旧 change log 目录写法修正为当前路径 `docs/prompt/change_logs`。
- 更新模块说明，准确记录当前 FastAPI 接口、settings 明文存储与接口脱敏、runs 目录结构、JSON 原子写入、路径安全边界、进程生命周期、repo/logs 限制和 qwen fallback 策略。
- 明确记录当前剩余风险，包括 `.local/web_settings.json` 明文存储、`pdf_markdown_path` 仍是 runs 级限制、`.downloads/` 暂无清理策略。

## 3. 验证方式

```powershell
python -m pytest tests\test_docs_utf8.py -q
```

该测试会验证 `docs/prompt/change_logs/*.md` 可按 UTF-8 读取，并阻止文档再次引用旧的 change log 目录写法。
