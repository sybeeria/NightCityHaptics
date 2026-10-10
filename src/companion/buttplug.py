"""Minimal Buttplug v3 client over a standard-library WebSocket. No dependencies.

Only ws:// on a local machine is needed: Intiface Engine and Intiface Central
both listen on ws://127.0.0.1:12345 by default.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import select
import socket
import struct
import time
from urllib.parse import urlsplit

SCALAR_TYPES = ("Vibrate", "Oscillate", "Constrict", "Inflate")
MAX_MESSAGE = 4 * 1024 * 1024
MAX_BUFFER = MAX_MESSAGE + 65536 + 14
RECV_BYTES = 256 * 1024
RECV_FRAMES = 64
RECV_WORK = .01
OUTPUT_COMMANDS = {"ScalarCmd", "RotateCmd", "LinearCmd", "StopDeviceCmd", "StopAllDevices"}
MAX_PROTOCOL_ID = 2 ** 32 - 1
MAX_PENDING = 512
REPLY_KINDS = {"RequestServerInfo": "ServerInfo", "RequestDeviceList": "DeviceList",
               **{kind: "Ok" for kind in OUTPUT_COMMANDS | {"Ping", "StartScanning", "StopScanning"}}}


def protocol_uint(value):
    return type(value) is int and 0 <= value <= MAX_PROTOCOL_ID


def protocol_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Nonfinite Intiface number")
    return value


def protocol_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate Intiface JSON field")
        result[key] = value
    return result


def protocol_constant(text):
    raise ValueError("Invalid Intiface JSON number")


def protocol_integer(text):
    # V3 integers fit in 64 bits. Leave ample room while bounding conversion
    # work even on Python versions/settings without the integer digit limit.
    if len(text) > 64:
        raise ValueError("Intiface integer too large")
    return int(text)


class ConnectionClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=2.):
        if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError("Invalid WebSocket address")
        parts = urlsplit(url)
        if (parts.scheme != "ws" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment):
            raise ValueError("Intiface address must look like ws://127.0.0.1:12345")
        port = parts.port if parts.port is not None else 80
        if port < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid WebSocket port or timeout")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if any(ord(c) < 33 or ord(c) > 126 for c in path):
            raise ValueError("Invalid WebSocket path")
        hostname = parts.hostname.encode("idna").decode("ascii")
        if any(ord(c) < 33 or ord(c) > 126 for c in hostname):
            raise ValueError("Invalid WebSocket host")
        host = f"[{hostname}]" if ":" in hostname else hostname
        deadline = time.monotonic() + timeout
        self.sock = socket.create_connection((hostname, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self._handshake_timeout(deadline)
            self.sock.sendall(request.encode())
            response = b""
            while b"\r\n\r\n" not in response:
                self._handshake_timeout(deadline)
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionClosed("Intiface closed the connection during the handshake")
                response += chunk
                if len(response) > 65536:
                    raise ConnectionClosed("Invalid handshake response")
            self._handshake_timeout(deadline)
            head, self.buffer = response.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            status = lines[0].split(" ", 2)
            if len(status) < 2 or status[:2] != ["HTTP/1.1", "101"]:
                raise ConnectionClosed(f"Not a WebSocket server: {lines[0]}")
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            headers = {}
            for line in lines[1:]:
                name, colon, value = line.partition(":")
                if not colon or not name or any(not (c.isascii() and (c.isalnum() or c == "-")) for c in name):
                    raise ConnectionClosed("Invalid WebSocket response header")
                name, value = name.lower(), value.strip()
                if name in headers and name in ("sec-websocket-accept", "sec-websocket-protocol", "sec-websocket-extensions", "upgrade"):
                    raise ConnectionClosed("Duplicate WebSocket response header")
                headers[name] = headers[name] + "," + value if name in headers else value
            if (headers.get("sec-websocket-accept") != accept
                    or headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in {v.strip().lower() for v in headers.get("connection", "").split(",")}
                    or headers.get("sec-websocket-extensions") or headers.get("sec-websocket-protocol")):
                raise ConnectionClosed("WebSocket handshake failed")
            self.sock.setblocking(False)
            self.fragments = bytearray()
            self.fragmenting = False
        except Exception:
            self.sock.close()
            raise

    def _handshake_timeout(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectionClosed("WebSocket handshake timed out")
        self.sock.settimeout(remaining)

    def _abort(self, message):
        self.sock.close()
        raise ConnectionClosed(message)

    def _send_frame(self, opcode, payload, deadline=None):
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        data = header + mask + masked
        deadline = time.monotonic() + 2 if deadline is None else deadline
        while data:
            if time.monotonic() >= deadline:
                self._abort("Send timed out")
            try:
                sent = self.sock.send(data)
                if sent == 0:
                    self._abort("Intiface closed the connection")
                data = data[sent:]
            except BlockingIOError:
                select.select([], [self.sock], [], min(.05, max(0., deadline - time.monotonic())))

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def _frame(self):
        """Parse one frame from the buffer, or return None if incomplete."""
        data = self.buffer
        if len(data) < 2:
            return None
        fin, opcode = data[0] & 0x80, data[0] & 0x0F
        masked, length, offset = data[1] & 0x80, data[1] & 0x7F, 2
        if data[0] & 0x70 or masked or opcode not in (0, 1, 8, 9, 10):
            self._abort("Invalid WebSocket frame")
        if length == 126:
            if len(data) < 4:
                return None
            length, offset = struct.unpack(">H", data[2:4])[0], 4
            if length < 126:
                self._abort("Invalid WebSocket frame length")
        elif length == 127:
            if len(data) < 10:
                return None
            length, offset = struct.unpack(">Q", data[2:10])[0], 10
            if length < 65536:
                self._abort("Invalid WebSocket frame length")
        if length > MAX_MESSAGE:
            self._abort("Frame too large")
        if opcode >= 8:
            if not fin or length > 125 or (opcode == 8 and length == 1):
                self._abort("Invalid WebSocket control frame")
        elif opcode == 0 and not self.fragmenting:
            self._abort("Unexpected WebSocket continuation")
        elif opcode == 1 and self.fragmenting:
            self._abort("Unfinished WebSocket message")
        if opcode < 8 and len(self.fragments) + length > MAX_MESSAGE:
            self._abort("Message too large")
        if len(data) < offset + length:
            return None
        payload = data[offset:offset + length]
        self.buffer = data[offset + length:]
        return fin, opcode, payload

    def recv(self, timeout=0.):
        """Return one text message, or None if nothing arrives within timeout."""
        deadline = time.monotonic() + timeout
        work_deadline = time.monotonic() + max(timeout, RECV_WORK)
        received, processed = 0, 0
        while True:
            if processed >= RECV_FRAMES or received >= RECV_BYTES or time.monotonic() >= work_deadline:
                return None
            frame = self._frame()
            while frame:
                processed += 1
                fin, opcode, payload = frame
                if opcode == 0x9:
                    self._send_frame(0xA, payload, deadline=work_deadline)
                elif opcode == 0x8:
                    self._abort("Intiface closed the connection")
                elif opcode in (0x1, 0x0):
                    self.fragments.extend(payload)
                    self.fragmenting = not fin
                    if fin:
                        message = bytes(self.fragments)
                        self.fragments.clear()
                        try:
                            return message.decode("utf-8")
                        except UnicodeError:
                            self._abort("Invalid WebSocket text")
                if processed >= RECV_FRAMES or time.monotonic() >= work_deadline:
                    return None
                frame = self._frame()
            # A zero timeout still polls the socket once.
            ready, _, _ = select.select([self.sock], [], [], max(0., deadline - time.monotonic()))
            if not ready:
                return None
            try:
                chunk = self.sock.recv(min(65536, RECV_BYTES - received))
            except BlockingIOError:
                continue
            if not chunk:
                self._abort("Intiface closed the connection")
            received += len(chunk)
            if len(self.buffer) + len(chunk) > MAX_BUFFER:
                self._abort("WebSocket receive buffer too large")
            self.buffer += chunk

    def close(self):
        try:
            self._send_frame(0x8, b"\x03\xe8")
        except Exception:
            pass
        self.sock.close()


def features(device, message, types=None):
    """Indices into the device's ORIGINAL capability array (never renumbered)."""
    items = device.get("DeviceMessages", {}).get(message, [])
    if not isinstance(items, list):
        return []
    return [i for i, f in enumerate(items) if isinstance(f, dict) and (types is None or f.get("ActuatorType") in types)]


