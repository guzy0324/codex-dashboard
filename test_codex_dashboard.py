import json
import os
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_dashboard as dashboard


class QuotaDashboardTests(unittest.TestCase):
    def setUp(self):
        with dashboard.STORE_LOCK:
            dashboard.CONVERSATIONS.clear()
            dashboard.ACCOUNT_QUOTA_CACHE.clear()

    def tearDown(self):
        with dashboard.STORE_LOCK:
            dashboard.CONVERSATIONS.clear()
            dashboard.ACCOUNT_QUOTA_CACHE.clear()

    def test_converts_app_server_rate_limits(self):
        result = dashboard.quota_summary_from_app_server_result(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 15,
                            "resetsAt": 1730947200,
                        },
                    },
                    "codex_other": {
                        "limitId": "codex_other",
                        "primary": {
                            "usedPercent": 42,
                            "windowDurationMins": 60,
                            "resetsAt": 1730950800,
                        },
                    },
                }
            },
            event_ts=1730940000,
        )

        self.assertEqual(result["limit_id"], "codex")
        self.assertEqual(result["updated_at_raw"], 1730940000)
        self.assertEqual(
            [
                (window["key"], window["remaining_percent"])
                for window in result["windows"]
            ],
            [("codex:primary", 75.0), ("codex_other:primary", 58.0)],
        )

    def test_hides_base_model_inference_bucket(self):
        result = dashboard.quota_summary_from_app_server_result(
            {
                "rateLimitsByLimitId": {
                    "base_model_inference": {
                        "limitId": "base_model_inference",
                        "primary": {
                            "usedPercent": 99,
                            "windowDurationMins": 300,
                        },
                    },
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                        },
                    },
                }
            }
        )

        self.assertEqual(
            [window["key"] for window in result["windows"]], ["codex:primary"]
        )

    def test_account_quota_is_available_without_a_conversation(self):
        quota = {
            "updated_at_raw": 10,
            "updated_at": "1970-01-01 00:00:10",
            "windows": [{"key": "primary", "remaining_percent": 75}],
        }
        dashboard.store_account_quota(quota)

        with dashboard.app.test_client() as client:
            response = client.get("/api/quota")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["windows"][0]["remaining_percent"], 75)

    def test_request_ignores_unsolicited_rate_limit_notification(self):
        class FakeStdin:
            def write(self, value):
                self.last_write = value

            def flush(self):
                pass

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()

            def poll(self):
                return None

        poller = dashboard.CodexQuotaPoller("codex")
        poller._process = FakeProcess()
        poller._messages = queue.Queue()
        poller._messages.put(
            {
                "method": "account/rateLimits/updated",
                "params": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 15,
                            "resetsAt": 1730947200,
                        },
                    }
                },
            }
        )
        poller._messages.put({"id": 1, "result": {"rateLimits": {}}})

        with patch.object(dashboard, "store_account_quota") as store:
            result = poller._request("account/rateLimits/read", {}, 1)

        self.assertEqual(result, {"rateLimits": {}})
        store.assert_not_called()

    def test_transport_close_wakes_refresh_wait(self):
        poller = dashboard.CodexQuotaPoller("codex", interval_seconds=60)
        transport_closed_event = threading.Event()
        transport_closed_event.set()

        with self.assertRaisesRegex(RuntimeError, "connection closed"):
            poller._wait_for_next_read(transport_closed_event)

    def test_quota_app_server_environment_uses_private_sqlite_home(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CODEX_SQLITE_HOME": "",
                "CODEX_DASHBOARD_SQLITE_HOME": temp_dir,
            },
            clear=False,
        ):
            environment, sqlite_home, source = dashboard.quota_app_server_environment()

        self.assertEqual(sqlite_home, os.path.abspath(temp_dir))
        self.assertEqual(environment["CODEX_SQLITE_HOME"], sqlite_home)
        self.assertEqual(source, "CODEX_DASHBOARD_SQLITE_HOME")

    def test_quota_app_server_environment_defaults_to_project_sqlite_home(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            dashboard, "PROJECT_DIR", temp_dir
        ), patch.dict(
            os.environ,
            {
                "CODEX_SQLITE_HOME": "",
                "CODEX_DASHBOARD_SQLITE_HOME": "",
            },
            clear=False,
        ):
            environment, sqlite_home, source = dashboard.quota_app_server_environment()
            sqlite_home_exists = os.path.isdir(sqlite_home)

        self.assertEqual(sqlite_home, os.path.join(temp_dir, "sqlite"))
        self.assertTrue(sqlite_home_exists)
        self.assertEqual(environment["CODEX_SQLITE_HOME"], sqlite_home)
        self.assertEqual(source, "default")

    def test_quota_log_creates_project_log_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            dashboard,
            "QUOTA_LOG_PATH",
            os.path.join(temp_dir, "logs", "quota.log"),
        ):
            dashboard.quota_log("test_event", detail="test")
            with open(dashboard.QUOTA_LOG_PATH, encoding="utf-8") as log_file:
                record = log_file.readline()

        self.assertEqual(json.loads(record)["event"], "test_event")

    def test_resolves_windows_npm_cli_when_path_is_missing(self):
        expected = os.path.join(r"C:\Users\guzy0\AppData\Roaming", "npm", "codex.cmd")

        with patch.dict(
            os.environ,
            {
                "CODEX_DASHBOARD_CODEX_CMD": "",
                "APPDATA": r"C:\Users\guzy0\AppData\Roaming",
            },
            clear=False,
        ), patch.object(dashboard.shutil, "which", return_value=None), patch.object(
            dashboard.os.path, "isfile", side_effect=lambda path: path == expected
        ):
            self.assertEqual(dashboard.codex_cli_command(), expected)


if __name__ == "__main__":
    unittest.main()
