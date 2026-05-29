# ~/.codex/dashboard_hook.py
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
