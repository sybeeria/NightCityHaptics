"""Class-only simulator checks; never start an engine, service or game."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


virtual = load("virtual_device_test", ROOT / "tools/virtual_device.py")
e2e = load("e2e_intiface_test", ROOT / "tools/e2e_intiface.py")


class MonitorTests(unittest.TestCase):
    def test_current_layout_and_companion_contributions(self):
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp)
            (mod / "companion").mkdir()
            (mod / "companion/catalog.json").write_bytes((ROOT / "src/companion/catalog.json").read_bytes())
            monitor = virtual.Monitor(mod)
            self.assertEqual(monitor.labels["gunfire"], "Gunfire")
            state = {"v": 2, "session": "test", "seq": 1, "active": True, "paused": False}
            status = {"running": True, "heartbeat": 1, "contributions": {"gunfire": .4}}
            (mod / "state.json").write_text(json.dumps(state))
            (mod / "status.json").write_text(json.dumps(status))
            monitor.poll_game(10)
            self.assertFalse(monitor.snapshot()["game"]["live"])
            self.assertEqual(monitor.reactions, [])
            state["seq"] = 2
            status["heartbeat"] = 2
            (mod / "state.json").write_text(json.dumps(state))
            (mod / "status.json").write_text(json.dumps(status))
            monitor.poll_game(10.1)
            monitor.handle_packet(b"Vibrate:10;")
            snapshot = monitor.snapshot()
            self.assertTrue(snapshot["game"]["active"])
            self.assertEqual(snapshot["history"][-1]["reactions"][0]["label"], "Gunfire")
            self.assertEqual(snapshot["current"], .5)
            self.assertEqual(snapshot["history"][-1]["sequence"], 1)
            state.update(seq=3, paused=True)
            (mod / "state.json").write_text(json.dumps(state))
            monitor.poll_game(10.2)
            self.assertTrue(monitor.game_live)
            self.assertFalse(monitor.game_active)
            monitor.poll_game(12)
            self.assertFalse(monitor.game_live)
            self.assertEqual(monitor.reactions, [])

    def test_packet_sequence_does_not_count_connection_markers(self):
        monitor = virtual.Monitor()
        monitor.set_connection(True)
        monitor.handle_packet("Vibrate:10;")
        boundary = monitor.snapshot()["command_count"]
        monitor.set_connection(False)
        self.assertEqual(e2e.received_since(monitor.snapshot(), boundary), [])
        monitor.handle_packet("Vibrate:0;")
        self.assertEqual(e2e.received_since(monitor.snapshot(), boundary), [0])


class E2EChecksTests(unittest.TestCase):
    def checks(self, receipt, after):
        return {"companion_connected": True, "device_listed": ["Lovense Hush"], "hurt_peak": .65,
                "engine_values": [.15, .2], "heartbeat_started_positive": True,
                "heartbeat_zero_received": True, "final_after_heartbeat_stops": 0,
                "try_peak": max(e2e.received_since(receipt, after), default=0),
                "quit_started_positive": True, "quit_zero_received": True, "final_after_quit": 0}

    def test_previous_hurt_cannot_satisfy_try(self):
        receipt = {"history": [{"received": True, "sequence": 5, "value": .65},
                               {"received": True, "sequence": 6, "value": 0}]}
        self.assertFalse(e2e.checks_pass(self.checks(receipt, 6), .63))
        receipt["history"].append({"received": True, "sequence": 7, "value": .4})
        self.assertTrue(e2e.checks_pass(self.checks(receipt, 6), .63))

    def test_quit_requires_active_output_and_fresh_zero(self):
        receipt = {"history": [{"received": True, "sequence": 7, "value": .4}]}
        checks = self.checks(receipt, 6)
        for key in ("quit_started_positive", "quit_zero_received", "heartbeat_zero_received"):
            with self.subTest(key=key):
                self.assertFalse(e2e.checks_pass(dict(checks, **{key: False}), .63))


if __name__ == "__main__":
    unittest.main()
