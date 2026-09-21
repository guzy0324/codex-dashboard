# Codex Dashboard

Chinese README: [README.zh-CN.md](README.zh-CN.md)

A small local Flask dashboard for OpenAI Codex hooks. It shows active Codex turns,
marks sessions as thinking/done/interrupted, plays a local sound when work finishes, and
highlights permission requests that need user approval.

The dashboard is intentionally in-memory. Restarting the server clears the page.

## Features

- Shows recent Codex conversations from hook events.
- Groups internal title-generation turns into the real conversation.
- Hides internal title prompts from the UI.
- Detects manually interrupted turns from transcript events.
- Shows remaining Codex quota from task hooks and, when the local Codex CLI is
  available, refreshes it through `codex app-server` once per minute.
- Optionally monitors a remote Codex quota keepalive service over HTTP, showing service health,
  the five-hour reset time, and the latest keepalive result.
- In tray mode, includes remote quota keepalive status, quota, and reset countdowns in the
  tooltip and context menu.
- Plays a sound and flashes a visual reminder when a real Codex turn finishes.
- Suppresses sound for internal title-generation turns.
- Shows permission requests at the top of the page.
- Flashes the Windows taskbar button for the dashboard browser window when possible.
- Flashes the Windows tray icon and shows a tray notification popup in tray mode.
- Monitors, starts, and stops `codex remote-control` from the tray context menu.
- Supports machine-prefixed paths, for example `gpu01:/mdata/project`.

## Requirements

- Python 3.9+
- Flask
- Waitress for the Codex quota keepalive service
- pystray and Pillow if you want the Windows notification-area icon
- `curl` available in the shell running Codex hooks
- a working `codex remote-control` command to toggle or automatically start Remote Control

Install Flask if needed:

```bash
pip install flask
```

Install the optional tray dependencies on Windows:

```bash
pip install pystray pillow
```

## Run

```bash
python codex_dashboard.py
```

Open:

```text
http://127.0.0.1:18765
```

On Windows, run with a notification-area icon:

```powershell
python codex_dashboard.py --tray
```

To also start Codex Remote Control in the background when the dashboard starts:

```powershell
python codex_dashboard.py --tray --start-remote-control
```

If Codex runs in WSL or on a remote host, run this dashboard in the same
environment that can reach `127.0.0.1:18765` from the hook commands.

## Windows Startup

On Windows, register a per-user scheduled task to start the dashboard after
logon. It starts the server in the background and shows a Windows
notification-area icon near the clock:

```powershell
python codex_dashboard.py --install-startup --tray
```

To start Codex Remote Control as part of the registered logon task:

```powershell
python codex_dashboard.py --install-startup --tray --start-remote-control
```

The task waits 20 seconds to avoid logon contention, runs while on battery
power, and retries up to three times if the process exits unexpectedly. It is
triggered at user logon rather than system boot because the tray icon requires
an interactive desktop session. The one-time management options
`--install-startup`, `--uninstall-startup`, and `--startup-status` are omitted
from the task action; all other supplied options are retained, including
`--tray`, `--open-browser`, and `--start-remote-control`.

Check the task state:

```powershell
python codex_dashboard.py --startup-status --tray
```

To check a task configured to auto-start Remote Control:

```powershell
python codex_dashboard.py --startup-status --tray --start-remote-control
```

`installed` means the task is enabled and matches the supplied options. For
`outdated` or `disabled`, run `--install-startup` again with the wanted options.

Remove the startup task:

```powershell
python codex_dashboard.py --uninstall-startup
```

Using the first installation example, the task uses the current Python
interpreter and runs:

```text
pythonw codex_dashboard.py --tray
```

Using the installation example with `--start-remote-control`, it runs:

```text
pythonw codex_dashboard.py --tray --start-remote-control
```

The tray tooltip and right-click menu show the latest known Codex usage. The
tray menu also includes Open Dashboard, Test Alert, Codex Remote Control,
Start at Logon, and Exit.

