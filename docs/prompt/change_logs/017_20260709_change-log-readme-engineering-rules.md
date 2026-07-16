# 017 change-log-readme-engineering-rules

修改时间：2026-07-09

## 简要总结

- 将 `docs/prompt/change_logs/README.md` 的记录规则从“改了哪些文件”导向，调整为更贴近工程维护的“行为变化、影响范围、验证结果、已知边界”导向。
- 同步更新 README 开头的目录摘要，使其与新的记录规则保持一致。

## 修改文件

- `docs/prompt/change_logs/README.md`
  - 重写“记录规则”段落，补充记录目标、记录单位、记录范围、写作原则、推荐结构，以及与测试和发布的关系。
  - 更新 README 开头“简要总结”，明确 change log 用于记录可验证变更，并服务于回归排查、发布检查、文档同步和后续维护。

## 修改原因

- 原有 README 更偏向“改动清单式”说明，适合记事，但不够贴近实际工程中的回归、排障、发布和维护场景。
- 本次调整后，change log 的写法与项目当前实际需求更一致，也能减少“把建议写成已完成”或“只记文件不记行为”的记录偏差。

## 验证方式

已执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_docs_utf8.py -q
```

结果：

- `3 passed`

## 当前边界

- 本次仅更新 change log 规范与 README 摘要，不改变项目运行逻辑、API 行为或测试边界。
