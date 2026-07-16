# 009 Pipeline 复用 Markdown 与评测模型自动降级

- 修改时间：2026-07-08 21:07
- 简要总结：为后续 FastAPI WebUI 做底层准备，`run_pipeline.py` 支持跳过 MinerU 直接复用已校验 Markdown；`eval.py` 支持评测阶段模型 quota fallback，例如 `qwen3.7-max` 额度不足时自动降级到 `qwen3.7-plus`。

## 修改文件

| 文件 | 修改内容 |
|---|---|
| `codes/run_pipeline.py` | 新增 `--skip_mineru`、`--pdf_markdown_path`、`--eval_fallback_gpt_versions` 参数；当启用 `--skip_mineru` 时不再调用 MinerU，直接把已有 Markdown 接入后续复现链路；评测命令会把 fallback 模型列表传给 `eval.py`。 |
| `codes/eval.py` | 新增评测模型 fallback 逻辑；当评测模型返回额度不足类错误时，自动尝试后备模型；默认支持 `qwen3.7-max -> qwen3.7-plus`。 |
| `docs/info/全仓目录结构与模块说明.md` | 补充 `run_pipeline.py --skip_mineru` 的使用语义，以及评测阶段 qwen fallback 说明。 |
| `docs/prompt/change_logs/README.md` | 新增第 009 篇变更记录索引。 |

## 修改原因

1. WebUI 场景下，用户可能已经完成 MinerU 解析和 Markdown 校验，重新调用 MinerU 会浪费时间，也可能覆盖人工校验后的输入。
2. 评测阶段使用 `qwen3.7-max` 时，如果免费额度耗尽，平台可能返回 403 错误。此时直接中断 pipeline 对用户体验不友好，因此先在评测阶段加入自动降级。
3. 当前只在评测阶段加入 fallback，不影响复现阶段模型选择，避免自动降级改变代码生成质量。

## 使用方式

复用已有 Markdown：

```powershell
python .\codes\run_pipeline.py `
  --paper_pdf_path ".\examples\2016GP_ZAR.pdf" `
  --paper_name 2016GP_ZAR `
  --domain statistics `
  --skip_mineru `
  --pdf_markdown_path ".\runs\<job_id>\mineru\2016GP_ZAR\auto\2016GP_ZAR.md" `
  --reproduce_provider deepseek `
  --reproduce_gpt_version deepseek-v4-pro `
  --eval_provider qwen `
  --eval_gpt_version qwen3.7-max `
  --auto_refine
```

指定评测 fallback 模型：

```powershell
--eval_fallback_gpt_versions qwen3.7-plus
```

如果不显式指定，`eval.py` 会对 `qwen3.7-max` 默认尝试 `qwen3.7-plus`。

## 验证方式

已执行语法检查：

```powershell
python -m py_compile codes\eval.py codes\run_pipeline.py
```

检查通过。
