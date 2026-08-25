from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import server


class WorkerSlotsRegressionTests(unittest.TestCase):
    def test_time_value_normalizes_minutes_to_seconds(self) -> None:
        self.assertEqual(server._time_value("19:20"), "19:20:00")
        self.assertEqual(server._time_value("22:12"), "22:12:00")
        self.assertEqual(server._time_value("19:20:35"), "19:20:35")

    def test_worker_missing_slots_returns_empty_and_status_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "slots.json"
            with patch.object(server, "SLOTS_PATH", missing), patch.dict(
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


if __name__ == "__main__":
    unittest.main()
