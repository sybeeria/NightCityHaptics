"""Black-box native app contracts; isolated files and unavailable Intiface only."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "src/companion/night-city-haptics"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(BINARY.is_file(), "Build the native companion first")
class NativeAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nch-rust-app-")
        self.addCleanup(self.temp.cleanup)
        self.mod = Path(self.temp.name) / "mod"
        self.mod.mkdir()
        shutil.copytree(ROOT / "src/companion", self.mod / "companion", ignore=shutil.ignore_patterns("*.py", "__pycache__", "night-city-haptics*"))
        self.settings = self.mod / "companion/settings.json"
        self.settings.write_text(json.dumps({"start_intiface": False, "intiface_address": "ws://127.0.0.1:1", "devices": {"saved-device": {"on": False, "strength": .3, "all_motors": True}}}))
        self.port = free_port()
        self.log = (self.mod / "app.log").open("w")
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([str(BINARY), "--mod-dir", str(self.mod), "--port", str(self.port), "--no-browser"], stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/api/ping")[0] == 200:
                    break
            except OSError:
                pass
            if self.process.poll() is not None:
                self.fail((self.mod / "app.log").read_text())
            time.sleep(.025)
        else:
            self.fail("Native HTTP server did not start")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=2)

    def request(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, None if value is None else json.dumps(value), {"X-NCH": "1", **(headers or {})})
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if response.getheader("Content-Type", "").startswith("application/json") else body
        finally:
            connection.close()

    def state(self):
        return self.request("GET", "/api/state")[1]

    def beat(self, seq, **changes):
        state = {"v": 2, "session": "native-test", "seq": seq, "t": seq * .1, "active": True, "paused": False, "levels": {"burning": 1}, "events": []}
        state.update(changes)
        temporary = self.mod / "state.json.tmp"
        temporary.write_text(json.dumps(state)); os.replace(temporary, self.mod / "state.json")
        time.sleep(.13)

    def test_assets_catalog_previews_and_keepalive(self):
        state = self.request("GET", "/api/state", headers={"X-NCH-Catalog": "1"})[1]
        self.assertEqual(state["version"], "0.5.0")
        self.assertEqual(len(state["catalog"]["interactions"]), 47)
        self.assertTrue(state["catalog"]["previews"])
        self.assertIsNone(self.state()["catalog"])
        for url, file in [("/", "ui.html"), ("/fonts/archivo.woff2", "fonts/archivo.woff2")]:
            self.assertEqual(self.request("GET", url), (200, (ROOT / "src/companion" / file).read_bytes()))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        for _ in range(3):
            connection.request("GET", "/api/ping")
            response = connection.getresponse(); self.assertEqual(response.status, 200); response.read()

    def test_stale_baseline_live_pause_corruption_and_timeout(self):
        self.beat(1)
        self.assertEqual(self.state()["output"], 0)
        self.assertEqual(self.state()["game"], "waiting")
        self.beat(2)
        self.assertEqual(self.state()["game"], "live")
        self.assertGreater(self.state()["output"], 0)
        self.beat(3, paused=True)
        self.assertEqual(self.state()["output"], 0)
        self.beat(4)
        self.assertGreater(self.state()["output"], 0)
        (self.mod / "state.json").write_text("{broken")
        time.sleep(.14)
        self.assertEqual(self.state()["output"], 0)
        self.beat(5); self.beat(6)
        time.sleep(1.2)
        self.assertEqual(self.state()["output"], 0)
        self.assertEqual(self.state()["game"], "offline")

    def test_settings_merge_rejection_reset_and_master_preview(self):
        self.assertEqual(self.request("POST", "/api/settings", {"interactions": {"gunfire": {"strength": .2}}, "enabled": False})[0], 200)
        saved = self.settings.read_bytes()
        for patch in [{"max_strength": True}, {"devices": {"x": {"outputs": {"spray": True}}}}, {"interactions": {"gunfire": {"feel": "scene"}}}, {"intiface_address": "ws://u:p@localhost:12345"}]:
            self.assertEqual(self.request("POST", "/api/settings", patch)[0], 400)
            self.assertEqual(self.settings.read_bytes(), saved)
        self.assertEqual(self.request("POST", "/api/try", {"id": "levelUp"})[0], 200)
        time.sleep(.15); self.assertEqual(self.state()["output"], 0)
        self.assertEqual(self.request("POST", "/api/settings", {"enabled": True})[0], 200)
        self.request("POST", "/api/try", {"id": "levelUp"})
        values = []
        for _ in range(12):
            time.sleep(.05); values.append(self.state()["output"])
        self.assertGreater(max(values), 0)
        self.request("POST", "/api/reset", {})
        state = self.state()
        self.assertEqual(state["settings"]["devices"]["saved-device"]["strength"], .3)
        self.assertFalse(state["settings"]["start_intiface"])

    def test_http_request_origin_and_framing_fail_closed(self):
        self.assertEqual(self.request("POST", "/api/settings", {"enabled": False}, {"X-NCH": "0"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "foreign.example"})[0], 403)
        for framing, expected in [("Content-Length: 2\r\nContent-Length: 2", 400), ("Content-Length: +2", 400), ("Content-Length: 262145", 413), ("Transfer-Encoding: chunked\r\nContent-Length: 2", 400)]:
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as stream:
                stream.sendall((f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-NCH: 1\r\n{framing}\r\n\r\n{{}}").encode())
                response = b""
                while True:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                self.assertIn(f" {expected} ".encode(), response.split(b"\r\n", 1)[0])
        self.assertTrue(self.state()["settings"]["enabled"])

    def test_status_heartbeat_shutdown_and_single_instance(self):
        status = self.mod / "status.json"
        first = json.loads(status.read_text())["heartbeat"]
        time.sleep(1.15)
        self.assertGreater(json.loads(status.read_text())["heartbeat"], first)
        second = subprocess.run([str(BINARY), "--port", str(self.port), "--mod-dir", str(self.mod), "--no-browser"], capture_output=True, timeout=3)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIsNone(self.process.poll())
        self.assertEqual(self.request("POST", "/api/quit", {})[0], 200)
        self.process.wait(timeout=7)
        stopped = json.loads(status.read_text())
        self.assertFalse(stopped["running"]); self.assertEqual(stopped["output"], 0)
