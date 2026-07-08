# Paper2Code 迭代变更记录

- 创建时间：2026-07-08 19:33
- 简要总结：本目录用于记录 Paper2Code 项目的关键代码改动。后续只要修改项目代码，就必须新增一篇 change log；纯问答、命令运行、结果分析不单独记录。

## 记录规则

1. 文件命名格式：

```text
001_YYYYMMDD_short_topic.md
002_YYYYMMDD_short_topic.md
```

2. 文档语言：中文为主，代码文件名、模型名、命令参数保留英文。
3. 每篇文档开头必须包含：
   - 修改时间
   - 简要总结
4. 每篇文档应说明：
   - 修改了哪些文件
   - 每个文件做了什么修改
   - 修改原因
   - 使用方式或验证方式
5. 记录范围：
   - 修改项目代码：必须新增 change log
   - 新增脚本、修改 prompt、修改状态流、修改评测逻辑：必须记录
   - 纯问答、运行命令、评测结果分析：不记录

## 历史记录索引

| 序号 | 文件 | 主题 |
|---:|---|---|
| 001 | [001_20260708_provider_api_and_kimi_pricing.md](001_20260708_provider_api_and_kimi_pricing.md) | 多 Provider API 路由与 Kimi 价格统计 |
| 002 | [002_20260708_mineru_markdown_and_statistics_domain.md](002_20260708_mineru_markdown_and_statistics_domain.md) | MinerU Markdown 输入与 statistics 领域模式 |
| 003 | [003_20260708_statistics_r_generation.md](003_20260708_statistics_r_generation.md) | 统计学论文 R 代码生成结构 |
| 004 | [004_20260708_eval_status_and_repair_loop.md](004_20260708_eval_status_and_repair_loop.md) | 评测状态与手动修复闭环 |
| 005 | [005_20260708_qwen_eval_and_encoding_compatibility.md](005_20260708_qwen_eval_and_encoding_compatibility.md) | Qwen 评测兼容与 Windows UTF-8 编码修复 |
| 006 | [006_20260708_webui_pipeline_backend.md](006_20260708_webui_pipeline_backend.md) | WebUI 后端统一 pipeline 编排 |
| 007 | [007_20260708_change_log_system.md](007_20260708_change_log_system.md) | 变更记录体系 |
| 008 | [008_20260708_pipeline_console_output_control.md](008_20260708_pipeline_console_output_control.md) | Pipeline 控制台输出收敛 |
