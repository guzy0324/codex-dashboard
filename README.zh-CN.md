# Codex Dashboard

英文 README: [README.md](README.md)

Codex Dashboard 是一个面向 OpenAI Codex hooks 的本地 Flask 小面板。它会显示正在运行的 Codex turn，把会话标记为 thinking/done/interrupted，在任务结束时播放本地提示音，并突出显示需要用户批准的权限请求。

这个面板只把状态保存在内存里。重启服务后，页面上的历史记录会被清空。

## 功能

- 显示最近的 Codex hook 事件和会话。
- 把内部标题生成 turn 归并到真实会话中。
- 在 UI 中隐藏内部标题提示词。
- 根据 transcript 事件识别人为中断的 turn。
- 在真实 Codex turn 结束时播放提示音并显示闪烁式视觉提醒。
- 对内部标题生成 turn 禁用提示音。
- 在页面顶部显示权限请求。
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮闪烁。
- 在 tray 模式下，Windows 通知区域图标会闪烁，并显示托盘通知弹窗。
- 支持带机器名前缀的路径，例如 `gpu01:/mdata/project`。

## 环境要求

- Python 3.10+
- Flask
- 如果需要 Windows 通知区域图标，需要安装 pystray 和 Pillow
- 运行 Codex hooks 的 shell 中需要有 `curl`

如有需要，安装 Flask：

```bash
pip install flask
```

在 Windows 上安装可选的托盘依赖：

```bash
pip install pystray pillow
```

## 运行

```bash
python codex_dashboard.py
```

打开：

```text
http://127.0.0.1:18765
```

在 Windows 上使用通知区域图标运行：

```powershell
python codex_dashboard.py --serve --tray
```

如果 Codex 运行在 WSL 或远程主机上，请把 dashboard 运行在 hook 命令可以访问 `127.0.0.1:18765` 的同一个环境中。

## Windows 开机自启

在 Windows 上，可以把一个小启动脚本放到用户的 Startup 文件夹中，让 dashboard 在登录时自动启动。该脚本会在后台启动服务，并在时钟附近显示 Windows 通知区域图标：

```powershell
python codex_dashboard.py --install-startup
```

检查启动脚本是否存在：

```powershell
python codex_dashboard.py --startup-status
```

移除开机自启脚本：

```powershell
python codex_dashboard.py --uninstall-startup
```

启动脚本会使用当前 Python 解释器，并执行：

```text
pythonw codex_dashboard.py --serve --tray
```

托盘菜单包含 Open Dashboard、Test Alert、Start at Logon 和 Exit。

## Codex Hook 配置

把下面这样的 hooks 加到 Codex 配置里。把 `gpu01` 替换为你希望在 dashboard 中显示的机器名。根据 Codex 用来运行 hooks 的 shell，选择对应的命令版本。

### macOS、Linux 或 WSL

```toml
[features]
codex_hooks = true

[[hooks.UserPromptSubmit]]
matcher = "*"

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "curl -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary @- http://127.0.0.1:18765/api/codex/user_prompt_submit >/dev/null 2>&1 || true"
timeout = 3
statusMessage = "Dashboard: thinking"

[[hooks.PermissionRequest]]
matcher = "*"

[[hooks.PermissionRequest.hooks]]
type = "command"
command = "curl -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary @- http://127.0.0.1:18765/api/codex/permission_request >/dev/null 2>&1 || true"
timeout = 3
statusMessage = "Dashboard: permission needed"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "curl -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary @- http://127.0.0.1:18765/api/codex/stop >/dev/null 2>&1 || true; printf '%s\\n' '{\"continue\":true}'"
timeout = 3
statusMessage = "Dashboard: done"
```

如果想在 POSIX shell 中动态使用机器名，可以在命令里使用双引号 header：

```toml
command = "curl -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H \"X-Codex-Machine: ${HOSTNAME:-local}\" --data-binary @- http://127.0.0.1:18765/api/codex/user_prompt_submit >/dev/null 2>&1 || true"
```

### Windows PowerShell

在 Windows 上请显式使用 `curl.exe`，避免 PowerShell 把 `curl` 解析为别名。把 `gpu01` 替换为你希望显示的名称。

```toml
[features]
codex_hooks = true

[[hooks.UserPromptSubmit]]
matcher = "*"

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "curl.exe -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary '@-' http://127.0.0.1:18765/api/codex/user_prompt_submit *> $null; exit 0"
timeout = 3
statusMessage = "Dashboard: thinking"

[[hooks.PermissionRequest]]
matcher = "*"

[[hooks.PermissionRequest.hooks]]
type = "command"
command = "curl.exe -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary '@-' http://127.0.0.1:18765/api/codex/permission_request *> $null; exit 0"
timeout = 3
statusMessage = "Dashboard: permission needed"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "curl.exe -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H 'X-Codex-Machine: gpu01' --data-binary '@-' http://127.0.0.1:18765/api/codex/stop *> $null; Write-Output '{\"continue\":true}'; exit 0"
timeout = 3
statusMessage = "Dashboard: done"
```

## 机器名

Dashboard 会按以下顺序读取机器名：

1. `X-Codex-Machine` header
2. `X-Machine-Name` header
3. `?machine=...` query parameter
4. JSON 字段：`machine`、`machine_name` 或 `host`

如果没有提供机器名，页面只会显示原始 `cwd` 路径。

## 完成提醒

当真实 Codex turn 结束时，dashboard 会：

- 播放本地提示音，
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮进入提醒闪烁状态，
- 在 tray 模式下闪烁 Windows 通知区域图标，
- 在 tray 模式下显示托盘通知弹窗。

完成类视觉提醒会在 dashboard 标签页获得焦点后清除；也可以通过托盘菜单里的 Open Dashboard 清除。

## 权限提醒

当 Codex 发出 `PermissionRequest` 时，dashboard 会：

- 播放本地提示音，
- 在顶部显示红色提醒，
- 把相关会话标记为 `needs permission`，
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮进入提醒闪烁状态，
- 在 tray 模式下闪烁 Windows 通知区域图标，并显示托盘通知弹窗。

这个提醒只负责显示状态，不会批准或拒绝任何请求。

## 安全说明

服务绑定到 `127.0.0.1`，并且没有身份验证。不要在没有访问控制的情况下把它暴露到公网接口。

Hook payload 可能包含路径、提示词、命令和工具输入。请把 dashboard 当作本地开发工具使用，而不是公开服务。

## 故障排查

在 Windows 上检查哪个进程占用了端口：

```powershell
Get-NetTCPConnection -LocalPort 18765 -State Listen |
  Select-Object LocalAddress,LocalPort,State,OwningProcess
```

然后查看进程信息：

```powershell
$procId = (Get-NetTCPConnection -LocalPort 18765 -State Listen).OwningProcess
Get-Process -Id $procId | Select-Object Id,ProcessName,Path,StartTime
```

在 Linux 或 WSL 上：

```bash
ss -ltnp | grep :18765
```

## 许可证

本项目使用 MIT License。详见 [LICENSE](LICENSE)。
