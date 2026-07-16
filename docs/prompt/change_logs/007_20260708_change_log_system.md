# 007 Change Log System

- 修改时间：2026-07-08 19:33
- 简要总结：新增 `docs/prompt/change_logs` 文档体系，补写关键历史改动，并建立后续代码修改必须记录的工程规则。

## 1. 修改文件

- `docs/prompt/change_logs/README.md`
- `docs/prompt/change_logs/001_20260708_provider_api_and_kimi_pricing.md`
- `docs/prompt/change_logs/002_20260708_mineru_markdown_and_statistics_domain.md`
- `docs/prompt/change_logs/003_20260708_statistics_r_generation.md`
- `docs/prompt/change_logs/004_20260708_eval_status_and_repair_loop.md`
- `docs/prompt/change_logs/005_20260708_qwen_eval_and_encoding_compatibility.md`
- `docs/prompt/change_logs/006_20260708_webui_pipeline_backend.md`
- `docs/prompt/change_logs/007_20260708_change_log_system.md`

## 2. 关键改动

- 新增 `docs/prompt/change_logs/` 目录，用于存放项目迭代记录。
- 新增 README 索引，说明命名规范、记录范围、历史索引和后续维护规则。
- 补写 001-006 的历史记录，覆盖 provider API、MinerU Markdown、统计学 R 生成、评测修复闭环、Qwen/Windows 编码兼容、WebUI pipeline 后端等关键变化。

## 3. 修改原因

项目已经包含多 provider、MinerU、统计学 prompt、R 代码生成、评测闭环和 WebUI 后端等多条工程线。change log 能帮助后续维护时快速理解每次改动的范围、原因和验证方式。

## 4. 后续规则

只要修改项目代码，就新增一篇 change log。纯问答、命令运行、结果分析不单独记录。

## 5. 使用方式

查看索引：

```text
docs/prompt/change_logs/README.md
```

新增记录时，按下一个序号创建文件：

```text
008_YYYYMMDD_short_topic.md
```
