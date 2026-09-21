#!/usr/bin/env python3
"""Flask service that keeps Codex quota active across five-hour resets."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from codex_dashboard import CodexQuotaPoller, codex_cli_command, quota_log

TOKEN_ENV = "CODEX_QUOTA_KEEPALIVE_TOKEN"
HOST_ENV = "CODEX_QUOTA_KEEPALIVE_HOST"
PORT_ENV = "CODEX_QUOTA_KEEPALIVE_PORT"
INTERVAL_ENV = "CODEX_QUOTA_KEEPALIVE_INTERVAL_SECONDS"
STATE_FILE_ENV = "CODEX_QUOTA_KEEPALIVE_STATE_FILE"
KEEPALIVE_TIMEOUT_ENV = "CODEX_QUOTA_KEEPALIVE_TIMEOUT_SECONDS"
KEEPALIVE_REQUEST_TIMEOUT_ENV = "CODEX_QUOTA_KEEPALIVE_REQUEST_TIMEOUT_SECONDS"
RESET_WINDOW_MINUTES = 300
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18766
DEFAULT_INTERVAL_SECONDS = 600.0
DEFAULT_KEEPALIVE_TIMEOUT_SECONDS = 120.0
DEFAULT_KEEPALIVE_REQUEST_TIMEOUT_SECONDS = 45.0
KEEPALIVE_RECHECK_TIMEOUT_SECONDS = 45.0
CODEX_COMMAND_ENV = "CODEX_DASHBOARD_CODEX_CMD"
HTTP_PROXY_ENV = "HTTP_PROXY"
HTTPS_PROXY_ENV = "HTTPS_PROXY"
ALL_PROXY_ENV = "ALL_PROXY"
NO_PROXY_ENV = "NO_PROXY"
SYSTEMD_SERVICE_NAME = "codex-quota-keepalive.service"
SYSTEMD_UNIT_FILE = f"/etc/systemd/system/{SYSTEMD_SERVICE_NAME}"
SYSTEMD_ENV_FILE = "/etc/codex-quota-keepalive.env"
SYSTEMD_MARKER = "# Managed by codex_quota_keepalive_service.py"

app = Flask(__name__)


def positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def service_port() -> int:
    try:
        value = int(os.environ.get(PORT_ENV, DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if 0 < value < 65536 else DEFAULT_PORT


def parse_service_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_proxy_url(value: str) -> str:
    proxy = value.strip()
    try:
        parsed = urlsplit(proxy)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid proxy URL: {error}") from error
    if (
        not proxy
        or any(character.isspace() or ord(character) < 32 for character in proxy)
        or any(character in proxy for character in "\\\"")
        or parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}
        or not parsed.netloc
    ):
        raise argparse.ArgumentTypeError(
            "proxy must be an http://, https://, socks5://, or socks5h:// URL"
        )
    return proxy


def configured_token() -> str:
    return (app.config.get("service_token") or os.environ.get(TOKEN_ENV) or "").strip()


class CodexQuotaKeepaliveService:
    def __init__(self) -> None:
        self._started_at = time.time()
        default_state_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "state",
            "quota_keepalive_status.json",
        )
        self._state_file = os.path.abspath(
            os.path.expanduser(
                os.environ.get(STATE_FILE_ENV, default_state_file).strip()
                or default_state_file
            )
        )
        self._state_lock = threading.Lock()
        saved_state = self._load_state()
        self._saved_poller_status = self._dict_or_empty(
            saved_state.get("poller_status")
        )
        self._saved_keepalive_check = self._dict_or_empty(
            saved_state.get("keepalive_check")
        ) or {
            "state": "waiting",
            "checked_at": None,
            "due": None,
            "reset_at": None,
            "error": "",
        }
        self._saved_last_keepalive = self._dict_or_empty(
            saved_state.get("last_keepalive")
        ) or {
            "state": "not_run",
            "success": None,
            "started_at": None,
            "finished_at": None,
            "reset_at_before": None,
            "reset_at_after": None,
            "return_code": None,
            "error": "",
        }
        if self._saved_last_keepalive.get("state") == "running":
            self._saved_last_keepalive.update(
                {
                    "state": "failed",
                    "success": False,
                    "finished_at": time.time(),
                    "error": "Service restarted while the keepalive command was running",
                }
            )

        self._command = codex_cli_command()
        self._interval_seconds = positive_float_env(
            INTERVAL_ENV, DEFAULT_INTERVAL_SECONDS
        )
        self._keepalive_timeout_seconds = positive_float_env(
            KEEPALIVE_TIMEOUT_ENV, DEFAULT_KEEPALIVE_TIMEOUT_SECONDS
        )
        self._request_timeout_seconds = positive_float_env(
            KEEPALIVE_REQUEST_TIMEOUT_ENV,
            DEFAULT_KEEPALIVE_REQUEST_TIMEOUT_SECONDS,
        )
        self._stop_event = threading.Event()
        self._poller_update_event = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_process_lock = threading.Lock()
        self._keepalive_process: subprocess.Popen | None = None
        self._last_processed_poll_attempt = 0.0
        self._poller = (
            CodexQuotaPoller(
                self._command,
                interval_seconds=self._interval_seconds,
                request_timeout_seconds=self._request_timeout_seconds,
                status_callback=self._persist_poller_status,
            )
            if self._command
            else None
        )
        if self._saved_last_keepalive.get("error") == (
            "Service restarted while the keepalive command was running"
        ):
            try:
                with self._state_lock:
                    self._persist_state_locked()
            except OSError as exc:
                quota_log(
                    "codex_quota_keepalive_state_persist_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

    def start(self) -> None:
        if not self._poller:
            return
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return

        self._stop_event.clear()
        self._keepalive_thread = threading.Thread(
            target=self._run_keepalive_controller,
            name="codex-quota-keepalive-controller",
            daemon=True,
        )
        self._keepalive_thread.start()
        self._poller.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._poller_update_event.set()
        self._terminate_keepalive_process()
        if self._poller:
            self._poller.stop()
        if (
            self._keepalive_thread
            and self._keepalive_thread is not threading.current_thread()
        ):
            self._keepalive_thread.join(timeout=3)

    def snapshot(self) -> dict:
        poller = self._poller
        live_status = poller.snapshot() if poller else {}
        with self._state_lock:
            saved_poller_status = json.loads(
                json.dumps(self._saved_poller_status, ensure_ascii=False)
            )
            keepalive_check = json.loads(
                json.dumps(self._saved_keepalive_check, ensure_ascii=False)
            )
            last_keepalive = json.loads(
                json.dumps(self._saved_last_keepalive, ensure_ascii=False)
            )
        if live_status.get("last_result"):
            poller_status = live_status
        else:
            poller_status = saved_poller_status
            poller_status["running"] = bool(live_status.get("running"))
        last_result = poller_status.get("last_result") or {}
        last_execution = self._last_execution(last_result, poller_status)
        poller_running = bool(poller_status.get("running"))

        return {
            "service_running": True,
            "service_started_at": self._started_at,
            "rate_limit_poller_running": poller_running,
            "keepalive_worker_running": bool(
                self._keepalive_thread and self._keepalive_thread.is_alive()
            ),
            "check_interval_seconds": self._interval_seconds,
            "keepalive_timeout_seconds": self._keepalive_timeout_seconds,
            "request_timeout_seconds": self._request_timeout_seconds,
            "last_execution": last_execution,
            "keepalive_check": keepalive_check,
            "last_keepalive": last_keepalive,
        }

    @staticmethod
    def _dict_or_empty(value) -> dict:
        return value if isinstance(value, dict) else {}

    def _load_state(self) -> dict:
        try:
            with open(self._state_file, encoding="utf-8") as state_file:
                saved = json.load(state_file)
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _persist_poller_status(self, poller_status: dict) -> None:
        saved = {
            "last_attempt_at": poller_status.get("last_attempt_at"),
            "last_success_at": poller_status.get("last_success_at"),
            "quota": poller_status.get("quota") or {},
            "last_result": poller_status.get("last_result") or {},
        }
        try:
            with self._state_lock:
                self._saved_poller_status = saved
                self._persist_state_locked()
        finally:
            self._poller_update_event.set()

    def _persist_state_locked(self) -> None:
        state_dir = os.path.dirname(self._state_file)
        os.makedirs(state_dir, exist_ok=True)
        temporary_file = f"{self._state_file}.tmp"
        with open(temporary_file, "w", encoding="utf-8") as state_file:
            json.dump(
                {
                    "poller_status": self._saved_poller_status,
                    "keepalive_check": self._saved_keepalive_check,
                    "last_keepalive": self._saved_last_keepalive,
                },
                state_file,
                ensure_ascii=False,
            )
            state_file.write("\n")
        os.replace(temporary_file, self._state_file)

    def _update_keepalive_state(
        self,
        keepalive_check: dict | None = None,
        last_keepalive: dict | None = None,
    ) -> None:
        with self._state_lock:
            if keepalive_check is not None:
                self._saved_keepalive_check = dict(keepalive_check)
            if last_keepalive is not None:
                self._saved_last_keepalive = dict(last_keepalive)
            self._persist_state_locked()

    def _run_keepalive_controller(self) -> None:
        quota_log("codex_quota_keepalive_controller_started")
        while not self._stop_event.is_set():
            self._poller_update_event.wait(0.5)
            if self._stop_event.is_set():
                break
            if not self._poller_update_event.is_set():
                continue
            self._poller_update_event.clear()

            poller = self._poller
            if not poller:
                break
            poller_status = poller.snapshot()
            last_result = poller_status.get("last_result") or {}
            try:
                attempted_at = float(last_result.get("attempted_at") or 0)
            except (TypeError, ValueError):
                attempted_at = 0
            if not attempted_at or attempted_at <= self._last_processed_poll_attempt:
                continue
            self._last_processed_poll_attempt = attempted_at

            try:
                self._process_rate_limit_result(poller_status, last_result)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                quota_log("codex_quota_keepalive_controller_error", error=error)
                try:
                    self._update_keepalive_state(
                        keepalive_check={
                            "state": "error",
                            "checked_at": last_result.get("completed_at"),
                            "due": None,
                            "reset_at": None,
                            "error": error,
                        }
                    )
                except OSError:
                    pass
        quota_log("codex_quota_keepalive_controller_stopped")

    @staticmethod
    def _five_hour_windows(quota: dict) -> list[dict]:
        windows = []
        for window in quota.get("windows") or []:
            if not isinstance(window, dict):
                continue
            try:
                window_minutes = int(window.get("window_minutes") or 0)
                reset_at = float(window.get("resets_at_raw") or 0)
            except (TypeError, ValueError):
                continue
            if window_minutes == RESET_WINDOW_MINUTES and reset_at > 0:
                windows.append(
                    {
                        "key": window.get("key") or "",
                        "label": window.get("label") or window.get("key") or "5h",
                        "reset_at": reset_at,
                    }
                )
        return windows

    def _process_rate_limit_result(
        self, poller_status: dict, last_result: dict
    ) -> None:
        checked_at = last_result.get("completed_at")
        if not last_result.get("success"):
            self._update_keepalive_state(
                keepalive_check={
                    "state": "read_error",
                    "checked_at": checked_at,
                    "due": None,
                    "reset_at": None,
                    "error": last_result.get("error") or "Rate-limit read failed",
                }
            )
            return

        windows = self._five_hour_windows(poller_status.get("quota") or {})
        if not windows:
            self._update_keepalive_state(
                keepalive_check={
                    "state": "unavailable",
                    "checked_at": checked_at,
                    "due": None,
                    "reset_at": None,
                    "error": "Codex did not return a 5-hour reset timestamp",
                }
            )
            return

        now = time.time()
        due_windows = [window for window in windows if window["reset_at"] <= now]
        next_reset_at = min(window["reset_at"] for window in windows)
        check_state = {
            "state": "due" if due_windows else "waiting_reset",
            "checked_at": checked_at,
            "due": bool(due_windows),
            "reset_at": min(
                window["reset_at"] for window in due_windows
            ) if due_windows else next_reset_at,
            "error": "",
        }
        self._update_keepalive_state(keepalive_check=check_state)
        if due_windows:
            due_reset_at = min(window["reset_at"] for window in due_windows)
            retry_at = self._recent_keepalive_retry_at(due_reset_at, now)
            if retry_at:
                check_state.update(
                    {
                        "state": "retry_wait",
                        "next_check_at": retry_at,
                    }
                )
                self._update_keepalive_state(keepalive_check=check_state)
                return
            self._run_keepalive_cycle(due_reset_at)

    def _recent_keepalive_retry_at(
        self, reset_at: float, now: float
    ) -> float | None:
        with self._state_lock:
            previous_attempt = dict(self._saved_last_keepalive)
        try:
            previous_reset_at = float(
                previous_attempt.get("reset_at_before") or 0
            )
            previous_started_at = float(previous_attempt.get("started_at") or 0)
        except (TypeError, ValueError):
            return None
        next_attempt_at = previous_started_at + self._interval_seconds
        if (
            previous_started_at
            and abs(previous_reset_at - reset_at) < 1
            and now < next_attempt_at
        ):
            return next_attempt_at
        return None

    def _run_keepalive_cycle(self, reset_at_before: float) -> None:
        started_at = time.time()
        attempt = {
            "state": "running",
            "success": None,
            "command_success": None,
            "refresh_success": None,
            "started_at": started_at,
            "finished_at": None,
            "reset_at_before": reset_at_before,
            "reset_at_after": None,
            "return_code": None,
            "error": "",
        }
        self._update_keepalive_state(last_keepalive=attempt)
        quota_log(
            "codex_quota_keepalive_started",
            reset_at_before=reset_at_before,
        )

        return_code, command_error = self._execute_keepalive_command()
        command_finished_at = time.time()
        if self._stop_event.is_set():
            return

        refreshed_status = None
        poller = self._poller
        if poller:
            refreshed_status = poller.refresh_now(
                after_timestamp=command_finished_at,
                timeout_seconds=KEEPALIVE_RECHECK_TIMEOUT_SECONDS,
            )

        refreshed_result = (refreshed_status or {}).get("last_result") or {}
        try:
            refreshed_attempted_at = float(
                refreshed_result.get("attempted_at") or 0
            )
        except (TypeError, ValueError):
            refreshed_attempted_at = 0
        if refreshed_attempted_at > self._last_processed_poll_attempt:
            # This read confirms the command's effect. Do not treat the same
            # due reset as a second scheduled keepalive cycle.
            self._last_processed_poll_attempt = refreshed_attempted_at
        refresh_success = bool(
            refreshed_result.get("success")
            and refreshed_attempted_at >= command_finished_at
        )
        reset_at_after = None
        if refresh_success:
            refreshed_windows = self._five_hour_windows(
                (refreshed_status or {}).get("quota") or {}
            )
            if refreshed_windows:
                reset_at_after = min(
                    window["reset_at"] for window in refreshed_windows
                )
            else:
                refresh_success = False

        command_success = return_code == 0
        success = command_success and refresh_success
        errors = []
        if command_error:
            errors.append(command_error)
        elif not command_success:
            errors.append(f"Codex exec exited with code {return_code}")
        if not refresh_success:
            if refreshed_status is None:
                refresh_error = "Timed out waiting for the post-keepalive rate-limit read"
            elif refreshed_result.get("success") and reset_at_after is None:
                refresh_error = "Post-keepalive read had no 5-hour reset timestamp"
            else:
                refresh_error = refreshed_result.get("error") or "Rate-limit re-read failed"
            errors.append(refresh_error)

        attempt.update(
            {
                "state": "succeeded" if success else "failed",
                "success": success,
                "command_success": command_success,
                "refresh_success": refresh_success,
                "finished_at": time.time(),
                "reset_at_after": reset_at_after,
                "return_code": return_code,
                "error": "; ".join(errors),
            }
        )
        if refresh_success and reset_at_after is not None:
            reset_is_due = reset_at_after <= time.time()
            updated_check = {
                "state": "due" if reset_is_due else "waiting_reset",
                "checked_at": refreshed_result.get("completed_at"),
                "due": reset_is_due,
                "reset_at": reset_at_after,
                "error": "",
            }
        else:
            updated_check = {
                "state": "read_error",
                "checked_at": refreshed_result.get("completed_at"),
                "due": None,
                "reset_at": None,
                "error": errors[-1] if errors else "Rate-limit re-read failed",
            }
        self._update_keepalive_state(
            keepalive_check=updated_check,
            last_keepalive=attempt,
        )
        quota_log(
            "codex_quota_keepalive_finished",
            success=success,
            command_success=command_success,
            refresh_success=refresh_success,
            return_code=return_code,
            reset_at_after=reset_at_after,
            error=attempt["error"],
        )

    def _execute_keepalive_command(self) -> tuple[int | None, str]:
        if not self._command:
            return None, "Codex CLI was not found"

        command_arguments = [
            self._command,
            "exec",
            "--ephemeral",
            "Reply only OK",
        ]
        try:
            process = subprocess.Popen(
                command_arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
            with self._keepalive_process_lock:
                self._keepalive_process = process
            if self._stop_event.is_set():
                self._terminate_process(process)
            try:
                return_code = process.wait(timeout=self._keepalive_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                return_code = process.poll()
                return return_code, (
                    "Codex exec timed out after "
                    f"{self._keepalive_timeout_seconds:g} seconds"
                )
            if return_code != 0:
                return return_code, f"Codex exec exited with code {return_code}"
            return return_code, ""
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            with self._keepalive_process_lock:
                if self._keepalive_process is not None:
                    self._keepalive_process = None

    def _terminate_keepalive_process(self) -> None:
        with self._keepalive_process_lock:
            process = self._keepalive_process
        self._terminate_process(process)
        with self._keepalive_process_lock:
            if self._keepalive_process is process:
                self._keepalive_process = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen | None) -> None:
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _last_execution(self, last_result: dict, poller_status: dict) -> dict:
        if not self._poller:
            return {
                "success": False,
                "reset_state": "error",
                "checked_at": None,
                "reset_at": None,
                "reset_windows": [],
                "error": "Codex CLI was not found",
            }

        if not last_result:
            return {
                "success": None,
                "reset_state": "pending",
                "checked_at": None,
                "reset_at": None,
                "reset_windows": [],
                "error": "",
            }

        checked_at = last_result.get("completed_at")
        if not last_result.get("success"):
            return {
                "success": False,
                "reset_state": "error",
                "checked_at": checked_at,
                "reset_at": None,
                "reset_windows": [],
                "error": last_result.get("error") or "Rate-limit check failed",
            }

        quota = poller_status.get("quota") or {}
        windows = [
            window
            for window in quota.get("windows") or []
            if int(window.get("window_minutes") or 0) == RESET_WINDOW_MINUTES
        ]
        if not windows:
            return {
                "success": False,
                "reset_state": "error",
                "checked_at": checked_at,
                "reset_at": None,
                "reset_windows": [],
                "error": "Codex did not return a 5-hour (300-minute) reset window",
            }

        now = time.time()
        reset_windows = []
        for window in windows:
            resets_at = window.get("resets_at_raw") or 0
            if not resets_at:
                continue
            reset_windows.append(
                {
                    "key": window.get("key") or "",
                    "label": window.get("label") or window.get("key") or "5h",
                    "reset_at": resets_at,
                    "reset_state": "passed" if resets_at <= now else "waiting",
                }
            )

        if not reset_windows:
            return {
                "success": False,
                "reset_state": "error",
                "checked_at": checked_at,
                "reset_at": None,
                "reset_windows": [],
                "error": "Codex returned a 5-hour window without a reset timestamp",
            }

        states = {window["reset_state"] for window in reset_windows}
        reset_state = (
            next(iter(states)) if len(states) == 1 else "mixed"
        )
        return {
            "success": True,
            "reset_state": reset_state,
            "checked_at": checked_at,
            "reset_at": reset_windows[0]["reset_at"],
            "reset_windows": reset_windows,
            "error": "",
        }


quota_keepalive_service = CodexQuotaKeepaliveService()


def authorized() -> bool:
    if not app.config.get("auth_required", True):
        return True
    token = configured_token()
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {token}" if token else ""
    return bool(expected) and hmac.compare_digest(authorization, expected)


@app.get("/api/status")
def status():
    if app.config.get("auth_required", True) and not configured_token():
        return jsonify({"error": f"{TOKEN_ENV} is not configured"}), 503
    if app.config.get("auth_required", True) and not authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(quota_keepalive_service.snapshot())


def require_systemd_admin() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("systemd install and uninstall are supported on Linux only")
    if os.geteuid() != 0:
        raise RuntimeError("run this command with sudo or as root")
    if shutil.which("systemctl") is None:
        raise RuntimeError("systemctl was not found; this Linux host may not use systemd")


def resolve_systemd_user(requested_user: str | None) -> str:
    import pwd

    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if requested_user:
        username = requested_user.strip()
    elif sudo_user and sudo_user != "root":
        username = sudo_user
    elif os.geteuid() == 0:
        raise RuntimeError(
            "specify --service-user when running directly as root; this should be "
            "the Linux user logged into the target Codex account"
        )
    else:
        username = pwd.getpwuid(os.getuid()).pw_name

    if not username or any(character.isspace() for character in username):
        raise RuntimeError("--service-user must be a valid Linux username")
    try:
        pwd.getpwnam(username)
    except KeyError as error:
        raise RuntimeError(f"Linux user {username!r} does not exist") from error
    return username


def systemd_quote(value: str) -> str:
    escaped = (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )
    return '"' + escaped + '"'


def systemd_path(value: str) -> str:
    """Escape a path for a systemd path setting without wrapping it in quotes.

    Path settings such as ``WorkingDirectory=`` do not strip surrounding
    quotes on all supported systemd versions.  Reuse the same escaping as
    ``systemd_quote`` while leaving the value unquoted so systemd sees the
    required absolute path.
    """

    return systemd_quote(value)[1:-1]


def systemd_environment_value(value: str) -> str:
    """Quote a value for a systemd EnvironmentFile assignment when needed."""

    if not any(character.isspace() or character in '\\"' for character in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def run_as_systemd_user(
    username: str, *arguments: str
) -> subprocess.CompletedProcess | None:
    """Run a read-only lookup as the account that will own the service."""

    runuser = shutil.which("runuser")
    if not runuser:
        return None
    try:
        return subprocess.run(
            [runuser, "-u", username, "--", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_systemd_codex_command(username: str, requested: str | None) -> str:
    """Resolve and validate the Codex executable before installing systemd."""

    import pwd

    account = pwd.getpwnam(username)
    configured = (requested or os.environ.get(CODEX_COMMAND_ENV) or "").strip()
    candidate = ""
    if configured:
        if len(configured) >= 2 and configured[0] == configured[-1]:
            if configured[0] in {"'", '"'}:
                configured = configured[1:-1].strip()
        if configured == "~" or configured.startswith("~/"):
            configured = os.path.join(
                account.pw_dir, configured[2:] if configured != "~" else ""
            )
        configured = os.path.expandvars(configured)
        if not os.path.isabs(configured):
            raise RuntimeError(
                "--codex-cmd and CODEX_DASHBOARD_CODEX_CMD must be an absolute path"
            )
        candidate = os.path.abspath(configured)
    else:
        lookup = run_as_systemd_user(username, "sh", "-lc", "command -v codex")
        if lookup and lookup.returncode == 0:
            candidate = next(
                (
                    line.strip()
                    for line in lookup.stdout.splitlines()
                    if line.strip().startswith("/")
                ),
                "",
            )
        if not candidate:
            for fallback in (
                os.path.join(account.pw_dir, ".local", "bin", "codex"),
                os.path.join(account.pw_dir, "bin", "codex"),
                "/usr/local/bin/codex",
                "/usr/bin/codex",
            ):
                if os.path.isfile(fallback):
                    candidate = fallback
                    break

    if not candidate or not os.path.isfile(candidate):
        hint = (
            f"; pass --codex-cmd /absolute/path/to/codex (for example "
            f"{account.pw_dir}/.local/bin/codex)"
        )
        raise RuntimeError(
            f"Codex CLI was not found for Linux user {username!r}{hint}"
        )

    check = run_as_systemd_user(username, "test", "-x", candidate)
    if check is not None and check.returncode != 0 and os.geteuid() == 0:
        raise RuntimeError(
            f"Codex CLI is not executable by Linux user {username!r}: {candidate}"
        )
    if (check is None or check.returncode != 0) and not os.access(
        candidate, os.X_OK
    ):
        raise RuntimeError(f"Codex CLI is not executable: {candidate}")
    return os.path.abspath(candidate)


def write_new_systemd_file(path: str, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def systemd_file_is_managed(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as managed_file:
            return managed_file.readline().strip() == SYSTEMD_MARKER
    except OSError:
        return False


def run_systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["systemctl", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"systemctl {' '.join(arguments)} failed"
            + (f": {detail}" if detail else "")
        )
    return result


def install_systemd(args: argparse.Namespace) -> int:
    require_systemd_admin()
    username = resolve_systemd_user(args.service_user)
    import pwd

    service_home = pwd.getpwnam(username).pw_dir
    existing = [
        path
        for path in (SYSTEMD_UNIT_FILE, SYSTEMD_ENV_FILE)
        if os.path.exists(path)
    ]
    if existing:
        raise RuntimeError(
            "refusing to overwrite existing installation files: "
            + ", ".join(existing)
            + "; back them up or uninstall them first"
        )
    codex_command = resolve_systemd_codex_command(
        username, getattr(args, "codex_cmd", None)
    )
    if args.token_file:
        raise RuntimeError(
            "--token-file cannot be used with --install-systemd; the installer "
            "stores the token in its mode-600 environment file"
        )
    if args.no_auth and args.token is not None:
        raise RuntimeError("--no-auth cannot be combined with --token")

    host = (args.host or DEFAULT_HOST).strip() or DEFAULT_HOST
    if any(character.isspace() for character in host) or any(
        character in host for character in "\\$\""
    ):
        raise RuntimeError(
            "--host cannot contain whitespace, backslashes, dollar signs, or quotes"
        )

    token = ""
    if not args.no_auth:
        token = (args.token or os.environ.get(TOKEN_ENV) or "").strip()
        if not token:
            token = secrets.token_hex(32)
        if any(character.isspace() for character in token) or any(
            character in token for character in "\\$\""
        ):
            raise RuntimeError(
                "the token cannot contain whitespace, backslashes, dollar signs, or quotes"
            )

    proxy = (getattr(args, "proxy", None) or "").strip()
    if proxy:
        proxy = parse_proxy_url(proxy)
    no_proxy = (getattr(args, "no_proxy", None) or "").strip()
    if any(ord(character) < 32 for character in no_proxy) or any(
        character in no_proxy for character in "\\\""
    ):
        raise RuntimeError("--no-proxy cannot contain newlines, backslashes, or quotes")

    project_dir = os.path.dirname(os.path.realpath(__file__))
    python_path = os.path.abspath(sys.executable)
    script_path = os.path.realpath(__file__)
    exec_start = f"{systemd_quote(python_path)} {systemd_quote(script_path)}"
    if args.no_auth:
        exec_start += " --no-auth"

    service_path_entries = [
        os.path.dirname(codex_command),
        os.path.join(service_home, ".local", "bin"),
        os.path.join(service_home, "bin"),
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    service_path = os.pathsep.join(
        entry
        for index, entry in enumerate(service_path_entries)
        if entry and entry not in service_path_entries[:index]
    )

    unit_lines = [
        SYSTEMD_MARKER,
        "[Unit]",
        "Description=Codex Quota Keepalive",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={username}",
        f"WorkingDirectory={systemd_path(project_dir)}",
        f"EnvironmentFile={SYSTEMD_ENV_FILE}",
        f"Environment=HOME={systemd_path(service_home)}",
        f"Environment=CODEX_HOME={systemd_path(os.path.join(service_home, '.codex'))}",
        f"Environment=PATH={systemd_path(service_path)}",
        "Environment=CODEX_DASHBOARD_SQLITE_HOME=/var/lib/codex-quota-keepalive/sqlite",
        "Environment=CODEX_DASHBOARD_QUOTA_LOG=/var/log/codex-quota-keepalive/quota.log",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=5",
        "UMask=0077",
        "StateDirectory=codex-quota-keepalive",
        "LogsDirectory=codex-quota-keepalive",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    interval = positive_float_env(INTERVAL_ENV, DEFAULT_INTERVAL_SECONDS)
    keepalive_timeout = positive_float_env(
        KEEPALIVE_TIMEOUT_ENV, DEFAULT_KEEPALIVE_TIMEOUT_SECONDS
    )
    request_timeout = positive_float_env(
        KEEPALIVE_REQUEST_TIMEOUT_ENV,
        DEFAULT_KEEPALIVE_REQUEST_TIMEOUT_SECONDS,
    )
    environment_lines = [
        SYSTEMD_MARKER,
        f"{CODEX_COMMAND_ENV}={systemd_environment_value(codex_command)}",
        f"{HOST_ENV}={host}",
        f"{PORT_ENV}={args.port}",
        f"{INTERVAL_ENV}={interval:g}",
        f"{KEEPALIVE_TIMEOUT_ENV}={keepalive_timeout:g}",
        f"{KEEPALIVE_REQUEST_TIMEOUT_ENV}={request_timeout:g}",
        f"{STATE_FILE_ENV}=/var/lib/codex-quota-keepalive/status.json",
    ]
    if token:
        environment_lines.insert(1, f"{TOKEN_ENV}={token}")
    if no_proxy:
        for environment_name in (NO_PROXY_ENV, "no_proxy"):
            environment_lines.insert(
                1, f"{environment_name}={systemd_environment_value(no_proxy)}"
            )
    if proxy:
        for environment_name in (
            HTTP_PROXY_ENV,
            HTTPS_PROXY_ENV,
            ALL_PROXY_ENV,
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment_lines.insert(
                1, f"{environment_name}={systemd_environment_value(proxy)}"
            )

    env_created = False
    try:
        write_new_systemd_file(SYSTEMD_ENV_FILE, "\n".join(environment_lines) + "\n", 0o600)
        env_created = True
        write_new_systemd_file(SYSTEMD_UNIT_FILE, "\n".join(unit_lines), 0o644)
    except BaseException:
        if env_created:
            try:
                os.unlink(SYSTEMD_ENV_FILE)
            except OSError:
                pass
        raise

    try:
        run_systemctl("daemon-reload")
        run_systemctl("enable", "--now", SYSTEMD_SERVICE_NAME)
    except (OSError, RuntimeError):
        try:
            run_systemctl("disable", "--now", SYSTEMD_SERVICE_NAME, check=False)
        except OSError:
            pass
        for path in (SYSTEMD_UNIT_FILE, SYSTEMD_ENV_FILE):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            run_systemctl("daemon-reload", check=False)
        except OSError:
            pass
        raise
    print(f"Installed and started {SYSTEMD_SERVICE_NAME} for Linux user {username}.")
    print(f"Environment: {SYSTEMD_ENV_FILE}")
    print(f"Service URL: http://<server>:{args.port}")
    if token:
        print(f"Dashboard token (save this now): {token}")
    else:
        print("Authentication is disabled; restrict access to a trusted private network.")
    return 0


def uninstall_systemd() -> int:
    require_systemd_admin()
    existing = [
        path
        for path in (SYSTEMD_UNIT_FILE, SYSTEMD_ENV_FILE)
        if os.path.exists(path)
    ]
    unmanaged = [path for path in existing if not systemd_file_is_managed(path)]
    if unmanaged:
        raise RuntimeError(
            "refusing to remove files not created by this installer: "
            + ", ".join(unmanaged)
        )
    if not existing:
        print(f"{SYSTEMD_SERVICE_NAME} is not installed.")
        return 0

    if os.path.exists(SYSTEMD_UNIT_FILE):
        run_systemctl("disable", "--now", SYSTEMD_SERVICE_NAME)
        os.unlink(SYSTEMD_UNIT_FILE)
    if os.path.exists(SYSTEMD_ENV_FILE):
        os.unlink(SYSTEMD_ENV_FILE)
    run_systemctl("daemon-reload")
    run_systemctl("reset-failed", SYSTEMD_SERVICE_NAME, check=False)
    print(f"Uninstalled {SYSTEMD_SERVICE_NAME} and removed its environment file.")
    print("The application files and service state/log directories were left in place.")
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep Codex quota active across five-hour resets and expose status."
    )
    systemd_actions = parser.add_mutually_exclusive_group()
    systemd_actions.add_argument(
        "--install-systemd",
        action="store_true",
        help="install and start this service with systemd (Linux; requires root)",
    )
    systemd_actions.add_argument(
        "--uninstall-systemd",
        action="store_true",
        help="stop and remove the systemd service and its environment file",
    )
    parser.add_argument(
        "--service-user",
        help="Linux user whose Codex account the systemd service should use",
    )
    parser.add_argument(
        "--codex-cmd",
        "--codex-command",
        dest="codex_cmd",
        help=(
            "absolute path to the Codex CLI; checked before installation and "
            f"saved as {CODEX_COMMAND_ENV}"
        ),
    )
    parser.add_argument(
        "--proxy",
        type=parse_proxy_url,
        help=(
            "proxy URL to save as HTTP_PROXY, HTTPS_PROXY, and ALL_PROXY in the "
            "systemd environment file"
        ),
    )
    parser.add_argument(
        "--no-proxy",
        help="comma-separated hosts that should bypass the configured proxy",
    )
    parser.add_argument(
        "--token",
        help="Bearer token for the status API (defaults to CODEX_QUOTA_KEEPALIVE_TOKEN)",
    )
    parser.add_argument(
        "--token-file",
        help="Read the Bearer token from a file; useful when you do not want it in process arguments",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable Bearer-token authentication; restrict access to a trusted private network",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(HOST_ENV, DEFAULT_HOST),
        help=f"Bind address (default: {HOST_ENV} or {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=parse_service_port,
        default=service_port(),
        help=f"HTTP port (default: {PORT_ENV} or {DEFAULT_PORT})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.install_systemd:
        try:
            return install_systemd(args)
        except (OSError, RuntimeError) as error:
            print(f"systemd installation failed: {error}", file=sys.stderr)
            return 1
    if args.uninstall_systemd:
        try:
            return uninstall_systemd()
        except (OSError, RuntimeError) as error:
            print(f"systemd uninstallation failed: {error}", file=sys.stderr)
            return 1
    if args.service_user:
        parser.error("--service-user can only be used with --install-systemd")
    if args.codex_cmd:
        parser.error("--codex-cmd can only be used with --install-systemd")
    if args.proxy:
        parser.error("--proxy can only be used with --install-systemd")
    if args.no_proxy:
        parser.error("--no-proxy can only be used with --install-systemd")
    if args.no_auth and (args.token is not None or args.token_file):
        parser.error("--no-auth cannot be combined with --token or --token-file")
    if args.token is not None and args.token_file:
        parser.error("--token and --token-file cannot be used together")

    token = ""
    if not args.no_auth:
        if args.token_file:
            try:
                with open(os.path.expanduser(args.token_file), encoding="utf-8") as token_file:
                    token = token_file.read().strip()
            except OSError as error:
                print(f"Could not read token file: {error}", file=sys.stderr)
                return 2
        else:
            token = (
                args.token
                if args.token is not None
                else os.environ.get(TOKEN_ENV) or ""
            ).strip()

        if not token:
            print(
                f"Provide --token, --token-file, or set {TOKEN_ENV}; "
                "use --no-auth only on a trusted private network.",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            "WARNING: bearer-token authentication is disabled; "
            "restrict network access to trusted clients.",
            file=sys.stderr,
        )

    app.config["auth_required"] = not args.no_auth
    app.config["service_token"] = token

    try:
        from waitress import serve
    except ImportError:
        print("Install Waitress to run the service: pip install waitress", file=sys.stderr)
        return 2

    host = (args.host or DEFAULT_HOST).strip() or DEFAULT_HOST
    quota_keepalive_service.start()
    try:
        serve(app, host=host, port=args.port, threads=4)
    finally:
        quota_keepalive_service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
