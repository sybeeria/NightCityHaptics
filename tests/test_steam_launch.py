"""Isolated launch-wrapper checks: fake processes and an ephemeral local port only."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools/nch-steam-launch"
SPEC = importlib.util.spec_from_loader("nch_steam_launch", SourceFileLoader("nch_steam_launch", str(SCRIPT)))
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


COMPANION = r'''import argparse, json, os, signal, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
p = argparse.ArgumentParser()
p.add_argument('--no-browser', action='store_true')
p.add_argument('--port', type=int)
p.add_argument('--mod-dir')
a = p.parse_args()
record = Path(os.environ['NCH_TEST_COMPANION_RECORD'])
record.write_text(json.dumps({'argv': __import__('sys').argv[1:], 'env': dict(os.environ), 'pid': os.getpid()}))
def stop(number, frame):
    Path(os.environ['NCH_TEST_COMPANION_STOP']).write_text(str(number))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        data = b'{"app":"night-city-haptics"}'
        self.send_response(200); self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)
HTTPServer(('127.0.0.1', a.port), Handler).serve_forever()
'''

GAME = r'''import json, os, sys, time
from pathlib import Path
Path(os.environ['NCH_TEST_GAME_RECORD']).write_text(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ), 'pid': os.getpid(), 'pgid': os.getpgid(0)}))
if os.environ.get('NCH_TEST_WAIT') == '1':
    while True: time.sleep(.05)
print('game stdout')
print('game stderr', file=sys.stderr)
raise SystemExit(int(os.environ.get('NCH_TEST_GAME_EXIT', '37')))
'''


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the stub")


class PingServer:
    def __init__(self, app="night-city-haptics"):
        self.app = app

    def __enter__(self):
        owner = self
        self.requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.requests.append(("GET", self.path))
                data = json.dumps({"app": owner.app}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                owner.requests.append(("POST", self.path))
                self.send_response(400)
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self
    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nch-steam-stubs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.companion = self.root / "mod folder/companion/nch.py"
        self.companion.parent.mkdir(parents=True)
        self.companion.write_text(COMPANION)
        self.game = self.root / "fake game.py"
        self.game.write_text(GAME)
        self.game_record = self.root / "game.json"
        self.companion_record = self.root / "companion.json"
        self.companion_stop = self.root / "companion.stop"
        self.environment = dict(os.environ)
        for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", *launcher.SAVED_VARIABLES.values()):
            self.environment.pop(name, None)
        self.environment.update({
            "NCH_TEST_GAME_RECORD": str(self.game_record),
            "NCH_TEST_COMPANION_RECORD": str(self.companion_record),
            "NCH_TEST_COMPANION_STOP": str(self.companion_stop),
            "NCH_TEST_GAME_EXIT": "37",
            "NCH_TEST_ENV_SENTINEL": "unchanged",
            "WINEDLLOVERRIDES": "dxgi,winmm,version=n,b",
        })
        self.port = free_port()

    def command(self, extra=(), companion=None, port=None):
        return [sys.executable, str(SCRIPT), "--companion",
                str(companion or self.companion), "--port", str(port or self.port),
                "--", sys.executable, str(self.game), *extra]

    def run_wrapper(self, extra=(), companion=None, port=None):
        return subprocess.run(self.command(extra, companion, port), env=self.environment,
                              text=True, capture_output=True, timeout=10)

    def test_exact_arguments_environment_stdio_exit_and_owned_cleanup(self):
        arguments = ["has spaces", "", "日本語", "$(touch should-not-exist)", ";", "--launcher-skip"]
        result = self.run_wrapper(arguments)
        self.assertEqual(result.returncode, 37, result.stderr)
        record = json.loads(self.game_record.read_text())
        self.assertEqual(record["argv"], arguments)
        self.assertEqual(record["env"]["WINEDLLOVERRIDES"], "dxgi,winmm,version=n,b")
        self.assertEqual(record["env"]["NCH_TEST_ENV_SENTINEL"], "unchanged")
        self.assertEqual(record["pid"], record["pgid"])
        self.assertIn("game stdout", result.stdout)
        self.assertIn("game stderr", result.stderr)
        self.assertEqual(self.companion_stop.read_text(), str(signal.SIGTERM))
        owned = json.loads(self.companion_record.read_text())
        self.assertEqual(owned["argv"], ["--no-browser", "--port", str(self.port),
                                        "--mod-dir", str(self.companion.parent.parent)])

    def test_existing_instance_is_reused_and_remains_running(self):
        with PingServer() as existing:
            result = self.run_wrapper(port=existing.port)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertFalse(self.companion_record.exists())
            self.assertFalse(self.companion_stop.exists())
            self.assertTrue(launcher.already_running(existing.port))
            self.assertTrue(existing.requests)
            self.assertTrue(all(request == ("GET", "/api/ping") for request in existing.requests))

    def test_missing_companion_does_not_block_game(self):
        result = self.run_wrapper(companion=self.root / "missing.py")
        self.assertEqual(result.returncode, 37)
        self.assertIn("companion not found", result.stderr)
        self.assertTrue(self.game_record.exists())

    def test_foreign_service_is_not_reused_and_companion_failure_is_fail_open(self):
        with PingServer(app="different-app") as foreign:
            result = self.run_wrapper(port=foreign.port)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertTrue(self.companion_record.exists())
            self.assertFalse(self.companion_stop.exists())
            self.assertFalse(launcher.already_running(foreign.port))
            self.assertTrue(all(request == ("GET", "/api/ping") for request in foreign.requests))

    def test_malformed_http_response_is_treated_as_not_running(self):
        with mock.patch.object(launcher.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = launcher.http.client.IncompleteRead(b"partial")
            self.assertFalse(launcher.already_running(self.port))

    def test_failed_companion_does_not_block_game(self):
        self.companion.write_text("raise SystemExit(23)\n")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 37)
        self.assertIn("companion exited with status 23", result.stderr)
        self.assertTrue(self.game_record.exists())

    def test_loader_variables_restored_for_game_and_removed_for_companion(self):
        self.environment.update({
            "NCH_STEAM_SAVED_LD_LIBRARY_PATH": "/tmp/steam runtime libs",
            "NCH_STEAM_SAVED_LD_PRELOAD": "",
        })
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 37, result.stderr)
        game = json.loads(self.game_record.read_text())["env"]
        companion = json.loads(self.companion_record.read_text())["env"]
        self.assertEqual(game["LD_LIBRARY_PATH"], "/tmp/steam runtime libs")
        self.assertEqual(game["LD_PRELOAD"], "")
        for name, marker in launcher.SAVED_VARIABLES.items():
            self.assertNotIn(marker, game)
            self.assertNotIn(marker, companion)
            self.assertNotIn(name, companion)

    def test_game_missing_returns_127_and_stops_owned_companion(self):
        command = self.command()
        command[command.index("--") + 1:] = [str(self.root / "missing executable")]
        result = subprocess.run(command, env=self.environment, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(self.companion_stop.read_text(), str(signal.SIGTERM))

    def test_sigterm_forwards_to_game_group_and_cleans_owned_companion(self):
        self.environment["NCH_TEST_WAIT"] = "1"
        process = subprocess.Popen(self.command(), env=self.environment,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(self.game_record.exists)
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, error)
            self.assertEqual(self.companion_stop.read_text(), str(signal.SIGTERM))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_no_command_rejected_before_side_effects(self):
        command = [sys.executable, str(SCRIPT), "--companion", str(self.companion)]
        result = subprocess.run(command, env=self.environment, text=True,
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.game_record.exists())
        self.assertFalse(self.companion_record.exists())

    def test_duplicate_race_does_not_stop_other_instance(self):
        with PingServer() as existing:
            exited = mock.Mock()
            exited.poll.return_value = 0
            with mock.patch.object(launcher, "already_running", side_effect=[False, True]), \
                    mock.patch.object(launcher.subprocess, "Popen", return_value=exited):
                child = launcher.start_companion(self.companion, existing.port,
                                                 self.environment, lambda: False)
            self.assertIsNone(child)
            launcher.stop_owned_companion(child)
            exited.terminate.assert_not_called()
            self.assertTrue(launcher.already_running(existing.port))

    def test_startup_wait_is_bounded_and_retains_cleanup_ownership(self):
        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(launcher, "STARTUP_SECONDS", 0.04), \
                mock.patch.object(launcher, "already_running", return_value=False), \
                mock.patch.object(launcher.subprocess, "Popen", return_value=child), \
                mock.patch.object(launcher, "warn") as warning:
            started = time.monotonic()
            result = launcher.start_companion(self.companion, self.port,
                                              self.environment, lambda: False)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIs(result, child)
        warning.assert_called_once()

    def test_unresponsive_owned_companion_is_killed_after_bounded_wait(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("stub", 5), 0]
        with mock.patch.object(launcher, "warn"):
            launcher.stop_owned_companion(child)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(5.0), mock.call(1.0)])

    def test_local_ping_cannot_follow_redirect_to_another_listener(self):
        with PingServer() as target:
            class Redirect(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def do_GET(self):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:{}/api/ping".format(target.port))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertFalse(launcher.already_running(server.server_port))
                self.assertEqual(target.requests, [])
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_ping_bypasses_configured_proxy(self):
        with PingServer() as existing, mock.patch.dict(os.environ, {
                "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "", "no_proxy": ""}):
            self.assertTrue(launcher.already_running(existing.port))

    def test_direct_invocation_preserves_game_loader_env_and_cleans_companion(self):
        game, companion = launcher.environments({"LD_LIBRARY_PATH": "direct", "LD_PRELOAD": "", "X": "1"})
        self.assertEqual(game, {"LD_LIBRARY_PATH": "direct", "LD_PRELOAD": "", "X": "1"})
        self.assertEqual(companion, {"X": "1"})

    def test_shell_entry_default_companion_spaces_and_loader_isolation(self):
        game_folder = self.root / "Steam library/Cyberpunk 2077"
        default_companion = game_folder / "bin/x64/plugins/cyber_engine_tweaks/mods/nightCityHaptics/companion/nch.py"
        default_companion.parent.mkdir(parents=True)
        default_companion.write_text(COMPANION)
        helper = game_folder / "nch-steam-launch"
        shutil.copyfile(SCRIPT, helper)
        self.environment["LD_LIBRARY_PATH"] = "/tmp/Steam test libraries"
        self.environment["LD_PRELOAD"] = ""
        # Stale inherited markers must be replaced by the shell's actual environment.
        self.environment["NCH_STEAM_SAVED_LD_LIBRARY_PATH"] = "stale-marker"
        self.environment["NCH_STEAM_SAVED_LD_PRELOAD"] = "stale-marker"
        arguments = ["has spaces", "", "日本語", "--launcher-skip"]
        command = ["/bin/sh", str(helper), "--port", str(self.port), "--",
                   sys.executable, str(self.game), *arguments]
        result = subprocess.run(command, env=self.environment, cwd=self.root,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 37, result.stderr)
        game = json.loads(self.game_record.read_text())
        owned = json.loads(self.companion_record.read_text())
        self.assertEqual(game["argv"], arguments)
        self.assertEqual(game["env"]["LD_LIBRARY_PATH"], "/tmp/Steam test libraries")
        self.assertEqual(game["env"]["LD_PRELOAD"], "")
        self.assertEqual(game["env"]["WINEDLLOVERRIDES"], "dxgi,winmm,version=n,b")
        self.assertEqual(owned["argv"], ["--no-browser", "--port", str(self.port),
                                       "--mod-dir", str(default_companion.parent.parent)])
        for name, marker in launcher.SAVED_VARIABLES.items():
            self.assertNotIn(marker, game["env"])
            self.assertNotIn(marker, owned["env"])
            self.assertNotIn(name, owned["env"])
        self.assertEqual(self.companion_stop.read_text(), str(signal.SIGTERM))

    def test_shell_entry_unset_loader_variables_clear_stale_markers(self):
        self.environment["NCH_STEAM_SAVED_LD_LIBRARY_PATH"] = "stale-marker"
        self.environment["NCH_STEAM_SAVED_LD_PRELOAD"] = "stale-marker"
        command = ["/bin/sh", str(SCRIPT), "--companion", str(self.companion),
                   "--port", str(self.port), "--", sys.executable, str(self.game)]
        result = subprocess.run(command, env=self.environment, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 37, result.stderr)
        game = json.loads(self.game_record.read_text())["env"]
        companion = json.loads(self.companion_record.read_text())["env"]
        for name, marker in launcher.SAVED_VARIABLES.items():
            self.assertNotIn(marker, game)
            self.assertNotIn(marker, companion)
            self.assertNotIn(name, game)
            self.assertNotIn(name, companion)


if __name__ == "__main__":
    unittest.main()