class Client:
    """Buttplug v3 client. Commands are fire-and-forget; replies are matched by id."""
    def __init__(self, url, name="Night City Haptics"):
        self.ws = WebSocket(url)
        self.next_id = 0
        self.pending = {}   # id -> (deadline, device index or None)
        self.pending_kind = {}
        self.devices = {}   # index -> device dict
        self.scanning = False
        self.scan_id = None
        self.closed = False
        self.errors = []
        self.ping_every = 0.
        self.next_ping = float("inf")
        try:
            info = self.request("RequestServerInfo", {"ClientName": name, "MessageVersion": 3})
            if info[0] != "ServerInfo" or type(info[1].get("MessageVersion")) is not int or info[1]["MessageVersion"] != 3:
                raise ConnectionClosed("Intiface did not accept Buttplug message version 3")
            name = info[1].get("ServerName")
            max_ping = info[1].get("MaxPingTime", 0)
            if (name is not None and (not isinstance(name, str) or len(name) > 256)) or not protocol_uint(max_ping):
                raise ConnectionClosed("Invalid Intiface server information")
            self.server_name = name or "Intiface"
            if max_ping > 0:
                self.ping_every = max_ping / 1000 / 2
                self.next_ping = time.monotonic() + self.ping_every
            kind, data = self.request("RequestDeviceList", {})
            if kind != "DeviceList" or not isinstance(data.get("Devices"), list) or len(data["Devices"]) > 128:
                raise ConnectionClosed("Invalid Intiface device list")
            listed = set()
            for device in data.get("Devices", []):
                self._add(device)
                if device["DeviceIndex"] in listed:
                    self._fail("Duplicate Intiface device index")
                listed.add(device["DeviceIndex"])
        except Exception:
            self.ws.close()
            raise

    def _add(self, device):
        if (not isinstance(device, dict) or not protocol_uint(device.get("DeviceIndex"))
                or not isinstance(device.get("DeviceName"), str)
                or len(device["DeviceName"]) > 256 or not isinstance(device.get("DeviceMessages"), dict)):
            self._fail("Invalid Intiface device")
        display = device.get("DeviceDisplayName")
        if display is not None and (not isinstance(display, str) or len(display) > 256):
            self._fail("Invalid Intiface device display name")
        for command in ("ScalarCmd", "RotateCmd", "LinearCmd"):
            attrs = device["DeviceMessages"].get(command, [])
            if not isinstance(attrs, list) or len(attrs) > 64 or any(not isinstance(a, dict) for a in attrs):
                self._fail("Invalid Intiface device capabilities")
            for attr in attrs:
                if (("StepCount" in attr and not protocol_uint(attr["StepCount"]))
                        or (command == "ScalarCmd" and "ActuatorType" not in attr)
                        or ("ActuatorType" in attr and (not isinstance(attr["ActuatorType"], str)
                            or len(attr["ActuatorType"]) > 64))):
                    self._fail("Invalid Intiface actuator attributes")
        gap = device.get("DeviceMessageTimingGap", 0)
        if not protocol_uint(gap):
            self._fail("Invalid Intiface device timing gap")
        if device["DeviceIndex"] not in self.devices and len(self.devices) >= 128:
            self._fail("Too many Intiface devices")
        self.devices[device["DeviceIndex"]] = device

    def _fail(self, message):
        # A rejected/unanswered output cannot be treated as an accepted stop.
        # Connection cleanup also invokes the server's client-disconnect stop path.
        if not self.closed:
            # Reserve room for the final stop even after a command flood.
            self.pending.clear()
            self.pending_kind.clear()
            try:
                self.stop_all()
            except Exception:
                pass
            finally:
                self.close()
        raise ConnectionClosed(message)

    def send(self, kind, fields, device=None, timeout=2.):
        if self.closed:
            raise ConnectionClosed("Intiface connection is closed")
        if len(self.pending) >= MAX_PENDING:
            self._fail("Too many pending Intiface commands")
        # Wrapping IDs must not overwrite an outstanding response match.
        for _ in range(len(self.pending) + 1):
            self.next_id = self.next_id % MAX_PROTOCOL_ID + 1
            if self.next_id not in self.pending:
                break
        ident = self.next_id
        self.ws.send(json.dumps([{kind: dict(fields, Id=ident)}], allow_nan=False))
        self.pending[ident] = (time.monotonic() + timeout, device)
        self.pending_kind[ident] = kind
        return ident

    def _handle(self, text):
        replies = {}
        try:
            batch = json.loads(text, parse_int=protocol_integer, parse_float=protocol_float,
                               parse_constant=protocol_constant, object_pairs_hook=protocol_object)
        except (ValueError, RecursionError):
            self._fail("Invalid Intiface JSON")
        if not isinstance(batch, list) or not batch or len(batch) > 128:
            self._fail("Invalid Intiface message batch")
        for message in batch:
            if not isinstance(message, dict) or len(message) != 1:
                self._fail("Invalid Intiface message")
            kind, data = next(iter(message.items()))
            if not isinstance(data, dict):
                self._fail("Invalid Intiface message fields")
            ident = data.get("Id")
            if not protocol_uint(ident):
                self._fail("Invalid Intiface message id")
            if kind in ("DeviceAdded", "DeviceRemoved", "ScanningFinished") and ident != 0:
                self._fail("Invalid Intiface system message id")
            if kind == "DeviceAdded":
                self._add(data)
            elif kind == "DeviceRemoved":
                index = data.get("DeviceIndex")
                if not protocol_uint(index):
                    self._fail("Invalid Intiface device index")
                self.devices.pop(index, None)
                for pending_id, (_, pending_device) in list(self.pending.items()):
                    if pending_device == index:
                        self.pending.pop(pending_id)
                        self.pending_kind.pop(pending_id, None)
            elif kind == "ScanningFinished":
                self.scanning = False
            elif kind == "Error":
                if not isinstance(data.get("ErrorMessage"), str) or not protocol_uint(data.get("ErrorCode")):
                    self._fail("Invalid Intiface error")
                error = data["ErrorMessage"][:200]
                self.errors.append(error)
                del self.errors[:-5]
                if ident == 0 or data["ErrorCode"] in (1, 2):
                    self._fail(error)
            if ident in self.pending:
                command = self.pending_kind.pop(ident, None)
                del self.pending[ident]
                if kind == "Error":
                    if command == "StartScanning" and ident == self.scan_id:
                        self.scanning = False
                    if command in OUTPUT_COMMANDS or command in ("Ping", "StopScanning"):
                        self._fail(data["ErrorMessage"][:200])
                else:
                    expected = REPLY_KINDS.get(command)
                    if expected and kind != expected:
                        self._fail("Intiface did not acknowledge output" if command in OUTPUT_COMMANDS
                                   else f"Invalid Intiface reply to {command}")
                replies[ident] = (kind, data)
        return replies

    def _maintain(self, now):
        for ident, (deadline, _) in list(self.pending.items()):
            if now >= deadline:
                command = self.pending_kind.get(ident, "command")
                self._fail(f"Intiface did not answer {command}")
        if now >= self.next_ping:
            self.send("Ping", {})
            self.next_ping = now + self.ping_every

    def request(self, kind, fields, timeout=3.):
        deadline = time.monotonic() + timeout
        ident = self.send(kind, fields, timeout=timeout)
        while time.monotonic() < deadline:
            now = time.monotonic()
            self._maintain(now)
            wake = min(deadline, self.next_ping, *(d for d, _ in self.pending.values()))
            text = self.ws.recv(max(0., wake - now))
            if text is None:
                continue
            reply = self._handle(text).get(ident)
            if reply:
                if reply[0] == "Error":
                    self._fail(reply[1]["ErrorMessage"][:200])
                return reply
        self._fail(f"Intiface did not answer {kind}")

    def poll(self):
        work_deadline = time.monotonic() + RECV_WORK
        for _ in range(64):
            text = self.ws.recv(0)
            if text is None:
                break
            self._handle(text)
            if time.monotonic() >= work_deadline:
                break
        self._maintain(time.monotonic())

    def busy(self, index):
        return any(device == index for _, device in self.pending.values())

    def start_scanning(self):
        if not self.scanning:
            self.scan_id = self.send("StartScanning", {})
            self.scanning = True

    def stop_scanning(self):
        if self.scanning:
            self.scan_id = self.send("StopScanning", {})
            self.scanning = False

    def stop_all(self):
        self.send("StopAllDevices", {})

    def close(self):
        self.closed = True
        self.scanning = False
        self.scan_id = None
        self.pending.clear()
        self.pending_kind.clear()
        self.ws.close()


