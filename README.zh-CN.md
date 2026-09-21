# Codex Dashboard

英文 README: [README.md](README.md)

Codex Dashboard 是一个面向 OpenAI Codex hooks 的本地 Flask 小面板。它会显示正在运行的 Codex turn，把会话标记为 thinking/done/interrupted，在任务结束时播放本地提示音，并突出显示需要用户批准的权限请求。

这个面板只把状态保存在内存里。重启服务后，页面上的历史记录会被清空。

## 功能

- 显示最近的 Codex hook 事件和会话。
- 把内部标题生成 turn 归并到真实会话中。
- 在 UI 中隐藏内部标题提示词。
- 根据 transcript 事件识别人为中断的 turn。
- 根据任务 hook 显示 Codex 剩余额度；本机 Codex CLI 可用时，通过
  `codex app-server` 每分钟刷新一次账户额度。
- 可选地通过 HTTP 监控远程 Codex 配额保活服务，显示服务状态、5 小时 reset 时间和最近一次保活结果。
- 在真实 Codex turn 结束时播放提示音并显示闪烁式视觉提醒。
- 对内部标题生成 turn 禁用提示音。
- 在页面顶部显示权限请求。
- 可用时让 dashboard 浏览器窗口的 Windows 任务栏按钮闪烁。
- 在 tray 模式下，Windows 通知区域图标会闪烁、显示托盘通知弹窗，并在悬停提示和右键菜单中显示远程配额保活状态、额度和 reset 倒计时。
- 在 tray 右键菜单中监测、启动和关闭 `codex remote-control`。
- 支持带机器名前缀的路径，例如 `gpu01:/mdata/project`。

## 环境要求

