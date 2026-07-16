# 004 Eval Status And Repair Loop

- 修改时间：2026-07-08 19:33
- 简要总结：新增评测状态、评测反馈文件和修复闭环，使代码生成后可以按评测结果回到 coding 阶段修复。

## 1. 修改文件

- `codes/utils.py`
- `codes/eval.py`
- `codes/3_coding.py`
- `codes/auto_refine.py`

## 2. 关键改动

- 新增 `repo_status.json` 和 `eval_feedback.json`。
- 评测通过规则设为 `score >= 4.0` 且没有 high severity findings。
- `eval.py` 在评测后汇总分数、severity、待修复文件和结构化反馈。
- `3_coding.py` 新增 `--repair_from_eval`、`--eval_feedback_path` 和 `--max_repair_rounds` 参数。
- 修复模式下只针对 `files_to_repair` 中的文件进行修改，修复后状态回到待评测。
- `auto_refine.py` 提供 `eval -> repair -> eval` 的自动闭环，直到通过或达到最大修复轮数。

## 3. 修改原因

一次性生成的代码可能存在公式、实验流程或输出组织问题。结构化评测反馈能把问题明确传回 coding 阶段，让修复更聚焦。

## 4. 使用方式

```powershell
python .\3_coding.py `
  --paper_name 2015SINAR `
  --paper_format Markdown `
  --pdf_markdown_path "...2015SINAR.md" `
  --domain statistics `
  --gpt_version deepseek-v4-pro `
  --output_dir ..\outputs\2015SINAR_R `
  --output_repo_dir ..\outputs\2015SINAR_R_repo `
  --repair_from_eval
```

```powershell
python .\auto_refine.py ... --max_repair_rounds 3
```
