# 004 Eval Status And Repair Loop

- 修改时间�?026-07-08 19:33
- 简要总结：新增评测状态、评测反馈文件和手动修复闭环，使代码生成后可以按评测结果回到 coding 阶段修复�?
## 1. 修改文件

- `codes/utils.py`
- `codes/eval.py`
- `codes/3_coding.py`
- `codes/auto_refine.py`

## 2. 修改内容

### 2.1 状态文�?
新增状态常量：

```text
待测�?测评但未通过
测评且通过
```

新增文件�?
```text
repo_status.json
eval_feedback.json
```

通过标准�?
```text
score >= 4.0 且没�?high severity
```

### 2.2 `eval.py`

评测结束后：

- 汇总分�?- 解析 `critique_list`
- 提取 high / medium / low severity
- 生成 `eval_feedback.json`
- 更新 `repo_status.json`

`eval_feedback.json` 包括�?
- `summary`
- `findings`
- `findings_by_file`
- `files_to_repair`
- `has_high_severity`
- `passed`

### 2.3 `3_coding.py`

新增�?
```text
--repair_from_eval
--eval_feedback_path
--max_repair_rounds
```

修复模式下：

- 读取 `eval_feedback.json`
- 只修�?`files_to_repair` 中的文件
- 修复完成后状态回�?`待测评`
- 最多修�?3 �?
### 2.4 `auto_refine.py`

新增手动以外的自动闭环脚本：

```text
eval -> repair -> eval -> repair
```

直到通过或达到最大修复轮数�?
## 3. 修改原因

一次性生成的代码经评测后可能存在公式、实验流程或输出组织问题。引入状态与反馈文件后，可以让评测结果结构化返回 coding 阶段，形成可控修复闭环�?
## 4. 使用方式

手动修复�?
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

自动修复�?
```powershell
python .\auto_refine.py ... --max_repair_rounds 3
```