- Python 3.9+
- Flask
- Waitress（仅 Codex 配额保活服务需要）
- 如果需要 Windows 通知区域图标，需要安装 pystray 和 Pillow
- 运行 Codex hooks 的 shell 中需要有 `curl`
- 如果需要从托盘启停或自动启动 Remote Control，需要本机可执行 `codex remote-control`

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
python codex_dashboard.py --tray
```

如果希望 dashboard 启动时同时在后台启动 Codex Remote Control：

```powershell
python codex_dashboard.py --tray --start-remote-control
```

如果 Codex 运行在 WSL 或远程主机上，请把 dashboard 运行在 hook 命令可以访问 `127.0.0.1:18765` 的同一个环境中。

## Windows 开机自启

在 Windows 上，可以注册一个当前用户的计划任务，让 dashboard 在登录后自动启动并在时钟附近显示通知区域图标：

```powershell
python codex_dashboard.py --install-startup --tray
```

如果希望登录启动 dashboard 时也自动启动 Codex Remote Control：

```powershell
python codex_dashboard.py --install-startup --tray --start-remote-control
```

该任务会延迟 20 秒启动，避开登录阶段的高负载；允许在使用电池时运行，并在进程异常退出时最多重启 3 次。由于托盘图标依赖用户桌面会话，此处使用“用户登录”触发而不是系统启动触发。一次性管理参数 `--install-startup`、`--uninstall-startup` 和 `--startup-status` 不会写入任务动作；其余传入参数会原样保留，例如 `--tray`、`--open-browser` 和 `--start-remote-control`。

检查计划任务状态：

```powershell
python codex_dashboard.py --startup-status --tray
```

如需检查带 Remote Control 自动启动配置的任务：

```powershell
python codex_dashboard.py --startup-status --tray --start-remote-control
```

输出为 `installed` 时任务已配置且与所传选项匹配；输出为 `outdated` 或 `disabled` 时，使用所需选项重新执行 `--install-startup` 即可修复配置。

移除开机自启任务：

```powershell
python codex_dashboard.py --uninstall-startup
```

使用第一个安装示例时，计划任务会使用当前 Python 解释器并执行：

```text
pythonw codex_dashboard.py --tray
```

使用带 `--start-remote-control` 的安装示例时执行：

```text
pythonw codex_dashboard.py --tray --start-remote-control
```

托盘悬停提示和右键菜单会显示最近一次已知 Codex 用量。托盘菜单还包含 Open Dashboard、Test Alert、Codex Remote Control、Start at Logon 和 Exit。

Dashboard 启动时如果能找到本机 Codex CLI，会在后台启动一个本地
`codex app-server` 进程，启动时以及每隔 1 分钟调用一次
`account/rateLimits/read`。如果进程退出或读取失败，会使用指数退避重启和
重试。任务 `stop` hook 带回的 quota 会立即使用，不会因为任务完成再额外
调用一次 app-server。若 app-server 不可用，仍会保留 transcript quota 作为
回退路径。可以设置 `CODEX_DASHBOARD_CODEX_CMD` 覆盖轮询器和 Remote
Control 使用的 Codex CLI 命令。

轮询器诊断日志默认写入项目目录下的 `logs\codex_dashboard_quota.log`；也可以通过
`CODEX_DASHBOARD_QUOTA_LOG` 指定路径。设置 `CODEX_DASHBOARD_QUOTA_DEBUG=1`
时，日志还会输出到 stderr。日志只记录启动、请求、响应字段、窗口数量和
错误信息，不记录完整响应或 token。轮询器默认会给 app-server 使用项目目录下的
`sqlite\` 作为独立 SQLite 状态目录，避免和其他 Codex 进程争用默认的 `.codex`
状态目录；如果已经设置 `CODEX_SQLITE_HOME` 会优先使用它，也可以用
`CODEX_DASHBOARD_SQLITE_HOME` 覆盖 dashboard 的默认目录。

`Codex Remote Control` 菜单项会在检测到 `codex remote-control` 正在运行时显示勾选。每次打开右键菜单时，该项会先显示 `Checking...` 并在后台检测，完成后直接更新当前已打开菜单中的勾选状态；如果提前关闭菜单，未完成的检测会被取消。未勾选时点击该项会在后台执行跨平台命令 `codex remote-control`，并在启动确认期间显示禁用的 `Starting...`，避免服务尚未出现时被重复启动；已勾选时再次点击会在后台关闭 Remote Control，并在确认停止期间显示禁用的 `Stopping...`。Windows 上该后台进程仍使用无控制台窗口的创建标志。如需覆盖用于启动的 Codex CLI 路径，可以设置环境变量 `CODEX_DASHBOARD_CODEX_CMD`。

## Codex 配额保活

服务器上的 `codex_quota_keepalive_service.py` 会使用该服务器已登录的 Codex CLI，默认每 10 分钟读取一次 `account/rateLimits/read`，检查 300 分钟窗口。若 `resetAt` 仍在未来，只更新检查记录；若 `resetAt` 已到或已过去，则执行一次 `codex exec --ephemeral "Reply only OK"`，随后立即重读额度并保存新 reset 时间。服务不会发送提醒。`--ephemeral` 不保存这次会话，但命令仍会实际调用 Codex 并使用该账号额度。状态文件记录最近一次额度读取、reset 判断和保活命令结果，服务重启后仍可通过状态接口查看。服务依赖同目录中的 `codex_dashboard.py`，复用其中的 Codex app-server 读取逻辑。

检查间隔由 `CODEX_QUOTA_KEEPALIVE_INTERVAL_SECONDS` 配置，默认 `600` 秒；额度读取请求超时由 `CODEX_QUOTA_KEEPALIVE_REQUEST_TIMEOUT_SECONDS` 配置，默认 `45` 秒；单次 `codex exec` 超时由 `CODEX_QUOTA_KEEPALIVE_TIMEOUT_SECONDS` 配置，默认 `120` 秒。较长的额度读取超时可以让 systemd 重启后的 Codex 冷模型缓存完成刷新。保活命令非零退出或执行后额度重读失败，都会作为最近一次保活失败显示在 dashboard 和 tray 中。

远程服务器需要 Python 3.9 或更高版本。创建虚拟环境并安装依赖：

```bash
python3.9 --version
python3.9 -m venv .venv
source .venv/bin/activate
python -m pip install flask waitress
```

请把项目文件放在服务器上一个固定目录中。Codex CLI 必须安装在运行服务的 Linux 用户下，并且该用户已经登录目标账号。systemd 安装器会在创建 unit 前检查 CLI 是否存在。如果 Codex CLI 不在 systemd 默认的 `PATH` 中（例如通过 `nvm` 安装），请用 `--codex-cmd` 传入绝对路径；安装器会把检查通过的路径写入环境文件。

手动启动时，可以把监听地址、端口和令牌作为命令行参数传入：

```bash
python codex_quota_keepalive_service.py --token '<已生成的令牌>' --host 0.0.0.0 --port 18766
```

如果只在可信的内网或 VPN 中使用，也可以显式关闭令牌验证：

```bash
python codex_quota_keepalive_service.py --no-auth --host 0.0.0.0 --port 18766
```

无鉴权时，所有能访问该端口的客户端都能读取状态；不要把这种模式直接暴露到公网。公网部署应启用令牌，或让反向代理提供访问控制。启用鉴权时，支持 `--token-file` 从权限受限的文件读取令牌；直接使用 `--token` 会让令牌出现在进程参数中。命令行参数优先于对应环境变量；不传参数时仍读取 `CODEX_QUOTA_KEEPALIVE_TOKEN`、`CODEX_QUOTA_KEEPALIVE_HOST` 和 `CODEX_QUOTA_KEEPALIVE_PORT`。可用 `openssl rand -hex 32` 生成令牌。

服务脚本可以直接安装或卸载自己的 systemd unit。把 `/opt/codex-dashboard` 和 `codex` 换成服务器上的项目目录和 Linux 用户名。该用户必须已经登录 Codex CLI。安装命令会创建 systemd unit 和仅 root 可读的令牌环境文件，并启用、启动服务：

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --install-systemd --service-user codex --codex-cmd /home/codex/.local/bin/codex --host 0.0.0.0
sudo systemctl status codex-quota-keepalive
sudo journalctl -u codex-quota-keepalive -f
```

