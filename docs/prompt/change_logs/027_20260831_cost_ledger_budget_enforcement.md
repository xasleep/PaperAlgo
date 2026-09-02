# Append-only Cost Ledger and Budget Enforcement

修改时间：2026-08-31

## 简要总结

PR-06B 为 SQLite runtime 新增远程 LLM attempt 的 append-only 成本账本, 并提供可选 hard budget 的预调用拦截。默认 job 仍不设置预算; 成本信息通过 job 级 summary 返回, 不新增 WebUI 成本面板。

## 问题背景

PR-04B 的 at-least-once recovery 会让同一 stage 在恢复时重新执行真实外部调用。PR-05 已经有 Provider Registry 和价格元数据, 但没有把每次 request/retry/fallback/recovery attempt 变成可审计成本记录。旧 `cost_entries` 只支持整数 microusd, 不能表达 unknown pricing、missing usage、多 currency 或 partial usage。

## 修改内容

- 新增 `remote_call_ledger` SQLite migration, 保留旧 `cost_entries` 兼容表。
- 新增 `codes/cost_ledger.py`, 使用 Decimal 字符串金额, 按 attempt append-only 记录 started/completed/failed/cancelled。
- 在 `codes/provider_registry.py` 中包裹真实 `chat.completions.create`; ledger 启用时关闭 SDK 内部 retry, 由 wrapper 逐次记录 retry attempt。
- Pipeline 的 SQLite checkpoint stage 会把 job/stage/stage attempt/recovery/repair 上下文传给阶段子进程。
- SQLite job 创建 API 新增可选 `cost_budget_policy`, `cost_budget_currency`, `cost_budget_amount`; job response/list/detail 返回安全的 `cost_summary`。
- WebUI 只更新 TypeScript API 类型, 不新增成本面板。

## 修复后的实际行为

每一次真实远程 attempt 都有独立记录。`generated_n > max_n` 被拆成多次请求时分别计费; fallback 和 retry 也是不同 attempt; PR-04B recovery 重跑外部调用会计为新的真实成本。同一个 `attempt_id` 的重复入账由唯一约束幂等处理, 不重复汇总。

未返回 usage 时成本保持 unknown, 不填 0。Registry 未验证价格时保持 unknown, 不猜价格。不同 currency 分别汇总, 不做汇率合并。失败、取消、timeout 和 provider error 如果暴露 partial usage, 会保留已知成本组件。

hard budget 在远程调用前通过 SQLite `BEGIN IMMEDIATE` 原子检查并预占上界。若已验证价格、输入 token、输出 token 上界或币种无法确定, hard budget fail closed, 不发起远程调用。

## 影响范围

影响 SQLite runtime、Provider Registry 客户端 wrapper、Pipeline 子进程环境、FastAPI job create/list/detail schema 和本地迁移。legacy runtime 不获得预算 enforcement。无真实付费调用、无 SSE、无 WebUI 成本面板、无汇率服务。

## 验证方式

- PR-06B 新增定向测试: red 阶段先因 codes.cost_ledger 缺失失败; 实现后 	ests/test_cost_ledger.py 为 11 passed, 1 warning。
- Provider Registry、evaluation fallback、PR-06A Evaluation Contract、JobRepository、SQLite runtime 定向回归为 241 passed, 1 warning。
- python -m compileall -q codes web_api tests 通过。
- 完整 python -m pytest -q -rs 为 615 passed, 1 warning。
- WebUI 	ypecheck, uild, erify:same-origin, smoke, smoke:prod 全部通过; smoke:prod 覆盖 /, /settings, /jobs, /jobs/new, /jobs/smoke-job, /api/v1/health, /api/v1/jobs。

## 已知限制 / 注意事项

hard budget 需要调用方提供可验证的输入 token 和输出 token 上界。若 stage 没有提供 `_input_token_count`, 或模型契约/请求没有输出上限, hard budget 会 fail closed。成本汇总只暴露 job 级必要字段; attempt 级明细留在本地 SQLite 内部表中。
