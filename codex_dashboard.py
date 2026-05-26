#!/usr/bin/env python3
import argparse
import csv
import json
import locale
import os
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, request, jsonify, Response
from werkzeug.serving import make_server

APP_NAME = "Codex Dashboard"
HOST = "127.0.0.1"
PORT = 18765
DASHBOARD_URL = f"http://{HOST}:{PORT}"
STARTUP_TASK_NAME = r"\Codex Dashboard"
STARTUP_TASK_DELAY = "PT20S"
STARTUP_CONTROL_ARGUMENTS = frozenset({
    "--install-startup",
    "--uninstall-startup",
    "--startup-status",
})
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MAX_ALERTS = 20
VSCODE_CODEX_SIDEBAR_COMMAND = "chatgpt.openSidebar"
CODEX_REMOTE_SUBCOMMAND = "remote-control"
CODEX_REMOTE_MENU_LABEL = "Codex Remote Control"
CODEX_REMOTE_START_TIMEOUT_SECONDS = 15.0
CODEX_REMOTE_STOP_TIMEOUT_SECONDS = 15.0

app = Flask(__name__)

STORE_LOCK = threading.Lock()
REMOTE_CONTROL_LOCK = threading.Lock()
REMOTE_CONTROL_STATUS_LOCK = threading.Lock()
CONVERSATIONS: dict[str, dict] = {}
DONE_ALERTS: list[dict] = []
TRANSCRIPT_QUOTA_CACHE: dict[str, dict] = {}
REMOTE_CONTROL_STATUS_CACHE = {
    "available": False,
    "running": False,
    "pids": [],
    "error": "",
    "loading": True,
    "starting": False,
    "starting_until": 0.0,
    "stopping": False,
    "stopping_until": 0.0,
}


class RemoteControlProbeCancelled(Exception):
    pass


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def run_daemon_thread(target, **kwargs) -> None:
    threading.Thread(target=target, kwargs=kwargs, daemon=True).start()


def play_sound():
    system = platform.system()

    try:
        if system == "Windows":
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


def stop_dashboard_taskbar_flash_async() -> None:
    run_daemon_thread(set_dashboard_taskbar_flash, stop=True)


def stop_dashboard_taskbar_flash_if_idle_async() -> None:
    if not current_attention().get("active"):
        stop_dashboard_taskbar_flash_async()


def notify_attention_async() -> None:
    run_daemon_thread(play_sound)
    run_daemon_thread(set_dashboard_taskbar_flash, stop=False)


def windows_background_executable() -> str:
    executable = sys.executable or "pythonw.exe"
    folder, name = os.path.split(executable)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return executable


def startup_action(arguments: list[str] | None = None) -> tuple[str, str, str]:
    script = os.path.abspath(__file__)
    command = windows_background_executable()
    startup_arguments = [
        argument
        for argument in (arguments or [])
        if argument not in STARTUP_CONTROL_ARGUMENTS
    ]
    command_line = subprocess.list2cmdline([script, *startup_arguments])
    return command, command_line, os.path.dirname(script)


def open_dashboard_url() -> None:
    try:
        webbrowser.open(DASHBOARD_URL, new=2, autoraise=True)
    except Exception:
        try:
            if is_windows():
                os.startfile(DASHBOARD_URL)
        except Exception:
            pass


def open_dashboard_browser() -> None:
    def _open() -> None:
        time.sleep(2)
        open_dashboard_url()

    run_daemon_thread(_open)


def decode_windows_output(output: bytes) -> str:
    if output.startswith((b"\xff\xfe", b"\xfe\xff")):
        return output.decode("utf-16", errors="replace")
    if b"\x00" in output[:80]:
        return output.decode("utf-16-le", errors="replace")
    return output.decode(locale.getpreferredencoding(False), errors="replace")