安装器会生成一个令牌，将它保存到权限为 `600` 的 `/etc/codex-quota-keepalive.env`，并在终端中显示一次。请保存这个令牌供本机 dashboard 使用。unit 会设置 `HOME`、`CODEX_HOME` 和运行用户的 `PATH`。如果交互式 Codex 命令依赖 `HTTP_PROXY`、`HTTPS_PROXY` 或其他只在 shell 中存在的环境变量，请把相同设置加入 `/etc/codex-quota-keepalive.env`，再重启 unit。若只在可信内网或 VPN 中使用，也可以显式关闭鉴权；只有网络访问本身受到限制时才这样配置：

安装时可以传入 `--proxy http://proxy.example:7890`，安装器会把同一个地址写入 systemd 环境中的 HTTP、HTTPS 和 SOCKS 代理变量；需要绕过代理时使用 `--no-proxy host1,127.0.0.1`。如果代理 URL 含账号密码，它会保存到权限为 `600` 的环境文件中，并可能短暂出现在安装命令的进程列表里。

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --install-systemd --service-user codex --no-auth --host 0.0.0.0
```

公网域名部署时，将监听地址设为 `127.0.0.1`，由反向代理提供 HTTPS 并转发到 18766 端口。两种部署方式都使用 `/api/status` 接口。卸载命令如下：

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --uninstall-systemd
```

卸载会停止并删除 systemd unit 和令牌环境文件，但保留项目文件、`/var/lib/codex-quota-keepalive` 状态目录和 `/var/log/codex-quota-keepalive` 日志目录。`deploy/` 中的示例文件仍可用于手动配置 systemd。

启动本地 dashboard 时，可以直接把远程服务 URL 作为参数传入；无鉴权模式下不用配置令牌：

```powershell
python codex_dashboard.py --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

Windows 托盘模式使用 `--tray`；悬停托盘图标或打开右键菜单即可查看远程状态：

```powershell
python codex_dashboard.py --tray --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

使用令牌鉴权时，在运行 dashboard 的环境中设置相同令牌，再传入 URL 参数。也可以通过 `CODEX_DASHBOARD_QUOTA_KEEPALIVE_URL` 环境变量指定 URL：

```powershell
$env:CODEX_DASHBOARD_QUOTA_KEEPALIVE_TOKEN = "与远程服务相同的令牌"
python codex_dashboard.py --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

dashboard 后台每 30 秒查询一次远程状态；页面上的可选监控卡片会显示 HTTP 连通性、服务与额度轮询线程状态、最近一次额度读取结果、最近一次保活命令结果和 5 小时窗口 reset 状态。未设置服务 URL 时，该卡片隐藏。公网 URL 应使用 `https://`。

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

### 远程主机的 quota

如果 dashboard 运行在 Windows 本机，而 Codex 运行在远程 Linux 主机上，普通 `curl --data-binary @-` hook 只能把远程 transcript 路径发给 dashboard。Windows 不能直接读取 `/mdata/.../.codex/sessions/...jsonl`，所以远程会话可能没有 quota。

这种情况下，在远程主机放一个 helper，例如 `/mdata/guzy0324/.codex/dashboard_hook.py`，由 helper 在远程读取 transcript 里的最新 `rate_limits` 后再转发给 dashboard：

```python
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

endpoint = sys.argv[1]
machine = sys.argv[2]
dashboard = os.environ.get("CODEX_DASHBOARD_URL", "http://127.0.0.1:18765").rstrip("/")

payload = json.loads(sys.stdin.read() or "{}")
transcript = payload.get("transcript_path") or ""


def read_latest_rate_limits(path):
    if not path or not os.path.isfile(path):
        return None

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
        lines = f.read().decode("utf-8", "replace").splitlines()

    latest = None
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue

        payload = item.get("payload") if item.get("type") == "event_msg" else None
        if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
            latest = payload["rate_limits"]

    return latest


rate_limits = None
for _ in range(6 if endpoint == "stop" else 1):
    rate_limits = read_latest_rate_limits(transcript)
    if rate_limits:
        break
    time.sleep(0.15)

if rate_limits:
    payload["rate_limits"] = rate_limits

subprocess.run(
    [
        "curl",
        "-fsS",
        "--max-time",
        "2",
        "-H",
        "Content-Type: application/json",
        "-H",
        f"X-Codex-Machine: {machine}",
        "--data-binary",
        "@-",
        f"{dashboard}/api/codex/{endpoint}",
    ],
    input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

if endpoint == "stop":
    print('{"continue":true}')
```

然后把远程 Codex config 里的三条 hook command 改成：

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py user_prompt_submit labgpu"
```

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py permission_request labgpu"
```

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py stop labgpu"
```

如果远程的 `127.0.0.1:18765` 不能访问本机 dashboard，可以设置 `CODEX_DASHBOARD_URL`，或者用 SSH 反向端口转发，例如 `ssh -R 18765:127.0.0.1:18765 labgpu`。修改 hook command 后，Codex 会要求重新信任新的 hook hash。

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
