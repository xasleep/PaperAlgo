# 003 Statistics R Generation

- 修改时间：2026-07-08 19:33
- 简要总结：将统计学论文复现的默认代码生成语言调整为 R，并收敛为更贴近统计复现的脚本结构。

## 1. 修改文件

- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`
- `codes/utils.py`

## 2. 关键改动

- `--domain statistics` 下默认生成 `config.yaml`、`simulation.R`、`estimators.R`、`experiments.R`、`main.R`。
- 不再默认生成深度学习项目式的 `model.R`、`metrics.R`、`utils.R`。
- 约定 `simulation.R` 负责 DGP，`estimators.R` 负责估计方法，`experiments.R` 负责指标和实验流程，`main.R` 负责入口与结果落盘。
- 要求 `main.R` 顶部说明论文复现中的必要假设和输出文件位置。
- 增强 Markdown code fence 的代码抽取逻辑，减少模型输出中混入 Markdown 造成的语法问题。
- 同步更新 evaluation rubric，使评测关注统计复现结构、实验设定和输出完整性。

## 3. 修改原因

统计学论文复现更关注模型公式、估计方法、模拟实验和表格指标。R 是该场景下常用语言，简洁的脚本结构也更容易被评测和后续修复。

## 4. 验证方式

对示例统计学论文执行 pipeline，检查生成仓库是否包含约定的 5 个核心文件。
