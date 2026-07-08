# 003 Statistics R Generation

- 修改时间�?026-07-08 19:33
- 简要总结：将统计学论文复现的默认代码生成语言改为 R，并压缩为简洁的统计复现脚本结构�?
## 1. 修改文件

- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`
- `codes/utils.py`

## 2. 修改内容

### 2.1 默认文件结构

`--domain statistics` 下默认生成：

```text
config.yaml
simulation.R
estimators.R
experiments.R
main.R
```

不再默认生成�?
```text
model.R
metrics.R
utils.R
```

设计约定�?
- `simulation.R`：DGP / 模拟生成机制
- `estimators.R`：参数估计、目标函数、参数检查、safe log 等局部数值工�?- `experiments.R`：指标定义、指标公式注释、Monte Carlo 或实例分析流�?- `main.R`：结果目录、表格保存、seed 记录、入口脚�?- `config.yaml`：实验设置、参数网格、输出路径、seed

### 2.2 `main.R` 顶部注释格式

强制 `main.R` 以两个段落开头：

```r
### 1. Paper details requiring assumptions:
# - ...

### 2. Output locations:
# - Raw estimates: results/raw_estimates.csv
# - Summary tables: results/summary_metrics.csv
# - Seed records: results/seeds.csv
# - Logs: results/run_log.txt
```

### 2.3 代码抽取

修改 `extract_code_from_content()`，支持剥离：

- ```r
- ```python
- 文件�?header
- Markdown code fence

减少模型输出 Markdown 代码块导致的语法错误�?
### 2.4 评测规则同步

`eval.py` �?statistics rubric 改为评估�?
- `simulation.R` �?DGP
- `estimators.R` 的估计方�?- `experiments.R` 的指标与实验流程
- `main.R` 的输出路径、seed、结果保�?
## 3. 修改原因

统计学论文更关注模型公式、估计方法、模拟实验和表格指标，不适合深度学习式项目结构。R 是统计复现中更常见且更贴近用户研究场景的语言�?
## 4. 验证方式

�?`2015SINAR` �?`2010TINAR` 论文执行完整 pipeline，检查生成仓库包含上�?5 个文件�?
