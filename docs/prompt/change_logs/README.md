# Paper2Code 迭代变更记录

创建时间：2026-07-08 19:33

## 简要总结

本目录用于记录会改变项目真实行为的工程化变更，重点服务于：

- 回归排查
- 发布检查
- 文档同步
- 后续维护

change log 记录的是“系统行为发生了什么变化”，而不是单纯记录改了哪些文件。

## 记录规则

### 1. 记录目标

每篇 change log 都应该帮助后来的读者快速回答：

- 这次修改解决了什么问题
- 行为和之前相比发生了什么变化
- 影响了哪些接口、模块、配置或用户路径
- 做了哪些验证
- 还剩哪些边界或风险

### 2. 记录单位

推荐一篇 change log 对应一个完整、可描述的变更主题，例如：

- 一个功能增量
- 一个缺陷修复主题
- 一个接口契约变更
- 一个安全或稳定性加固主题
- 一个测试覆盖补齐主题

不建议把同一主题拆成很多碎片化记录。

### 3. 需要记录的变更范围

以下情况应新增 change log：

- 修改代码且影响真实行为、接口、状态流、输出或运行方式
- 新增、删除或显著调整接口
- 修改配置项语义、默认值、参数边界或 fallback 逻辑
- 修改任务状态、文件访问边界、安全边界、日志或导出行为
- 增补或显著调整测试，并改变了项目质量边界
- 修改文档且会影响真实使用、部署、安全认知或维护方式

以下情况通常不单独记录：

- 纯问答
- 纯命令执行
- 临时调试但没有落地代码/测试/文档
- 无行为变化的格式化、重命名或微调注释

### 4. 每篇文档必须包含的内容

每篇 change log 至少包含：

- 修改时间
- 简要总结
- 问题背景
- 修改内容
- 修复后的实际行为
- 影响范围
- 验证方式
- 已知限制 / 注意事项

如果是接口或状态语义变更，应额外明确：

- 变更前行为
- 变更后行为
- 是否影响已有调用方
- 是否需要迁移或特别注意

### 5. 写作原则

- 以“行为变化”为中心，不以“改了哪些文件”为中心
- 只记录已经落地并验证过的事实
- 优先描述外部可感知影响，再补充内部实现
- 必须写清验证方法，而不是只写“改了”

### 6. 命名规则

推荐文件名格式：

```text
001_YYYYMMDD_short_topic.md
002_YYYYMMDD_short_topic.md
```

主题命名应描述“变更结果”，避免使用过于模糊的名称，如 `fix_bug.md`。

### 7. 推荐结构

```md
# 标题

修改时间：YYYY-MM-DD

## 简要总结

## 问题背景

## 修改内容

## 修复后的实际行为

## 影响范围

## 验证方式

## 已知限制 / 注意事项
```

## 历史记录索引

| 序号 | 文件 | 主题 |
|---:|---|---|
| 001 | [001_20260708_provider_api_and_kimi_pricing.md](001_20260708_provider_api_and_kimi_pricing.md) | Provider API 路由与 Kimi 价格统计 |
| 002 | [002_20260708_mineru_markdown_and_statistics_domain.md](002_20260708_mineru_markdown_and_statistics_domain.md) | MinerU Markdown 输入与 statistics 领域模式 |
| 003 | [003_20260708_statistics_r_generation.md](003_20260708_statistics_r_generation.md) | 统计学论文 R 代码生成结构 |
| 004 | [004_20260708_eval_status_and_repair_loop.md](004_20260708_eval_status_and_repair_loop.md) | 评测状态与手动修复闭环 |
| 005 | [005_20260708_qwen_eval_and_encoding_compatibility.md](005_20260708_qwen_eval_and_encoding_compatibility.md) | Qwen 评测兼容与 Windows UTF-8 修复 |
| 006 | [006_20260708_webui_pipeline_backend.md](006_20260708_webui_pipeline_backend.md) | WebUI 后端统一 pipeline 编排 |
| 007 | [007_20260708_change_log_system.md](007_20260708_change_log_system.md) | change log 体系 |
| 008 | [008_20260708_pipeline_console_output_control.md](008_20260708_pipeline_console_output_control.md) | pipeline 控制台输出控制 |
| 009 | [009_20260708_pipeline_resume_and_eval_model_fallback.md](009_20260708_pipeline_resume_and_eval_model_fallback.md) | pipeline 复用 Markdown 与评测模型 fallback |
| 010 | [010_20260708_fastapi_backend_single_user_jobs.md](010_20260708_fastapi_backend_single_user_jobs.md) | FastAPI 后端骨架与单用户任务接口 |
| 011 | [011_20260708_fastapi_job_status_field_fix.md](011_20260708_fastapi_job_status_field_fix.md) | FastAPI 任务状态字段冲突修复 |
| 012 | [012_20260709_webui_job_artifacts_api.md](012_20260709_webui_job_artifacts_api.md) | WebUI 任务浏览与结果文件接口 |
| 013 | [013_20260709_docs_utf8_and_behavior_alignment.md](013_20260709_docs_utf8_and_behavior_alignment.md) | 文档 UTF-8 与行为说明对齐 |
| 014 | [014_20260709_web-api-hardening-and-eval-fallback-chain-fixes.md](014_20260709_web-api-hardening-and-eval-fallback-chain-fixes.md) | Web API 加固与 eval fallback 链延续修复 |
| 015 | [015_20260709_settings-atomic-write-and-eval-fallback-chain-fixes.md](015_20260709_settings-atomic-write-and-eval-fallback-chain-fixes.md) | settings 原子写入与 eval fallback 链延续修复 |
| 016 | [016_20260709_local-settings-permissions-and-export-boundary.md](016_20260709_local-settings-permissions-and-export-boundary.md) | 本地 settings 权限收紧与导出边界确认 |
| 017 | [017_20260709_change-log-readme-engineering-rules.md](017_20260709_change-log-readme-engineering-rules.md) | change log README 工程化规则更新 |
| 018 | [018_20260709_api-contract-and-error-semantics.md](018_20260709_api-contract-and-error-semantics.md) | WebUI 前 API 契约收口与错误语义统一 |
| 019 | [019_20260709_minimal-react-vite-webui-mvp.md](019_20260709_minimal-react-vite-webui-mvp.md) | Minimal React Vite WebUI MVP |
| 020 | [020_20260709_webui-delivery-and-static-serving.md](020_20260709_webui-delivery-and-static-serving.md) | WebUI Delivery and Static Serving |
| 021 | [021_20260709_same-origin-production-api-default.md](021_20260709_same-origin-production-api-default.md) | Same-origin production API default |
| 022 | [022_20260709_webui-mvp-end-to-end-hardening.md](022_20260709_webui-mvp-end-to-end-hardening.md) | WebUI MVP End-to-End Hardening |
| 023 | [023_20260709_local-dev-cors-for-vite.md](023_20260709_local-dev-cors-for-vite.md) | Local dev CORS for Vite |
| 024 | [024_20260709_vite-strict-dev-port.md](024_20260709_vite-strict-dev-port.md) | Vite strict dev port |
| 025 | [025_20260806_explicit_provider_model_registry.md](025_20260806_explicit_provider_model_registry.md) | Explicit Provider/Model Registry |
| 026 | [026_20260807_evaluation_contract_and_repair_policy.md](026_20260807_evaluation_contract_and_repair_policy.md) | Evaluation Contract and Repair Policy |
| 027 | [027_20260831_cost_ledger_budget_enforcement.md](027_20260831_cost_ledger_budget_enforcement.md) | Append-only Cost Ledger and Budget Enforcement |
