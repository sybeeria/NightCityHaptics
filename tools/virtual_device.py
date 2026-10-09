#!/usr/bin/env python3
"""Local-only output monitor for a simulated Intiface WebSocket device.

This receives hardware-protocol packets from the real Intiface Engine. It never
connects physical hardware or writes game telemetry. Requires
the `websockets` package (development only; the app itself needs none).
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import time
from urllib.parse import urlsplit

from websockets.asyncio.client import connect

IDENTIFIER = "NCHVirtual"
ADDRESS = "NCHSIM000001"
VIBRATE = re.compile(r"Vibrate:(\d+);\Z")
PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Night City Haptics · Virtual device</title>
<style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;color:#e7eff4;background:#0a1118}
body{max-width:880px;margin:0 auto;padding:28px 22px}h1{font-size:27px;margin:8px 0}
.tag{color:#65dde1;font-size:13px;text-transform:uppercase;letter-spacing:1.5px}
.quiet{color:#a0b2c0}p{line-height:1.5}section{background:#131f2a;border:1px solid #294052;border-radius:14px;padding:20px;margin:22px 0}
#value{font-size:72px;font-weight:700;line-height:1.15}.bar{height:20px;background:#263847;border-radius:10px;margin:14px 0;overflow:hidden}.bar div{height:100%;width:0;background:#56dbe0;transition:width .1s}
.stats{display:flex;gap:28px;flex-wrap:wrap;margin-top:18px}.stats strong{display:block;font-size:22px}canvas{width:100%;height:160px;background:#0e1822;border-radius:8px;margin:12px 0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 0;border-bottom:1px solid #294052;font-size:14px}th{color:#a0b2c0;font-weight:500}td:last-child{width:62%}#game{font-size:15px}.error{color:#ffc28a}
</style>
<body><div class="tag">Night City Haptics</div><h1>Virtual device · no hardware</h1>
<p>Start the companion → load a save → <b>Test buzz</b> → close CET.</p>
<p class="quiet">Drive, shoot, or trigger an explosion. The meter shows commands received by the virtual device.</p>
<section><div id="link" class="quiet">Connecting to monitor…</div><div id="value">0%</div>
<div class="bar"><div id="fill"></div></div><div id="game" class="quiet">Game: waiting for live updates</div>
<div class="stats"><div>Peak<strong id="peak">0%</strong></div><div>Commands received<strong id="count">0</strong></div></div>
<canvas id="chart" width="830" height="160" aria-label="Recent received intensity history"></canvas>
<div class="quiet">Recent output stays here when you switch away from the game.</div></section>
<section><b>Recent commands</b><table><thead><tr><th>Time</th><th>Output</th><th>Live game reactions</th></tr></thead><tbody id="rows"></tbody></table></section>
<p class="quiet">This checks game events and the companion connection. It does not test a physical device.</p>
<script>
const byId=id=>document.getElementById(id),percent=v=>(v*100).toFixed(1).replace(/\.0$/,'')+'%';
function show(data){
 byId('link').textContent=data.connected?'Connected to Intiface · receiving real commands':'Waiting for Intiface';
 byId('link').className=data.connected?'quiet':'error';
 byId('value').textContent=percent(data.current);byId('fill').style.width=percent(data.current);
 byId('peak').textContent=percent(data.peak);byId('count').textContent=data.command_count;
 byId('game').textContent=data.game.live?'Game: live · '+(data.game.active?'sampling':'feedback suspended'):'Game: no live updates';
 const rows=byId('rows');rows.replaceChildren();
 for(const item of data.history.filter(x=>x.received).slice(-10).reverse()){
  const row=document.createElement('tr');
  const labels=item.reactions.length?item.reactions.map(x=>x.label).join(', '):(item.game_live?'None / test pulse':'Game not live');
  for(const text of [new Date(item.time*1000).toLocaleTimeString(),percent(item.value),labels]){let cell=document.createElement('td');cell.textContent=text;row.append(cell)}rows.append(row);
 }
 if(!rows.children.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=3;cell.textContent='No commands yet';row.append(cell);rows.append(row)}
 const canvas=byId('chart'),ctx=canvas.getContext('2d'),history=data.history;
 ctx.clearRect(0,0,canvas.width,canvas.height);ctx.strokeStyle='#294052';ctx.lineWidth=1;
 for(const y of [20,80,140]){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(canvas.width,y);ctx.stroke()}
 ctx.fillStyle='#a0b2c0';ctx.font='12px system-ui';ctx.fillText('100%',5,15);ctx.fillText('0%',5,155);
 if(history.length){const start=history[0].time,end=Math.max(start+1,data.now);ctx.strokeStyle='#56dbe0';ctx.lineWidth=2;ctx.beginPath();history.forEach((item,i)=>{const x=(item.time-start)/(end-start)*canvas.width,y=140-item.value*120;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.lineTo(canvas.width,140-data.current*120);ctx.stroke()}
}
async function update(){try{const reply=await fetch('/api/status',{cache:'no-store'});if(!reply.ok)throw Error();show(await reply.json())}catch{byId('link').textContent='Monitor disconnected';byId('link').className='error'}finally{setTimeout(update,250)}}update();
</script></body></html>"""


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Monitor:
    def __init__(self, mod_dir=None, state_file=None, heartbeat_timeout=1.0):
        self.mod_dir = Path(mod_dir).resolve() if mod_dir else None
        self.state_file = Path(state_file).resolve() if state_file else None
        if self.mod_dir and self.state_file and self.state_file.is_relative_to(self.mod_dir):
            raise ValueError("The simulator receipt must be outside the installed mod folder")
        self.heartbeat_timeout = heartbeat_timeout
        self.lock = threading.RLock()
        self.connected = False
        self.current = self.peak = 0.0
        self.command_count = self.connection_count = self.packet_count = 0
        self.history = deque(maxlen=600)
        self.error = ""
        self.game_session = self.game_seq = self.game_changed = None
        self.game_live = self.game_active = False
        self.status_heartbeat = self.status_changed = None
        self.reactions = []
        self.labels = {}
        self.started = datetime.now(timezone.utc).isoformat()
        if self.mod_dir:
            try:
                catalog = json.loads((self.mod_dir / "companion/catalog.json").read_text(encoding="utf-8"))
                self.labels = {x["id"]: x.get("label", x["id"]) for x in catalog["interactions"]}
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def poll_game(self, now=None):
        now = time.monotonic() if now is None else now
        state = {}
        status = {}
        if self.mod_dir:
            try:
                path = self.mod_dir / "state.json"
                if path.stat().st_size <= 262144:
                    state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                pass
            try:
                path = self.mod_dir / "status.json"
                if path.stat().st_size <= 262144:
                    status = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                pass
        with self.lock:
            session = state.get("session") if isinstance(state, dict) else None
            seq = state.get("seq") if isinstance(state, dict) else None
            if not isinstance(session, str) or not session or type(seq) is not int or seq < 0:
                self.game_session = self.game_seq = self.game_changed = None
            elif session != self.game_session or self.game_seq is None or seq < self.game_seq:
                self.game_session, self.game_seq, self.game_changed = session, seq, None
            elif seq > self.game_seq:
                self.game_seq, self.game_changed = seq, now
            self.game_live = self.game_changed is not None and now - self.game_changed < self.heartbeat_timeout
            self.game_active = (self.game_live and state.get("active") is True
                                and state.get("paused") is not True)
            heartbeat = status.get("heartbeat") if isinstance(status, dict) else None
            if type(heartbeat) is not int or heartbeat < 0 or status.get("running") is not True:
                self.status_heartbeat = self.status_changed = None
            elif self.status_heartbeat is None or heartbeat < self.status_heartbeat:
                self.status_heartbeat, self.status_changed = heartbeat, None
            elif heartbeat > self.status_heartbeat:
                self.status_heartbeat, self.status_changed = heartbeat, now
            status_live = self.status_changed is not None and now - self.status_changed < 1.5
            contributions = status.get("contributions", {}) if status_live else {}
            self.reactions = []
            if isinstance(contributions, dict):
                for name, amount in contributions.items():
                    if isinstance(name, str) and type(amount) in (int, float) and math.isfinite(amount) and amount > 0:
                        key = name[4:] if name.startswith("try:") else name
                        label = self.labels.get(key, key)
                        self.reactions.append({"id": name, "label": "Try: " + label if name.startswith("try:") else label,
                                               "contribution": amount})
                self.reactions.sort(key=lambda x: x["contribution"], reverse=True)
                self.reactions = self.reactions[:8]

    def set_connection(self, connected, error=""):
        with self.lock:
            if connected:
                self.connection_count += 1
            self.connected = connected
            self.error = error
            # Socket loss must not leave a positive meter or restart old output.
            self.current = 0.0
            self.history.append({"time": time.time(), "value": 0.0, "received": False,
                                 "game_live": False, "reactions": []})

    def handle_packet(self, packet):
        if not isinstance(packet, (bytes, str)):
            return None
        text = packet.decode("ascii", errors="replace") if isinstance(packet, bytes) else packet
        with self.lock:
            self.packet_count += 1
            if text == "DeviceType;":
                return f"Z:10:{ADDRESS};".encode("ascii")
            if text == "Battery;":
                return b"100;"
            match = VIBRATE.fullmatch(text)
            if match and 0 <= int(match[1]) <= 20:
                self.current = int(match[1]) / 20.0
                self.peak = max(self.peak, self.current)
                self.command_count += 1
                self.history.append({"time": time.time(), "value": self.current, "received": True,
                    "sequence": self.command_count,
                    "packet": text, "transport": "binary" if isinstance(packet, bytes) else "text",
                    "game_live": self.game_live, "reactions": [dict(x) for x in self.reactions]})
        return None

    def snapshot(self):
        with self.lock:
            return {"kind": "intiface-websocket-virtual-device", "no_hardware": True,
                    "started_at_utc": self.started, "now": time.time(),
                    "connected": self.connected, "current": self.current, "peak": self.peak,
                    "command_count": self.command_count, "packet_count": self.packet_count,
                    "connection_count": self.connection_count, "last_error": self.error,
                    "history": list(self.history),
                    "game": {"live": self.game_live, "active": self.game_active,
                             "reactions": list(self.reactions)}}

    def save(self):
        if self.state_file:
            atomic_json(self.state_file, self.snapshot())


