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
- Shows remaining Codex quota from transcript `token_count` rate-limit events.
- Plays a sound and flashes a visual reminder when a real Codex turn finishes.
- Suppresses sound for internal title-generation turns.
- Shows permission requests at the top of the page.
- Flashes the Windows taskbar button for the dashboard browser window when possible.
- Flashes the Windows tray icon and shows a tray notification popup in tray mode.
- Supports machine-prefixed paths, for example `gpu01:/mdata/project`.

## Requirements

- Python 3.10+
- Flask
- pystray and Pillow if you want the Windows notification-area icon
- `curl` available in the shell running Codex hooks

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
python codex_dashboard.py --serve --tray
```

If Codex runs in WSL or on a remote host, run this dashboard in the same
environment that can reach `127.0.0.1:18765` from the hook commands.

## Windows Startup

On Windows, you can register the dashboard to start at logon by placing a
small launcher in the user's Startup folder. The launcher starts the server in
the background and shows a Windows notification-area icon near the clock:

```powershell
python codex_dashboard.py --install-startup
```

Check whether the launcher exists:

```powershell
python codex_dashboard.py --startup-status
```

Remove the startup launcher:

```powershell
python codex_dashboard.py --uninstall-startup
```

The launcher starts the dashboard with the current Python interpreter and runs:

```text
pythonw codex_dashboard.py --serve --tray
```

The tray tooltip and right-click menu show the latest known Codex usage. The
tray menu also includes Open Dashboard, Test Alert, Startup, and Exit.

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

## Click To Open VS Code

Click a conversation title or path to open VS Code:

- If `cwd` is a Windows local path, such as `C:\Users\me\project`, the dashboard runs `code <cwd>`.
- If a machine name is present and `cwd` is not a Windows local path, the dashboard runs `code --remote ssh-remote+<machine> <cwd>`.

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
Code when a path is available, otherwise it opens a new dashboard page. The
completion visual alert is cleared when the dashboard tab is focused, when the
tray notification is clicked, or when Open Dashboard is selected from the tray
menu.

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
Code when a path is available, otherwise it opens a new dashboard page.

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
