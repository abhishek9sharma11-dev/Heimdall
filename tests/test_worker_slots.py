from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dashboard import server
from dashboard.session_time import normalize_session_time
from scripts import day_ops_runner as runner


class WorkerSlotsRegressionTests(unittest.TestCase):
    def test_time_value_normalizes_minutes_to_seconds(self) -> None:
        self.assertEqual(server._time_value("19:20"), "19:20:00")
        self.assertEqual(server._time_value("22:12"), "22:12:00")
        self.assertEqual(server._time_value("19:20:35"), "19:20:35")

    def test_worker_missing_slots_returns_empty_and_status_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "slots.json"
            with patch.object(server, "SLOTS_PATH", missing), patch.object(server, "REPO", Path(tmp)), patch.dict(
                os.environ, {"HERMES_WORKER_MODE": "1"}, clear=False
            ):
                self.assertEqual(server._load_slots(), [])
                status = server.build_status()
                self.assertEqual(status["slots"], [])

    def test_local_existing_slots_behavior_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            slots = Path(tmp) / "slots.json"
            slots.write_text(
                json.dumps(
                    [{
                        "id": "local-slot",
                        "account": "Local",
                        "port": 8765,
                        "meeting_id": "12345678900",
                        "env_file": ".env.local-slot",
                        "schedule_file": "schedules/local-slot.json",
                        "enabled": True,
                    }]
                )
            )
            with patch.object(server, "SLOTS_PATH", slots), patch.dict(
                os.environ, {"HERMES_WORKER_MODE": "0"}, clear=False
            ):
                loaded = server._load_slots()
                self.assertTrue(any(slot["id"] == "local-slot" for slot in loaded))

    def test_shared_time_parser_rejects_invalid_and_preserves_seconds(self) -> None:
        self.assertEqual(normalize_session_time("19:20"), "19:20:00")
        self.assertEqual(normalize_session_time("19:20:35"), "19:20:35")
        with self.assertRaises(ValueError):
            normalize_session_time("19:75")

    def test_old_format_registration_is_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "today_sessions.json"
            manifest.write_text(json.dumps({"sessions": [{
                "id": "86136452840", "meeting_id": "86136452840", "port": 8765,
                "env_file": ".env.86136452840", "schedule_file": "schedules/86136452840.json",
                "session_start_ist": "19:20", "session_end_ist": "22:12",
                "enabled": True, "join_url_present": True,
            }]}))
            with patch.object(runner, "MANIFEST", manifest):
                sessions = runner.load_manifest_sessions()
            self.assertEqual([s["id"] for s in sessions], ["86136452840"])

    def test_missing_and_malformed_registration_are_logged_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with patch.object(runner, "MANIFEST", missing), patch.object(runner, "log") as log:
                self.assertEqual(runner.load_manifest_sessions(), [])
                log.assert_called()
            manifest = Path(tmp) / "bad.json"
            manifest.write_text(json.dumps({"sessions": [
                {"id": "bad", "port": 8765, "env_file": ".env.bad", "schedule_file": "bad.json",
                 "session_start_ist": "not-a-time", "session_end_ist": "22:00"},
                {"id": "good", "port": 8766, "env_file": ".env.good", "schedule_file": "good.json",
                 "session_start_ist": "19:20", "session_end_ist": "22:00"},
            ]}))
            with patch.object(runner, "MANIFEST", manifest), patch.object(runner, "log") as log:
                sessions = runner.load_manifest_sessions()
            self.assertEqual([s["id"] for s in sessions], ["good"])
            self.assertTrue(any("bad" in call.args[0] and "ValueError" in call.args[0] for call in log.call_args_list))

    def test_lead_time_and_started_or_ended_windows(self) -> None:
        with patch.object(runner, "now", return_value=datetime(2026, 8, 25, 20, 28, tzinfo=runner.TZ)):
            start = runner.parse_hhmmss("19:20")
            end = runner.parse_hhmmss("22:12")
            self.assertGreaterEqual(runner.now(), start - timedelta(minutes=30))
            self.assertLessEqual(runner.now(), end)
        with patch.object(runner, "now", return_value=datetime(2026, 8, 25, 22, 30, tzinfo=runner.TZ)):
            self.assertGreater(runner.now(), runner.parse_hhmmss("22:12") + timedelta(minutes=10))

    def test_failed_start_is_retried_by_runtime_worker(self) -> None:
        session = {"id": "one", "port": 8765, "env_file": ".env.one"}
        with patch.object(runner, "start_slot", return_value=False) as start:
            self.assertFalse(runner.start_slot(session["port"], session["env_file"]))
            start.assert_called_once_with(8765, ".env.one")


if __name__ == "__main__":
    unittest.main()