def start_http(monitor, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == "/":
                body, content_type = PAGE.encode("utf-8"), "text/html; charset=utf-8"
            elif path == "/api/status":
                body = json.dumps(monitor.snapshot(), allow_nan=False).encode("utf-8")
                content_type = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def receive_device(monitor, endpoint, stop):
    while not stop.is_set():
        try:
            async with connect(endpoint, open_timeout=3, close_timeout=1, ping_interval=5,
                               max_size=262144, proxy=None) as ws:
                await ws.send(json.dumps({"identifier": IDENTIFIER, "address": ADDRESS, "version": 0}))
                monitor.set_connection(True)
                while not stop.is_set():
                    try:
                        packet = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    reply = monitor.handle_packet(packet)
                    if reply is not None:
                        await ws.send(reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            monitor.set_connection(False, type(exc).__name__ + ": " + str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        finally:
            monitor.set_connection(False, monitor.error)


async def run(monitor, endpoint, port):
    loop, stop = asyncio.get_running_loop(), asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    server = start_http(monitor, port)
    monitor.save()
    print(f"Virtual device monitor: http://127.0.0.1:{server.server_port}/", flush=True)
    receiver = asyncio.create_task(receive_device(monitor, endpoint, stop))
    try:
        while not stop.is_set():
            monitor.poll_game()
            monitor.save()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        monitor.set_connection(False)
        monitor.game_live = monitor.game_active = False
        monitor.reactions = []
        monitor.save()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="ws://127.0.0.1:54817")
    parser.add_argument("--port", type=int, default=19875, help="Local HTTP monitor port; 0 chooses a free port")
    parser.add_argument("--mod-dir", type=Path, help="Read-only game telemetry folder")
    parser.add_argument("--state-file", type=Path, help="Simulator receipt outside the installed mod folder")
    args = parser.parse_args()
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme != "ws" or endpoint.hostname != "127.0.0.1" or endpoint.username or endpoint.password:
        parser.error("The virtual device endpoint must be ws://127.0.0.1:PORT")
    if not 0 <= args.port <= 65535:
        parser.error("The HTTP port must be between 0 and 65535")
    monitor = Monitor(args.mod_dir, args.state_file)
    asyncio.run(run(monitor, args.endpoint, args.port))


if __name__ == "__main__":
    main()