def run_windows_process(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to run {arguments[0]}: {exc}") from exc


def process_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = decode_windows_output(result.stderr or result.stdout).strip()
    return detail or f"process exited with status {result.returncode}"


def current_windows_user_sid() -> str:
    result = run_windows_process(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    if result.returncode != 0:
        raise RuntimeError(f"Unable to determine the current Windows user: {process_error(result)}")

    text = decode_windows_output(result.stdout).lstrip("\ufeff").strip()
    try:
        row = next(csv.reader(text.splitlines()))
    except StopIteration as exc:
        raise RuntimeError("Unable to determine the current Windows user SID") from exc
    if len(row) < 2 or not row[1].strip():
        raise RuntimeError("Unable to determine the current Windows user SID")
    return row[1].strip()


def task_xml_element(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
    element = ET.SubElement(parent, f"{{{TASK_XML_NAMESPACE}}}{name}")
    if text is not None:
        element.text = text
    return element


def build_startup_task_xml(
    user_sid: str,
    arguments: list[str] | None = None,
) -> bytes:
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(f"{{{TASK_XML_NAMESPACE}}}Task", {"version": "1.3"})
    command, action_arguments, working_directory = startup_action(arguments)

    registration_info = task_xml_element(task, "RegistrationInfo")
    task_xml_element(registration_info, "Description", "Start Codex Dashboard at user logon.")

    triggers = task_xml_element(task, "Triggers")
    logon_trigger = task_xml_element(triggers, "LogonTrigger")
    task_xml_element(logon_trigger, "Enabled", "true")
    task_xml_element(logon_trigger, "UserId", user_sid)
    task_xml_element(logon_trigger, "Delay", STARTUP_TASK_DELAY)

    principals = task_xml_element(task, "Principals")
    principal = task_xml_element(principals, "Principal")
    principal.set("id", "Author")
    task_xml_element(principal, "UserId", user_sid)
    task_xml_element(principal, "LogonType", "InteractiveToken")
    task_xml_element(principal, "RunLevel", "LeastPrivilege")

    settings = task_xml_element(task, "Settings")
    task_xml_element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    task_xml_element(settings, "DisallowStartIfOnBatteries", "false")
    task_xml_element(settings, "StopIfGoingOnBatteries", "false")
    task_xml_element(settings, "AllowHardTerminate", "true")
    task_xml_element(settings, "AllowStartOnDemand", "true")
    task_xml_element(settings, "Enabled", "true")
    task_xml_element(settings, "ExecutionTimeLimit", "PT0S")
    restart = task_xml_element(settings, "RestartOnFailure")
    task_xml_element(restart, "Interval", "PT1M")
    task_xml_element(restart, "Count", "3")

    actions = task_xml_element(task, "Actions")
    actions.set("Context", "Author")
    action = task_xml_element(actions, "Exec")
    task_xml_element(action, "Command", command)
    task_xml_element(action, "Arguments", action_arguments)
    task_xml_element(action, "WorkingDirectory", working_directory)

    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def query_startup_task() -> ET.Element | None:
    result = run_windows_process(["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME, "/XML"])
    if result.returncode != 0:
        return None
    try:
        return ET.fromstring(decode_windows_output(result.stdout))
    except ET.ParseError as exc:
        raise RuntimeError("Windows returned an invalid startup task definition") from exc


def install_startup(arguments: list[str] | None = None) -> None:
    if not is_windows():
        raise RuntimeError("Startup installation is currently only supported on Windows")

    task_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="codex-dashboard-", suffix=".xml", delete=False) as task_file:
            task_file.write(
                build_startup_task_xml(
                    current_windows_user_sid(),
                    arguments=arguments,
                )
            )
            task_path = task_file.name
        result = run_windows_process(
            ["schtasks.exe", "/Create", "/TN", STARTUP_TASK_NAME, "/XML", task_path, "/F"]
        )
        if result.returncode != 0:
            raise RuntimeError(f"Unable to register the startup task: {process_error(result)}")
    finally:
        if task_path and os.path.exists(task_path):
            os.remove(task_path)


def uninstall_startup() -> None:
    if not is_windows():
        raise RuntimeError("Startup uninstallation is currently only supported on Windows")

    if query_startup_task() is not None:
        result = run_windows_process(["schtasks.exe", "/Delete", "/TN", STARTUP_TASK_NAME, "/F"])
        if result.returncode != 0 and query_startup_task() is not None:
            raise RuntimeError(f"Unable to remove the startup task: {process_error(result)}")


def startup_status(arguments: list[str] | None = None) -> str:
    if not is_windows():
        return "not installed"

    task = query_startup_task()
    if task is None:
        return "not installed"

    namespace = {"task": TASK_XML_NAMESPACE}
    enabled = task.findtext("./task:Settings/task:Enabled", "true", namespace).lower()
    logon_trigger = task.find("./task:Triggers/task:LogonTrigger", namespace)
    action = task.find("./task:Actions/task:Exec", namespace)
    command, expected_arguments, working_directory = startup_action(arguments)
    installed_action = (
        action is not None
        and os.path.normcase(action.findtext("task:Command", "", namespace)) == os.path.normcase(command)
        and action.findtext("task:Arguments", "", namespace) == expected_arguments
        and os.path.normcase(action.findtext("task:WorkingDirectory", "", namespace))
        == os.path.normcase(working_directory)
    )
    if enabled != "true":
        return "disabled"
    if logon_trigger is None or not installed_action:
        return "outdated"
    return "installed"


def has_startup_task(arguments: list[str] | None = None) -> bool:
    return startup_status(arguments) == "installed"


def codex_cli_command() -> str | None:
    configured = (os.environ.get("CODEX_DASHBOARD_CODEX_CMD") or "").strip()
    if configured:
        return configured

    return "codex" if shutil.which("codex") else None


def cancellable_communicate(process: subprocess.Popen, cancel_event: threading.Event | None):
    if cancel_event is None:
        return process.communicate()

    while process.poll() is None:
        if cancel_event.wait(0.05):
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.communicate()
            raise RemoteControlProbeCancelled()

    return process.communicate()


def process_rows(cancel_event: threading.Event | None = None) -> list[dict]:
    if is_windows():
        script = (
            """Get-CimInstance Win32_Process -Filter "Name='codex.exe' OR Name='node.exe'" | """
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        arguments = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to run {arguments[0]}: {exc}") from exc

        stdout, stderr = cancellable_communicate(process, cancel_event)
        result = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
        if result.returncode != 0:
            raise RuntimeError(f"Unable to inspect processes: {process_error(result)}")

        text = decode_windows_output(result.stdout).lstrip("\ufeff").strip()
        if not text:
            return []
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise RuntimeError("Unable to inspect processes: invalid Windows process data") from exc
        return rows if isinstance(rows, list) else [rows]

    try:
        process = subprocess.Popen(
            ["ps", "-eo", "pid=,comm=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to inspect processes: {exc}") from exc
    stdout, stderr = cancellable_communicate(process, cancel_event)
    result = subprocess.CompletedProcess(
        ["ps", "-eo", "pid=,comm=,args="],
        process.returncode,
        stdout,
        stderr,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeError(detail or f"Unable to inspect processes: ps exited with status {result.returncode}")

    rows = []
    for line in (result.stdout or "").splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        rows.append({
            "ProcessId": fields[0],
            "Name": fields[1],
            "CommandLine": fields[2] if len(fields) > 2 else fields[1],
        })
    return rows


def is_codex_remote_control_process(row: dict) -> bool:
    process_name = os.path.basename(str(row.get("Name") or "")).lower()
    command_line = str(row.get("CommandLine") or "").lower()
    if CODEX_REMOTE_SUBCOMMAND not in command_line:
        return False
    if process_name in ("codex", "codex.exe"):
        return True
    return (
        process_name in ("node", "node.exe")
        and ("codex.js" in command_line or "@openai/codex" in command_line.replace("\\", "/"))
    )


def codex_remote_control_status(cancel_event: threading.Event | None = None) -> dict:
    command = codex_cli_command()
    try:
        processes = [
            row
            for row in process_rows(cancel_event)
            if is_codex_remote_control_process(row)
        ]
    except RuntimeError as exc:
        return {
            "available": bool(command),
            "running": False,
            "pids": [],
            "error": str(exc),
        }

    pids = []
    for row in processes:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid and pid not in pids:
            pids.append(pid)

    return {
        "available": bool(command),
        "running": bool(processes),
        "pids": pids,
        "error": "",
    }


def cached_codex_remote_control_status() -> dict:
    with REMOTE_CONTROL_STATUS_LOCK:
        clear_expired_codex_remote_control_transition_unlocked()
        status = REMOTE_CONTROL_STATUS_CACHE.copy()
        status["pids"] = list(REMOTE_CONTROL_STATUS_CACHE["pids"])
        return status


def clear_expired_codex_remote_control_transition_unlocked() -> None:
    if (
        REMOTE_CONTROL_STATUS_CACHE["starting"]
        and REMOTE_CONTROL_STATUS_CACHE["starting_until"] <= time.monotonic()
    ):
        REMOTE_CONTROL_STATUS_CACHE["starting"] = False
        REMOTE_CONTROL_STATUS_CACHE["starting_until"] = 0.0
    if (
        REMOTE_CONTROL_STATUS_CACHE["stopping"]
        and REMOTE_CONTROL_STATUS_CACHE["stopping_until"] <= time.monotonic()
    ):
        REMOTE_CONTROL_STATUS_CACHE["stopping"] = False
        REMOTE_CONTROL_STATUS_CACHE["stopping_until"] = 0.0


def store_codex_remote_control_status(
    status: dict,
    finish_starting: bool = False,
    finish_stopping: bool = False,
) -> dict:
    with REMOTE_CONTROL_STATUS_LOCK:
        clear_expired_codex_remote_control_transition_unlocked()
        running = bool(status.get("running"))
        starting = REMOTE_CONTROL_STATUS_CACHE["starting"]
        if running or finish_starting:
            starting = False
        stopping = REMOTE_CONTROL_STATUS_CACHE["stopping"]
        if not running or finish_stopping:
            stopping = False

        REMOTE_CONTROL_STATUS_CACHE.update({
            "available": bool(status.get("available")),
            "running": running,
            "pids": list(status.get("pids") or []),
            "error": str(status.get("error") or ""),
            "loading": False,
            "starting": starting,
            "starting_until": (
                REMOTE_CONTROL_STATUS_CACHE["starting_until"]
                if starting
                else 0.0
            ),
            "stopping": stopping,
            "stopping_until": (
                REMOTE_CONTROL_STATUS_CACHE["stopping_until"]
                if stopping
                else 0.0
            ),
        })
        stored = REMOTE_CONTROL_STATUS_CACHE.copy()
        stored["pids"] = list(REMOTE_CONTROL_STATUS_CACHE["pids"])
        return stored


def mark_codex_remote_control_loading() -> None:
    with REMOTE_CONTROL_STATUS_LOCK:
        clear_expired_codex_remote_control_transition_unlocked()
        REMOTE_CONTROL_STATUS_CACHE["loading"] = not (
            REMOTE_CONTROL_STATUS_CACHE["starting"]
            or REMOTE_CONTROL_STATUS_CACHE["stopping"]
        )
        REMOTE_CONTROL_STATUS_CACHE["error"] = ""


def set_codex_remote_control_starting() -> bool:
    with REMOTE_CONTROL_STATUS_LOCK:
        clear_expired_codex_remote_control_transition_unlocked()
        if (
            REMOTE_CONTROL_STATUS_CACHE["starting"]
            or REMOTE_CONTROL_STATUS_CACHE["stopping"]
            or REMOTE_CONTROL_STATUS_CACHE["running"]
        ):
            return False
        REMOTE_CONTROL_STATUS_CACHE["loading"] = False
        REMOTE_CONTROL_STATUS_CACHE["error"] = ""
        REMOTE_CONTROL_STATUS_CACHE["starting"] = True
        REMOTE_CONTROL_STATUS_CACHE["starting_until"] = (
            time.monotonic() + CODEX_REMOTE_START_TIMEOUT_SECONDS
        )
        return True


def set_codex_remote_control_stopping() -> bool:
    with REMOTE_CONTROL_STATUS_LOCK:
        clear_expired_codex_remote_control_transition_unlocked()
        if (
            REMOTE_CONTROL_STATUS_CACHE["starting"]
            or REMOTE_CONTROL_STATUS_CACHE["stopping"]
            or not REMOTE_CONTROL_STATUS_CACHE["running"]
        ):
            return False
        REMOTE_CONTROL_STATUS_CACHE["loading"] = False
        REMOTE_CONTROL_STATUS_CACHE["error"] = ""
        REMOTE_CONTROL_STATUS_CACHE["stopping"] = True
        REMOTE_CONTROL_STATUS_CACHE["stopping_until"] = (
            time.monotonic() + CODEX_REMOTE_STOP_TIMEOUT_SECONDS
        )
        return True


def start_codex_remote_control(check_running: bool = True) -> None:
    with REMOTE_CONTROL_LOCK:
        if check_running:
            status = codex_remote_control_status()
            if status["error"]:
                raise RuntimeError(status["error"])
            if status["running"]:
                return

        command = codex_cli_command()
        if not command:
            raise RuntimeError("Codex CLI was not found in PATH")

        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if is_windows():
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen([command, CODEX_REMOTE_SUBCOMMAND], **kwargs)
        except OSError as exc:
            raise RuntimeError(f"Unable to start Codex Remote Control: {exc}") from exc

        time.sleep(0.2)
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(
                f"Codex Remote Control exited immediately with status {returncode}"
            )


def stop_codex_remote_control() -> None:
    with REMOTE_CONTROL_LOCK:
        status = codex_remote_control_status()
        if status["error"]:
            raise RuntimeError(status["error"])
        if not status["running"]:
            return

        pids = [str(pid) for pid in status["pids"] if pid]
        if not pids:
            return

        if is_windows():
            arguments = ["taskkill.exe", "/T", "/F"]
            for pid in pids:
                arguments.extend(["/PID", pid])
            result = run_windows_process(arguments)
            if result.returncode != 0:
                remaining = codex_remote_control_status()
                if remaining["running"]:
                    raise RuntimeError(
                        f"Unable to stop Codex Remote Control: {process_error(result)}"
                    )
            return

        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise RuntimeError(f"Unable to stop Codex Remote Control: {exc}") from exc


def start_codex_remote_control_async(
    check_running: bool = True,
    on_error=None,
) -> bool:
    if not set_codex_remote_control_starting():
        return False

    def _start() -> None:
        error = ""
        try:
            start_codex_remote_control(check_running=check_running)
            status = codex_remote_control_status()
        except RuntimeError as exc:
            error = str(exc)
            status = {
                "available": bool(codex_cli_command()),
                "running": False,
                "pids": [],
                "error": error,
            }
        store_codex_remote_control_status(status, finish_starting=bool(error))
        if error and on_error:
            on_error(error)

    run_daemon_thread(_start)
    return True


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
    args.extend(["--command", VSCODE_CODEX_SIDEBAR_COMMAND])

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
        "open_command": VSCODE_CODEX_SIDEBAR_COMMAND,
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
    del DONE_ALERTS[MAX_ALERTS:]


def permission_alerts_unlocked() -> list[dict]:
    alerts = []
    for row in CONVERSATIONS.values():
        alert = row.get("permission")
        if row.get("status") == "permission" and isinstance(alert, dict):
            alerts.append(alert.copy())

    alerts.sort(key=lambda alert: alert.get("created_at_raw") or 0, reverse=True)
    return alerts


def current_attention_unlocked() -> dict:
    permission_alerts = permission_alerts_unlocked()
    if permission_alerts:
        alerts = permission_alerts
        alert = permission_alerts[0]
        kind, title, label = "permission", "Permission Needed", "Needs Permission"
        description = (
            alert.get("description")
            or alert.get("command")
            or alert.get("tool_name")
            or ""
        )
    elif DONE_ALERTS:
        alerts = DONE_ALERTS
        alert = DONE_ALERTS[0]
        kind, title, label = "done", "Codex Done", "Done"
        description = alert.get("description") or ""
    else:
        return {
            "active": False,
            "kind": "",
            "id": "",
            "title": APP_NAME,
            "label": "",
            "count": 0,
            "machine": "",
            "cwd": "",
            "display_cwd": "",
            "description": "",
            "created_at": "",
            "created_at_raw": 0,
        }

    return {
        "active": True,
        "kind": kind,
        "id": alert.get("id") or "",
        "title": title,
        "label": label,
        "count": len(alerts),
        "machine": alert.get("machine") or "",
        "cwd": alert.get("cwd") or "",
        "display_cwd": alert.get("display_cwd") or "",
        "description": description,
        "created_at": alert.get("created_at") or "",
        "created_at_raw": alert.get("created_at_raw") or 0,
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
    notify_attention_async()


def should_sound_on_stop(row: dict | None, transcript_path: str, last_msg: str) -> bool:
    if row and is_internal_title_prompt(row.get("prompt")):
        return False

    # Title-generation turns do not have a transcript path and return
    # {"title": "..."}; they should update the dashboard silently.
    if not transcript_path and extract_generated_title(last_msg):
        return False

    return True


def is_current_turn(row: dict, turn_id: str) -> bool:
    current_turn_id = row.get("current_turn_id") or ""
    return not current_turn_id or not turn_id or current_turn_id == turn_id


TITLE_PROMPT_MARKER = (
    "You are a helpful assistant. You will be presented with a user prompt"
)
TITLE_ATTACH_WINDOW_SECONDS = 300
TITLE_FUTURE_TOLERANCE_SECONDS = 5
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
RATE_LIMIT_WINDOW_KEYS = ("primary", "secondary")
TRAY_QUOTA_REFRESH_SECONDS = 5.0


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
        value = float(value)
        if value > 10_000_000_000:
            value = value / 1000
        return value

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        try:
            return coerce_event_timestamp(float(value))
        except ValueError:
            pass

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    return None


def coerce_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.endswith("%"):
            value = value[:-1].strip()
        try:
            return float(value)
        except ValueError:
            return None
    return None


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def rounded_percent(value: float) -> float:
    return round(clamp_percent(value), 1)


def format_percent_value(value) -> str:
    num = coerce_float(value)
    if num is None:
        return ""

    rounded = rounded_percent(num)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"


def rate_limit_window_label(window_key: str, window_minutes) -> str:
    minutes_float = coerce_float(window_minutes)
    if minutes_float and minutes_float > 0:
        minutes = int(minutes_float)
        if minutes % 1440 == 0:
            return f"{minutes // 1440}d"
        if minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"

    return window_key


def normalize_rate_limit_window(window_key: str, window: dict) -> dict | None:
    if not isinstance(window, dict):
        return None

    used_percent = coerce_float(window.get("used_percent"))
    if used_percent is None:
        return None

    used_percent = rounded_percent(used_percent)
    remaining_percent = rounded_percent(100 - used_percent)
    window_minutes = coerce_float(window.get("window_minutes"))
    resets_at_raw = coerce_event_timestamp(window.get("resets_at"))

    return {
        "key": window_key,
        "label": rate_limit_window_label(window_key, window_minutes),
        "used_percent": used_percent,
        "remaining_percent": remaining_percent,
        "window_minutes": int(window_minutes) if window_minutes else 0,
        "resets_at_raw": resets_at_raw or 0,
        "resets_at": fmt_ts(resets_at_raw),
    }


def quota_summary_from_rate_limits(rate_limits: dict | None, event_ts: float | None) -> dict:
    if not isinstance(rate_limits, dict):
        return {}

    windows = []
    handled_keys = set()
    for key in RATE_LIMIT_WINDOW_KEYS:
        window = normalize_rate_limit_window(key, rate_limits.get(key))
        handled_keys.add(key)
        if window:
            windows.append(window)

    for key, value in rate_limits.items():
        if key in handled_keys:
            continue
        window = normalize_rate_limit_window(str(key), value)
        if window:
            windows.append(window)

    if not windows:
        return {}

    event_ts = event_ts or now_ts()
    return {
        "limit_id": rate_limits.get("limit_id") or "",
        "limit_name": rate_limits.get("limit_name") or "",
        "plan_type": rate_limits.get("plan_type") or "",
        "rate_limit_reached_type": rate_limits.get("rate_limit_reached_type") or "",
        "windows": windows,
        "updated_at_raw": event_ts,
        "updated_at": fmt_ts(event_ts),
    }


def quota_summary_from_payload(payload: dict, event_ts: float | None = None) -> dict:
    if not isinstance(payload, dict):
        return {}

    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict):
            rate_limits = nested_payload.get("rate_limits")

    return quota_summary_from_rate_limits(rate_limits, event_ts)


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


def transcript_file_signature(transcript_path: str) -> tuple[int, int] | None:
    if not transcript_path or not os.path.isfile(transcript_path):
        return None

    try:
        stat = os.stat(transcript_path)
    except OSError:
        return None

    return (stat.st_mtime_ns, stat.st_size)


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


def find_latest_quota_summary(transcript_path: str) -> dict:
    latest = {}

    for line in read_transcript_tail(transcript_path).splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue

        if item.get("type") != "event_msg":
            continue

        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue

        if payload.get("type") != "token_count" and "rate_limits" not in payload:
            continue

        event_ts = coerce_event_timestamp(item.get("timestamp")) or now_ts()
        quota = quota_summary_from_payload(payload, event_ts)
        if quota:
            latest = quota

    return latest


def cached_latest_quota_summary(transcript_path: str) -> dict:
    signature = transcript_file_signature(transcript_path)
    cached = TRANSCRIPT_QUOTA_CACHE.get(transcript_path)
    if cached and cached.get("signature") == signature:
        return cached.get("quota") or {}

    if signature is None:
        return cached.get("quota") if cached else {}

    quota = find_latest_quota_summary(transcript_path)
    TRANSCRIPT_QUOTA_CACHE[transcript_path] = {
        "signature": signature,
        "quota": quota,
    }
    return quota


def refresh_rate_limits() -> None:
    with STORE_LOCK:
        candidates = [
            (session_id, row.get("transcript_path") or "")
            for session_id, row in CONVERSATIONS.items()
            if row.get("transcript_path")
        ]

    if not candidates:
        return

    quota_by_path = {}
    updates = []
    for session_id, transcript_path in candidates:
        if transcript_path not in quota_by_path:
            quota_by_path[transcript_path] = cached_latest_quota_summary(transcript_path)

        quota = quota_by_path[transcript_path]
        if quota:
            updates.append((session_id, transcript_path, quota))

    if not updates:
        return

    with STORE_LOCK:
        for session_id, transcript_path, quota in updates:
            row = CONVERSATIONS.get(session_id)
            if not row or row.get("transcript_path") != transcript_path:
                continue

            current_quota = row.get("quota")
            current_updated_at = (
                current_quota.get("updated_at_raw")
                if isinstance(current_quota, dict)
                else 0
            ) or 0
            if quota.get("updated_at_raw", 0) >= current_updated_at:
                row["quota"] = quota


def copy_quota_summary(quota: dict | None) -> dict:
    if not isinstance(quota, dict):
        return {}

    copied = {
        key: value
        for key, value in quota.items()
        if key != "windows"
    }
    copied["windows"] = [
        window.copy()
        for window in quota.get("windows") or []
        if isinstance(window, dict)
    ]
    return copied


def latest_quota_summary_unlocked() -> dict:
    latest = {}
    latest_updated_at = 0

    for row in CONVERSATIONS.values():
        quota = row.get("quota")
        if not isinstance(quota, dict):
            continue

        updated_at = quota.get("updated_at_raw", 0) or 0
        if updated_at >= latest_updated_at:
            latest = quota
            latest_updated_at = updated_at

    return copy_quota_summary(latest)


def latest_quota_for_display(refresh: bool = False) -> dict:
    if refresh:
        refresh_rate_limits()

    with STORE_LOCK:
        return latest_quota_summary_unlocked()


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
                "permission": None,
            })


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
        "quota": row.get("quota") or None,
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
        "quota": item["quota"],
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

    item_quota = item.get("quota")
    if isinstance(item_quota, dict):
        group_quota = group.get("quota")
        group_quota_updated_at = (
            group_quota.get("updated_at_raw")
            if isinstance(group_quota, dict)
            else 0
        ) or 0
        if item_quota.get("updated_at_raw", 0) >= group_quota_updated_at:
            group["quota"] = item_quota


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
        "quota": group["quota"],
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
    quota = quota_summary_from_payload(payload, t)

    with STORE_LOCK:
        row = CONVERSATIONS.get(session_id)
        if row:
            updates = {
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
                "permission": None,
            }
            if quota:
                updates["quota"] = quota
            row.update(updates)
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
                "quota": quota or None,
                "permission": None,
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
    quota = quota_summary_from_payload(payload, t)

    should_sound = False

    with STORE_LOCK:
        row = CONVERSATIONS.get(session_id)
        if row:
            # Prevent Stop from an older turn from overwriting a newer thinking turn.
            if is_current_turn(row, turn_id):
                updates = {
                    "status": "done",
                    "current_turn_id": turn_id,
                    "machine": machine,
                    "cwd": cwd,
                    "model": model,
                    "last_assistant_message": last_msg,
                    "transcript_path": transcript_path,
                    "updated_at": t,
                    "finished_at": t,
                    "permission": None,
                }
                if quota:
                    updates["quota"] = quota
                row.update(updates)
                should_sound = should_sound_on_stop(row, transcript_path, last_msg)
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
                "quota": quota or None,
                "permission": None,
            }
            should_sound = should_sound_on_stop(None, transcript_path, last_msg)
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
        notify_attention_async()
    else:
        stop_dashboard_taskbar_flash_if_idle_async()

    return jsonify({"ok": True})


@app.post("/api/codex/permission_request")
def permission_request():
    payload = request.get_json(force=True, silent=True) or {}
    alert = permission_summary(payload)

    with STORE_LOCK:
        session_id = alert["session_id"]
        turn_id = alert["turn_id"]
        row = CONVERSATIONS.get(session_id)
        if row and not is_current_turn(row, turn_id):
            return jsonify({"ok": True, "ignored": True})

        if row:
            alert = alert.copy()
            alert["machine"] = alert.get("machine") or row.get("machine") or ""
            alert["cwd"] = alert.get("cwd") or row.get("cwd") or ""
            alert["display_cwd"] = display_path(alert.get("machine"), alert.get("cwd"))
            alert["model"] = alert.get("model") or row.get("model") or ""
            row.update({
                "status": "permission",
                "current_turn_id": turn_id or row.get("current_turn_id") or "",
                "machine": alert.get("machine") or row.get("machine") or "",
                "cwd": alert.get("cwd") or row.get("cwd") or "",
                "model": alert.get("model") or row.get("model") or "",
                "updated_at": alert["created_at_raw"],
                "permission": alert,
            })
        else:
            CONVERSATIONS[session_id] = {
                "session_id": session_id,
                "status": "permission",
                "current_turn_id": turn_id,
                "machine": alert.get("machine") or "",
                "cwd": alert.get("cwd") or "",
                "model": alert.get("model") or "",
                "prompt": "",
                "last_assistant_message": "",
                "transcript_path": payload.get("transcript_path") or "",
                "created_at": alert["created_at_raw"],
                "updated_at": alert["created_at_raw"],
                "started_at": None,
                "finished_at": None,
                "turn_count": 0,
                "quota": None,
                "permission": alert,
            }

    notify_attention_async()
    return jsonify({"ok": True})


@app.get("/bell")
def bell():
    trigger_test_alert()
    return Response("ok\n", mimetype="text/plain")


@app.get("/api/permission_requests")
def permission_requests():
    with STORE_LOCK:
        return jsonify(permission_alerts_unlocked())


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
    refresh_rate_limits()

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
    .quota-strip {
      display: none;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      background: #020617;
      border: 1px solid #1f2937;
      border-radius: 12px;
      padding: 12px 14px;
      margin-bottom: 18px;
    }
    .quota-strip.visible {
      display: flex;
    }
    .quota-heading {
      color: #f8fafc;
      font-weight: 800;
    }
    .quota-window {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 24px;
      color: #cbd5e1;
      font-size: 13px;
    }
    .quota-label {
      color: #94a3b8;
      font-weight: 700;
    }
    .quota-value {
      color: #f8fafc;
      font-weight: 800;
    }
    .quota-reset {
      color: #64748b;
      font-size: 12px;
      white-space: nowrap;
    }
    .quota-meter {
      width: 96px;
      height: 7px;
      overflow: hidden;
      border-radius: 999px;
      background: #334155;
    }
    .quota-fill {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: #22c55e;
    }
    .quota-fill.warn {
      background: #f59e0b;
    }
    .quota-fill.danger {
      background: #ef4444;
    }
    .quota-updated {
      color: #64748b;
      font-size: 12px;
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
  <div id="quota" class="quota-strip"></div>
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
  renderQuota(items);

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
    const quota = quotaSummary(item.quota);

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
          ${quota ? `<span>quota: ${escapeHtml(quota)}</span>` : ''}
          ${sessionCount > 1 ? `<span>sessions: ${sessionCount}</span>` : ''}
        </div>
        <div class="muted">${sessionCount > 1 ? 'latest session' : 'session'}: <span class="mono">${escapeHtml(item.session_id || '')}</span></div>
        ${prompt ? `<div class="prompt">${escapeHtml(prompt)}</div>` : ''}
        ${answer ? `<div class="answer">${escapeHtml(answer)}</div>` : ''}
      </div>
    `;
  }).join('');
}

function renderQuota(items) {
  const quotaEl = document.getElementById('quota');
  if (!quotaEl) {
    return;
  }

  const quotaItem = items.find(item => quotaSummary(item.quota));
  if (!quotaItem) {
    quotaEl.innerHTML = '';
    quotaEl.classList.remove('visible');
    return;
  }

  const quota = quotaItem.quota || {};
  const windows = Array.isArray(quota.windows) ? quota.windows : [];
  const updated = quota.updated_at || '';
  quotaEl.innerHTML = `
    <span class="quota-heading">Remaining quota</span>
    ${windows.map(renderQuotaWindow).join('')}
    ${updated ? `<span class="quota-updated">updated: ${escapeHtml(updated)}</span>` : ''}
  `;
  quotaEl.classList.add('visible');
}

function renderQuotaWindow(window) {
  const remaining = clampNumber(Number(window.remaining_percent), 0, 100);
  const used = clampNumber(Number(window.used_percent), 0, 100);
  const remainingText = formatPercent(remaining);
  const usedText = formatPercent(used);
  const label = window.label || window.key || 'quota';
  const state = remaining <= 10 ? 'danger' : remaining <= 25 ? 'warn' : '';
  const titleParts = [
    `${remainingText}% left`,
    `${usedText}% used`,
    window.resets_at ? `resets: ${window.resets_at}` : ''
  ].filter(Boolean);

  return `
    <span class="quota-window" title="${escapeHtml(titleParts.join(' / '))}">
      <span class="quota-label">${escapeHtml(label)}</span>
      <span class="quota-value">${remainingText}%</span>
      <span class="quota-meter"><span class="quota-fill ${state}" style="width: ${remaining}%"></span></span>
      ${window.resets_at ? `<span class="quota-reset">reset: ${escapeHtml(window.resets_at)}</span>` : ''}
    </span>
  `;
}

function quotaSummary(quota) {
  const windows = quota && Array.isArray(quota.windows) ? quota.windows : [];
  return windows.map(window => {
    const remaining = formatPercent(window.remaining_percent);
    if (!remaining) {
      return '';
    }
    const label = window.label || window.key || 'quota';
    return `${label}: ${remaining}% left`;
  }).filter(Boolean).join(' / ');
}

function formatPercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return '';
  }

  const rounded = Math.round(num * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function clampNumber(value, min, max) {
  if (!Number.isFinite(value)) {
    return min;
  }
  return Math.min(max, Math.max(min, value));
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
  return `data-open-machine="${escapeHtml(item.machine || '')}" data-open-cwd="${escapeHtml(cwd)}" title="Click to open in VS Code and Codex Sidebar"`;
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
    const suffix = body.open_command ? ' and Codex sidebar' : '';
    showToast(`${prefix}${suffix}: ${body.target || cwd}`);
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


def open_current_attention_in_vscode() -> bool:
    attention = current_attention()
    if not attention.get("active") or not attention.get("cwd"):
        return False

    body, status = open_vscode_path(attention.get("machine"), attention.get("cwd"))
    if status >= 400 or not body.get("ok"):
        return False

    if attention.get("kind") == "done":
        acknowledge_done_alerts()

    return True


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


def quota_window_usage_text(window: dict, include_reset: bool = False) -> str:
    if not isinstance(window, dict):
        return ""

    label = str(window.get("label") or window.get("key") or "quota")
    remaining = format_percent_value(window.get("remaining_percent"))
    details = []

    if remaining:
        details.append(f"{remaining}% left")
    if include_reset and window.get("resets_at"):
        details.append(f"resets {window['resets_at']}")

    if not details:
        return ""
    return f"{label}: {', '.join(details)}"


def tray_quota_display_lines(
    quota: dict,
    max_windows: int = 4,
    include_reset: bool = True,
    include_updated: bool = True,
    no_quota_text: str = "Usage: no quota yet",
) -> list[str]:
    windows = quota.get("windows") if isinstance(quota, dict) else []
    if not isinstance(windows, list):
        windows = []

    lines = []
    for window in windows[:max_windows]:
        text = quota_window_usage_text(window, include_reset=include_reset)
        if text:
            lines.append(f"Usage {text}")

    remaining_count = max(0, len(windows) - max_windows)
    if remaining_count:
        lines.append(f"Usage: +{remaining_count} more quota windows")

    updated_at = quota.get("updated_at") if isinstance(quota, dict) else ""
    if lines and include_updated and updated_at:
        lines.append(f"Usage updated: {updated_at}")

    return lines or ([no_quota_text] if no_quota_text else [])


def truncate_tray_tooltip_lines(lines: list[str], limit: int = 120) -> str:
    kept = []
    used = 0
    for line in lines:
        original = str(line)
        text = original
        separator = 1 if kept else 0
        remaining = limit - used - separator
        if remaining <= 0:
            break
        if len(text) > remaining:
            if kept and remaining < 24:
                break
            text = text[:remaining] if remaining <= 3 else text[:remaining - 3] + "..."
        kept.append(text)
        used += separator + len(text)
        if len(text) < len(original):
            break

    return "\n".join(kept) or APP_NAME


def tray_hover_title(attention: dict, quota: dict) -> str:
    quota_lines = tray_quota_display_lines(quota, max_windows=3, no_quota_text="")
    if not quota_lines:
        return tray_attention_title(attention)

    lines = [APP_NAME, *quota_lines]
    if attention.get("active"):
        title = tray_attention_title(attention)
        prefix = f"{APP_NAME}: "
        attention_part = title[len(prefix):] if title.startswith(prefix) else title
        if attention_part and attention_part != APP_NAME:
            lines.append(safe_short(attention_part, 60))

    return truncate_tray_tooltip_lines(lines)


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
    last_quota = {}
    last_quota_refresh_at = 0.0

    while not stop_event.is_set():
        monotonic_now = time.monotonic()
        if monotonic_now - last_quota_refresh_at >= TRAY_QUOTA_REFRESH_SECONDS:
            last_quota_refresh_at = monotonic_now
            try:
                last_quota = latest_quota_for_display(refresh=True)
            except Exception:
                last_quota = latest_quota_for_display(refresh=False)

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
        title = tray_hover_title(attention, last_quota)

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


def run_server_blocking(open_browser: bool, start_remote_control: bool = False) -> int:
    print(f"Codex dashboard: {DASHBOARD_URL}")
    if open_browser:
        open_dashboard_browser()
    if start_remote_control:
        start_codex_remote_control_async(
            on_error=lambda error: print(
                f"Could not start Codex Remote Control: {error}",
                file=sys.stderr,
            )
        )
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
    return 0


def run_server_with_tray(
    open_browser: bool,
    start_remote_control: bool = False,
    startup_arguments: list[str] | None = None,
) -> int:
    if not is_windows():
        print("Tray mode is currently only supported on Windows; falling back to normal service mode.")
        return run_server_blocking(open_browser, start_remote_control)

    try:
        import ctypes
        from ctypes import wintypes

        import pystray
        from pystray._util import win32 as pystray_win32
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
    notification_click_event = pystray_win32.WM_USER + 5
    tray_activate_events = {pystray_win32.WM_LBUTTONUP, 0x0203}
    tray_context_menu_event = getattr(pystray_win32, "WM_RBUTTONUP", 0x0205)
    tray_open_cooldown_seconds = 0.75
    remote_menu_item_id = {"value": 0}
    set_menu_item_info = ctypes.windll.user32.SetMenuItemInfoW
    set_menu_item_info.argtypes = (
        wintypes.HMENU,
        wintypes.UINT,
        wintypes.BOOL,
        ctypes.POINTER(pystray_win32.MENUITEMINFO),
    )
    set_menu_item_info.restype = wintypes.BOOL
    draw_menu_bar = ctypes.windll.user32.DrawMenuBar
    draw_menu_bar.argtypes = (wintypes.HWND,)
    draw_menu_bar.restype = wintypes.BOOL

    class DashboardTrayIcon(pystray.Icon):
        def __init__(self, *args, notification_click=None, tray_activate=None, **kwargs):
            self._notification_click = notification_click
            self._tray_activate = tray_activate
            self._last_tray_open_at = 0.0
            self._remote_probe_lock = threading.Lock()
            self._remote_probe_id = 0
            self._remote_probe_cancel = None
            self._remote_menu_open = False
            super().__init__(*args, **kwargs)

        def _handle_callback(self, callback) -> None:
            if callback:
                callback(self)
                self.update_menu()

        def _handle_tray_activate(self) -> None:
            opened_at = time.monotonic()
            if opened_at - self._last_tray_open_at <= tray_open_cooldown_seconds:
                return
            self._last_tray_open_at = opened_at
            self._handle_callback(self._tray_activate)

        def _update_open_remote_item(self, probe_id: int, status: dict) -> None:
            with self._remote_probe_lock:
                if probe_id != self._remote_probe_id or not self._remote_menu_open:
                    return
                menu_handle = self._menu_handle

            command_id = remote_menu_item_id["value"]
            if not menu_handle or not command_id:
                return

            checked = remote_control_checked_value(status)
            state = (
                pystray_win32.MFS_CHECKED
                if checked
                else pystray_win32.MFS_UNCHECKED
            )
            if not remote_control_enabled_value(status):
                state |= pystray_win32.MFS_DISABLED

            menu_item = pystray_win32.MENUITEMINFO(
                cbSize=ctypes.sizeof(pystray_win32.MENUITEMINFO),
                fMask=pystray_win32.MIIM_STRING | pystray_win32.MIIM_STATE,
                dwTypeData=remote_control_text_value(status),
                fState=state,
            )
            try:
                set_menu_item_info(menu_handle[0], command_id, False, ctypes.byref(menu_item))
                draw_menu_bar(self._menu_hwnd)
            except Exception:
                pass

        def _begin_remote_probe(self) -> None:
            with self._remote_probe_lock:
                previous_cancel = self._remote_probe_cancel
                self._remote_probe_id += 1
                probe_id = self._remote_probe_id
                cancel_event = threading.Event()
                self._remote_probe_cancel = cancel_event
                self._remote_menu_open = True

            if previous_cancel:
                previous_cancel.set()

            mark_codex_remote_control_loading()
            self.update_menu()

            def _probe() -> None:
                try:
                    status = codex_remote_control_status(cancel_event)
                except RemoteControlProbeCancelled:
                    return

                with self._remote_probe_lock:
                    if (
                        probe_id != self._remote_probe_id
                        or not self._remote_menu_open
                        or cancel_event.is_set()
                    ):
                        return

                status = store_codex_remote_control_status(status)
                self._update_open_remote_item(probe_id, status)

            run_daemon_thread(_probe)

        def _end_remote_probe(self) -> None:
            with self._remote_probe_lock:
                self._remote_menu_open = False
                self._remote_probe_id += 1
                cancel_event = self._remote_probe_cancel
                self._remote_probe_cancel = None

            if cancel_event:
                cancel_event.set()

        def _show_context_menu(self) -> None:
            self._begin_remote_probe()
            if not self._menu_handle:
                self._end_remote_probe()
                return

            pystray_win32.SetForegroundWindow(self._hwnd)
            point = wintypes.POINT()
            pystray_win32.GetCursorPos(ctypes.byref(point))
            hmenu, descriptors = self._menu_handle
            try:
                index = pystray_win32.TrackPopupMenuEx(
                    hmenu,
                    pystray_win32.TPM_RIGHTALIGN
                    | pystray_win32.TPM_BOTTOMALIGN
                    | pystray_win32.TPM_RETURNCMD,
                    point.x,
                    point.y,
                    self._menu_hwnd,
                    None,
                )
            finally:
                self._end_remote_probe()

            if index > 0:
                descriptors[index - 1](self)

        def _on_notify(self, wparam, lparam):
            if lparam == notification_click_event:
                self._handle_callback(self._notification_click)
                return 0
            if lparam in tray_activate_events:
                self._handle_tray_activate()
                return 0
            if lparam == tray_context_menu_event:
                try:
                    latest_quota_for_display(refresh=True)
                except Exception:
                    pass
                self._show_context_menu()
                return 0
            return super()._on_notify(wparam, lparam)

    def open_from_tray() -> None:
        acknowledge_done_alerts()
        open_dashboard_url()

    def on_open_dashboard(icon, item=None) -> None:
        open_from_tray()

    def on_notification_click(icon) -> None:
        if open_current_attention_in_vscode():
            return
        open_from_tray()

    def on_bell(icon, item) -> None:
        trigger_test_alert()

    def on_toggle_remote_control(icon, item) -> None:
        status = cached_codex_remote_control_status()
        if status.get("starting") or status.get("stopping"):
            return

        if status["running"]:
            if not set_codex_remote_control_stopping():
                return
            icon.update_menu()

            def _stop() -> None:
                error = ""
                try:
                    stop_codex_remote_control()
                    status = codex_remote_control_status()
                except RuntimeError as exc:
                    error = str(exc)
                    status = {
                        "available": bool(codex_cli_command()),
                        "running": True,
                        "pids": cached_codex_remote_control_status()["pids"],
                        "error": error,
                    }
                store_codex_remote_control_status(status, finish_stopping=bool(error))
                if error:
                    try:
                        icon.notify(safe_short(error, 180), CODEX_REMOTE_MENU_LABEL)
                    except Exception:
                        pass

            run_daemon_thread(_stop)
            return

        def _notify_start_error(error: str) -> None:
            try:
                icon.notify(safe_short(error, 180), CODEX_REMOTE_MENU_LABEL)
            except Exception:
                pass

        if not start_codex_remote_control_async(
            check_running=False,
            on_error=_notify_start_error,
        ):
            return
        icon.update_menu()

    def remote_control_text_value(status: dict) -> str:
        if status.get("starting", False):
            return f"{CODEX_REMOTE_MENU_LABEL} (Starting...)"
        if status.get("stopping", False):
            return f"{CODEX_REMOTE_MENU_LABEL} (Stopping...)"
        if status.get("loading", False):
            return f"{CODEX_REMOTE_MENU_LABEL} (Checking...)"
        if status.get("error"):
            return f"{CODEX_REMOTE_MENU_LABEL} (Check failed)"
        if not status.get("available", False):
            return f"{CODEX_REMOTE_MENU_LABEL} (CLI not found)"
        return CODEX_REMOTE_MENU_LABEL

    def remote_control_checked_value(status: dict) -> bool | None:
        if (
            status.get("starting", False)
            or status.get("stopping", False)
            or status.get("loading", False)
            or status.get("error")
            or not status.get("available", False)
        ):
            return None
        return bool(status.get("running"))

    def remote_control_enabled_value(status: dict) -> bool:
        return (
            not status.get("starting", False)
            and not status.get("stopping", False)
            and not status.get("loading", False)
            and not status.get("error")
            and status.get("available", False)
        )

    def remote_control_text(item) -> str:
        return remote_control_text_value(cached_codex_remote_control_status())

    def remote_control_checked(item) -> bool | None:
        return remote_control_checked_value(cached_codex_remote_control_status())

    def remote_control_enabled(item) -> bool:
        return remote_control_enabled_value(cached_codex_remote_control_status())

    def on_toggle_startup(icon, item) -> None:
        try:
            if has_startup_task(startup_arguments):
                uninstall_startup()
            else:
                install_startup(startup_arguments)
        finally:
            icon.update_menu()

    def startup_checked(item) -> bool:
        return has_startup_task(startup_arguments)

    def on_exit(icon, item) -> None:
        exit_requested.set()
        icon.visible = False
        icon.stop()

    def build_tray_menu():
        quota_items = tuple(
            pystray.MenuItem(line, None, enabled=False)
            for line in tray_quota_display_lines(latest_quota_for_display(refresh=False))
        )
        remote_item = pystray.MenuItem(
            remote_control_text,
            on_toggle_remote_control,
            checked=remote_control_checked,
            enabled=remote_control_enabled,
        )
        items = (
            pystray.MenuItem("Open Dashboard", on_open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            *quota_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Test Alert", on_bell),
            remote_item,
            pystray.MenuItem("Start at Logon", on_toggle_startup, checked=startup_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        )
        remote_menu_item_id["value"] = items.index(remote_item) + 1
        return items

    icon = DashboardTrayIcon(
        APP_NAME,
        tray_image,
        APP_NAME,
        notification_click=on_notification_click,
        tray_activate=on_open_dashboard,
        menu=pystray.Menu(build_tray_menu),
    )
    if start_remote_control:
        def _notify_auto_start_error(error: str) -> None:
            try:
                icon.notify(safe_short(error, 180), CODEX_REMOTE_MENU_LABEL)
            except Exception:
                pass

        start_codex_remote_control_async(on_error=_notify_auto_start_error)

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
    command_arguments = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Codex Dashboard")
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
        help="print the Windows startup task status",
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
    parser.add_argument(
        "--start-remote-control",
        action="store_true",
        help="start codex remote-control in the background when the dashboard starts",
    )
    args = parser.parse_args()

    if args.install_startup:
        install_startup(command_arguments)
        print(f"Installed startup task: {STARTUP_TASK_NAME}")
        return 0

    if args.uninstall_startup:
        uninstall_startup()
        print(f"Removed startup task: {STARTUP_TASK_NAME}")
        return 0

    if args.startup_status:
        print(startup_status(command_arguments))
        return 0

    if args.tray:
        return run_server_with_tray(
            args.open_browser,
            args.start_remote_control,
            startup_arguments=command_arguments,
        )

    return run_server_blocking(args.open_browser, args.start_remote_control)


if __name__ == "__main__":
    raise SystemExit(main())
