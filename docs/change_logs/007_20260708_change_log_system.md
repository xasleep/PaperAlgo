# 007 Change Log System

- 修改时间�?026-07-08 19:33
- 简要总结：新�?`docs/change_logs` 文档体系，补写关键历史改动，并建立后续代码修改必须记录的工程规则�?
## 1. 修改文件

- `docs/change_logs/README.md`
- `docs/change_logs/001_20260708_provider_api_and_kimi_pricing.md`
- `docs/change_logs/002_20260708_mineru_markdown_and_statistics_domain.md`
- `docs/change_logs/003_20260708_statistics_r_generation.md`
- `docs/change_logs/004_20260708_eval_status_and_repair_loop.md`
- `docs/change_logs/005_20260708_qwen_eval_and_encoding_compatibility.md`
- `docs/change_logs/006_20260708_webui_pipeline_backend.md`
- `docs/change_logs/007_20260708_change_log_system.md`

## 2. 修改内容

### 2.1 新增目录

```text
docs/change_logs
```

用于存放项目迭代记录�?
### 2.2 新增索引

`README.md` 记录�?
- 文件命名规范
- 记录范围
- 历史记录索引
- 后续维护规则

### 2.3 补写历史记录

补写此前关键工程改动�?
1. �?Provider API 路由�?Kimi 价格统计
2. MinerU Markdown 输入�?statistics 领域模式
3. 统计学论�?R 代码生成
4. 评测状态与修复闭环
5. Qwen 评测�?Windows 编码兼容
6. WebUI 后端 pipeline

## 3. 修改原因

项目迭代已经包含�?provider、MinerU、统计学 prompt、R 代码生成、评测闭环、WebUI 后端编排等多条工程线。新�?change log 能帮助后续维护时快速理解每次修改的范围、原因和使用方式�?
## 4. 后续规则

从本次开始：

```text
只要修改项目代码，就必须新增一�?change log�?不为纯问答、运行命令、分析结果生�?change log�?```

## 5. 使用方式

查看索引�?
```text
docs/change_logs/README.md
```

新增记录时，按下一个序号创建文件：

```text
008_YYYYMMDD_short_topic.md
```
