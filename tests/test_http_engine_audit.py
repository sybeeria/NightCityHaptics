"""App mutations, HTTP framing and engine lifecycle using local fixtures only."""
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/companion"))
import nch


class AppInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"enabled": False, "start_intiface": False,
                                             "interactions": {"hurt": {"on": False}}}))
        self.app = nch.App(self.root, self.settings)

    def test_bad_mutations_never_reset_saved_output_policy(self):
        before = copy.deepcopy(self.app.settings)
        disk = self.settings.read_bytes()
        changes = [{"enabled": "false"}, {"start_intiface": 1}, {"max_strength": True},
                   {"max_strength": float("nan")}, {"max_strength": 10**1000},
                   {"max_strength": -1}, {"max_strength": 2},
                   {"interactions": {"hurt": {"on": "false"}}},
                   {"interactions": {"hurt": {"feel": "no-such-feel"}}},
                   {"interactions": {"hurt": {"feel": []}}},
                   {"interactions": {"no-such-interaction": {"on": True}}},
                   {"devices": {"device": {"all_motors": "false"}}},
                   {"devices": {"device": {"strength": float("inf")}}},
                   {"intiface_address": "ws://localhost:99999"},
                   {"intiface_address": "ws://localhost:0"},
                   {"intiface_address": "ws://user:pass@localhost:12345"},
                   {"intiface_address": "ws://localhost:12345\r\n"},
                   {"intiface_address": "WS://localhost:23456"},
                   {"intiface_address": " ws://localhost:23456"},
                   {"intiface_address": "ws://localhost:23456/a b"},
                   {"intiface_address": "ws://localhost:23456/日本語"}]
        for patch in changes:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.app.save(patch)
            self.assertEqual(self.app.settings, before)
            self.assertEqual(self.settings.read_bytes(), disk)

    def test_remote_failure_does_not_start_local_engine(self):
        self.app.save({"start_intiface": True, "intiface_address": "ws://192.0.2.1:23456"})
        with mock.patch.object(nch.buttplug, "Client", side_effect=OSError("offline")), \
             mock.patch.object(self.app.engine, "running", return_value=False), \
             mock.patch.object(self.app.engine, "executable", return_value=self.root / "engine"), \
             mock.patch.object(self.app.engine, "start") as start:
            self.app.connect(10)
        start.assert_not_called()
        self.assertEqual(self.app.link, "error")
        self.app.settings["intiface_address"] = "ws://localhost"
        self.assertEqual(self.app.port(), 80)

    def test_ipv6_engine_address_and_old_download_error_do_not_trap_startup(self):
        self.app.save({"start_intiface": True, "intiface_address": "ws://[::1]:23456"})
        self.assertFalse(self.app.local_engine_address())
        self.app.save({"intiface_address": "ws://127.0.0.1:23456"})
        self.app.engine.download_state = "Download failed: earlier attempt"
        with mock.patch.object(nch.buttplug, "Client", side_effect=OSError("offline")), \
             mock.patch.object(self.app.engine, "executable", return_value=self.root / "engine"), \
             mock.patch.object(self.app.engine, "start", return_value=True) as start:
            self.app.connect(10)
        start.assert_called_once_with(23456)
        self.assertEqual(self.app.link, "starting")

    def test_address_change_retires_only_the_owned_engine_after_persisting(self):
        self.app.started_engine_at = 10
        with mock.patch.object(self.app.engine, "stop") as stop:
            self.app.save({"intiface_address": "ws://127.0.0.1:12346"})
            stop.assert_called_once()
            self.assertIsNone(self.app.started_engine_at)
            self.assertEqual(json.loads(self.settings.read_text())["intiface_address"], "ws://127.0.0.1:12346")
        with mock.patch.object(self.app.engine, "stop") as stop, \
             mock.patch.object(nch, "write_json", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                self.app.save({"intiface_address": "ws://127.0.0.1:12347"})
            stop.assert_not_called()

    def test_output_graph_uses_time_instead_of_idle_tick_count(self):
        self.app.history = [(10, .9), (15.1, .4), (19.95, .2)]
        with mock.patch.object(nch.time, "monotonic", return_value=20):
            history = self.app.snapshot()["history"]
        self.assertEqual(len(history), 150)
        self.assertNotIn(.9, history)
        self.assertEqual(max(history), .4)
        self.assertEqual(history[-2], .2)

    def test_deep_json_file_recovers_without_startup_crash(self):
        self.settings.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
        with mock.patch.object(nch.json, "loads", side_effect=RecursionError):
            self.assertIsNone(nch.read_json(self.settings))
        self.assertIsInstance(nch.App(self.root, self.settings).settings, dict)

    def test_http_rejects_framing_and_unregistered_routes_before_action(self):
        server = nch.make_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(self.app, "save") as save:
            for headers, expected in (({"Content-Length": "262145"}, 413),
                                      ({"Content-Length": "-1"}, 400),
                                      ({"Content-Length": "bad"}, 400),
                                      ({"Transfer-Encoding": "chunked"}, 400)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("POST", "/api/settings", headers=dict(headers, **{"X-NCH": "1"}))
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("POST", "/unregistered/settings", "{}", {"X-NCH": "1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.close()
            save.assert_not_called()

    def test_ping_probe_handles_wrong_types_and_malformed_http(self):
        for payload in (b"[]", b"null", b"[" * 2000 + b"0" + b"]" * 2000):
            with mock.patch.object(nch.urllib.request, "build_opener") as factory:
                factory.return_value.open.return_value = io.BytesIO(payload)
                self.assertFalse(nch.already_running(19870))
        with mock.patch.object(nch.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = http.client.IncompleteRead(b"partial")
            self.assertFalse(nch.already_running(19870))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = nch.Engine()

    def test_download_status_is_set_before_worker_and_duplicates_are_ignored(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker():
            try:
                entered.set()
                release.wait(2)
                self.engine.download_state = ""
            finally:
                self.engine.download_lock.release()
                finished.set()
        with mock.patch.object(self.engine, "_download", side_effect=worker):
            self.assertTrue(self.engine.request_download())
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.engine.request_download())
            self.assertEqual(self.engine.download_state, "Downloading Intiface Engine…")
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(self.engine.download_state, "")

    def test_replacement_failure_preserves_previous_engine(self):
        binary = self.root / ("intiface-engine-" + nch.ENGINE_RELEASE) / "intiface-engine"
        binary.parent.mkdir()
        binary.write_bytes(b"old engine")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("intiface-engine", b"new engine")
        payload = data.getvalue()
        with mock.patch.object(nch, "data_dir", return_value=self.root), \
             mock.patch.object(self.engine, "platform_key", return_value="fixture"), \
             mock.patch.dict(nch.ENGINE_DOWNLOADS, {"fixture": ("engine.zip", hashlib.sha256(payload).hexdigest())}), \
             mock.patch.object(nch.urllib.request, "urlopen", return_value=io.BytesIO(payload)), \
             mock.patch.object(nch.shutil, "move", side_effect=OSError("fixture move failed")):
            self.engine.download()
        self.assertEqual(binary.read_bytes(), b"old engine")
        self.assertIn("fixture move failed", self.engine.download_state)
        self.assertFalse(self.engine.download_lock.locked())

    def test_running_engine_is_never_replaced(self):
        with mock.patch.object(self.engine, "running", return_value=True), \
             mock.patch.object(nch.urllib.request, "urlopen") as download:
            self.engine.download()
        download.assert_not_called()
        self.assertIn("Stop the running", self.engine.download_state)

    def test_spawn_failure_closes_log_and_killed_process_is_reaped(self):
        with mock.patch.object(nch, "data_dir", return_value=self.root), \
             mock.patch.object(self.engine, "executable", return_value=self.root / "engine"), \
             mock.patch.object(nch.subprocess, "Popen", side_effect=OSError("fixture spawn failed")):
            with self.assertRaises(OSError):
                self.engine.start(12345)
        self.assertIsNone(self.engine.log)
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [nch.subprocess.TimeoutExpired("engine", 3), 0]
        self.engine.process = process
        self.engine.stop()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(self.engine.process)


if __name__ == "__main__":
    unittest.main()
