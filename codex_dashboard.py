#!/usr/bin/env python3
import os
import json
import argparse
import threading
import time
import platform
import subprocess
import shutil
import sys
import webbrowser
from datetime import datetime
from flask import Flask, request, jsonify, Response
from werkzeug.serving import make_server

APP_NAME = "Codex Dashboard"
HOST = "127.0.0.1"
PORT = 18765
DASHBOARD_URL = f"http://{HOST}:{PORT}"
STARTUP_LAUNCHER_NAME = "CodexDashboard.vbs"

app = Flask(__name__)

STORE_LOCK = threading.Lock()
CONVERSATIONS: dict[str, dict] = {}
PERMISSION_REQUESTS: list[dict] = []


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def play_sound():
    system = platform.system()

    try:
        if system == "Windows":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return

        if system == "Darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            return

        if system == "Linux":
            for cmd, sound in [
                ("paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
                ("paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"),
                ("aplay", "/usr/share/sounds/alsa/Front_Center.wav"),
            ]:
                if shutil.which(cmd) and os.path.exists(sound):
                    subprocess.Popen([cmd, sound])
                    return
    except Exception:
        pass


def play_sound_async():
    threading.Thread(target=play_sound, daemon=True).start()


def is_windows() -> bool:
    return platform.system() == "Windows"


def windows_background_executable() -> str:
    executable = sys.executable or "pythonw.exe"
    folder, name = os.path.split(executable)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return executable


def startup_command(background: bool = False) -> str:
    script = os.path.abspath(__file__)
    executable = windows_background_executable() if background else (sys.executable or "python")
    args = [executable, script, "--serve"]
    if background:
        args.append("--tray")
    else:
        args.append("--open-browser")
    return subprocess.list2cmdline(args)


def escape_vbs_string(value: str) -> str:
    return value.replace('"', '""')


def open_dashboard_browser() -> None:
    def _open() -> None:
        time.sleep(2)
        try:
            webbrowser.open(DASHBOARD_URL, new=1, autoraise=True)
        except Exception:
            try:
                if is_windows():
                    os.startfile(DASHBOARD_URL)
            except Exception:
                pass

    threading.Thread(target=_open, daemon=True).start()


def startup_folder_path() -> str:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot install the startup launcher")
    return os.path.join(
        appdata,
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
    )


def startup_launcher_path() -> str:
    return os.path.join(startup_folder_path(), STARTUP_LAUNCHER_NAME)


def install_startup() -> None:
    if not is_windows():
        raise RuntimeError("Startup installation is currently only supported on Windows")

    launcher_path = startup_launcher_path()
    command = startup_command(background=True)
    os.makedirs(os.path.dirname(launcher_path), exist_ok=True)
    content = (
        "' Codex Dashboard startup launcher\n"
        "Set shell = CreateObject(\"WScript.Shell\")\n"
        f"shell.Run \"{escape_vbs_string(command)}\", 0, False\n"
    )
    with open(launcher_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(content)


def uninstall_startup() -> None:
    if not is_windows():
        raise RuntimeError("Startup uninstallation is currently only supported on Windows")

    path = startup_launcher_path()
    if os.path.exists(path):
        os.remove(path)


def has_startup_task() -> bool:
    if not is_windows():
        return False

    return os.path.exists(startup_launcher_path())


def safe_short(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def safe_json_short(value, limit: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return safe_short(value, limit)
    try:
        return safe_short(json.dumps(value, ensure_ascii=False), limit)
    except (TypeError, ValueError):
        return safe_short(str(value), limit)


def request_machine(payload: dict) -> str:
    return (
        request.headers.get("X-Codex-Machine")
        or request.headers.get("X-Machine-Name")
        or request.args.get("machine")
        or payload.get("machine")
        or payload.get("machine_name")
        or payload.get("host")
        or ""
    ).strip()


def display_path(machine: str | None, cwd: str | None) -> str:
    machine = (machine or "").strip()
    cwd = (cwd or "").strip()
    if machine and cwd:
        return f"{machine}:{cwd}"
    return cwd or machine


def permission_summary(payload: dict) -> dict:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    machine = request_machine(payload)
    cwd = payload.get("cwd") or ""
    command = tool_input.get("command") or ""
    description = tool_input.get("description") or payload.get("description") or ""
    t = now_ts()

    return {
        "id": f"{payload.get('session_id') or 'unknown-session'}:{payload.get('turn_id') or ''}:{t}",
        "session_id": payload.get("session_id") or "unknown-session",
        "turn_id": payload.get("turn_id") or "",
        "machine": machine,
        "cwd": cwd,
        "display_cwd": display_path(machine, cwd),
        "model": payload.get("model") or "",
        "tool_name": payload.get("tool_name") or "unknown",
        "description": safe_short(description, 300),
        "command": safe_short(command, 600),
        "tool_input": safe_json_short(tool_input, 600),
        "created_at_raw": t,
        "created_at": fmt_ts(t),
    }


def should_sound_on_stop(row: dict | None, transcript_path: str, last_msg: str) -> bool:
    if row and is_internal_title_prompt(row.get("prompt")):
        return False

    # Title-generation turns do not have a transcript path and return
    # {"title": "..."}; they should update the dashboard silently.
    if not transcript_path and extract_generated_title(last_msg):
        return False

    return True


def clear_permission_requests(session_id: str, turn_id: str) -> None:
    if not session_id:
        return

    PERMISSION_REQUESTS[:] = [
        alert for alert in PERMISSION_REQUESTS
        if alert.get("session_id") != session_id
        or (turn_id and alert.get("turn_id") != turn_id)
    ]


TITLE_PROMPT_MARKER = (
    "You are a helpful assistant. You will be presented with a user prompt"
)
TITLE_ATTACH_WINDOW_SECONDS = 300


def is_internal_title_prompt(prompt: str | None) -> bool:
    prompt = (prompt or "").strip()
    return (
        prompt.startswith(TITLE_PROMPT_MARKER)
        and "Generate a concise UI title" in prompt
    )


def extract_generated_title(last_msg: str | None) -> str:
    if not last_msg:
        return ""

    try:
        data = json.loads(last_msg)
    except (TypeError, ValueError):
        return ""

    if not isinstance(data, dict):
        return ""

    return safe_short(data.get("title"), 80)


def conversation_key(item: dict) -> str:
    if item["transcript_path"]:
        return f"transcript:{item['transcript_path']}"
    if item["cwd"]:
        return f"cwd:{item['machine']}:{item['cwd']}:{item['model']}"
    return f"session:{item['session_id']}"


def stored_to_item(row: dict) -> dict:
    return {
        "session_id": row.get("session_id", ""),
        "status": row.get("status", ""),
        "current_turn_id": row.get("current_turn_id", ""),
        "machine": row.get("machine") or "",
        "cwd": row.get("cwd") or "",
        "display_cwd": display_path(row.get("machine"), row.get("cwd")),
        "model": row.get("model") or "",
        "prompt": safe_short(row.get("prompt"), 600),
        "last_assistant_message": safe_short(row.get("last_assistant_message"), 600),
        "transcript_path": row.get("transcript_path") or "",
        "created_at_raw": row.get("created_at") or 0,
        "updated_at_raw": row.get("updated_at") or 0,
        "started_at_raw": row.get("started_at") or 0,
        "finished_at_raw": row.get("finished_at") or 0,
        "turn_count": row.get("turn_count") or 0,
    }


def new_group(key: str, item: dict) -> dict:
    return {
        "group_id": key,
        "session_id": item["session_id"],
        "session_ids": {item["session_id"]},
        "status": item["status"],
        "current_turn_id": item["current_turn_id"],
        "machine": item["machine"],
        "cwd": item["cwd"],
        "display_cwd": item["display_cwd"],
        "model": item["model"],
        "prompt": item["prompt"],
        "last_assistant_message": item["last_assistant_message"],
        "transcript_path": item["transcript_path"],
        "created_at_raw": item["created_at_raw"],
        "updated_at_raw": item["updated_at_raw"],
        "started_at_raw": item["started_at_raw"],
        "finished_at_raw": item["finished_at_raw"],
        "turn_count": item["turn_count"],
        "title": "",
        "title_updated_at_raw": 0,
        "internal_count": 0,
    }


def merge_item(group: dict, item: dict) -> None:
    group["session_ids"].add(item["session_id"])
    group["turn_count"] += item["turn_count"]
    group["created_at_raw"] = min(group["created_at_raw"], item["created_at_raw"])
    group["started_at_raw"] = max(group["started_at_raw"], item["started_at_raw"])
    group["finished_at_raw"] = max(group["finished_at_raw"], item["finished_at_raw"])

    if item["status"] == "thinking":
        group["status"] = "thinking"

    if item["updated_at_raw"] >= group["updated_at_raw"]:
        group["session_id"] = item["session_id"]
        group["current_turn_id"] = item["current_turn_id"]
        group["machine"] = item["machine"]
        group["cwd"] = item["cwd"]
        group["display_cwd"] = item["display_cwd"]
        group["model"] = item["model"]
        group["prompt"] = item["prompt"]
        group["last_assistant_message"] = item["last_assistant_message"]
        group["transcript_path"] = item["transcript_path"]
        group["updated_at_raw"] = item["updated_at_raw"]


def attach_title_row(groups: dict[str, dict], item: dict) -> None:
    title = extract_generated_title(item["last_assistant_message"])
    if not title or not groups:
        return

    candidates = []
    for group in groups.values():
        if item["machine"] and group["machine"] != item["machine"]:
            continue
        if item["cwd"] and group["cwd"] != item["cwd"]:
            continue
        if item["model"] and group["model"] and group["model"] != item["model"]:
            continue
        delta = abs(group["created_at_raw"] - item["updated_at_raw"])
        candidates.append((delta, group))

    if not candidates:
        return

    delta, group = min(candidates, key=lambda pair: pair[0])
    if delta > TITLE_ATTACH_WINDOW_SECONDS:
        return

    group["internal_count"] += 1
    if item["updated_at_raw"] >= group["title_updated_at_raw"]:
        group["title"] = title
        group["title_updated_at_raw"] = item["updated_at_raw"]
    group["updated_at_raw"] = max(group["updated_at_raw"], item["updated_at_raw"])


def finalize_group(group: dict) -> dict:
    session_ids = sorted(group["session_ids"])
    return {
        "group_id": group["group_id"],
        "session_id": group["session_id"],
        "session_ids": session_ids,
        "session_count": len(session_ids),
        "status": group["status"],
        "current_turn_id": group["current_turn_id"],
        "machine": group["machine"],
        "cwd": group["cwd"],
        "display_cwd": group["display_cwd"],
        "model": group["model"],
        "title": group["title"],
        "prompt": group["prompt"],
        "last_assistant_message": group["last_assistant_message"],
        "transcript_path": group["transcript_path"],
        "created_at": fmt_ts(group["created_at_raw"]),
        "updated_at": fmt_ts(group["updated_at_raw"]),
        "started_at": fmt_ts(group["started_at_raw"]),
        "finished_at": fmt_ts(group["finished_at_raw"]),
        "turn_count": group["turn_count"],
        "internal_count": group["internal_count"],
        "_updated_at_raw": group["updated_at_raw"],
    }


@app.post("/api/codex/user_prompt_submit")
def user_prompt_submit():
    payload = request.get_json(force=True, silent=True) or {}

    session_id = payload.get("session_id") or "unknown-session"
    turn_id = payload.get("turn_id") or ""
    machine = request_machine(payload)
    cwd = payload.get("cwd") or ""
    model = payload.get("model") or ""
    prompt = payload.get("prompt") or ""
    transcript_path = payload.get("transcript_path") or ""
    t = now_ts()

    with STORE_LOCK:
        row = CONVERSATIONS.get(session_id)
        if row:
            row.update({
                "status": "thinking",
                "current_turn_id": turn_id,
                "machine": machine,
                "cwd": cwd,
                "model": model,
                "prompt": prompt,
                "transcript_path": transcript_path,
                "updated_at": t,
                "started_at": t,
                "finished_at": None,
                "turn_count": row.get("turn_count", 0) + 1,
            })
        else:
            CONVERSATIONS[session_id] = {
                "session_id": session_id,
                "status": "thinking",
                "current_turn_id": turn_id,
                "machine": machine,
                "cwd": cwd,
                "model": model,
                "prompt": prompt,
                "last_assistant_message": "",
                "transcript_path": transcript_path,
                "created_at": t,
                "updated_at": t,
                "started_at": t,
                "finished_at": None,
                "turn_count": 1,
            }

    return jsonify({"ok": True})


@app.post("/api/codex/stop")
def stop():
    payload = request.get_json(force=True, silent=True) or {}

    session_id = payload.get("session_id") or "unknown-session"
    turn_id = payload.get("turn_id") or ""
    machine = request_machine(payload)
    cwd = payload.get("cwd") or ""
    model = payload.get("model") or ""
    last_msg = payload.get("last_assistant_message") or ""
    transcript_path = payload.get("transcript_path") or ""
    t = now_ts()

    should_sound = False

    with STORE_LOCK:
        row = CONVERSATIONS.get(session_id)
        if row:
            current_turn_id = row.get("current_turn_id") or ""

            # Prevent Stop from an older turn from overwriting a newer thinking turn.
            if not current_turn_id or not turn_id or current_turn_id == turn_id:
                row.update({
                    "status": "done",
                    "current_turn_id": turn_id,
                    "machine": machine,
                    "cwd": cwd,
                    "model": model,
                    "last_assistant_message": last_msg,
                    "transcript_path": transcript_path,
                    "updated_at": t,
                    "finished_at": t,
                })
                should_sound = should_sound_on_stop(row, transcript_path, last_msg)
                clear_permission_requests(session_id, turn_id)
        else:
            CONVERSATIONS[session_id] = {
                "session_id": session_id,
                "status": "done",
                "current_turn_id": turn_id,
                "machine": machine,
                "cwd": cwd,
                "model": model,
                "prompt": "",
                "last_assistant_message": last_msg,
                "transcript_path": transcript_path,
                "created_at": t,
                "updated_at": t,
                "started_at": None,
                "finished_at": t,
                "turn_count": 0,
            }
            should_sound = should_sound_on_stop(None, transcript_path, last_msg)
            clear_permission_requests(session_id, turn_id)

    if should_sound:
        play_sound_async()

    return jsonify({"ok": True})


@app.post("/api/codex/permission_request")
def permission_request():
    payload = request.get_json(force=True, silent=True) or {}
    alert = permission_summary(payload)

    with STORE_LOCK:
        PERMISSION_REQUESTS.insert(0, alert)
        del PERMISSION_REQUESTS[20:]

        session_id = alert["session_id"]
        if session_id in CONVERSATIONS:
            CONVERSATIONS[session_id]["status"] = "permission"
            CONVERSATIONS[session_id]["updated_at"] = alert["created_at_raw"]

    play_sound_async()
    return jsonify({"ok": True})


@app.get("/bell")
def bell():
    play_sound_async()
    return Response("ok\n", mimetype="text/plain")


@app.get("/api/permission_requests")
def permission_requests():
    with STORE_LOCK:
        return jsonify([alert.copy() for alert in PERMISSION_REQUESTS])


@app.get("/api/conversations")
def conversations():
    with STORE_LOCK:
        rows = sorted(
            (row.copy() for row in CONVERSATIONS.values()),
            key=lambda row: row.get("updated_at") or 0,
            reverse=True,
        )[:200]

    groups = {}
    title_rows = []

    for r in rows:
        item = stored_to_item(r)
        if is_internal_title_prompt(item["prompt"]):
            title_rows.append(item)
            continue

        key = conversation_key(item)
        if key in groups:
            merge_item(groups[key], item)
        else:
            groups[key] = new_group(key, item)

    for item in title_rows:
        attach_title_row(groups, item)

    data = [finalize_group(group) for group in groups.values()]
    data.sort(key=lambda item: item["_updated_at_raw"], reverse=True)
    for item in data:
        item.pop("_updated_at_raw", None)

    return jsonify(data)


@app.get("/")
def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Codex Dashboard</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e5e7eb;
      margin: 0;
      padding: 24px;
    }
    h1 {
      margin: 0 0 16px;
      font-size: 26px;
    }
    body.needs-attention {
      min-height: 100vh;
      animation: attentionPulse 1s ease-in-out infinite;
    }
    @keyframes attentionPulse {
      0%, 100% {
        box-shadow: inset 0 0 0 0 rgba(239,68,68,0);
      }
      50% {
        box-shadow: inset 0 0 0 5px rgba(239,68,68,0.45);
      }
    }
    .card {
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.25);
    }
    .card.thinking {
      border-color: #f59e0b;
      box-shadow: 0 0 0 1px rgba(245,158,11,0.25), 0 8px 24px rgba(0,0,0,0.25);
    }
    .card.permission {
      border-color: #ef4444;
      box-shadow: 0 0 0 1px rgba(239,68,68,0.28), 0 8px 24px rgba(0,0,0,0.25);
    }
    .card.done {
      border-color: #22c55e;
    }
    .row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 13px;
      font-weight: 700;
    }
    .badge.thinking {
      background: rgba(245,158,11,0.15);
      color: #fbbf24;
    }
    .badge.permission {
      background: rgba(239,68,68,0.16);
      color: #fca5a5;
    }
    .badge.done {
      background: rgba(34,197,94,0.15);
      color: #4ade80;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: #cbd5e1;
      font-size: 13px;
    }
    .muted {
      color: #94a3b8;
      font-size: 13px;
    }
    .prompt {
      white-space: pre-wrap;
      color: #f8fafc;
      line-height: 1.45;
      margin-top: 10px;
    }
    .answer {
      white-space: pre-wrap;
      color: #cbd5e1;
      line-height: 1.45;
      margin-top: 10px;
      border-top: 1px solid #1f2937;
      padding-top: 10px;
    }
    .alerts {
      margin-bottom: 18px;
    }
    .alert {
      background: #1f1111;
      border: 1px solid #ef4444;
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 12px;
      box-shadow: 0 0 0 1px rgba(239,68,68,0.22), 0 8px 24px rgba(0,0,0,0.25);
    }
    .alert-title {
      color: #fecaca;
      font-weight: 800;
    }
  </style>
</head>
<body>
  <h1>Codex Dashboard</h1>
  <div id="alerts" class="alerts"></div>
  <div id="list"></div>

<script>
const BASE_TITLE = 'Codex Dashboard';
const ATTENTION_TITLE = 'Permission Needed';
let flashTimer = null;
let lastFlashingAlertId = '';

async function load() {
  const [alertRes, conversationRes] = await Promise.all([
    fetch('/api/permission_requests'),
    fetch('/api/conversations')
  ]);
  const alerts = await alertRes.json();
  const items = await conversationRes.json();

  const alertList = document.getElementById('alerts');
  const list = document.getElementById('list');
  alertList.innerHTML = alerts.map(renderAlert).join('');
  updateAttention(alerts);

  if (!items.length) {
    list.innerHTML = '<div class="muted">No Codex events in this run yet.</div>';
    return;
  }

  list.innerHTML = items.map(item => {
    const status = item.status || 'unknown';
    const location = item.display_cwd || item.cwd || '';
    const title = item.title || location || item.session_id;
    const prompt = item.prompt || '';
    const answer = item.last_assistant_message || '';
    const sessionCount = item.session_count || 1;
    const statusText = status === 'thinking' ? 'Thinking' : status === 'permission' ? 'Needs Permission' : 'Done';

    return `
      <div class="card ${status}">
        <div class="row">
          <span class="badge ${status}">${statusText}</span>
          <span class="mono">${escapeHtml(title)}</span>
          ${item.title && location ? `<span class="muted mono">${escapeHtml(location)}</span>` : ''}
        </div>
        <div class="row muted">
          <span>updated: ${escapeHtml(item.updated_at || '')}</span>
          <span>turns: ${item.turn_count || 0}</span>
          <span>model: ${escapeHtml(item.model || '')}</span>
          ${sessionCount > 1 ? `<span>sessions: ${sessionCount}</span>` : ''}
        </div>
        <div class="muted">${sessionCount > 1 ? 'latest session' : 'session'}: <span class="mono">${escapeHtml(item.session_id || '')}</span></div>
        ${prompt ? `<div class="prompt">${escapeHtml(prompt)}</div>` : ''}
        ${answer ? `<div class="answer">${escapeHtml(answer)}</div>` : ''}
      </div>
    `;
  }).join('');
}

function updateAttention(alerts) {
  const latestAlertId = alerts.length ? String(alerts[0].id || '') : '';
  document.body.classList.toggle('needs-attention', Boolean(latestAlertId));

  if (!latestAlertId) {
    stopTitleFlash();
    return;
  }

  if (latestAlertId !== lastFlashingAlertId || !flashTimer) {
    startTitleFlash(latestAlertId);
  }
}

function startTitleFlash(alertId) {
  stopTitleFlash(false);
  lastFlashingAlertId = alertId;
  let showAttention = true;
  document.title = ATTENTION_TITLE;
  flashTimer = setInterval(() => {
    document.title = showAttention ? ATTENTION_TITLE : BASE_TITLE;
    showAttention = !showAttention;
  }, 800);
}

function stopTitleFlash(resetAlert = true) {
  if (flashTimer) {
    clearInterval(flashTimer);
    flashTimer = null;
  }
  document.title = BASE_TITLE;
  if (resetAlert) {
    lastFlashingAlertId = '';
  }
}

function renderAlert(item) {
  const title = item.tool_name || 'permission';
  const command = item.command || item.description || item.tool_input || '';

  return `
    <div class="alert">
      <div class="row">
        <span class="badge permission">Needs Permission</span>
        <span class="alert-title">${escapeHtml(title)}</span>
        <span class="muted">${escapeHtml(item.created_at || '')}</span>
      </div>
      ${item.display_cwd ? `<div class="muted mono">${escapeHtml(item.display_cwd)}</div>` : ''}
      ${item.description ? `<div class="prompt">${escapeHtml(item.description)}</div>` : ''}
      ${command ? `<div class="answer">${escapeHtml(command)}</div>` : ''}
      <div class="muted">session: <span class="mono">${escapeHtml(item.session_id || '')}</span></div>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

load();
setInterval(load, 1000);
</script>
</body>
</html>
"""


class DashboardServer:
    def __init__(self) -> None:
        self._server = make_server(HOST, PORT, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="codex-dashboard-server",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def open_dashboard_now() -> None:
    try:
        webbrowser.open(DASHBOARD_URL, new=1, autoraise=True)
    except Exception:
        try:
            if is_windows():
                os.startfile(DASHBOARD_URL)
        except Exception:
            pass


def create_tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (15, 23, 42, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((7, 7, 57, 57), radius=10, fill=(17, 24, 39, 255))
    draw.rectangle((17, 18, 47, 24), fill=(34, 197, 94, 255))
    draw.rectangle((17, 30, 39, 36), fill=(245, 158, 11, 255))
    draw.rectangle((17, 42, 50, 48), fill=(59, 130, 246, 255))
    return image


def run_server_blocking(open_browser: bool) -> int:
    print(f"Codex dashboard: {DASHBOARD_URL}")
    if open_browser:
        open_dashboard_browser()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
    return 0


def run_server_with_tray(open_browser: bool) -> int:
    if not is_windows():
        print("Tray mode is currently only supported on Windows; falling back to normal service mode.")
        return run_server_blocking(open_browser)

    try:
        import pystray
        tray_image = create_tray_image()
    except ImportError:
        print("Missing tray dependencies: install pystray and pillow first.")
        print("Command: pip install pystray pillow")
        return 1

    try:
        server = DashboardServer()
    except OSError as exc:
        print(f"Could not start the Codex Dashboard service: {exc}")
        return 1

    server.start()
    print(f"Codex dashboard: {DASHBOARD_URL}")

    if open_browser:
        open_dashboard_browser()

    exit_requested = threading.Event()

    def on_open(icon, item) -> None:
        open_dashboard_now()

    def on_bell(icon, item) -> None:
        play_sound_async()

    def on_toggle_startup(icon, item) -> None:
        try:
            if has_startup_task():
                uninstall_startup()
            else:
                install_startup()
        finally:
            icon.update_menu()

    def startup_checked(item) -> bool:
        return has_startup_task()

    def on_exit(icon, item) -> None:
        exit_requested.set()
        icon.visible = False
        icon.stop()

    icon = pystray.Icon(
        APP_NAME,
        tray_image,
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Open Dashboard", on_open, default=True),
            pystray.MenuItem("Test Bell", on_bell),
            pystray.MenuItem("Start at Logon", on_toggle_startup, checked=startup_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        ),
    )

    try:
        icon.run()
    finally:
        server.shutdown()
        if exit_requested.is_set():
            os._exit(0)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Dashboard")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run the dashboard server",
    )
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="install a Windows logon task for startup",
    )
    parser.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="remove the Windows logon task",
    )
    parser.add_argument(
        "--startup-status",
        action="store_true",
        help="print whether the startup launcher exists",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="open the dashboard in a browser after the server starts",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="show a Windows notification-area icon while serving",
    )
    args = parser.parse_args()

    if args.install_startup:
        install_startup()
        print(f"Installed startup launcher: {startup_launcher_path()}")
        return 0

    if args.uninstall_startup:
        uninstall_startup()
        print(f"Removed startup launcher: {startup_launcher_path()}")
        return 0

    if args.startup_status:
        print("installed" if has_startup_task() else "not installed")
        return 0

    if args.tray:
        return run_server_with_tray(args.open_browser)

    return run_server_blocking(args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
