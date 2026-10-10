#!/usr/bin/env python3
"""Night City Haptics companion: one app for Cyberpunk 2077 + Intiface devices.

Run start.sh (Linux/Steam Deck) or start.cmd (Windows), or `python3 nch.py`.
It connects to Intiface (starting Intiface Engine itself when nothing is
running), finds your devices and opens the control page in your browser.
Needs Python 3.9+ and nothing else.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import buttplug  # noqa: E402
import haptics  # noqa: E402

VERSION = "0.2.4"
PORT = 19870
TICK = 1 / 30
IDLE_TICK = 1 / 10   # while no game heartbeat is live and nothing plays
ENGINE_RELEASE = "5.0.4"
ENGINE_DOWNLOADS = {
    "linux-x86_64": ("intiface-engine-v5.0.4-linux-x64.zip",
                     "ea2b40eca8369e01edcaa00076315bde0d7352108d38c226b77f1818395d2e66"),
    "linux-aarch64": ("intiface-engine-v5.0.4-linux-arm64.zip",
                      "01536003a3075ddb896552b70d2024f19992099b9ea1aef93459f9084aacb7e2"),
    "windows-amd64": ("intiface-engine-v5.0.4-win-x64.zip",
                      "29ea7051b23f7a05124e65581df48c8600070c83adc0348f0738b9ba75b58782"),
    "darwin-arm64": ("intiface-engine-v5.0.4-macos-arm64.zip",
                     "430fbea694cbbde60e567c2f8e01c297db040259f02e541a5bf3d2abdbdf3d0d"),
}
ENGINE_URL = "https://github.com/buttplugio/buttplug/releases/download/intiface-engine-{0}/{1}"
SCAN_SECONDS = 30


def data_dir():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "NightCityHaptics"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "night-city-haptics"


def read_json(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def write_json(path, value):
    # Replace atomically so the game never reads half a file.
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1)
        os.replace(temporary, path)  # Windows refuses while the game has the file open
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class Engine:
    """Finds, downloads and runs Intiface Engine when no Intiface server is running."""
    def __init__(self):
        self.process = None
        self.log = None
        self.download_state = ""

    def platform_key(self):
        machine = platform.machine().lower()
        machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
        if os.name == "nt":
            return "windows-amd64"
        if sys.platform == "darwin":
            return "darwin-" + {"aarch64": "arm64"}.get(machine, machine)
        return f"linux-{machine}"

    def executable(self):
        name = "intiface-engine.exe" if os.name == "nt" else "intiface-engine"
        candidates = [os.environ.get("NCH_INTIFACE_ENGINE", ""),
                      str(data_dir() / f"intiface-engine-{ENGINE_RELEASE}" / name),
                      str(HERE / name), shutil.which("intiface-engine") or ""]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return Path(candidate)
        return None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, port):
        path = self.executable()
        if not path or self.running():
            return False
        arguments = [str(path), "--websocket-port", str(port), "--server-name", "Night City Haptics",
                     "--max-ping-time", "3000",
                     "--use-bluetooth-le", "--use-serial", "--use-lovense-dongle-hid",
                     "--use-lovense-dongle-serial", "--log", "info"]
        self.log = open(data_dir() / "intiface-engine.log", "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(arguments, stdout=self.log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=flags)
        logging.info("Started Intiface Engine: %s", path)
        return True

    def stop(self):
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def download(self):
        """Fetch the official release for this platform and check its SHA-256."""
        key = self.platform_key()
        if key not in ENGINE_DOWNLOADS:
            self.download_state = f"No Intiface Engine build for {key}; install Intiface Central instead"
            return
        name, digest = ENGINE_DOWNLOADS[key]
        target = data_dir() / f"intiface-engine-{ENGINE_RELEASE}"
        try:
            self.download_state = "Downloading Intiface Engine…"
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
                archive = Path(temporary) / name
                with urllib.request.urlopen(ENGINE_URL.format(ENGINE_RELEASE, name), timeout=60) as response:
                    archive.write_bytes(response.read())
                if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                    raise ValueError("download failed its checksum")
                unpacked = Path(temporary) / "engine"
                with zipfile.ZipFile(archive) as bundle:
                    for member in bundle.namelist():
                        if member.startswith("/") or ".." in Path(member).parts:
                            raise ValueError("unexpected file in the download")
                    bundle.extractall(unpacked)
                binary = next((p for p in unpacked.rglob("intiface-engine*") if p.is_file()
                               and p.suffix in ("", ".exe")), None)
                if binary is None:
                    raise ValueError("the download did not contain intiface-engine")
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(binary.parent), str(target))
                engine = target / binary.name
                engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
            self.download_state = ""
        except Exception as exc:  # shown in the page
            self.download_state = f"Download failed: {exc}"


class App:
    def __init__(self, mod_dir, settings_path=None):
        self.mod_dir = mod_dir
        self.catalog = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
        self.settings_path = settings_path or HERE / "settings.json"
        self.settings = haptics.normalize_settings(read_json(self.settings_path), self.catalog)
        self.lock = threading.RLock()
        self.mixer = haptics.Mixer(self.catalog)
        self.game = haptics.GameLink()
        self.outputs = buttplug.Outputs()
        self.engine = Engine()
        self.client = None
        self.device_errors = []
        self.link = "connecting"       # connecting | starting | connected | missing | error
        self.link_detail = ""
        self.next_connect = 0.
        self.scan_until = 0.
        self.scan_count = 0
        self.next_status = 0.
        self.started_engine_at = None
        self.history = []              # recent (time, output) samples for the page
        self.status_written = (None, 0.)  # (content, time) of the last status.json
        self.status_heartbeat = 0
        self.scene_key = None
        self.stopping = False
        self.output = 0.

    # settings ---------------------------------------------------------------
    def save(self, changes):
        with self.lock:
            if not isinstance(changes, dict):
                raise ValueError("Settings must be an object")
            merged = json.loads(json.dumps(self.settings))
            for key, value in changes.items():
                if key in ("interactions", "devices"):
                    if not isinstance(value, dict):
                        raise ValueError(key + " must be an object")
                    for name, options in value.items():
                        if not isinstance(name, str) or not isinstance(options, dict):
                            raise ValueError(key + " entries must be named objects")
                        merged[key].setdefault(name, {}).update(options)
                else:
                    merged[key] = value
            settings = haptics.normalize_settings(merged, self.catalog)
            address_changed = settings["intiface_address"] != self.settings["intiface_address"]
            # A rejected disk write must not silently change live output limits.
            write_json(self.settings_path, settings)
            self.settings = settings
            if address_changed:
                self.disconnect("Address changed")
                self.next_connect = 0.

    def reset(self):
        with self.lock:
            settings = json.loads(json.dumps(self.settings))
            settings["interactions"] = haptics.default_settings(self.catalog)["interactions"]
            write_json(self.settings_path, settings)
            self.settings = settings

    # Intiface connection --------------------------------------------------------
    def port(self):
        try:
            return int(self.settings["intiface_address"].rsplit(":", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            return 12345

    def disconnect(self, reason):
        if self.client:
            self.device_errors = (self.device_errors + list(self.client.errors))[-5:]
            try:
                self.client.stop_all()
            except Exception:
                pass
            finally:
                try:
                    self.client.close()
                except Exception:
                    pass
        self.client = None
        self.outputs.reset()
        self.mixer.clear()
        self.output = 0.
        self.scene_key = None
        self.link, self.link_detail = "connecting", reason

    def connect(self, now):
        if now < self.next_connect:
            return
        try:
            self.client = buttplug.Client(self.settings["intiface_address"])
        except (OSError, buttplug.ConnectionClosed, ValueError) as exc:
            self.client = None
            self.next_connect = now + 2
            if self.engine.running():
                self.link, self.link_detail = "starting", "Starting Intiface Engine…"
                if self.started_engine_at and now - self.started_engine_at > 20:
                    self.link, self.link_detail = "error", "Intiface Engine did not start; see intiface-engine.log"
            elif self.settings["start_intiface"] and self.engine.executable() and not self.engine.download_state:
                if self.engine.start(self.port()):
                    self.started_engine_at = now
                    self.link, self.link_detail = "starting", "Starting Intiface Engine…"
                    self.next_connect = now + .7
            elif self.engine.download_state.startswith("Downloading"):
                self.link, self.link_detail = "starting", self.engine.download_state
            elif self.settings["start_intiface"] and not self.engine.executable():
                self.link, self.link_detail = "missing", self.engine.download_state or "Intiface Engine is not installed yet"
            else:
                self.link, self.link_detail = "error", f"No Intiface server at {self.settings['intiface_address']} ({exc})"
            return
        self.link, self.link_detail = "connected", self.client.server_name
        logging.info("Connected to %s; %d device(s)", self.client.server_name, len(self.client.devices))
        if not self.client.devices:
            self.scan()

    def scan(self):
        """Look for devices until a new one appears or SCAN_SECONDS pass."""
        if self.client:
            self.client.start_scanning()
            self.scan_until = time.monotonic() + SCAN_SECONDS
            self.scan_count = len(self.client.devices)

    # main loop -----------------------------------------------------------------
    def tick(self, now):
        with self.lock:
            if self.client is None:
                self.connect(now)
            raw = read_json(self.mod_dir / "state.json")
            events = self.game.update(raw, now)
            active = self.game.active(now)
            live = self.game.live(now) and not self.game.state.get("paused")
            for name, magnitude, seconds in events:
                rule = self.settings["interactions"].get(name.partition(":")[0])
                if name == "test" and live:
                    self.mixer.trigger("test", 1, now, "double")
                elif active and rule:
                    self.mixer.game_event(name, magnitude, now, rule["feel"], seconds)
            if not active:
                self.mixer.clear(keep=("try:", "test"))
            else:
                self.sync_scene(now)
            output, _ = self.mixer.tick(now, self.game.levels(now), self.settings)
            self.output = output
            self.history.append(round(output, 3))
            del self.history[:-300]
            if self.client:
                try:
                    self.client.poll()
                    if self.client.scanning and (now > self.scan_until or len(self.client.devices) > self.scan_count):
                        self.client.stop_scanning()
                    self.outputs.update(self.client, output, self.settings["devices"], now)
                except (OSError, buttplug.ConnectionClosed) as exc:
                    logging.warning("Intiface connection lost: %s", exc)
                    self.disconnect(f"Connection lost: {exc}")
                    self.next_connect = now + 1
            if now >= self.next_status:
                self.next_status = now + .2
                self.write_status(now)

    def busy(self, now):
        """True while anything could need a fast tick: a live game or sound still playing."""
        return self.game.live(now) or bool(self.mixer.voices) or self.output > 0

    def sync_scene(self, now):
        """Restore the current scene at CET's simulation-time offset after a gate.

        Heartbeats before this protocol addition still use event-only playback.
        A scene is keyed by session and encounter, never by permanent quest facts.
        """
        if "scene" not in self.game.state:
            return
        scene = self.game.state["scene"]
        if scene is None:
            if self.scene_key is not None:
                self.mixer.release("romance", now)
            self.scene_key = None
            return
        key = (self.game.session, scene["instance"], scene["name"])
        rule = self.settings["interactions"]["romance"]
        track = "track:" + scene["name"]
        feel = track if rule["feel"] == "scene" else rule["feel"]
        effect = self.mixer.effects.get(feel)
        if track not in self.mixer.effects or not effect:
            self.scene_key = key
            return
        # CET elapsed measures simulation time at the accepted heartbeat. Let
        # it advance between reads, then realign when the next sequence arrives.
        start = self.game.changed - scene["elapsed"]
        if now - start > effect["points"][-1][0]:
            # A heartbeat still describing the same long encounter must never
            # restart a shorter custom/legacy envelope that already finished.
            self.mixer.voices.pop("romance", None)
            self.scene_key = key
            return
        voice = self.mixer.voices.get("romance")
        if self.scene_key != key or not voice or voice.get("feel") != feel:
            pulse = self.mixer.voices.get("romance#pulse")
            if self.scene_key != key and pulse and pulse["start"] < self.game.changed:
                self.mixer.voices.pop("romance#pulse", None)
            self.mixer.voices["romance"] = {"start": start, "magnitude": 1., "feel": feel, "scale": 1.}
        elif "release" not in voice:
            voice["start"] = start
        self.scene_key = key

    def write_status(self, now):
        # The overlay reads this; CET treats it as fresh for 3 s, so unchanged content is
        # rewritten once a second rather than five times.
        devices = [buttplug.summary(d)["name"] for d in self.client.devices.values()] if self.client else []
        status = {"v": 2, "running": not self.stopping, "link": self.link, "devices": devices,
                  "output": round(self.output, 2), "enabled": self.settings["enabled"],
                  "active": self.game.active(now),
                  "contributions": {name: round(value, 2) for name, value in self.mixer.contributions.items()}}
        content, written = self.status_written
        if status == content and now - written < 1:
            return
        try:
            heartbeat = self.status_heartbeat + 1
            write_json(self.mod_dir / "status.json", dict(status, heartbeat=heartbeat))
            self.status_heartbeat = heartbeat
            self.status_written = (status, now)
        except OSError:
            pass

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            game, game_detail = self.game.describe(now)
            devices = []
            for device in (self.client.devices.values() if self.client else []):
                info = buttplug.summary(device)
                info["settings"] = dict({"on": True, "strength": 1., "all_motors": False},
                                        **buttplug.device_options(device, self.settings["devices"]))
                devices.append(info)
            return {"version": VERSION, "settings": self.settings, "output": self.output,
                    "contributions": dict(self.mixer.contributions), "history": self.history[-150:],
                    "link": self.link, "link_detail": self.link_detail,
                    "scanning": bool(self.client and self.client.scanning), "devices": devices,
                    "device_errors": (self.device_errors + (list(self.client.errors) if self.client else []))[-5:],
                    "game": game, "game_detail": game_detail,
                    "engine_path": str(self.engine.executable() or ""), "engine_download": self.engine.download_state}

    def try_interaction(self, name):
        with self.lock:
            rule = self.settings["interactions"].get(name)
            if rule is None:
                raise ValueError("Unknown interaction")
            kind = self.mixer.kinds[name]
            if kind == "event":
                feel, seconds = rule["feel"], 4 if rule["feel"] == "slowbuild" else None
                if feel == "scene":
                    # A scene track squeezed into a few seconds gives its shape.
                    track = {"romance": "stout"}.get(name, name)
                    feel, seconds = "track:" + track, 8 if name == "romance" else None
                self.mixer.trigger("try:" + name, 1, time.monotonic(), feel, seconds)
            else:
                # Preview a level interaction as a 2.5 s burst of its texture.
                self.mixer.effects["_level:" + name] = {"points": [[0, 1], [2.5, 1], [2.6, 0]], "texture": rule["feel"]}
                self.mixer.trigger("try:" + name, 1, time.monotonic(), "_level:" + name)

    def test_device(self, key):
        with self.lock:
            if not self.client:
                raise ValueError("Intiface is not connected")
            for index, device in self.client.devices.items():
                if buttplug.summary(device)["key"] == key:
                    strength = buttplug.device_options(device, self.settings["devices"]).get("strength", 1.)
                    self.outputs.hold[index] = (round(.5 * strength, 3), time.monotonic() + .8)
                    return
        raise ValueError("Device not found")

    def run(self):
        while not self.stopping:
            started = time.monotonic()
            try:
                self.tick(started)
            except Exception:
                logging.exception("Unexpected error; stopping devices and disconnecting")
                with self.lock:
                    self.disconnect("Unexpected processing error; devices stopped")
                    self.game = haptics.GameLink()
                    self.next_connect = started + 1
                    self.write_status(started)
            period = TICK if self.busy(started) else IDLE_TICK
            time.sleep(max(0., period - (time.monotonic() - started)))
        with self.lock:
            self.disconnect("Stopped")
            self.engine.stop()
            try:
                write_json(self.mod_dir / "status.json", {"v": 2, "running": False, "link": "stopped",
                                                         "devices": [], "output": 0, "enabled": False,
                                                         "active": False, "contributions": {},
                                                         "heartbeat": self.status_heartbeat + 1})
            except OSError:
                pass


def make_server(app, port):
    page = HERE / "ui.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "NightCityHaptics/" + VERSION
        # Keep-alive: the page polls ten times a second, and a new connection plus
        # thread per poll cost more than the answer itself.
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, code, body, kind="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def local(self):
            # Blocks DNS-rebinding pages; POSTs also need a custom header, which
            # browsers never let another site send without a CORS preflight.
            host = self.headers.get("Host", "")
            return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.local():
                return self.reply(403, {"error": "Open the page from 127.0.0.1"})
            if self.path in ("/", "/index.html"):
                return self.reply(200, page.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                state = app.snapshot()
                state["catalog"] = app.catalog if self.headers.get("X-NCH-Catalog") else None
                return self.reply(200, state)
            if self.path == "/fonts/archivo.woff2":
                return self.reply(200, (HERE / "fonts/archivo.woff2").read_bytes(), "font/woff2")
            if self.path == "/api/ping":
                return self.reply(200, {"app": "night-city-haptics", "version": VERSION})
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            # Read the body first, even for a rejected request, so a kept-alive
            # connection never parses leftover body bytes as the next request.
            try:
                length = max(0, int(self.headers.get("Content-Length", "0")))
            except ValueError:
                length = 0
            data = self.rfile.read(min(length, 262144))
            if length > 262144:
                self.close_connection = True
            if not self.local() or self.headers.get("X-NCH") != "1":
                return self.reply(403, {"error": "Rejected"})
            try:
                body = json.loads(data or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Request body must be an object")
                action = self.path.rsplit("/", 1)[-1]
                if action == "settings":
                    app.save(body)
                elif action == "reset":
                    app.reset()
                elif action == "try":
                    app.try_interaction(body.get("id"))
                elif action == "test-device":
                    app.test_device(body.get("key"))
                elif action == "scan":
                    with app.lock:
                        app.scan()
                elif action == "download-engine":
                    threading.Thread(target=app.engine.download, daemon=True).start()
                    app.engine.download_state = "Downloading Intiface Engine…"
                elif action == "reconnect":
                    with app.lock:
                        app.disconnect("Reconnecting")
                        app.next_connect = 0.
                elif action == "quit":
                    app.stopping = True
                else:
                    return self.reply(404, {"error": "Unknown action"})
                self.reply(200, {"ok": True})
            except (ValueError, TypeError, OSError) as exc:
                self.reply(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def already_running(port):
    try:
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        with local.open(f"http://127.0.0.1:{port}/api/ping", timeout=1) as response:
            return json.load(response).get("app") == "night-city-haptics"
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mod-dir", type=Path, default=HERE.parent, help="the nightCityHaptics CET mod folder")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open the control page")
    parser.add_argument("--settings", type=Path, help="settings file (default: settings.json next to nch.py)")
    args = parser.parse_args()
    data_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(data_dir() / "companion.log", encoding="utf-8")])
    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):
        print("Night City Haptics is already running: " + url)
        if not args.no_browser:
            webbrowser.open(url)
        return
    app = App(args.mod_dir.resolve(), args.settings)
    server = make_server(app, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(app, "stopping", True))
    logging.info("Night City Haptics %s: %s", VERSION, url)
    if not args.no_browser:
        threading.Timer(.3, webbrowser.open, (url,)).start()
    try:
        app.run()
    finally:
        server.shutdown()
        server.server_close()
        logging.info("Stopped")


if __name__ == "__main__":
    main()