The dashboard starts a local `codex app-server` process when the Codex CLI is
available, reads `account/rateLimits/read` at startup and once per minute, and
restarts the process with exponential backoff if it exits or a request fails.
The quota included by the task `stop` hook is applied immediately; the stop
hook does not trigger another app-server read. If app-server is unavailable,
transcript quota handling remains available as a fallback. Set
`CODEX_DASHBOARD_CODEX_CMD` to override the Codex CLI command used by the
poller as well as Remote Control.

Quota poller diagnostics are written to `logs/codex_dashboard_quota.log` under
the project directory by default. Set `CODEX_DASHBOARD_QUOTA_LOG` to choose
another path, or set `CODEX_DASHBOARD_QUOTA_DEBUG=1` to also print them to
stderr. The log records startup, requests, response keys, window counts, and
errors; it does not record full responses or tokens. The poller uses
`sqlite/` under the project directory as its private app-server SQLite state
directory by default, avoiding contention with other Codex processes using
`.codex`. An existing `CODEX_SQLITE_HOME` value takes precedence; set
`CODEX_DASHBOARD_SQLITE_HOME` to override the dashboard default.

`Codex Remote Control` is checked while a running `codex remote-control`
process is detected. Each time the context menu opens, the item first displays
`Checking...`, runs the probe in the background, and updates the currently open
menu when the result arrives. Closing the menu cancels an unfinished probe.
Launching also runs in the background, so it does not block the tray menu.
Click the unchecked item to start Remote Control; the item stays disabled as
`Starting...` until startup is confirmed or its grace period expires, avoiding
duplicate launches while the process is still appearing. Click the checked
item again to stop Remote Control in the background; it remains disabled as
`Stopping...` until shutdown is confirmed or its grace period expires.
Background startup invokes the cross-platform `codex remote-control` command;
Windows still uses no-console process creation flags. Set
`CODEX_DASHBOARD_CODEX_CMD` to override the Codex CLI command used for launching.

## Codex Quota Keepalive

Run `codex_quota_keepalive_service.py` on the server where the target Codex CLI account is logged in. It uses the existing app-server rate-limit reader from `codex_dashboard.py` to check the 300-minute window every 10 minutes by default. If `resetAt` is still in the future, it only records the check. If `resetAt` has passed, it runs `codex exec --ephemeral "Reply only OK"`, immediately reads rate limits again, and saves the new reset time. The command makes a real Codex request and uses the account's quota; `--ephemeral` prevents the session from being persisted. The service persists its latest rate-limit check and keepalive result, and exposes both through its status API without sending notifications. Keep both Python files in the same directory.

Configure the interval with `CODEX_QUOTA_KEEPALIVE_INTERVAL_SECONDS` (default `600`), the rate-limit request timeout with `CODEX_QUOTA_KEEPALIVE_REQUEST_TIMEOUT_SECONDS` (default `45`), and the Codex keepalive command timeout with `CODEX_QUOTA_KEEPALIVE_TIMEOUT_SECONDS` (default `120`). The longer rate-limit timeout allows Codex to refresh a cold model cache after a systemd restart. The dashboard and tray show whether the service and keepalive worker are running, plus the latest keepalive result.

The server needs Python 3.9 or newer. Create a virtual environment and install the dependencies:

```bash
python3.9 --version
python3.9 -m venv .venv
source .venv/bin/activate
python -m pip install flask waitress
```

Keep the project files in a stable directory on the server. The Codex CLI must be installed and logged in as the Linux account that will run the service. The systemd installer checks that the CLI exists before creating the unit. If it is outside systemd's default `PATH` (for example through `nvm`), pass its absolute path with `--codex-cmd`; the installer stores the resolved path in the environment file.

For a manual launch, pass the bind address, port, and token as command-line arguments:

```bash
python codex_quota_keepalive_service.py --token '<generated-token>' --host 0.0.0.0 --port 18766
```

For a trusted private network or VPN, you can explicitly disable token authentication:

```bash
python codex_quota_keepalive_service.py --no-auth --host 0.0.0.0 --port 18766
```

