# 002 MinerU Markdown And Statistics Domain

- 修改时间�?026-07-08 19:33
- 简要总结：为 Paper2Code 增加 MinerU Markdown 输入能力，并新增 `--domain statistics` 统计学复现模式�?
## 1. 修改文件

- `codes/utils.py`
- `codes/1_planning.py`
- `codes/2_analyzing.py`
- `codes/3_coding.py`
- `codes/eval.py`
- `codes/1.1_extract_config.py`

## 2. 修改内容

### 2.1 Markdown 输入

�?`utils.py` 新增�?
```python
load_paper_content(paper_format, json_path=None, latex_path=None, markdown_path=None)
```

支持�?
- `JSON`
- `LaTeX`
- `Markdown`

`1_planning.py`、`2_analyzing.py`、`3_coding.py`、`eval.py` 均新增：

```text
--paper_format Markdown
--pdf_markdown_path
```

用于直接读取 MinerU 输出�?`.md` 文件�?
### 2.2 `--domain statistics`

新增�?
```text
--domain general/statistics
```

�?`domain=statistics` 时，prompt 会转向统计学论文复现，重点关注：

- 统计模型公式
- 参数估计方法
- 数值模�?DGP
- Monte Carlo 实验
- 论文指标

并避免默认生成深度学习项目中的：

- `trainer.py`
- `dataset_loader.py`
- epoch / batch / dataloader 结构

### 2.3 `1.1_extract_config.py`

修复原来固定索引提取 `config.yaml` 的脆弱逻辑，改为从 `planning_trajectories.json` 中反向查找最后一个包�?YAML �?assistant 消息�?
## 3. 修改原因

用户研究方向偏统计学，公式和表格复现要求较高。`s2orc-doc2json` 对公式转换不够稳定，因此引入 MinerU Markdown 作为论文输入格式�?
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

运行对应脚本�?`py_compile`，并确认 `planning_config.yaml` 能从 planning 输出中提取�?
