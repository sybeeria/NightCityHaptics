"""Public simulator robustness checks; no engine or physical device is started."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nch_monitor_audit", ROOT / "tools/virtual_device.py")
virtual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(virtual)


class MonitorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mod = self.root / "mod"
        self.mod.joinpath("companion").mkdir(parents=True)
        (self.mod / "companion/catalog.json").write_text(json.dumps({"interactions": [
            {"id": "gunfire", "label": 42}, {"id": "hurt", "label": "Hurt"}, {"id": [], "label": "invalid"}, "wrong"]}))
        self.monitor = virtual.Monitor(self.mod)
        self.state = {"v": 2, "session": "test", "seq": 1, "active": True, "paused": False}
        self.status = {"running": True, "heartbeat": 1, "contributions": {}}
        self.write()
        self.monitor.poll_game(10)

    def write(self):
        (self.mod / "state.json").write_text(json.dumps(self.state))
        (self.mod / "status.json").write_text(json.dumps(self.status))

    def advance(self, contributions):
        self.state["seq"] += 1; self.status["heartbeat"] += 1
        self.status["contributions"] = contributions
        self.write(); self.monitor.poll_game(10.1)

    def test_malformed_catalog_label_falls_back_to_safe_string(self):
        self.advance({"try:gunfire": .5})
        self.assertEqual(self.monitor.reactions, [{"id": "try:gunfire", "label": "Try: gunfire", "contribution": .5}])

    def test_huge_nonfinite_and_wrong_type_contributions_are_discarded(self):
        self.advance({"too-big": 10 ** 400, "inf": float("inf"), "nan": float("nan"), "bool": True,
                      "negative": -1, "string": ".5", "hurt": .4, "bounded": 50})
        self.assertEqual({item["id"]: item["contribution"] for item in self.monitor.reactions}, {"hurt": .4, "bounded": 1})
        json.dumps(self.monitor.snapshot(), allow_nan=False)

    def test_recursive_corrupt_telemetry_clears_heartbeat_without_raising(self):
        self.advance({"hurt": .5})
        self.assertTrue(self.monitor.game_active)
        (self.mod / "state.json").write_text('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}')
        (self.mod / "status.json").write_text('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}')
        self.monitor.poll_game(10.2)
        self.assertFalse(self.monitor.game_live)
        self.assertFalse(self.monitor.game_active)
        self.assertEqual(self.monitor.reactions, [])

    def test_bounded_read_checks_actual_bytes_instead_of_a_racy_stat(self):
        stream = io.BytesIO(b' ' * 262145)
        original = stream.read
        with mock.patch.object(stream, "read", wraps=original) as read, mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(virtual.read_json(self.mod / "state.json"), {})
            read.assert_called_once_with(262145)

    def test_invalid_gate_types_and_wrong_schema_fail_closed(self):
        self.advance({"hurt": .5})
        for key, value in (("paused", "false"), ("active", 1), ("v", 1)):
            previous = self.state[key]
            self.state[key] = value; self.state["seq"] += 1; self.write()
            self.monitor.poll_game(10.2)
            self.assertFalse(self.monitor.game_live)
            self.assertFalse(self.monitor.game_active)
            self.state[key] = previous

    def test_overlong_and_unicode_vibration_packets_cannot_crash_or_change_output(self):
        self.monitor.handle_packet(b"Vibrate:10;")
        count = self.monitor.command_count
        for packet in ("Vibrate:" + "9" * 10000 + ";", "Vibrate:999;", "Vibrate:٢;", "Vibrate:21;", b"Vibrate:" + b"9" * 10000 + b";"):
            self.monitor.handle_packet(packet)
        self.assertEqual(self.monitor.command_count, count)
        self.assertEqual(self.monitor.current, .5)
        self.assertEqual(self.monitor.handle_packet(b"Battery;"), b"100;")
        self.monitor.handle_packet(b"Vibrate:0;")
        self.assertEqual(self.monitor.current, 0)

    def test_receipt_parent_cannot_be_redirected_into_read_only_mod(self):
        parent = self.root / "receipt-folder"; parent.mkdir()
        monitor = virtual.Monitor(self.mod, parent / "receipt.json")
        parent.rmdir(); parent.symlink_to(self.mod, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            monitor.save()
        self.assertFalse((self.mod / "receipt.json").exists())

    def test_cli_rejects_bad_endpoint_before_network_start(self):
        for endpoint in ("ws://127.0.0.1:bad", "ws://127.0.0.1:0", "ws://127.0.0.1", "ws://example.com:54817", "ws://127.0.0.1:54817?query=1"):
            with self.subTest(endpoint=endpoint), mock.patch.object(sys, "argv", ["virtual_device.py", "--endpoint", endpoint]), \
                    mock.patch.object(virtual.asyncio, "run") as run, mock.patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    virtual.main()
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
