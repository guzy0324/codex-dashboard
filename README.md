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
- In tray mode, includes quota and reset countdowns in the
  tooltip and context menu.
- Plays a sound and flashes a visual reminder when a real Codex turn finishes.
- Suppresses sound for internal title-generation turns.
- Shows permission requests at the top of the page.
- Flashes the Windows taskbar button for the dashboard browser window when possible.
- Flashes the Windows tray icon and shows a tray notification popup in tray mode.
- Supports machine-prefixed paths, for example `gpu01:/mdata/project`.

## Requirements

- Python 3.9+
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
python codex_dashboard.py --tray
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

The task waits 20 seconds to avoid logon contention, runs while on battery
power, and retries up to three times if the process exits unexpectedly. It is
triggered at user logon rather than system boot because the tray icon requires
an interactive desktop session. The one-time management options
`--install-startup`, `--uninstall-startup`, and `--startup-status` are omitted
from the task action; all other supplied options are retained, including
`--tray` and `--open-browser`.

Check the task state:

```powershell
python codex_dashboard.py --startup-status --tray
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

The tray tooltip and right-click menu show the latest known Codex usage. The
tray menu also includes Open Dashboard, Test Alert,
Start at Logon, and Exit.

The dashboard starts a local `codex app-server` process when the Codex CLI is
available, reads `account/rateLimits/read` at startup and once per minute, and
restarts the process with exponential backoff if it exits or a request fails.
The quota included by the task `stop` hook is applied immediately; the stop
hook does not trigger another app-server read. If app-server is unavailable,
transcript quota handling remains available as a fallback. Set
`CODEX_DASHBOARD_CODEX_CMD` to override the Codex CLI command used by the
poller.

Quota poller diagnostics are written to `logs/codex_dashboard_quota.log` under
the project directory by default. Set `CODEX_DASHBOARD_QUOTA_LOG` to choose
another path, or set `CODEX_DASHBOARD_QUOTA_DEBUG=1` to also print them to
stderr. The log records startup, requests, response keys, window counts, and
errors; it does not record full responses or tokens. The poller uses
`sqlite/` under the project directory as its private app-server SQLite state
directory by default, avoiding contention with other Codex processes using
`.codex`. An existing `CODEX_SQLITE_HOME` value takes precedence; set
`CODEX_DASHBOARD_SQLITE_HOME` to override the dashboard default.

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

Copy the latest `dashboard_hook.py` from this repository to your hook location.
Copy `dashboard_hook.example.json` beside it as `dashboard_hook.json` and replace the example command.
Alternatively, set `CODEX_DASHBOARD_HOOK_CONFIG` to a custom config path.

```json
{
  "commands": {
    "*": [],
    "stop": [
      {"command": ["python3", "/absolute/path/to/on_done.py"], "timeout": 2}
    ]
  }
}
```

Supported events are `user_prompt_submit`, `permission_request`, `stop`, and `interrupt`.

To report user interruptions, add this to Codex `config.toml` (adjust the path and machine name; on Windows use `python` and the local script path):

```toml
[[hooks.Interrupt]]

[[hooks.Interrupt.hooks]]
type = "command"
command = "python3 /mdata/guzy0324/.codex/dashboard_hook.py interrupt labgpu"
timeout = 3
```

Restart Codex after configuring the hook. It marks the current turn as `Interrupted`
and clears its permission request without a completion notification. Older turn IDs
are ignored. Interrupt hooks allow at most 3 seconds, so keep extra commands short.
Commands under `*` run first for every event. Commands run sequentially after dashboard reporting,
receive the enriched JSON payload on stdin, and get the event and machine through
`CODEX_HOOK_ENDPOINT` and `CODEX_HOOK_MACHINE` environment variables.
`command` is an argument array without shell interpretation; invoke `bash -lc` or
`powershell -Command` explicitly for shell syntax. `timeout` is in seconds (default 2, maximum 3600).
Output is discarded; errors go to stderr. Failures do not prevent later commands or the Stop response.
Increase the Codex hook timeout to cover reporting, quota retries, and all additional commands.
Without a config file, existing behavior is preserved. Editing the config requires no hook command changes.

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
