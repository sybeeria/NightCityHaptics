"""Isolated native-launcher fixtures: fake children and loopback HTTP only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time


COMPANION = r'''import argparse, json, os, signal
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
        time.sleep(.02)
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