Without authentication, every client that can reach the port can read the status. Do not expose this mode directly to the public internet. Use a token or access control at the reverse proxy for public deployments. When authentication is enabled, use `--token-file` to read the token from a restricted file. A value passed directly with `--token` is visible in process arguments. Command-line arguments override their corresponding environment variables; if omitted, the service still reads `CODEX_QUOTA_KEEPALIVE_TOKEN`, `CODEX_QUOTA_KEEPALIVE_HOST`, and `CODEX_QUOTA_KEEPALIVE_PORT`. Generate a token with `openssl rand -hex 32`.

The service script can install and remove its own systemd unit. Replace `/opt/codex-dashboard` and `codex` with the project directory and Linux account on your server. The account must be logged into the Codex CLI. This command creates a systemd unit and a root-readable token environment file, then enables and starts the service:

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --install-systemd --service-user codex --codex-cmd /home/codex/.local/bin/codex --host 0.0.0.0
sudo systemctl status codex-quota-keepalive
sudo journalctl -u codex-quota-keepalive -f
```

The installer generates a token, saves it in `/etc/codex-quota-keepalive.env` with mode `600`, and prints it once. Save it for the local dashboard. The unit sets `HOME`, `CODEX_HOME`, and a service-user `PATH`. If the interactive Codex command relies on `HTTP_PROXY`, `HTTPS_PROXY`, or another shell-only environment variable, add the same setting to `/etc/codex-quota-keepalive.env` before restarting the unit. For a trusted private network or VPN, you can disable authentication explicitly with `--no-auth`; only do this when network access is restricted:

Pass `--proxy http://proxy.example:7890` during installation to write the same proxy URL to the systemd environment for HTTP, HTTPS, and SOCKS requests. Use `--no-proxy host1,127.0.0.1` for bypasses. Proxy credentials, if included in the URL, are stored in the mode-600 environment file and may briefly appear in the install command's process list.

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --install-systemd --service-user codex --no-auth --host 0.0.0.0
```

For a public domain, bind to `127.0.0.1` and let a reverse proxy provide HTTPS and forward requests to port 18766. Both deployment modes use the same `/api/status` endpoint. To uninstall, run:

```bash
sudo /opt/codex-dashboard/.venv/bin/python /opt/codex-dashboard/codex_quota_keepalive_service.py --uninstall-systemd
```

Uninstalling stops and removes the systemd unit and its environment file. It leaves the project files and `/var/lib/codex-quota-keepalive` state and `/var/log/codex-quota-keepalive` log directories in place. The example files in `deploy/` remain available for manual systemd setup.

Pass the remote URL directly when starting the local dashboard. No token is needed when the remote service uses no-auth mode:

```powershell
python codex_dashboard.py --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

On Windows, add `--tray` to show the remote status in the tray tooltip and
right-click menu:

```powershell
python codex_dashboard.py --tray --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

If token authentication is enabled, set the same token in the dashboard environment before starting it. The URL can also be configured with `CODEX_DASHBOARD_QUOTA_KEEPALIVE_URL`:

```powershell
$env:CODEX_DASHBOARD_QUOTA_KEEPALIVE_TOKEN = "the-same-token-as-on-the-server"
python codex_dashboard.py --codex-quota-keepalive-url "http://10.0.0.12:18766"
```

The dashboard backend checks the remote service every 30 seconds. Its optional monitor card shows HTTP reachability, service and rate-limit-poller status, the latest rate-limit read and keepalive results, and the five-hour reset state. The card stays hidden when no service URL is configured. Use an `https://` URL for public deployments.

## Codex Hook Config

Add hooks like this to your Codex config. Replace `gpu01` with the machine name
you want displayed in the dashboard. Use the command variant that matches the
shell Codex uses to run hooks.

### macOS, Linux, or WSL

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

For a dynamic machine name in a POSIX shell, use a double-quoted header inside
the command:

```toml
command = "curl -fsS --max-time 2 -X POST -H 'Content-Type: application/json' -H \"X-Codex-Machine: ${HOSTNAME:-local}\" --data-binary @- http://127.0.0.1:18765/api/codex/user_prompt_submit >/dev/null 2>&1 || true"
```

### Remote Host Quota

