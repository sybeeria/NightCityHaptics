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
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
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
from urllib.parse import urlsplit
import webbrowser
import zipfile

HERE = Path(__file__).resolve().parents[2] / "src/companion"
sys.path.insert(0, str(HERE))
import buttplug  # noqa: E402
import haptics  # noqa: E402

VERSION = "0.3.0"
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
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return None
        return json.loads(data)
    except (OSError, ValueError, UnicodeError, RecursionError):
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
        self.download_lock = threading.Lock()

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
        if not path or self.running() or self.download_lock.locked():
            return False
        arguments = [str(path), "--websocket-port", str(port), "--server-name", "Night City Haptics",
                     "--max-ping-time", "3000",
                     "--use-bluetooth-le", "--use-serial", "--use-lovense-dongle-hid",
                     "--use-lovense-dongle-serial", "--log", "info"]
        data_dir().mkdir(parents=True, exist_ok=True)
        if self.log:
            self.log.close()
        self.log = open(data_dir() / "intiface-engine.log", "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(arguments, stdout=self.log, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError:
            self.log.close()
            self.log = None
            self.process = None
            raise
        logging.info("Started Intiface Engine: %s", path)
        return True

    def stop(self):
        process, self.process = self.process, None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(3)
        except (OSError, subprocess.TimeoutExpired):
            logging.warning("Could not finish stopping the owned Intiface Engine")
        finally:
            if self.log:
                self.log.close()
                self.log = None

    def download(self):
        """Fetch the official release for this platform and check its SHA-256."""
        if not self.download_lock.acquire(blocking=False):
            return False
        self._download()
        return True

    def request_download(self):
        # Claim the download before starting its worker. A fast completion must
        # never be overwritten by the HTTP handler's initial status message.
        if not self.download_lock.acquire(blocking=False):
            return False
        self.download_state = "Downloading Intiface Engine…"
        try:
            threading.Thread(target=self._download, daemon=True).start()
        except BaseException:
            self.download_lock.release()
            self.download_state = "Download could not start"
            raise
        return True

    def _download(self):
        try:
            key = self.platform_key()
            if self.running():
                raise ValueError("Stop the running Intiface Engine before replacing it")
            if key not in ENGINE_DOWNLOADS:
                self.download_state = f"No Intiface Engine build for {key}; install Intiface Central instead"
                return
            name, digest = ENGINE_DOWNLOADS[key]
            target = data_dir() / f"intiface-engine-{ENGINE_RELEASE}"
            self.download_state = "Downloading Intiface Engine…"
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
                archive = Path(temporary) / name
                with urllib.request.urlopen(ENGINE_URL.format(ENGINE_RELEASE, name), timeout=60) as response:
                    size = 0
                    with archive.open("wb") as output:
                        while True:
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > 64 * 1024 * 1024:
                                raise ValueError("engine download is too large")
                            output.write(chunk)
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
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
                previous = Path(temporary) / "previous"
                if target.exists():
                    target.replace(previous)
                try:
                    shutil.move(str(binary.parent), str(target))
                except BaseException:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    if previous.exists():
                        previous.replace(target)
                    raise
            self.download_state = ""
        except Exception as exc:  # shown in the page
            self.download_state = f"Download failed: {exc}"
        finally:
            self.download_lock.release()


class App:
    def __init__(self, mod_dir, settings_path=None):
        self.mod_dir = mod_dir
        self.catalog = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
        # Each feel's shape, drawn next to its picker on the control page.
        self.previews = {kind: {f["id"]: haptics.preview(self.catalog, kind, f["id"]) for f in feels}
                         for kind, feels in self.catalog["feels"].items()}
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
            def validate(options, allowed):
                for key, value in options.items():
                    if key not in allowed:
                        raise ValueError("Unknown setting: " + key)
                    if key in ("enabled", "start_intiface", "on", "all_motors") and type(value) is not bool:
                        raise ValueError(key + " must be true or false")
                    if key == "outputs" and (not isinstance(value, dict) or any(
                            name not in haptics.CHANNELS or type(on) is not bool for name, on in value.items())):
                        raise ValueError("outputs must map device outputs to true or false")
                    if key in ("max_strength", "strength"):
                        try:
                            valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
                        except OverflowError:
                            valid = False
                        if not valid:
                            raise ValueError(key + " must be a finite number between 0 and 1")
            validate(changes, {"enabled", "start_intiface", "max_strength", "intiface_address", "interactions", "devices"})
            if "intiface_address" in changes:
                address = changes["intiface_address"]
                if not isinstance(address, str) or not address.startswith("ws://") or len(address) >= 200 or any(ord(c) < 33 or ord(c) == 127 for c in address):
                    raise ValueError("Invalid Intiface address")
                parts = urlsplit(address)
                if (parts.scheme != "ws" or not parts.hostname or parts.username is not None
                        or parts.password is not None or parts.fragment or parts.port == 0):
                    raise ValueError("Invalid Intiface address")
                path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
                if any(ord(c) > 126 for c in path):
                    raise ValueError("Invalid Intiface path")
                try:
                    hostname = parts.hostname.encode("idna").decode("ascii")
                except UnicodeError as exc:
                    raise ValueError("Invalid Intiface host") from exc
                if any(ord(c) < 33 or ord(c) > 126 for c in hostname):
                    raise ValueError("Invalid Intiface host")
            merged = json.loads(json.dumps(self.settings))
            for key, value in changes.items():
                if key in ("interactions", "devices"):
                    if not isinstance(value, dict):
                        raise ValueError(key + " must be an object")
                    for name, options in value.items():
                        if not isinstance(name, str) or not isinstance(options, dict):
                            raise ValueError(key + " entries must be named objects")
                        validate(options, {"on", "strength", "feel"} if key == "interactions" else {"on", "strength", "all_motors", "outputs"})
                        if key == "interactions":
                            item = next((item for item in self.catalog["interactions"] if item["id"] == name), None)
                            if item is None:
                                raise ValueError("Unknown interaction: " + name)
                            if "feel" in options and (not isinstance(options["feel"], str)
                                    or options["feel"] not in haptics.feel_ids(self.catalog, item)):
                                raise ValueError("Invalid feel for " + name)
                        target = merged[key].setdefault(name, {})
                        for option, value in options.items():
                            if option == "outputs":
                                target["outputs"] = dict(target.get("outputs") or {}, **value)
                            else:
                                target[option] = value
                else:
                    merged[key] = value
            settings = haptics.normalize_settings(merged, self.catalog)
            address_changed = settings["intiface_address"] != self.settings["intiface_address"]
            # A rejected disk write must not silently change live output limits.
            write_json(self.settings_path, settings)
            self.settings = settings
            if address_changed:
                self.disconnect("Address changed")
                self.engine.stop()  # only the engine process this app owns
                self.started_engine_at = None
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
            return urlsplit(self.settings["intiface_address"]).port or 80
        except ValueError:
            return 12345

    def local_engine_address(self):
        try:
            address = urlsplit(self.settings["intiface_address"])
            return (address.scheme == "ws" and address.hostname in ("127.0.0.1", "localhost")
                    and not address.username and not address.password)
        except ValueError:
            return False

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
                if self.started_engine_at is not None and now - self.started_engine_at > 20:
                    self.link, self.link_detail = "error", "Intiface Engine did not start; see intiface-engine.log"
            elif (self.settings["start_intiface"] and self.local_engine_address()
                  and self.engine.executable() and not self.engine.download_lock.locked()):
                if self.engine.start(self.port()):
                    self.started_engine_at = now
                    self.link, self.link_detail = "starting", "Starting Intiface Engine…"
                    self.next_connect = now + .7
            elif self.engine.download_state.startswith("Downloading"):
                self.link, self.link_detail = "starting", self.engine.download_state
            elif self.settings["start_intiface"] and self.local_engine_address() and not self.engine.executable():
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
            self.history.append((now, round(output, 3)))
            self.history = [(stamp, value) for stamp, value in self.history[-300:] if now - stamp <= 5]
            if self.client:
                try:
                    self.client.poll()
                    if self.client.scanning and (now > self.scan_until or len(self.client.devices) > self.scan_count):
                        self.client.stop_scanning()
                    # Nothing can play: suction/heat stop at once instead of easing out.
                    quiet = not self.settings["enabled"] or (not active and not self.mixer.voices)
                    self.outputs.update(self.client, output, self.settings["devices"], now, quiet)
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

    def output_history(self, now):
        # Keep the graph's five-second window accurate at both idle and live rates.
        values = [0.] * 150
        for stamp, value in self.history:
            if 0 <= now - stamp < 5:
                index = min(149, int((stamp - (now - 5)) * 30))
                values[index] = max(values[index], value)
        return values

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            game, game_detail = self.game.describe(now)
            devices = []
            for device in (self.client.devices.values() if self.client else []):
                info = buttplug.summary(device)
                options = buttplug.device_options(device, self.settings["devices"])
                enabled = buttplug.enabled_channels(device, options)
                info["settings"] = {"on": options.get("on") is not False, "strength": options.get("strength", 1.),
                                    "outputs": {channel: channel in enabled for channel in info["channels"]}}
                info["battery"] = self.client.battery.get(device["DeviceIndex"])
                devices.append(info)
            return {"version": VERSION, "settings": self.settings, "output": self.output,
                    "contributions": dict(self.mixer.contributions), "history": self.output_history(now),
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

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

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
                state["catalog"] = dict(app.catalog, previews=app.previews) if self.headers.get("X-NCH-Catalog") else None
                return self.reply(200, state)
            if self.path == "/fonts/archivo.woff2":
                return self.reply(200, (HERE / "fonts/archivo.woff2").read_bytes(), "font/woff2")
            if self.path == "/api/ping":
                return self.reply(200, {"app": "night-city-haptics", "version": VERSION})
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            # Reject ambiguous or oversized framing before dispatching an action.
            # Close rejected connections so no unread bytes become a new request.
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                self.close_connection = True
                return self.reply(400, {"error": "A valid Content-Length is required"})
            try:
                length = int(lengths[0])
            except ValueError:
                self.close_connection = True
                return self.reply(400, {"error": "Invalid Content-Length"})
            if length > 262144:
                self.close_connection = True
                return self.reply(413, {"error": "Request body is too large"})
            data = self.rfile.read(length)
            if len(data) != length:
                self.close_connection = True
                return self.reply(400, {"error": "Incomplete request body"})
            if not self.local() or self.headers.get("X-NCH") != "1":
                return self.reply(403, {"error": "Rejected"})
            try:
                body = json.loads(data or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Request body must be an object")
                if not self.path.startswith("/api/") or self.path.count("/") != 2:
                    return self.reply(404, {"error": "Unknown action"})
                action = self.path[5:]
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
                    with app.lock:
                        app.engine.request_download()
                elif action == "reconnect":
                    with app.lock:
                        app.disconnect("Reconnecting")
                        app.next_connect = 0.
                elif action == "quit":
                    app.stopping = True
                else:
                    return self.reply(404, {"error": "Unknown action"})
                self.reply(200, {"ok": True})
            except (ValueError, TypeError, OSError, RecursionError) as exc:
                self.reply(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def already_running(port):
    try:
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        with local.open(f"http://127.0.0.1:{port}/api/ping", timeout=1) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            return False
        reply = json.loads(payload)
        return isinstance(reply, dict) and reply.get("app") == "night-city-haptics"
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mod-dir", type=Path, default=HERE.parent, help="the nightCityHaptics CET mod folder")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open the control page")
    parser.add_argument("--settings", type=Path, help="settings file (default: settings.json next to nch.py)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
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