def summary(device):
    scalar = device.get("DeviceMessages", {}).get("ScalarCmd", [])
    motors = [f.get("ActuatorType", "") for f in scalar if isinstance(f, dict)]
    motors += ["Rotate"] * len(features(device, "RotateCmd"))
    motors += ["Linear"] * len(features(device, "LinearCmd"))
    return {"index": device["DeviceIndex"], "name": device.get("DeviceDisplayName") or device.get("DeviceName", "Device"),
            "key": "device:" + str(device["DeviceIndex"]), "settings_key": settings_key(device), "motors": motors,
            "extra_motors": any(m != "Vibrate" for m in motors)}


def settings_key(device):
    """Named units keep preferences across reindexing; unnamed units need an index.

    V3 provides no persistent physical identity for identical unnamed devices.
    Keep old model-name preferences as a fallback, never as the live Test token.
    """
    return "device:" + json.dumps([device.get("DeviceName", ""),
                                   device.get("DeviceDisplayName") or device["DeviceIndex"]],
                                  separators=(",", ":"))


def device_options(device, settings):
    options = settings.get(settings_key(device), settings.get(device.get("DeviceName", ""), {}))
    return options if isinstance(options, dict) else {}


class Outputs:
    """Coalesces ordinary output per command type; stops always take priority.

    Client owns acknowledgements and closes on failed/unanswered output. The
    local strength cache is desired output, never evidence of hardware acceptance.
    """
    def __init__(self):
        self.sent = {}     # index -> (coalesced value, time)
        self.stroke = {}   # index -> (moving up, next stroke time)
        self.hold = {}     # index -> (value, until): a device test overrides the mix briefly
        self.selection = {}
        self.queues = {}
        self.last_send = {}
        self.identity = {}

    def reset(self):
        self.sent.clear()
        self.stroke.clear()
        self.hold.clear()
        self.selection.clear()
        self.queues.clear()
        self.last_send.clear()
        self.identity.clear()

    def _forget(self, index):
        for state in (self.sent, self.stroke, self.hold, self.selection, self.queues, self.last_send, self.identity):
            state.pop(index, None)

    def update(self, client, strength, device_settings, now):
        for index in set(self.identity) | set(self.hold):
            if index not in client.devices:
                self._forget(index)
        for index, device in client.devices.items():
            if index in self.identity and self.identity[index] is not device:
                # The index may now advertise fewer capabilities. A stop covers
                # the old actuator selection before any replacement output.
                self._stop(client, device, now)
                self._forget(index)
                self.last_send[index] = now
            self.identity[index] = device
            options = device_options(device, device_settings)
            value = 0. if options.get("on") is False else strength * options.get("strength", 1.)
            if index in self.hold:
                if now < self.hold[index][1]:
                    value = self.hold[index][0]
                else:
                    del self.hold[index]
            value = round(max(0., min(1., value)), 3)
            previous, last = self.sent.get(index, (None, 0.))
            gap = device.get("DeviceMessageTimingGap", 0)
            gap = gap / 1000 if isinstance(gap, (int, float)) and math.isfinite(gap) and gap > 0 else 0.
            all_motors = options.get("all_motors") is True
            changed_selection = index in self.selection and all_motors != self.selection[index]
            stopped = False
            if changed_selection:
                self._stop(client, device, now)
                stopped = True
                self.sent.pop(index, None)
                previous = None
            self.selection[index] = all_motors
            if value <= 0:
                if previous != 0.:
                    if not stopped:
                        self._stop(client, device, now)
                    self.sent[index] = (0., now)
                continue
            changed = value != previous
            if (changed and previous is not None and abs(value - previous) < .01
                    and now - last < 1. and not changed_selection):
                changed = False
            due = all_motors and bool(features(device, "LinearCmd")) and self._stroke_due(index, now)
            if changed or changed_selection or due:
                self._queue(device, value if changed else previous, all_motors, changed, due)
                if changed:
                    self.sent[index] = (value, now)
            queue = self.queues.get(index, {})
            while queue and not client.busy(index):
                if now - self.last_send.get(index, -float("inf")) < gap:
                    break
                kind = next(iter(queue))
                fields, motion = queue.pop(kind)
                client.send(kind, fields, index)
                self.last_send[index] = now
                if motion:
                    up, duration = motion
                    self.stroke[index] = (up, now + duration / 1000)

    def _stroke_due(self, index, now):
        return now >= self.stroke.get(index, (0, 0))[1]

    def _stop(self, client, device, now):
        index = device["DeviceIndex"]
        self.queues.pop(index, None)
        self.stroke.pop(index, None)
        # Explicitly stop ALL capabilities, including ones no longer selected.
        client.send("StopDeviceCmd", {"DeviceIndex": index}, index)
        scalars = features(device, "ScalarCmd", SCALAR_TYPES)
        if scalars:
            attrs = device["DeviceMessages"]["ScalarCmd"]
            client.send("ScalarCmd", {"DeviceIndex": index, "Scalars": [
                {"Index": i, "Scalar": 0., "ActuatorType": attrs[i]["ActuatorType"]} for i in scalars]}, index)
        self.last_send[index] = now

    def _queue(self, device, value, all_motors, changed, due):
        index = device["DeviceIndex"]
        queue = self.queues.setdefault(index, {})
        types = SCALAR_TYPES if all_motors else ("Vibrate",)
        scalars = features(device, "ScalarCmd", types)
        attributes = device.get("DeviceMessages", {}).get("ScalarCmd", [])
        if scalars and changed:
            queue["ScalarCmd"] = ({"DeviceIndex": index, "Scalars": [
                {"Index": i, "Scalar": value, "ActuatorType": attributes[i]["ActuatorType"]} for i in scalars]}, None)
        rotations = features(device, "RotateCmd") if all_motors else []
        if rotations and changed:
            queue["RotateCmd"] = ({"DeviceIndex": index, "Rotations": [
                {"Index": i, "Speed": value, "Clockwise": True} for i in rotations]}, None)
        linear = features(device, "LinearCmd") if all_motors else []
        if linear and due:
            up = not self.stroke.get(index, (False, 0))[0]
            position = .1 + .8 * value if up else .1
            duration = int(900 - 600 * value)
            queue["LinearCmd"] = ({"DeviceIndex": index, "Vectors": [
                {"Index": i, "Duration": duration, "Position": position} for i in linear]}, (up, duration))