If the dashboard runs on a Windows workstation while Codex runs on a remote
Linux host, a plain `curl --data-binary @-` hook only sends the remote
transcript path to the dashboard. Windows cannot read a path such as
`/mdata/.../.codex/sessions/...jsonl`, so remote conversations may not show
quota.

In that setup, put a helper on the remote host, for example
`/mdata/guzy0324/.codex/dashboard_hook.py`. The helper reads the latest
`rate_limits` from the remote transcript and forwards the enriched payload to
the dashboard:

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

Then change the three remote hook commands to:

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py user_prompt_submit labgpu"
```

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py permission_request labgpu"
```

```toml
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py stop labgpu"
```

If remote `127.0.0.1:18765` cannot reach the local dashboard, set
`CODEX_DASHBOARD_URL` or use SSH reverse forwarding, for example
`ssh -R 18765:127.0.0.1:18765 labgpu`. After changing hook commands, Codex will
ask you to trust the new hook hashes.

### Windows PowerShell

Use `curl.exe` explicitly on Windows so PowerShell does not resolve `curl` as an
alias. Replace `gpu01` with the name you want displayed.

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

## Machine Name

The dashboard reads the machine name from these sources, in order:

1. `X-Codex-Machine` header
2. `X-Machine-Name` header
3. `?machine=...` query parameter
4. JSON fields: `machine`, `machine_name`, or `host`

If no machine name is provided, only the original `cwd` path is shown.

## Click To Open VS Code And Codex Sidebar

Click a conversation title or path to open VS Code and focus the Codex sidebar:

- If `cwd` is a Windows local path, such as `C:\Users\me\project`, the dashboard runs `code <cwd> --command chatgpt.openSidebar`.
- If a machine name is present and `cwd` is not a Windows local path, the dashboard runs `code --remote ssh-remote+<machine> <cwd> --command chatgpt.openSidebar`.

Remote opens require the local VS Code Remote-SSH extension and a usable SSH Host alias such as `labgpu`. Set `CODEX_DASHBOARD_CODE_CMD` to override the VS Code CLI path.

## Completion Alerts

When a real Codex turn finishes, the dashboard:

- plays a local sound,
- marks the dashboard browser window for Windows taskbar attention when possible,
- flashes the Windows tray icon in tray mode,
- shows a tray notification popup in tray mode.

The tray tooltip and right-click menu include the latest known Codex usage when
transcript rate-limit data is available.

Clicking the tray notification popup opens the related working directory in VS
Code and focuses the Codex sidebar when a path is available, otherwise it opens
a new dashboard page. The completion visual alert is cleared when the dashboard
tab is focused, when the tray notification is clicked, or when Open Dashboard is
selected from the tray menu.

Left-clicking or double-clicking the tray icon opens a new dashboard page.

## Permission Alerts

When Codex emits `PermissionRequest`, the dashboard:

- plays a local sound,
- shows a red alert at the top,
- marks the related conversation as `needs permission`,
- marks the dashboard browser window for Windows taskbar attention when possible,
- flashes the Windows tray icon and shows a tray notification popup in tray mode.

The tray tooltip and right-click menu include the latest known Codex usage when
transcript rate-limit data is available.

Clicking the tray notification popup opens the related working directory in VS
Code and focuses the Codex sidebar when a path is available, otherwise it opens
a new dashboard page.

Left-clicking or double-clicking the tray icon opens a new dashboard page.

The alert is visual only. It does not approve or deny anything.

## Security Notes

The server binds to `127.0.0.1` and has no authentication. Do not expose it on a
public interface without adding access control.

Hook payloads can include paths, prompts, commands, and tool input. Treat the
dashboard as local developer tooling, not as a public service.

## Troubleshooting

Check what owns the port on Windows:

```powershell
Get-NetTCPConnection -LocalPort 18765 -State Listen |
  Select-Object LocalAddress,LocalPort,State,OwningProcess
```

Then inspect the process:

```powershell
$procId = (Get-NetTCPConnection -LocalPort 18765 -State Listen).OwningProcess
Get-Process -Id $procId | Select-Object Id,ProcessName,Path,StartTime
```

On Linux or WSL:

```bash
ss -ltnp | grep :18765
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
