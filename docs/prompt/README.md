# Paper2Code Prompt 与变更记录文档目录

- 创建时间：2026-07-08
- 简要总结：本目录存放 Paper2Code 的 prompt / agent 工程约定与迭代变更记录；全仓信息说明放在 `docs/info/`。

## 文档索引

| 文档 | 说明 |
|---|---|
| [全仓目录结构与模块说明](../info/全仓目录结构与模块说明.md) | 说明仓库顶层目录、`codes/` 核心脚本、推荐执行流程和运行产物。 |
| [迭代变更记录](change_logs/README.md) | 记录项目关键代码修改历史和后续 change log 维护规则。 |

## 维护约定

- 项目级信息说明文档放在 `docs/info/`。
- Prompt、agent 工程约定和代码迭代记录放在 `docs/prompt/`。
- 代码迭代记录放在 `docs/prompt/change_logs/`。
- 纯运行产物不放入 `docs/`，应放入 `runs/`、`outputs/`、`results/` 等目录。
