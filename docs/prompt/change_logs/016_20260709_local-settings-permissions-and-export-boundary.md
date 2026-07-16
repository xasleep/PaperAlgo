# 016 local-settings-permissions-and-export-boundary

修改时间：2026-07-09

## 简要总结

- 为 `.local/` 和 `.local/web_settings.json` 增加“最低配”本地权限收紧：Windows 使用 `icacls` 去掉继承并仅保留当前用户与 `SYSTEM`，POSIX 使用私有目录/文件权限。
- `save_settings()` 在写入前后都会执行本地权限加固；读取已有 settings 时也会 best-effort 尝试补齐权限，避免老文件长期保持宽松权限。
- `POST /settings` 在无法安全收紧本地 settings 存储时，会返回明确的 500 错误，而不是静默继续写入。
- 补充导出边界测试，确认 `repo/download` 只打包 `runs/<job_id>/repo/`，不会把仓库根目录 `.local/`、任务同级 `.local/` 或 `.downloads/` 带进导出 zip。

## 修改文件

- `web_api/storage_security.py`
  - 新增本地 settings 存储权限加固 helper。
  - Windows 下通过 `icacls` 去掉 ACL 继承并移除常见宽权限组；POSIX 下使用 `0o700` / `0o600`。
- `web_api/settings_store.py`
  - `save_settings()` 在写入前后执行权限加固。
  - `load_settings()` 在读取已有文件时 best-effort 尝试补齐权限。
- `web_api/main.py`
  - `POST /settings` 捕获本地权限加固失败，返回 `Failed to secure local settings storage.`。
- `.gitignore`
  - 明确 `.local/` 是本地运行态与 secrets 目录，不应进入 git 历史。
- `tests/test_settings_store_resilience.py`
  - 覆盖保存时前后权限加固调用、读取旧文件时补权限、补权限失败时读取容错，以及 `POST /settings` 的明确错误返回。
- `tests/test_artifacts_logs_boundaries.py`
  - 覆盖 `repo/download` 不打包根目录 `.local/`、任务同级 `.local/` 和 `.downloads/`。

## 修改原因

- `.local/web_settings.json` 仍是明文本地文件，最低配防护的首要目标是减少“同机其他普通用户/误操作”直接读到 API key 的机会。
- 之前 `.gitignore` 已忽略 `.local/`，但本次需要把“本地权限”和“导出边界”也补成代码与测试层面的显式约束，而不是只靠约定。

## 验证方式

已执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_settings_store_resilience.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_artifacts_logs_boundaries.py -q
.\.venv\Scripts\python.exe -m py_compile web_api\storage_security.py web_api\settings_store.py web_api\main.py
```

结果：

- `test_settings_store_resilience.py`：`11 passed`
- `test_artifacts_logs_boundaries.py`：`9 passed`
- `py_compile`：通过

## 当前边界

- 这仍然不是磁盘加密；拥有管理员权限或主机控制权的攻击者仍可绕过本地 ACL。
- 现阶段仅对 `.local/` 这一单用户本地 secrets 目录做最低配保护，尚未引入 Windows Credential Manager 或多用户隔离存储。
