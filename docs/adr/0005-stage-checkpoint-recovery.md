# ADR 0005：SQLite 阶段 checkpoint 与受限恢复

- 状态：已接受并已实现（PR-04B）
- 日期：2026-07-22

## 背景

PR-04A 能以 PID/create time 验证并重新监控存活进程，但已登记 Pipeline 在 Worker 停止期间死亡时没有安全的阶段恢复证据；同时，当前 Worker 在线监控到本地 `Popen` 非零退出时也不能绕过 checkpoint 恢复而直接失败。仅凭状态文件或“数据库里没有活跃 PID”猜测进度，会重跑已完成模型阶段、并行启动第二个 Pipeline，或把部分产物误当成完整边界。

## 决策

- `codes/run_pipeline.py` 保持执行内核，并增加默认关闭的最小 checkpoint adapter；只有 SQLite Worker 显式启用。默认 `JOB_RUNTIME=legacy` 和 CLI 直接运行不依赖 SQLite。
- 真实阶段顺序为 `mineru_parse`/`mineru_skipped`、`planning`、`extract_config`、`analyzing`、`coding`、`evaluation`，auto-refine 时交替 `repair`/`evaluation`，最后为现有 `completed` 状态阶段。
- 每个阶段开始写 `running`，异常写 `failed`，只有阶段命令成功、必要产物落盘并可生成指纹后才原子替换为 `completed`。只有 completed 边界可恢复；当前 running/failed/partial 阶段从上一个可信 completed 边界所指向的阶段开头重跑，resume attempt 在该阶段已有 attempt 基础上递增。进程被强制终止时旧 attempt 可能永久保留为 `running`；同 sequence 的后续连续 attempt 会取代这条陈旧观察，真实并发仍由 SQLite lease、进程身份和 launch-token fencing 阻止。
- checkpoint 位于 `runs/<job_id>/checkpoints/`，版本为 1，固定字段，最大 64 KiB。链验证要求固定阶段白名单、从 sequence 1 连续推进、stage 1 只能选择 MinerU parse 或 skipped 之一、同一 sequence 不得冲突、attempt 单调无冲突、`resume_from_stage` 必须匹配下一 sequence、job id 与文件名内容必须一致。
- 每个 completed checkpoint 保存下一阶段所需的完整当前状态闭包，而不是只保存本阶段新产物：stage 1 绑定可信 Markdown；Planning 还绑定 TaskManifest、planning response/trajectories；extract/analyzing 逐步加入配置、planning artifacts 和分析结果；coding/evaluation/repair 绑定全部当前 TaskManifest repo 成员、必要 repo 配置和相应状态结果；completed 再绑定 run status/summary。
- TaskManifest 的路径、size 与 SHA-256 必须始终匹配可信 Planning 边界，不能只对替换后的 Manifest 重新做结构校验。后续阶段允许合法变化的 repo 文件由最新 completed checkpoint 记录当前版本；恢复不复核已经过时的历史 repo 指纹。artifact 只记录 run 目录内的相对 POSIX 路径、size 和 SHA-256；读取拒绝绝对路径/traversal、symlink、junction/reparse point、硬链接、缺失文件、闭包不完整和指纹不匹配。
- SQLite migration 5 扩展 `stage_runs` 的 sequence、checkpoint key/version、resume eligibility 和 launch token，增加 job recovery 元数据、process attempt 及 `job_process_history`。旧进程身份在新 attempt 前归档，不被静默覆盖。
- 在线本地 `Popen` 非零退出和 Worker 重启后发现 registered 进程 confirmed-missing 共用同一恢复资格判断。exit 0 直接 completed 且 recovery count 不变；非零退出且无 cancel 时先同步退出前已原子落盘的 checkpoint，再验证恢复；晚到 cancel 以自然退出为准，命令失败为 `already_finished`，不得写 canceled 或启动恢复。
- 恢复只允许当前 lease owner 在 `BEGIN IMMEDIATE` 中准备；job 必须仍为 running，原 process 必须是完整登记身份的 `registered` launch、已确认死亡，旧 launch token、`worker_id + instance_token` fencing、无 pending/claimed cancel、完整状态闭包和恢复预算必须同时有效。事务一次性增加 recovery count、归档旧身份及真实可得的 exit code、创建新 process attempt 和新 launch token；默认最多恢复 1 次。
- `identity_unresolved` 永不自动恢复。无 checkpoint 使用 `process_exited_without_checkpoint`；非法 checkpoint、缺失依赖或预算耗尽使用固定 failure code 并先形成当前 job 终态，才允许队列继续。
- checkpoint、SQLite event 和 API 只保存脱敏控制元数据，不保存 API key、Prompt、完整模型响应、完整命令或环境变量。API 只公开 current stage/attempt、last checkpoint stage、recovery count/status/error。

## 后果

恢复语义为 stage-boundary at-least-once。MinerU、Provider 请求、planning/analyzing/coding/evaluation/repair 和成本累计可能重复；PR-06 成本治理尚未实现。文件指纹、TaskManifest 或完整依赖发生变化会拒绝恢复，不会猜测、修补路径、重写 Manifest 或自动删除损坏状态。恢复预算耗尽、`identity_unresolved` 或本地输入不可用时仍需要人工检查；不保证任意语句级恢复或 exactly-once。

本决策不实现任意阶段内部快照、通用 DAG、自动任务重放、多 Worker 高可用、SSE 或新的 WebUI 工作流；PR-05 Provider Registry、PR-06 evaluation/cost governance 和 PR-07 SSE 仍未实现。
