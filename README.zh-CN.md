# Codex Dashboard

英文 README: [README.md](README.md)

Codex Dashboard 是一个面向 OpenAI Codex hooks 的本地 Flask 小面板。它会显示正在运行的 Codex turn，把会话标记为 thinking/done/interrupted，在任务结束时播放本地提示音，并突出显示需要用户批准的权限请求。

这个面板只把状态保存在内存里。重启服务后，页面上的历史记录会被清空。

## 功能

- 显示最近的 Codex hook 事件和会话。
- 把内部标题生成 turn 归并到真实会话中。
- 在 UI 中隐藏内部标题提示词。
- 根据 transcript 事件识别人为中断的 turn。
- 根据 transcript 中的 `token_count` rate-limit 事件显示 Codex 剩余额度。
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

在 Windows 上，可以注册一个当前用户的计划任务，让 dashboard 在登录后自动启动并在时钟附近显示通知区域图标：

```powershell
python codex_dashboard.py --install-startup
```

该任务会延迟 20 秒启动，避开登录阶段的高负载；允许在使用电池时运行，并在进程异常退出时最多重启 3 次。由于托盘图标依赖用户桌面会话，此处使用“用户登录”触发而不是系统启动触发。

检查计划任务状态：

```powershell
python codex_dashboard.py --startup-status
```

输出为 `installed` 时任务已配置且与当前脚本位置匹配；输出为 `outdated` 或 `disabled` 时，重新执行 `--install-startup` 即可修复配置。

移除开机自启任务：

```powershell
python codex_dashboard.py --uninstall-startup
```

计划任务会使用当前 Python 解释器，并执行：

```text
pythonw codex_dashboard.py --serve --tray
```

托盘悬停提示和右键菜单会显示最近一次已知 Codex 用量。托盘菜单还包含 Open Dashboard、Test Alert、Start at Logon 和 Exit。

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

## 单击打开 VS Code 和 Codex 侧边栏

会话标题或路径可以单击打开 VS Code 并聚焦 Codex 侧边栏：

- `cwd` 是 Windows 本地路径（例如 `C:\Users\me\project`）时，dashboard 执行 `code <cwd> --command chatgpt.openSidebar`。
- 提供了机器名且 `cwd` 不是 Windows 本地路径时，dashboard 执行 `code --remote ssh-remote+<machine> <cwd> --command chatgpt.openSidebar`。

远程打开依赖本机 VS Code 已安装 Remote-SSH 扩展，并且 `<machine>` 是本机 SSH 配置中可用的 Host alias，例如 `labgpu`。如果需要覆盖 VS Code CLI 路径，可以设置环境变量 `CODEX_DASHBOARD_CODE_CMD`。

## 完成提醒

当真实 Codex turn 结束时，dashboard 会：

- 播放本地提示音，
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮进入提醒闪烁状态，
- 在 tray 模式下闪烁 Windows 通知区域图标，
- 在 tray 模式下显示托盘通知弹窗。

托盘悬停提示和右键菜单会在 transcript rate-limit 数据可用时显示最近一次已知 Codex 用量。

点击托盘通知弹窗会在有路径时打开对应工作目录的 VS Code 并聚焦 Codex 侧边栏；没有路径时会新开一个 dashboard 页面。完成类视觉提醒会在 dashboard 标签页获得焦点后清除；也可以通过点击托盘通知弹窗或托盘菜单里的 Open Dashboard 清除。

左键点击或双击托盘图标会新开一个 dashboard 页面。

## 权限提醒

当 Codex 发出 `PermissionRequest` 时，dashboard 会：

- 播放本地提示音，
- 在顶部显示红色提醒，
- 把相关会话标记为 `needs permission`，
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮进入提醒闪烁状态，
- 在 tray 模式下闪烁 Windows 通知区域图标，并显示托盘通知弹窗。

托盘悬停提示和右键菜单会在 transcript rate-limit 数据可用时显示最近一次已知 Codex 用量。

点击托盘通知弹窗会在有路径时打开对应工作目录的 VS Code 并聚焦 Codex 侧边栏；没有路径时会新开一个 dashboard 页面。

左键点击或双击托盘图标会新开一个 dashboard 页面。

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
