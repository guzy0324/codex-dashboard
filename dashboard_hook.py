# ~/.codex/dashboard_hook.py
import json
import os
import subprocess
import sys
import time

def run_extra_commands(endpoint, machine, payload):
    path = os.environ.get(
        "CODEX_DASHBOARD_HOOK_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_hook.json"),
    )
    try:
        with open(path, encoding="utf-8-sig") as stream:
            config = json.load(stream)
        commands = config.get("commands", {})
        if not isinstance(commands, dict):
            raise ValueError("commands must be an object")
    except FileNotFoundError:
        return
    except (OSError, ValueError, AttributeError) as exc:
        print(f"dashboard_hook: invalid config: {exc}", file=sys.stderr)
        return

    env = dict(os.environ, CODEX_HOOK_ENDPOINT=endpoint, CODEX_HOOK_MACHINE=machine)
    for event in ("*", endpoint):
        entries = commands.get(event, [])
        if not isinstance(entries, list):
            print(f"dashboard_hook: commands.{event} must be a list", file=sys.stderr)
            continue
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("command entry must be an object")
                argv = entry.get("command")
                timeout = entry.get("timeout", 2)
                if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                    raise ValueError("command must be a nonempty list of strings")
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
                    raise ValueError("timeout must be between 0 (exclusive) and 3600 seconds")
                subprocess.run(
                    argv, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout, env=env, check=True,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"dashboard_hook: extra command failed: {exc}", file=sys.stderr)


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

        p = item.get("payload") if item.get("type") == "event_msg" else None
        if isinstance(p, dict) and isinstance(p.get("rate_limits"), dict):
            latest = p["rate_limits"]

    return latest


rate_limits = None
for _ in range(6 if endpoint == "stop" else 1):
    rate_limits = read_latest_rate_limits(transcript)
    if rate_limits:
        break
    time.sleep(0.15)

if rate_limits:
    payload["rate_limits"] = rate_limits

try:
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
except OSError as exc:
    print(f"dashboard_hook: {exc}", file=sys.stderr)

run_extra_commands(endpoint, machine, payload)

if endpoint == "stop":
    print('{"continue":true}')
