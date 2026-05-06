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
DONE_ALERTS: list[dict] = []


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
            # import winsound
            # winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
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


TASKBAR_TITLE_MARKERS = (
    APP_NAME,
    "Codex Done",
    "Permission Needed",
)


def set_dashboard_taskbar_flash(stop: bool = False) -> None:
    if not is_windows():
        return

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        FLASHW_STOP = 0
        FLASHW_TRAY = 2
        FLASHW_TIMERNOFG = 12
        flags = FLASHW_STOP if stop else (FLASHW_TRAY | FLASHW_TIMERNOFG)

        def flash_window(hwnd) -> None:
            info = FLASHWINFO(
                ctypes.sizeof(FLASHWINFO),
                hwnd,
                flags,
                0,
                0,
            )
            user32.FlashWindowEx(ctypes.byref(info))

        def window_title(hwnd) -> str:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value

        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_proc(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = window_title(hwnd)
            if title and any(marker in title for marker in TASKBAR_TITLE_MARKERS):
                flash_window(hwnd)
            return True

        user32.EnumWindows(enum_proc_type(enum_proc), 0)
    except Exception:
        pass


def flash_dashboard_taskbar_async() -> None:
    threading.Thread(
        target=set_dashboard_taskbar_flash,
        kwargs={"stop": False},
        daemon=True,
    ).start()


def stop_dashboard_taskbar_flash_async() -> None:
    threading.Thread(
        target=set_dashboard_taskbar_flash,
        kwargs={"stop": True},
        daemon=True,
    ).start()


def stop_dashboard_taskbar_flash_if_idle_async() -> None:
    if not current_attention().get("active"):
        stop_dashboard_taskbar_flash_async()


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


def is_windows_drive_path(path: str) -> bool:
    return len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in ("\\", "/")


def is_windows_unc_path(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def should_open_vscode_remote(machine: str, cwd: str) -> bool:
    if not machine:
        return False
    if is_windows_drive_path(cwd) or is_windows_unc_path(cwd):
        return False
    return True


def vscode_remote_authority(machine: str) -> str:
    if machine.startswith("ssh-remote+"):
        return machine
    return f"ssh-remote+{machine}"


def vscode_command() -> str | None:
    configured = (os.environ.get("CODEX_DASHBOARD_CODE_CMD") or "").strip()
    if configured:
        return configured

    for command in ("code", "Code.exe", "code-insiders", "Code - Insiders.exe"):
        found = shutil.which(command)
        if found:
            return found

    if is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA") or ""
        program_files = os.environ.get("ProgramFiles") or ""
        program_files_x86 = os.environ.get("ProgramFiles(x86)") or ""
        for candidate in (
            os.path.join(local_app_data, "Programs", "Microsoft VS Code", "Code.exe"),
            os.path.join(program_files, "Microsoft VS Code", "Code.exe"),
            os.path.join(program_files_x86, "Microsoft VS Code", "Code.exe"),
        ):
            if candidate and os.path.exists(candidate):
                return candidate

    return None


def open_vscode_path(machine: str | None, cwd: str | None) -> tuple[dict, int]:
    machine = (machine or "").strip()
    cwd = (cwd or "").strip()

    if not cwd:
        return {"ok": False, "error": "missing cwd"}, 400
    if "\x00" in machine or "\x00" in cwd:
        return {"ok": False, "error": "invalid path"}, 400

    command = vscode_command()
    if not command:
        return {"ok": False, "error": "VS Code CLI was not found in PATH"}, 500

    mode = "remote" if should_open_vscode_remote(machine, cwd) else "local"
    args = [command]
    if mode == "remote":
        args.extend(["--remote", vscode_remote_authority(machine)])
    args.append(cwd)

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if is_windows():
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    try:
        subprocess.Popen(args, **kwargs)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 500

    return {
        "ok": True,
        "mode": mode,
        "target": display_path(machine, cwd),
    }, 200


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


def done_summary(
    session_id: str,
    turn_id: str,
    machine: str,
    cwd: str,
    model: str,
    transcript_path: str,
    last_msg: str,
    created_at: float,
) -> dict:
    return {
        "id": f"done:{session_id}:{turn_id}:{created_at}",
        "session_id": session_id,
        "turn_id": turn_id,
        "machine": machine,
        "cwd": cwd,
        "display_cwd": display_path(machine, cwd),
        "model": model,
        "transcript_path": transcript_path,
        "description": safe_short(last_msg, 180),
        "created_at_raw": created_at,
        "created_at": fmt_ts(created_at),
    }


def add_done_attention_unlocked(
    session_id: str,
    turn_id: str,
    machine: str,
    cwd: str,
    model: str,
    transcript_path: str,
    last_msg: str,
    created_at: float,
) -> None:
    DONE_ALERTS.insert(
        0,
        done_summary(
            session_id,
            turn_id,
            machine,
            cwd,
            model,
            transcript_path,
            last_msg,
            created_at,
        ),
    )
    del DONE_ALERTS[20:]


def current_attention_unlocked() -> dict:
    if PERMISSION_REQUESTS:
        alert = PERMISSION_REQUESTS[0]
        return {
            "active": True,
            "kind": "permission",
            "id": alert.get("id") or "",
            "title": "Permission Needed",
            "label": "Needs Permission",
            "count": len(PERMISSION_REQUESTS),
            "display_cwd": alert.get("display_cwd") or "",
            "description": (
                alert.get("description")
                or alert.get("command")
                or alert.get("tool_name")
                or ""
            ),
            "created_at": alert.get("created_at") or "",
            "created_at_raw": alert.get("created_at_raw") or 0,
        }

    if DONE_ALERTS:
        alert = DONE_ALERTS[0]
        return {
            "active": True,
            "kind": "done",
            "id": alert.get("id") or "",
            "title": "Codex Done",
            "label": "Done",
            "count": len(DONE_ALERTS),
            "display_cwd": alert.get("display_cwd") or "",
            "description": alert.get("description") or "",
            "created_at": alert.get("created_at") or "",
            "created_at_raw": alert.get("created_at_raw") or 0,
        }

    return {
        "active": False,
        "kind": "",
        "id": "",
        "title": APP_NAME,
        "label": "",
        "count": 0,
        "display_cwd": "",
        "description": "",
        "created_at": "",
        "created_at_raw": 0,
    }


def current_attention() -> dict:
    with STORE_LOCK:
        return current_attention_unlocked()


def acknowledge_done_alerts() -> None:
    with STORE_LOCK:
        DONE_ALERTS.clear()
    stop_dashboard_taskbar_flash_if_idle_async()


def trigger_test_alert() -> None:
    t = now_ts()
    with STORE_LOCK:
        add_done_attention_unlocked(
            "test-alert",
            "",
            "",
            "",
            "",
            "",
            "Test alert",
            t,
        )
    play_sound_async()
    flash_dashboard_taskbar_async()


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
TITLE_FUTURE_TOLERANCE_SECONDS = 5
TRANSCRIPT_TAIL_BYTES = 1024 * 1024


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


def normalized_prompt_text(text: str | None) -> str:
    return " ".join((text or "").split())


def title_prompt_match_score(title_prompt: str | None, prompt: str | None) -> int:
    title_text = normalized_prompt_text(title_prompt)
    prompt_text = normalized_prompt_text(prompt)
    if not title_text or len(prompt_text) < 4:
        return 0

    if prompt_text in title_text:
        return len(prompt_text)

    excerpt = prompt_text[:160].strip()
    if len(excerpt) >= 20 and excerpt in title_text:
        return len(excerpt)

    first_line = ""
    if prompt:
        first_line = normalized_prompt_text(prompt.strip().splitlines()[0])
    if len(first_line) >= 20 and first_line in title_text:
        return len(first_line)

    return 0


def coerce_event_timestamp(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        try:
            return float(value)
        except ValueError:
            pass

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    return None


def read_transcript_tail(transcript_path: str) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(-TRANSCRIPT_TAIL_BYTES, os.SEEK_END)
            data = f.read()
    except OSError:
        return ""

    return data.decode("utf-8", errors="replace")


def find_turn_aborted_event(transcript_path: str, turn_id: str) -> dict | None:
    latest = None

    for line in read_transcript_tail(transcript_path).splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue

        if item.get("type") != "event_msg":
            continue

        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "turn_aborted":
            continue

        event_turn_id = payload.get("turn_id") or ""
        if turn_id and event_turn_id and event_turn_id != turn_id:
            continue

        completed_at = (
            coerce_event_timestamp(payload.get("completed_at"))
            or coerce_event_timestamp(item.get("timestamp"))
            or now_ts()
        )
        latest = {
            "turn_id": event_turn_id,
            "reason": payload.get("reason") or "",
            "completed_at": completed_at,
        }

    return latest


def refresh_interrupted_turns() -> None:
    with STORE_LOCK:
        candidates = [
            (
                session_id,
                row.get("current_turn_id") or "",
                row.get("transcript_path") or "",
            )
            for session_id, row in CONVERSATIONS.items()
            if row.get("status") == "thinking" and row.get("transcript_path")
        ]

    updates = []
    for session_id, turn_id, transcript_path in candidates:
        event = find_turn_aborted_event(transcript_path, turn_id)
        if event:
            updates.append((session_id, turn_id, event))

    if not updates:
        return

    with STORE_LOCK:
        for session_id, turn_id, event in updates:
            row = CONVERSATIONS.get(session_id)
            if not row or row.get("status") != "thinking":
                continue

            current_turn_id = row.get("current_turn_id") or ""
            if current_turn_id and turn_id and current_turn_id != turn_id:
                continue

            completed_at = event.get("completed_at") or now_ts()
            row.update({
                "status": "interrupted",
                "updated_at": max(row.get("updated_at") or 0, completed_at),
                "finished_at": completed_at,
            })
            clear_permission_requests(session_id, turn_id)


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

    scored_candidates = []
    candidates = []
    for group in groups.values():
        if item["machine"] and group["machine"] != item["machine"]:
            continue
        if item["cwd"] and group["cwd"] != item["cwd"]:
            continue
        if item["model"] and group["model"] and group["model"] != item["model"]:
            continue

        group_ts = group["started_at_raw"] or group["created_at_raw"]
        age = item["updated_at_raw"] - group_ts
        if age < -TITLE_FUTURE_TOLERANCE_SECONDS or age > TITLE_ATTACH_WINDOW_SECONDS:
            continue

        score = title_prompt_match_score(item["prompt"], group["prompt"])
        if score:
            scored_candidates.append((score, abs(age), group))
        else:
            candidates.append((abs(age), group))

    if scored_candidates:
        _, _, group = min(scored_candidates, key=lambda pair: (-pair[0], pair[1]))
    elif candidates:
        _, group = min(candidates, key=lambda pair: pair[0])
    else:
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
                if should_sound:
                    add_done_attention_unlocked(
                        session_id,
                        turn_id,
                        machine,
                        cwd,
                        model,
                        transcript_path,
                        last_msg,
                        t,
                    )
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
                add_done_attention_unlocked(
                    session_id,
                    turn_id,
                    machine,
                    cwd,
                    model,
                    transcript_path,
                    last_msg,
                    t,
                )

    if should_sound:
        play_sound_async()
        flash_dashboard_taskbar_async()
    else:
        stop_dashboard_taskbar_flash_if_idle_async()

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
    flash_dashboard_taskbar_async()
    return jsonify({"ok": True})


@app.get("/bell")
def bell():
    trigger_test_alert()
    return Response("ok\n", mimetype="text/plain")


@app.get("/api/permission_requests")
def permission_requests():
    with STORE_LOCK:
        return jsonify([alert.copy() for alert in PERMISSION_REQUESTS])


@app.get("/api/attention")
def attention():
    return jsonify(current_attention())


@app.post("/api/attention/ack")
def acknowledge_attention():
    payload = request.get_json(force=True, silent=True) or {}
    if (payload.get("kind") or "") == "done":
        acknowledge_done_alerts()
    return jsonify({"ok": True})


@app.post("/api/open_vscode")
def open_vscode():
    payload = request.get_json(force=True, silent=True) or {}
    body, status = open_vscode_path(payload.get("machine"), payload.get("cwd"))
    return jsonify(body), status


@app.get("/api/conversations")
def conversations():
    refresh_interrupted_turns()

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
    .card.interrupted {
      border-color: #64748b;
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
    .badge.interrupted {
      background: rgba(100,116,139,0.22);
      color: #cbd5e1;
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
    .open-target {
      cursor: pointer;
      border-bottom: 1px dotted rgba(148,163,184,0.7);
    }
    .open-target:hover {
      color: #f8fafc;
      border-bottom-color: #f8fafc;
    }
    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: min(520px, calc(100vw - 36px));
      background: #020617;
      color: #e5e7eb;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 10px 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.35);
      opacity: 0;
      pointer-events: none;
      transform: translateY(8px);
      transition: opacity 0.16s ease, transform 0.16s ease;
      z-index: 20;
    }
    .toast.visible {
      opacity: 1;
      transform: translateY(0);
    }
    .toast.error {
      border-color: #ef4444;
      color: #fecaca;
    }
  </style>
</head>
<body>
  <h1>Codex Dashboard</h1>
  <div id="toast" class="toast" role="status" aria-live="polite"></div>
  <div id="alerts" class="alerts"></div>
  <div id="list"></div>

<script>
const BASE_TITLE = 'Codex Dashboard';
const ATTENTION_TITLES = {
  permission: 'Permission Needed',
  done: 'Codex Done',
};
let pendingDoneAckId = '';
let doneAckTimer = null;
let toastTimer = null;

async function load() {
  const [alertRes, attentionRes, conversationRes] = await Promise.all([
    fetch('/api/permission_requests'),
    fetch('/api/attention'),
    fetch('/api/conversations')
  ]);
  const alerts = await alertRes.json();
  const attention = await attentionRes.json();
  const items = await conversationRes.json();

  const alertList = document.getElementById('alerts');
  const list = document.getElementById('list');
  alertList.innerHTML = alerts.map(renderAlert).join('');
  updateAttention(attention);

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
    const statusText = status === 'thinking' ? 'Thinking' : status === 'permission' ? 'Needs Permission' : status === 'interrupted' ? 'Interrupted' : 'Done';
    const openAttrs = openTargetAttrs(item);

    return `
      <div class="card ${status}">
        <div class="row">
          <span class="badge ${status}">${statusText}</span>
          <span class="mono ${openAttrs ? 'open-target' : ''}" ${openAttrs}>${escapeHtml(title)}</span>
          ${item.title && location ? `<span class="muted mono ${openAttrs ? 'open-target' : ''}" ${openAttrs}>${escapeHtml(location)}</span>` : ''}
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

function updateAttention(attention) {
  const isActive = Boolean(attention && attention.active && attention.id);
  const kind = isActive ? String(attention.kind || '') : '';
  const latestAlertId = isActive ? String(attention.id || '') : '';
  const attentionTitle = ATTENTION_TITLES[kind] || (attention && attention.title) || 'Codex Attention';

  document.title = isActive ? attentionTitle : BASE_TITLE;

  if (!isActive) {
    clearDoneAckSchedule();
    return;
  }

  if (kind === 'done') {
    scheduleDoneAck(latestAlertId);
  } else {
    clearDoneAckSchedule();
  }
}

function scheduleDoneAck(alertId) {
  if (pendingDoneAckId === alertId && doneAckTimer) {
    return;
  }
  clearDoneAckSchedule();
  if (document.visibilityState !== 'visible' || !document.hasFocus()) {
    return;
  }

  pendingDoneAckId = alertId;
  doneAckTimer = setTimeout(() => acknowledgeDone(alertId), 800);
}

function clearDoneAckSchedule() {
  if (doneAckTimer) {
    clearTimeout(doneAckTimer);
    doneAckTimer = null;
  }
  pendingDoneAckId = '';
}

async function acknowledgeDone(alertId) {
  try {
    await fetch('/api/attention/ack', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'done', id: alertId})
    });
  } catch (err) {
  } finally {
    if (pendingDoneAckId === alertId) {
      pendingDoneAckId = '';
      doneAckTimer = null;
    }
  }
}

function renderAlert(item) {
  const title = item.tool_name || 'permission';
  const command = item.command || item.description || item.tool_input || '';
  const openAttrs = openTargetAttrs(item);

  return `
    <div class="alert">
      <div class="row">
        <span class="badge permission">Needs Permission</span>
        <span class="alert-title">${escapeHtml(title)}</span>
        <span class="muted">${escapeHtml(item.created_at || '')}</span>
      </div>
      ${item.display_cwd ? `<div class="muted mono ${openAttrs ? 'open-target' : ''}" ${openAttrs}>${escapeHtml(item.display_cwd)}</div>` : ''}
      ${item.description ? `<div class="prompt">${escapeHtml(item.description)}</div>` : ''}
      ${command ? `<div class="answer">${escapeHtml(command)}</div>` : ''}
      <div class="muted">session: <span class="mono">${escapeHtml(item.session_id || '')}</span></div>
    </div>
  `;
}

function openTargetAttrs(item) {
  const cwd = item.cwd || '';
  if (!cwd) {
    return '';
  }
  return `data-open-machine="${escapeHtml(item.machine || '')}" data-open-cwd="${escapeHtml(cwd)}" title="Click to open in VS Code"`;
}

async function openInVscode(machine, cwd) {
  try {
    const res = await fetch('/api/open_vscode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({machine, cwd})
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ok) {
      showToast(body.error || 'Failed to open VS Code', true);
      return;
    }

    const prefix = body.mode === 'remote' ? 'Opening SSH target' : 'Opening local path';
    showToast(`${prefix}: ${body.target || cwd}`);
  } catch (err) {
    showToast('Failed to call dashboard open API', true);
  }
}

function showToast(message, isError = false) {
  const toast = document.getElementById('toast');
  if (!toast) {
    return;
  }

  toast.textContent = message;
  toast.classList.toggle('error', Boolean(isError));
  toast.classList.add('visible');

  if (toastTimer) {
    clearTimeout(toastTimer);
  }
  toastTimer = setTimeout(() => {
    toast.classList.remove('visible');
    toastTimer = null;
  }, 2600);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

window.addEventListener('focus', load);
window.addEventListener('blur', clearDoneAckSchedule);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    load();
  } else {
    clearDoneAckSchedule();
  }
});

document.addEventListener('click', event => {
  const target = event.target.closest('[data-open-cwd]');
  if (!target) {
    return;
  }
  event.preventDefault();
  openInVscode(target.dataset.openMachine || '', target.dataset.openCwd || '');
});

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


def create_tray_image(status: str = "idle"):
    from PIL import Image, ImageDraw

    colors = {
        "idle": ((15, 23, 42), (17, 24, 39)),
        "done": ((5, 46, 22), (20, 83, 45)),
        "permission": ((69, 10, 10), (127, 29, 29)),
    }
    bg, panel = colors.get(status, colors["idle"])

    image = Image.new("RGBA", (64, 64), (*bg, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((7, 7, 57, 57), radius=10, fill=(*panel, 255))
    draw.rectangle((17, 18, 47, 24), fill=(34, 197, 94, 255))
    draw.rectangle((17, 30, 39, 36), fill=(245, 158, 11, 255))
    draw.rectangle((17, 42, 50, 48), fill=(59, 130, 246, 255))

    if status == "permission":
        draw.ellipse((41, 7, 59, 25), fill=(239, 68, 68, 255))
        draw.rectangle((49, 11, 51, 18), fill=(254, 242, 242, 255))
        draw.rectangle((49, 21, 51, 23), fill=(254, 242, 242, 255))
    elif status == "done":
        draw.ellipse((41, 7, 59, 25), fill=(34, 197, 94, 255))
        draw.line((45, 17, 49, 21, 55, 12), fill=(240, 253, 244, 255), width=3)

    return image


def create_tray_images() -> dict[str, object]:
    return {
        "idle": create_tray_image("idle"),
        "done": create_tray_image("done"),
        "permission": create_tray_image("permission"),
    }


def tray_attention_title(attention: dict) -> str:
    if not attention.get("active"):
        return APP_NAME

    title = attention.get("title") or APP_NAME
    count = attention.get("count") or 0
    if count > 1:
        title = f"{title} ({count})"

    location = safe_short(attention.get("display_cwd"), 42)
    if location:
        title = f"{APP_NAME}: {title} - {location}"
    else:
        title = f"{APP_NAME}: {title}"

    return safe_short(title, 120)


def tray_notification_message(attention: dict) -> str:
    location = safe_short(attention.get("display_cwd"), 80)
    description = safe_short(attention.get("description"), 180)

    if location and description:
        return f"{location}\n{description}"
    return description or location or attention.get("label") or APP_NAME


def notify_tray_attention(icon, attention: dict) -> None:
    try:
        icon.notify(
            tray_notification_message(attention),
            safe_short(attention.get("title"), 80) or APP_NAME,
        )
    except Exception:
        pass


def run_tray_attention_loop(icon, tray_images: dict, stop_event: threading.Event) -> None:
    blink_on = False
    last_attention_id = ""
    last_image_key = ""
    last_title = ""
    last_notified_attention_id = ""

    while not stop_event.is_set():
        attention = current_attention()
        attention_id = attention.get("id") or ""
        status = attention.get("kind") if attention.get("active") else "idle"
        status = status if status in tray_images else "idle"

        if status == "idle":
            blink_on = False
            last_attention_id = ""
            last_notified_attention_id = ""
        elif attention_id != last_attention_id:
            blink_on = True
            last_attention_id = attention_id
        else:
            blink_on = not blink_on

        if status != "idle" and attention_id != last_notified_attention_id:
            notify_tray_attention(icon, attention)
            last_notified_attention_id = attention_id

        image_key = status if blink_on else "idle"
        title = tray_attention_title(attention)

        if image_key != last_image_key:
            try:
                icon.icon = tray_images[image_key]
            except Exception:
                pass
            last_image_key = image_key

        if title != last_title:
            try:
                icon.title = title
            except Exception:
                pass
            last_title = title

        stop_event.wait(0.65)

    try:
        icon.icon = tray_images["idle"]
        icon.title = APP_NAME
    except Exception:
        pass


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
        tray_images = create_tray_images()
        tray_image = tray_images["idle"]
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
        acknowledge_done_alerts()
        open_dashboard_now()

    def on_bell(icon, item) -> None:
        trigger_test_alert()

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
            pystray.MenuItem("Test Alert", on_bell),
            pystray.MenuItem("Start at Logon", on_toggle_startup, checked=startup_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        ),
    )

    attention_stop = threading.Event()
    attention_thread = threading.Thread(
        target=run_tray_attention_loop,
        args=(icon, tray_images, attention_stop),
        name="codex-dashboard-tray-attention",
        daemon=True,
    )
    attention_thread.start()

    try:
        icon.run()
    finally:
        attention_stop.set()
        attention_thread.join(timeout=2)
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
