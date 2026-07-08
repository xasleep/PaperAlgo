# 005 Qwen Eval And Encoding Compatibility

- 修改时间�?026-07-08 19:33
- 简要总结：修�?Qwen 评测请求限制、Windows GBK 编码错误，以�?R 文件 UTF-8 读取问题�?
## 1. 修改文件

- `codes/eval.py`
- `codes/utils.py`
- `codes/run_pipeline.py`

## 2. 修改内容

### 2.1 Qwen `n` 参数限制

`eval.py` 新增 `model_max_choices_per_request()`�?
当前规则�?
```text
Kimi: n=1
DeepSeek: n=1
Qwen: n=1
```

当用户指定：

```text
--generated_n 8
```

脚本会自动拆成多次请求，而不是让 provider 报错�?
Qwen 的关键限制：

```text
The n parameter must be 1 when enable_thinking is true
```

因此 Qwen 也按 `n=1` 拆分�?
### 2.2 R 文件读取编码

`read_all_files()` 改为优先使用�?
```text
utf-8
utf-8-sig
```

避免 Windows 默认 GBK 读取 `.R` 文件时出现：

```text
'gbk' codec can't decode byte ...
```

导致 `experiments.R`、`main.R`、`simulation.R` 被跳过�?
### 2.3 控制台输出编�?
`utils.py` 在加载时尝试�?`stdout/stderr` reconfigure �?UTF-8，并使用 `errors="replace"`�?
`run_pipeline.py` 启动子进程时设置�?
```text
PYTHONIOENCODING=utf-8
PYTHONUTF8=1
```

避免模型输出中包�?Unicode 空格或数学符号时触发�?
```text
UnicodeEncodeError: 'gbk' codec can't encode character
```

## 3. 修改原因

Windows PowerShell 默认编码和多模型 provider 的请求限制会导致 pipeline 中断。该改动提高�?Qwen 评测�?Windows 本地运行的稳定性�?
## 4. 验证方式

使用 Qwen 评测�?
```powershell
python .\eval.py ... --generated_n 8 --gpt_version qwen3.7-max
```

预期自动拆成 8 次请求�?
