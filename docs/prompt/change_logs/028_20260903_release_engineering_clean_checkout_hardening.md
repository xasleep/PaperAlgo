# Release Engineering and Clean-checkout Hardening

修改时间：2026-09-03

## 简要总结

PR-08 将依赖、CI、忽略规则和浏览器端到端验证收口到可重复的 Windows 本地单用户发布检查。默认安装不再混入可选重型 Provider 依赖，CI 使用 lock/constraint 绑定缓存，并新增 fake-provider Playwright E2E。

## 问题背景

此前 Python CI 直接在 workflow 中写死少量包版本，runtime、dev/test 和 optional-heavy 依赖没有清晰分层；WebUI smoke 只覆盖静态页面和生产同源基础路由，没有通过浏览器验证 settings、Provider discovery、SSE、artifacts 和任务控制闭环。数据库、WAL/SHM、运行产物、coverage、Playwright 产物和 credential 忽略规则也需要显式审计。

## 修改内容

- 新增 `requirements-runtime.txt`、`requirements-dev.txt`、`requirements-optional-heavy.txt` 和 `constraints.txt`，并让 `requirements.txt` 保持向后兼容聚合入口。
- 前端依赖通过 `web_ui/package-lock.json` 固定，并将 React Router 升至有安全证据的修复版本。
- `PAPER2CODE_LOCAL_DIR`、`PAPER2CODE_RUNS_DIR` 和 `PAPER2CODE_DB_PATH` 可重定向本地 settings、SQLite 和 runs。
- GitHub Actions 使用 least-privilege `contents: read`，不使用 `pull_request_target`，Python 和 npm cache key 分别绑定约束/lock 文件。
- CI 增加 fake-provider Playwright E2E，并继续运行 backend pytest、typecheck、build、same-origin verification、development smoke 和 production smoke。
- `.gitignore` 显式覆盖数据库、WAL/SHM、runs、dist、coverage、Playwright、pytest 和 credential 文件。
- README、baseline 和 ADR 记录 Windows 本地安装、升级、SQLite 迁移、备份、回滚和 optional-heavy 边界。

## 修复后的实际行为

Windows 本地用户默认按 `requirements-dev.txt` 和 `npm ci` 安装即可运行完整本地验证。可选 vLLM/Transformers 等重型依赖不进入默认 Web/API 开发路径。fake E2E 使用临时 Provider Registry、临时 SQLite、临时 runs、临时 settings 和本地 loopback FastAPI，不读取或暴露真实 API key、settings、Prompt 或本机路径。

## 影响范围

影响安装文档、依赖文件、CI workflow、WebUI package lock、本地配置路径、fake E2E 测试脚本和版本管理忽略规则。不改变产品功能、不发布 tag 或 GitHub Release、不增加公网部署、多用户认证或真实付费 Provider E2E。

## 验证方式

- failure-first release hardening 定向测试：修改前 4 failed。
- `python -m compileall -q codes web_api tests`：通过。
- `python -m pytest tests/test_release_hardening.py -q -rs`：4 passed。
- `python -m pytest tests -q -rs`：638 passed, 1 warning；该全量测试在 `web_ui/dist/` 构建后执行。
- `pip install -r requirements-dev.txt` 与 `pip check`：exit 0。
- `npm ci`、`npm audit --omit=dev`、`npm audit`：全部 exit 0，0 vulnerabilities。
- `npm run typecheck`、`npm run build`、`npm run verify:same-origin`、`npm run smoke`、`npm run smoke:prod`、`npm run e2e:fake`：全部 exit 0。

## 已知限制 / 注意事项

Windows 是正式支持平台；当前没有声明 Linux 完整支持。MinerU 和 vLLM 仍是外部/可选重型路径，自动化测试不会运行它们。SQLite schema 迁移在 API/Worker 打开数据库时自动执行；需要可回滚升级时，应先停止本地进程并备份 `.local/paper2code.db*`、`.local/web_settings.json` 和 `runs/`。
