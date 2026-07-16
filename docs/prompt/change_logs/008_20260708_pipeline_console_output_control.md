# 008 Pipeline Console Output Control

- 修改时间：2026-07-08 19:42
- 简要总结：收敛 `run_pipeline.py` 的终端输出，默认只显示阶段进度和关键状态行，完整 LLM 输出保留到 `logs/`。

## 1. 修改文件

- `codes/run_pipeline.py`
- `docs/prompt/change_logs/README.md`
- `docs/prompt/change_logs/008_20260708_pipeline_console_output_control.md`

## 2. 关键改动

- 新增 `should_echo_line(line, console_output)`，用于判断子进程 stdout 是否显示到终端。
- 新增 `--console_output` 参数：

```text
progress  默认模式，只显示阶段进度和关键状态行
full      完整输出所有子进程 stdout/stderr
quiet     只显示 pipeline 自身阶段信息，不显示子进程输出
```

- 所有子进程输出仍完整写入 `runs/<job_id>/logs/*.log`。
- 默认终端输出只保留当前阶段、log 文件路径、INFO/WARNING/ERROR、评测请求进度、状态、分数、修复轮次和 cost summary 等关键行。
- 更新 change log README 索引。

## 3. 修改原因

`run_pipeline.py` 面向 WebUI 后端和本地 CLI。默认把完整 LLM 输出刷到终端会影响进度捕获和日志可读性，因此改为终端简洁输出、日志完整保留。

## 4. 使用方式

```powershell
python .\run_pipeline.py ... --console_output progress
python .\run_pipeline.py ... --console_output full
python .\run_pipeline.py ... --console_output quiet
```

## 5. 验证方式

```powershell
python -m py_compile codes\run_pipeline.py
python .\run_pipeline.py --help
```
