"""Minimal Buttplug v3 client over a standard-library WebSocket. No dependencies.

Only ws:// on a local machine is needed: Intiface Engine and Intiface Central
both listen on ws://127.0.0.1:12345 by default.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import time
from urllib.parse import urlsplit

SCALAR_TYPES = ("Vibrate", "Oscillate", "Constrict", "Inflate")


class ConnectionClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=2.):
        parts = urlsplit(url)
        if parts.scheme != "ws" or not parts.hostname:
            raise ValueError("Intiface address must look like ws://127.0.0.1:12345")
        port = parts.port or 80
        self.sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        request = (f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionClosed("Intiface closed the connection during the handshake")
            response += chunk
            if len(response) > 65536:
                raise ConnectionClosed("Invalid handshake response")
        head, self.buffer = response.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionClosed(f"Not a WebSocket server: {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        headers = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip() for line in lines[1:] if ":" in line}
        if headers.get("sec-websocket-accept") != accept:
            raise ConnectionClosed("WebSocket handshake failed")
        self.sock.setblocking(False)
        self.fragments = b""

    def _send_frame(self, opcode, payload):
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
        deadline = time.monotonic() + 2
        while data:
            try:
                sent = self.sock.send(data)
                data = data[sent:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ConnectionClosed("Send timed out")
                select.select([], [self.sock], [], .05)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def _frame(self):
        """Parse one frame from the buffer, or return None if incomplete."""
        data = self.buffer
        if len(data) < 2:
            return None
        fin, opcode = data[0] & 0x80, data[0] & 0x0F
        masked, length, offset = data[1] & 0x80, data[1] & 0x7F, 2
        if length == 126:
            if len(data) < 4:
                return None
            length, offset = struct.unpack(">H", data[2:4])[0], 4
        elif length == 127:
            if len(data) < 10:
                return None
            length, offset = struct.unpack(">Q", data[2:10])[0], 10
        if length > 4 * 1024 * 1024:
            raise ConnectionClosed("Frame too large")
        mask = b""
        if masked:
            mask, offset = data[offset:offset + 4], offset + 4
        if len(data) < offset + length:
            return None
        payload = data[offset:offset + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.buffer = data[offset + length:]
        return fin, opcode, payload

    def recv(self, timeout=0.):
        """Return one text message, or None if nothing arrives within timeout."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._frame()
            while frame:
                fin, opcode, payload = frame
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                elif opcode == 0x8:
                    raise ConnectionClosed("Intiface closed the connection")
                elif opcode in (0x1, 0x2, 0x0):
                    self.fragments += payload
                    if fin:
                        message, self.fragments = self.fragments, b""
                        return message.decode("utf-8")
                frame = self._frame()
            # A zero timeout still polls the socket once.
            ready, _, _ = select.select([self.sock], [], [], max(0., deadline - time.monotonic()))
            if not ready:
                return None
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise ConnectionClosed("Intiface closed the connection")
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
        self.devices = {}   # index -> device dict
        self.scanning = False
        self.errors = []
        self.ping_every = 0.
        self.next_ping = float("inf")
        try:
            info = self.request("RequestServerInfo", {"ClientName": name, "MessageVersion": 3})
            if info[0] != "ServerInfo" or info[1].get("MessageVersion") != 3:
                raise ConnectionClosed("Intiface did not accept Buttplug message version 3")
            self.server_name = str(info[1].get("ServerName", "Intiface"))
            max_ping = info[1].get("MaxPingTime", 0)
            if isinstance(max_ping, int) and max_ping > 0:
                self.ping_every = max_ping / 1000 / 2
                self.next_ping = time.monotonic() + self.ping_every
            kind, data = self.request("RequestDeviceList", {})
            for device in data.get("Devices", []):
                self._add(device)
        except Exception:
            self.ws.close()
            raise

    def _add(self, device):
        if isinstance(device, dict) and type(device.get("DeviceIndex")) is int:
            self.devices[device["DeviceIndex"]] = device

    def send(self, kind, fields, device=None):
        self.next_id = self.next_id % 4294967294 + 1
        ident = self.next_id
        self.ws.send(json.dumps([{kind: dict(fields, Id=ident)}]))
        self.pending[ident] = (time.monotonic() + 2., device)
        return ident

    def _handle(self, text):
        replies = {}
        try:
            batch = json.loads(text)
        except ValueError:
            return replies
        for message in batch if isinstance(batch, list) else []:
            if not isinstance(message, dict) or len(message) != 1:
                continue
            kind, data = next(iter(message.items()))
            if not isinstance(data, dict):
                continue
            if kind == "DeviceAdded":
                self._add(data)
            elif kind == "DeviceRemoved":
                self.devices.pop(data.get("DeviceIndex"), None)
            elif kind == "ScanningFinished":
                self.scanning = False
            elif kind == "Error":
                self.errors.append(str(data.get("ErrorMessage", "Intiface error"))[:200])
                del self.errors[:-5]
            ident = data.get("Id")
            if ident in self.pending:
                del self.pending[ident]
                replies[ident] = (kind, data)
        return replies

    def request(self, kind, fields, timeout=3.):
        ident = self.send(kind, fields)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.ws.recv(max(0., deadline - time.monotonic()))
            if text is None:
                continue
            reply = self._handle(text).get(ident)
            if reply:
                if reply[0] == "Error":
                    raise ConnectionClosed(reply[1].get("ErrorMessage", "Intiface error"))
                return reply
        raise ConnectionClosed(f"Intiface did not answer {kind}")

    def poll(self):
        for _ in range(64):
            text = self.ws.recv(0)
            if text is None:
                break
            self._handle(text)
        now = time.monotonic()
        for ident, (deadline, _) in list(self.pending.items()):
            if now > deadline:
                del self.pending[ident]
        if now >= self.next_ping:
            self.send("Ping", {})
            self.next_ping = now + self.ping_every

    def busy(self, index):
        return any(device == index for _, device in self.pending.values())

    def start_scanning(self):
        self.send("StartScanning", {})
        self.scanning = True

    def stop_scanning(self):
        if self.scanning:
            self.send("StopScanning", {})
            self.scanning = False

    def stop_all(self):
        self.send("StopAllDevices", {})

    def close(self):
        self.ws.close()


