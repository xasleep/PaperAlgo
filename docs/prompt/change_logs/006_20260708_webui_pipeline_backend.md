# 006 WebUI Pipeline Backend

- 修改时间：2026-07-08 19:33
- 简要总结：新增面向 WebUI 后端调用的统一 pipeline 脚本，支持 PDF 输入、MinerU 解析、代码复现、评测和自动修复闭环。

## 1. 修改文件

- `codes/run_pipeline.py`
- `codes/auto_refine.py`
- `codes/eval.py`
- `codes/3_coding.py`
- `codes/utils.py`

## 2. 关键改动

- 新增 `codes/run_pipeline.py` 作为统一入口，流程为 `PDF -> MinerU -> planning -> extract_config -> analyzing -> coding -> eval/repair`。
- 支持 `--paper_pdf_path`、`--paper_name`、`--domain`、`--reproduce_provider`、`--eval_provider`、`--generated_n`、`--auto_refine`、`--max_repair_rounds` 等参数。
- 每个任务写入独立的 `runs/<job_id>/` 目录，包含 `input/`、`mineru/`、`output/`、`repo/`、`results/`、`logs/`、`run_status.json` 和 `run_summary.json`。
- 通过 `run_status.json` 支持前端轮询当前阶段，通过 `run_summary.json` 支持前端展示最终结果。
- 复现阶段和评测阶段分别使用对应的 provider / model / key 配置，避免两个角色互相覆盖。

## 3. 修改原因

WebUI 不应直接串联多个底层脚本。统一 pipeline 能降低前端复杂度，并通过 job id、状态文件、摘要文件和日志文件形成稳定的后端协议。

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
