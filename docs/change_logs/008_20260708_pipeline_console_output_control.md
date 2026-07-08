# 008 Pipeline Console Output Control

- 修改时间�?026-07-08 19:42
- 简要总结：收�?`run_pipeline.py` 的终端输出，默认只显示阶段进度和关键状态行，完�?LLM 输出保留�?`logs/`�?
## 1. 修改文件

- `codes/run_pipeline.py`
- `docs/change_logs/README.md`
- `docs/change_logs/008_20260708_pipeline_console_output_control.md`

## 2. 修改内容

### 2.1 `codes/run_pipeline.py`

新增 `should_echo_line(line, console_output)`，用于判断子进程 stdout 是否应该显示到终端�?
新增 `--console_output` 参数�?
```text
progress  默认模式，只显示阶段进度和关键状态行
full      完整输出所有子进程 stdout/stderr
quiet     只显�?pipeline 自身阶段信息，不显示子进程输�?```

`run_command()` 行为调整�?
- 所有子进程输出仍完整写�?`logs/*.log`
- 默认不再�?planning/analyzing/coding 的长 LLM 分析内容打印到终�?- 终端默认显示�?  - 当前阶段
  - log 文件路径
  - `[INFO]` / `[WARNING]` / `[ERROR]`
  - evaluation request 进度
  - 状态、分数、修复轮�?  - cost summary 关键�?
### 2.2 `docs/change_logs/README.md`

新增�?008 条索引�?
## 3. 修改原因

`run_pipeline.py` 面向未来 WebUI 后端。pipeline 运行时不应把完整 LLM 输出刷到终端，否则会让日志难以阅读，也会影响前端/后端捕获进度信息�?
完整模型输出仍然保存在：

```text
runs/<job_id>/logs/
```

因此调试能力没有丢失�?
## 4. 使用方式

默认简洁输出：

```powershell
python .\run_pipeline.py ... --console_output progress
```

完整输出，用于排错：

```powershell
python .\run_pipeline.py ... --console_output full
```

最安静模式�?
```powershell
python .\run_pipeline.py ... --console_output quiet
```

## 5. 验证方式

运行�?
```powershell
python -m py_compile codes\run_pipeline.py
```

并检�?`run_pipeline.py --help` 中存�?`--console_output`�?
