import json
import os
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_dashboard as dashboard


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.session_id = "interrupt-test"
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard.CONVERSATIONS.pop(self.session_id, None)

    def test_interrupt_clears_permission_and_preserves_prompt(self):
        payload = {"session_id": self.session_id, "turn_id": "current", "prompt": "Work"}
        self.client.post("/api/codex/user_prompt_submit", json=payload)
        dashboard.CONVERSATIONS[self.session_id]["permission"] = {"tool_name": "Bash"}
        with patch.object(dashboard, "stop_dashboard_taskbar_flash_if_idle_async"):
            response = self.client.post("/api/codex/interrupt", json=payload)
        self.assertEqual(response.status_code, 200)
        row = dashboard.CONVERSATIONS[self.session_id]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["prompt"], "Work")
        self.assertIsNone(row["permission"])
        self.assertIsNotNone(row["finished_at"])

    def test_old_interrupt_does_not_overwrite_new_turn(self):
        self.client.post("/api/codex/user_prompt_submit", json={
            "session_id": self.session_id, "turn_id": "new",
        })
        response = self.client.post("/api/codex/interrupt", json={
            "session_id": self.session_id, "turn_id": "old",
        })
        self.assertTrue(response.get_json()["ignored"])
        self.assertEqual(dashboard.CONVERSATIONS[self.session_id]["status"], "thinking")

    def test_interrupt_without_submit_creates_visible_row(self):
        with patch.object(dashboard, "stop_dashboard_taskbar_flash_if_idle_async"):
            self.client.post("/api/codex/interrupt", json={"session_id": self.session_id})
        item = dashboard.stored_to_item(dashboard.CONVERSATIONS[self.session_id])
        self.assertEqual(item["status"], "interrupted")


class UserInputTests(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.app.test_client()
        self.payload = {"session_id": "question-test", "turn_id": "turn", "tool_use_id": "question-1",
                        "tool_name": "request_user_input", "tool_input": {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]}}

    def tearDown(self):
        dashboard.CONVERSATIONS.pop("question-test", None)

    def test_question_alert_and_answer_lifecycle(self):
        with patch.object(dashboard, "notify_attention_async") as notify:
            self.client.post("/api/codex/request_user_input", json=self.payload)
        notify.assert_called_once()
        row = dashboard.CONVERSATIONS["question-test"]
        self.assertEqual(dashboard.stored_to_item(row)["status"], "input")
        self.assertIn("Which?", row["permission"]["command"])
        with dashboard.STORE_LOCK:
            self.assertEqual(dashboard.current_attention_unlocked()["label"], "Needs Input")
        with patch.object(dashboard, "stop_dashboard_taskbar_flash_if_idle_async"):
            self.client.post("/api/codex/user_input_answered", json=self.payload)
        self.assertEqual(row["status"], "thinking")
        self.assertIsNone(row["permission"])

    def test_stale_question_and_answer_are_ignored(self):
        self.client.post("/api/codex/user_prompt_submit", json=self.payload)
        stale = dict(self.payload, turn_id="old")
        self.assertTrue(self.client.post("/api/codex/request_user_input", json=stale).get_json()["ignored"])
        with patch.object(dashboard, "notify_attention_async"):
            self.client.post("/api/codex/request_user_input", json=self.payload)
        wrong_call = dict(self.payload, tool_use_id="question-0")
        self.assertTrue(self.client.post("/api/codex/user_input_answered", json=wrong_call).get_json()["ignored"])
        self.assertIsNotNone(dashboard.CONVERSATIONS["question-test"]["permission"])


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
