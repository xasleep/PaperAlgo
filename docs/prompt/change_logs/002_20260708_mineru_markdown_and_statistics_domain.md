# 002 MinerU Markdown And Statistics Domain

- 修改时间：2026-07-08 19:33
- 简要总结：为 Paper2Code 增加 MinerU Markdown 输入能力，并新增 `--domain statistics` 统计学复现模式。

## 1. 修改文件

- `codes/utils.py`
- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`
- `codes/1.1_extract_config.py`

## 2. 关键改动

- `load_paper_content()` 支持 JSON、LaTeX 和 Markdown 三种论文输入格式。
- planning、analyzing、coding、evaluation 脚本新增 `--paper_format Markdown` 和 `--pdf_markdown_path` 参数。
- 新增 `--domain general/statistics`，统计学模式下 prompt 更关注模型公式、参数估计、DGP、Monte Carlo 实验和论文指标。
- 统计学模式避免默认生成深度学习项目常见的 trainer、dataloader、epoch、batch 等结构。
- `1.1_extract_config.py` 改为从 planning 轨迹中查找最后一条包含 YAML 的 assistant 消息，降低固定索引带来的脆弱性。

## 3. 修改原因

统计学论文通常更依赖公式、表格和实验设定。引入 MinerU Markdown 能保留更适合复现的信息结构，也让后续统计学代码生成更稳定。

## 4. 使用方式

```powershell
python .\1_planning.py `
  --paper_name 2015SINAR `
  --paper_format Markdown `
  --pdf_markdown_path "...\2015SINAR.md" `
  --domain statistics `
  --gpt_version deepseek-v4-pro `
  --output_dir ..\outputs\2015SINAR_R
```

## 5. 验证方式

运行相关脚本的 `py_compile`，并确认 `planning_config.yaml` 能从 planning 输出中提取。