def summary(device):
    scalar = device.get("DeviceMessages", {}).get("ScalarCmd", [])
    motors = [f.get("ActuatorType", "") for f in scalar if isinstance(f, dict)]
    motors += ["Rotate"] * len(features(device, "RotateCmd"))
    motors += ["Linear"] * len(features(device, "LinearCmd"))
    return {"index": device["DeviceIndex"], "name": device.get("DeviceDisplayName") or device.get("DeviceName", "Device"),
            "key": device.get("DeviceName", ""), "motors": motors,
            "extra_motors": any(m != "Vibrate" for m in motors)}


class Outputs:
    """Sends the current strength to every selected device, only when it changes."""
    def __init__(self):
        self.sent = {}     # index -> (value, time)
        self.stroke = {}   # index -> (moving up, next stroke time)
        self.hold = {}     # index -> (value, until): a device test overrides the mix briefly

    def reset(self):
        self.sent.clear()
        self.stroke.clear()
        self.hold.clear()

    def update(self, client, strength, device_settings, now):
        for index in list(self.sent):
            if index not in client.devices:
                self.sent.pop(index, None)
                self.stroke.pop(index, None)
        for index, device in client.devices.items():
            options = device_settings.get(device.get("DeviceName", ""), {})
            value = 0. if options.get("on") is False else strength * options.get("strength", 1.)
            if index in self.hold:
                if now < self.hold[index][1]:
                    value = self.hold[index][0]
                else:
                    del self.hold[index]
            value = round(max(0., min(1., value)), 3)
            previous, last = self.sent.get(index, (None, 0.))
            gap = device.get("DeviceMessageTimingGap", 0)
            gap = gap / 1000 if isinstance(gap, (int, float)) and gap > 0 else 0.
            stroking = value > 0 and options.get("all_motors") is True and bool(features(device, "LinearCmd"))
            if value == previous and not (stroking and self._stroke_due(index, now)):
                continue
            if previous is not None and abs(value - previous) < .01 and value > 0 and now - last < 1.:
                continue
            if value > 0 and (client.busy(index) or now - last < gap):
                continue
            self._send(client, device, value, options.get("all_motors") is True, now)
            self.sent[index] = (value, now)

    def _stroke_due(self, index, now):
        return now >= self.stroke.get(index, (0, 0))[1]

    def _send(self, client, device, value, all_motors, now):
        index = device["DeviceIndex"]
        types = SCALAR_TYPES if all_motors else ("Vibrate",)
        scalars = features(device, "ScalarCmd", types)
        attributes = device.get("DeviceMessages", {}).get("ScalarCmd", [])
        if scalars:
            client.send("ScalarCmd", {"DeviceIndex": index, "Scalars": [
                {"Index": i, "Scalar": value, "ActuatorType": attributes[i]["ActuatorType"]} for i in scalars]}, index)
        rotations = features(device, "RotateCmd") if all_motors else []
        if rotations:
            client.send("RotateCmd", {"DeviceIndex": index, "Rotations": [
                {"Index": i, "Speed": value, "Clockwise": True} for i in rotations]}, index)
        linear = features(device, "LinearCmd") if all_motors else []
        if linear:
            if value <= 0:
                client.send("StopDeviceCmd", {"DeviceIndex": index}, index)
                self.stroke.pop(index, None)
            elif self._stroke_due(index, now):
                # Strokes get longer and faster with strength.
                up = not self.stroke.get(index, (False, 0))[0]
                position = .1 + .8 * value if up else .1
                duration = int(900 - 600 * value)
                client.send("LinearCmd", {"DeviceIndex": index, "Vectors": [
                    {"Index": i, "Duration": duration, "Position": position} for i in linear]}, index)
                self.stroke[index] = (up, now + duration / 1000)
