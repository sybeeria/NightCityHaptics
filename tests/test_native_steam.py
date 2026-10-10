"""Native Steam supervisor tests. Only stub processes and loopback listeners."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('NCH_NATIVE_BINARY', ROOT / 'src/companion/night-city-haptics'))
SPEC = importlib.util.spec_from_file_location('steam_stub_support', ROOT / 'tests/steam_stubs.py')
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


@unittest.skipUnless(BINARY.is_file(), 'Build native companion first')
class NativeSteamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nch-native-steam-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.companion = self.root / 'mod folder/companion/stub'
        self.companion.parent.mkdir(parents=True)
        self.companion.write_text('#!' + sys.executable + '\n' + support.COMPANION)
        self.companion.chmod(0o755)
        self.game = self.root / 'game stub.py'
        self.game.write_text(support.GAME)
        self.record = self.root / 'game.json'
        self.owned = self.root / 'companion.json'
        self.stopped = self.root / 'companion.stop'
        self.environment = dict(os.environ)
        for name in ['LD_LIBRARY_PATH', 'LD_PRELOAD', 'NCH_STEAM_SAVED_LD_LIBRARY_PATH', 'NCH_STEAM_SAVED_LD_PRELOAD']:
            self.environment.pop(name, None)
        self.environment.update(NCH_TEST_GAME_RECORD=str(self.record), NCH_TEST_COMPANION_RECORD=str(self.owned), NCH_TEST_COMPANION_STOP=str(self.stopped), NCH_TEST_GAME_EXIT='37')
        self.port = support.free_port()

    def command(self, *arguments, path=None, port=None):
        return [str(BINARY), '--steam', '--companion', str(path or self.companion), '--port', str(port or self.port), '--', sys.executable, str(self.game), *arguments]

    def run_wrapper(self, *arguments, **options):
        return subprocess.run(self.command(*arguments, **options), env=self.environment, text=True, capture_output=True, timeout=10)

    def test_exact_arguments_exit_stdio_loader_environment_and_owned_cleanup(self):
        self.environment.update(NCH_STEAM_SAVED_LD_LIBRARY_PATH='/tmp/stub libs', NCH_STEAM_SAVED_LD_PRELOAD='')
        arguments = ['space value', '', '日本語', '$(touch should-not-exist)', ';']
        result = self.run_wrapper(*arguments)
        self.assertEqual(result.returncode, 37, result.stderr)
        game = json.loads(self.record.read_text())
        owned = json.loads(self.owned.read_text())
        self.assertEqual(game['argv'], arguments)
        self.assertEqual(game['pid'], game['pgid'])
        self.assertIn('game stdout', result.stdout)
        self.assertIn('game stderr', result.stderr)
        self.assertEqual(game['env']['LD_LIBRARY_PATH'], '/tmp/stub libs')
        for name in ['LD_LIBRARY_PATH', 'LD_PRELOAD', 'NCH_STEAM_SAVED_LD_LIBRARY_PATH', 'NCH_STEAM_SAVED_LD_PRELOAD']:
            self.assertNotIn(name, owned['env'])
        self.assertEqual(self.stopped.read_text(), str(signal.SIGTERM))

    def test_existing_instance_is_reused_and_not_stopped(self):
        with support.PingServer() as existing:
            result = self.run_wrapper(port=existing.port)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertFalse(self.owned.exists())
            self.assertFalse(self.stopped.exists())
            self.assertTrue(existing.requests)
            self.assertTrue(all(request == ('GET', '/api/ping') for request in existing.requests))

    def test_missing_companion_and_failed_companion_still_run_command(self):
        self.assertEqual(self.run_wrapper(path=self.root / 'missing').returncode, 37)
        self.companion.write_text('#!' + sys.executable + '\nraise SystemExit(23)\n')
        self.assertEqual(self.run_wrapper().returncode, 37)

    def test_termination_reaches_game_group_and_stops_only_child(self):
        self.environment['NCH_TEST_WAIT'] = '1'
        process = subprocess.Popen(self.command(), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            support.wait_for(self.record.exists)
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, error)
            self.assertEqual(self.stopped.read_text(), str(signal.SIGTERM))
        finally:
            if process.poll() is None:
                process.kill(); process.communicate(timeout=2)

    def test_missing_command_is_rejected_before_startup(self):
        result = subprocess.run([str(BINARY), '--steam', '--companion', str(self.companion)], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.owned.exists())

    def test_repeated_termination_is_forwarded_to_a_child_that_handles_it(self):
        signals = self.root / 'signals.txt'
        self.environment['NCH_TEST_SIGNALS'] = str(signals)
        self.game.write_text(r'''import json, os, signal, time
from pathlib import Path
count = 0
def terminate(number, frame):
    global count
    count += 1
    Path(os.environ['NCH_TEST_SIGNALS']).write_text(str(count))
    if count == 2: raise SystemExit(37)
signal.signal(signal.SIGTERM, terminate)
Path(os.environ['NCH_TEST_GAME_RECORD']).write_text(json.dumps({'pid': os.getpid()}))
while True: time.sleep(.02)
''')
        process = subprocess.Popen(self.command(), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            support.wait_for(self.record.exists)
            process.terminate()
            support.wait_for(lambda: signals.exists() and signals.read_text() == '1')
            self.assertIsNone(process.poll())
            process.terminate()
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 37, error)
            self.assertEqual(signals.read_text(), '2')
            self.assertEqual(self.stopped.read_text(), str(signal.SIGTERM))
        finally:
            if process.poll() is None:
                if self.record.exists():
                    os.kill(json.loads(self.record.read_text())['pid'], signal.SIGKILL)
                process.kill(); process.communicate(timeout=2)

    def test_dripping_ping_listener_cannot_extend_absolute_probe_deadline(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            def drip():
                with listener.accept()[0] as peer:
                    try:
                        peer.recv(4096)
                        for _ in range(40):
                            peer.sendall(b'H')
                            time.sleep(.05)
                    except OSError:
                        pass
            worker = threading.Thread(target=drip, daemon=True)
            worker.start()
            start = time.monotonic()
            result = self.run_wrapper(path=self.root / 'missing', port=listener.getsockname()[1])
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertLess(elapsed, .8, 'probe must stop after 200 ms despite incoming bytes')
            worker.join(timeout=1)

    def test_shell_fallback_preserves_arguments_environment_and_exit(self):
        self.environment['LD_LIBRARY_PATH'] = '/tmp/fake Steam libraries'
        result = subprocess.run(['/bin/sh', str(ROOT / 'tools/nch-steam-launch'), '--companion', str(self.root / 'absent'), '--', sys.executable, str(self.game), 'space value', ''], env=self.environment, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 37)
        game = json.loads(self.record.read_text())
        self.assertEqual(game['argv'], ['space value', ''])
        self.assertEqual(game['env']['LD_LIBRARY_PATH'], '/tmp/fake Steam libraries')
