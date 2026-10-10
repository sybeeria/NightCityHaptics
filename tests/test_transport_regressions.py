"""Transport regression checks with in-memory peers; no engine or hardware."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/companion"))
import buttplug


def device(index=1, name="Test model", display=None, gap=0, linear=False):
    result = {"DeviceIndex": index, "DeviceName": name, "DeviceMessageTimingGap": gap,
              "DeviceMessages": {"ScalarCmd": [{"ActuatorType": "Vibrate", "StepCount": 20},
                                               {"ActuatorType": "Constrict", "StepCount": 20}],
                                 "RotateCmd": [{"StepCount": 20}], "StopDeviceCmd": {}}}
    if linear:
        result["DeviceMessages"]["LinearCmd"] = [{"StepCount": 99}]
    if display:
        result["DeviceDisplayName"] = display
    return result


class Recorder:
    def __init__(self, *devices):
        self.devices = {d["DeviceIndex"]: d for d in devices}
        self.commands = []
        self.now = 0

    def busy(self, index): return False

    def send(self, kind, fields, index=None):
        self.commands.append((self.now, kind, fields))

    def update(self, outputs, strength, settings, now):
        self.now = now
        outputs.update(self, strength, settings, now)


class OutputRegressions(unittest.TestCase):
    def test_retired_capabilities_are_stopped_before_vibration_only_output(self):
        d = device()
        client, outputs = Recorder(d), buttplug.Outputs()
        on = {buttplug.settings_key(d): {"all_motors": True}}
        client.update(outputs, .5, on, 0)
        before = len(client.commands)
        client.update(outputs, .5, {}, .1)
        commands = client.commands[before:]
        self.assertEqual(commands[0][1], "StopDeviceCmd")
        self.assertTrue(all(s["Scalar"] == 0 for s in commands[1][2]["Scalars"]))
        self.assertEqual(commands[-1][2]["Scalars"], [{"Index": 0, "Scalar": .5, "ActuatorType": "Vibrate"}])
        before = len(client.commands)
        client.update(outputs, 0, {}, .2)
        self.assertEqual(client.commands[before][1], "StopDeviceCmd")

    def test_enabling_extra_motors_at_identical_strength_is_not_cached_away(self):
        d = device()
        client, outputs = Recorder(d), buttplug.Outputs()
        client.update(outputs, .5, {}, 0)
        before = len(client.commands)
        client.update(outputs, .5, {buttplug.settings_key(d): {"all_motors": True}}, .1)
        commands = client.commands[before:]
        self.assertEqual(commands[0][1], "StopDeviceCmd")
        self.assertIn("RotateCmd", [kind for _, kind, _ in commands])
        self.assertEqual([s["Index"] for s in commands[-2][2]["Scalars"]], [0, 1])

    def test_same_index_capability_replacement_stops_previous_selection(self):
        old = device(gap=100)
        client, outputs = Recorder(old), buttplug.Outputs()
        client.update(outputs, .5, {buttplug.settings_key(old): {"all_motors": True}}, 0)
        client.update(outputs, .5, {buttplug.settings_key(old): {"all_motors": True}}, .1)
        replacement = device(gap=100)
        replacement["DeviceMessages"].pop("RotateCmd")
        client.devices[1] = replacement
        before = len(client.commands)
        client.update(outputs, .5, {}, .11)
        self.assertEqual(client.commands[before][1], "StopDeviceCmd")
        self.assertFalse(any(kind == "ScalarCmd" and any(s["Scalar"] > 0 for s in fields["Scalars"])
                             for _, kind, fields in client.commands[before:]))
        client.update(outputs, .5, {}, .22)
        self.assertEqual(client.commands[-1][2]["Scalars"][0]["Scalar"], .5)

    def test_constant_strength_reverses_linear_strokes_when_they_finish(self):
        d = device(linear=True)
        d["DeviceMessages"].pop("ScalarCmd")
        d["DeviceMessages"].pop("RotateCmd")
        client, outputs = Recorder(d), buttplug.Outputs()
        settings = {buttplug.settings_key(d): {"all_motors": True}}
        for i in range(61): client.update(outputs, 1, settings, i / 30)
        strokes = [(t, fields["Vectors"][0]) for t, kind, fields in client.commands if kind == "LinearCmd"]
        self.assertGreaterEqual(len(strokes), 6)
        for (start, vector), (next_start, _) in zip(strokes, strokes[1:]):
            self.assertGreaterEqual(next_start - start + 1e-9, vector["Duration"] / 1000)
            self.assertLessEqual(next_start - start, vector["Duration"] / 1000 + 1 / 30 + 1e-9)
        self.assertEqual([round(v["Position"], 1) for _, v in strokes[:4]], [.9, .1, .9, .1])

    def test_gap_applies_to_each_positive_command_and_latest_values_do_not_starve_types(self):
        d = device(gap=100, linear=True)
        client, outputs = Recorder(d), buttplug.Outputs()
        settings = {buttplug.settings_key(d): {"all_motors": True}}
        for i in range(31): client.update(outputs, .2 + i / 100, settings, i / 30)
        positive = [(t, kind) for t, kind, _ in client.commands]
        self.assertEqual({kind for _, kind in positive}, {"ScalarCmd", "RotateCmd", "LinearCmd"})
        for (t, _), (later, _) in zip(positive, positive[1:]):
            self.assertGreaterEqual(later - t + 1e-9, .1)
        client.update(outputs, 0, settings, 1.001)
        self.assertEqual(client.commands[-2][1], "StopDeviceCmd", "urgent stop bypasses ordinary output delay")
        self.assertFalse(outputs.queues.get(1))

    def test_named_and_unnamed_same_model_preferences_are_independent_with_legacy_fallback(self):
        for first, second in ((device(1, display="Left"), device(2, display="Right")), (device(1), device(2))):
            with self.subTest(named=bool(first.get("DeviceDisplayName"))):
                self.assertNotEqual(buttplug.summary(first)["key"], buttplug.summary(second)["key"])
                self.assertNotEqual(buttplug.settings_key(first), buttplug.settings_key(second))
                settings = {"Test model": {"strength": .4}, buttplug.settings_key(first): {"strength": .2}}
                self.assertEqual(buttplug.device_options(first, settings)["strength"], .2)
                self.assertEqual(buttplug.device_options(second, settings)["strength"], .4)
        renamed_index = device(99, display="Left")
        self.assertEqual(buttplug.settings_key(renamed_index), buttplug.settings_key(device(1, display="Left")))

    def test_removed_device_test_hold_does_not_leak_to_reused_index(self):
        d = device()
        client, outputs = Recorder(d), buttplug.Outputs()
        outputs.hold[1] = (.5, 2)
        client.update(outputs, 0, {}, 0)
        client.devices.clear()
        client.update(outputs, 0, {}, .1)
        client.devices[1] = device()
        client.update(outputs, 0, {}, .2)
        self.assertNotIn(1, outputs.hold)
        self.assertEqual(outputs.sent[1][0], 0)


class TextSocket:
    def __init__(self): self.sent, self.closed = [], False
    def send(self, text): self.sent.extend(json.loads(text))
    def recv(self, timeout): return None
    def close(self): self.closed = True


def actual_client():
    client = buttplug.Client.__new__(buttplug.Client)
    client.ws, client.next_id = TextSocket(), 0
    client.pending, client.pending_kind = {}, {}
    client.devices, client.errors = {1: device()}, []
    client.scanning, client.next_ping, client.ping_every = False, float("inf"), 0
    client.closed = False
    client.scan_id = None
    return client


class AcknowledgementRegressions(unittest.TestCase):
    def test_failed_zero_acknowledgement_stops_and_closes_instead_of_trusting_cached_zero(self):
        client, outputs = actual_client(), buttplug.Outputs()
        outputs.update(client, .5, {}, 0)
        client._handle('[{"Ok":{"Id":1}}]')
        outputs.update(client, 0, {}, .1)
        zero_id = client.next_id
        with self.assertRaisesRegex(buttplug.ConnectionClosed, "zero failed"):
            client._handle(json.dumps([{"Error": {"Id": zero_id, "ErrorCode": 4, "ErrorMessage": "zero failed"}}]))
        self.assertTrue(client.ws.closed)
        self.assertEqual(next(iter(client.ws.sent[-1])), "StopAllDevices")

    def test_unanswered_output_acknowledgement_times_out_and_closes(self):
        client = actual_client()
        with patch.object(buttplug.time, "monotonic", return_value=0):
            ident = client.send("StopDeviceCmd", {"DeviceIndex": 1}, 1)
        with patch.object(buttplug.time, "monotonic", return_value=3):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "did not answer StopDeviceCmd"):
                client.poll()
        self.assertTrue(client.ws.closed)
        self.assertNotIn(ident, client.pending)

    def test_ok_output_replies_preserve_connection_and_release_backpressure(self):
        client = actual_client()
        ident = client.send("ScalarCmd", {"DeviceIndex": 1, "Scalars": []}, 1)
        self.assertTrue(client.busy(1))
        client._handle(json.dumps([{"Ok": {"Id": ident}}]))
        self.assertFalse(client.busy(1))
        self.assertFalse(client.ws.closed)

    def test_removed_device_cancels_its_old_pending_output(self):
        client = actual_client()
        ident = client.send("ScalarCmd", {"DeviceIndex": 1, "Scalars": []}, 1)
        client._handle('[{"DeviceRemoved":{"Id":0,"DeviceIndex":1}}]')
        self.assertNotIn(ident, client.pending)
        self.assertNotIn(1, client.devices)

    def test_malformed_protocol_ids_and_nested_json_close(self):
        for text in ('[{"Ok":{"Id":[]}}]', '{"unexpected":"object"}', '[' * 2000 + ']' * 2000):
            client = actual_client()
            with self.subTest(text=text[:30]):
                with self.assertRaises(buttplug.ConnectionClosed): client._handle(text)
                self.assertTrue(client.ws.closed)

    def test_integer_conversion_is_bounded_even_without_python_digit_limit(self):
        previous = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
        try:
            if previous is not None:
                sys.set_int_max_str_digits(0)
            for sign in ("", "-"):
                client = actual_client()
                text = '[{"Ok":{"Id":0,"unknown":' + sign + "1" * 100000 + "}}]"
                with self.subTest(sign=sign), self.assertRaises(buttplug.ConnectionClosed):
                    client._handle(text)
                self.assertTrue(client.ws.closed)
            client = actual_client()
            ident = client.send("ScalarCmd", {"DeviceIndex": 1, "Scalars": []}, 1)
            client._handle(json.dumps([{"Ok": {"Id": ident, "unknown": 2 ** 64 - 1}}]))
            self.assertFalse(client.busy(1))
            self.assertFalse(client.ws.closed)
        finally:
            if previous is not None:
                sys.set_int_max_str_digits(previous)


class TransportAuditRegressions(unittest.TestCase):
    def test_ping_requires_ok_and_rejects_unrelated_matching_reply(self):
        client = actual_client()
        ident = client.send("Ping", {})
        with self.assertRaisesRegex(buttplug.ConnectionClosed, "reply to Ping"):
            client._handle(json.dumps([{"DeviceList": {"Id": ident, "Devices": []}}]))
        self.assertTrue(client.closed)

    def test_failed_scan_clears_scanning_and_remains_retryable(self):
        client = actual_client()
        client.start_scanning()
        client.start_scanning()
        self.assertEqual(len(client.ws.sent), 1, "repeated clicks reuse an active scan")
        client._handle(json.dumps([{"Error": {"Id": client.next_id, "ErrorCode": 4,
                                               "ErrorMessage": "Bluetooth unavailable"}}]))
        self.assertFalse(client.scanning)
        self.assertFalse(client.closed)
        client.start_scanning()
        self.assertTrue(client.scanning)
        self.assertEqual(len(client.ws.sent), 2)

    def test_late_failure_from_finished_scan_does_not_clear_a_new_scan(self):
        client = actual_client()
        client.start_scanning()
        old_id = client.next_id
        client._handle('[{"ScanningFinished":{"Id":0}}]')
        client.start_scanning()
        client._handle(json.dumps([{"Error": {"Id": old_id, "ErrorCode": 4,
                                               "ErrorMessage": "old scan error"}}]))
        self.assertTrue(client.scanning)
        self.assertEqual(client.scan_id, 2)
        self.assertFalse(client.closed)

    def test_unanswered_scan_closes_instead_of_leaving_false_scan_state(self):
        client = actual_client()
        with patch.object(buttplug.time, "monotonic", return_value=0):
            client.start_scanning()
        with patch.object(buttplug.time, "monotonic", return_value=3):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "did not answer StartScanning"):
                client.poll()
        self.assertFalse(client.scanning)
        self.assertTrue(client.closed)
        self.assertFalse(client.pending)

    def test_failed_stop_scan_closes_uncertain_server_session(self):
        client = actual_client()
        client.start_scanning()
        client._handle('[{"Ok":{"Id":1}}]')
        client.stop_scanning()
        with self.assertRaisesRegex(buttplug.ConnectionClosed, "stop failed"):
            client._handle('[{"Error":{"Id":2,"ErrorCode":4,"ErrorMessage":"stop failed"}}]')
        self.assertTrue(client.closed)

    def test_server_watchdog_error_without_matching_id_disconnects(self):
        client = actual_client()
        with self.assertRaisesRegex(buttplug.ConnectionClosed, "watchdog expired"):
            client._handle('[{"Error":{"Id":0,"ErrorCode":2,"ErrorMessage":"watchdog expired"}}]')
        self.assertTrue(client.closed)

    def test_pending_command_bound_still_sends_final_stop_and_closes(self):
        client = actual_client()
        with patch.object(buttplug, "MAX_PENDING", 4):
            for _ in range(4): client.send("ScalarCmd", {"DeviceIndex": 1, "Scalars": []}, 1)
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "Too many pending"):
                client.send("ScalarCmd", {"DeviceIndex": 1, "Scalars": []}, 1)
        self.assertTrue(client.closed)
        self.assertFalse(client.pending)
        self.assertEqual(next(iter(client.ws.sent[-1])), "StopAllDevices")

    def test_wrapping_ids_skip_an_outstanding_match(self):
        client = actual_client()
        first = client.send("Ping", {})
        client.next_id = buttplug.MAX_PROTOCOL_ID
        second = client.send("StartScanning", {})
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(client.pending_kind[first], "Ping")
        self.assertEqual(client.pending_kind[second], "StartScanning")

    def test_closed_client_rejects_further_commands(self):
        client = actual_client()
        client.send("Ping", {})
        client.close()
        self.assertFalse(client.pending)
        with self.assertRaisesRegex(buttplug.ConnectionClosed, "is closed"):
            client.send("Ping", {})
        self.assertEqual(len(client.ws.sent), 1)

    def test_protocol_ids_system_ids_and_json_numbers_are_strict(self):
        samples = ['[{"Ok":{"Id":-1}}]', '[{"Ok":{"Id":4294967296}}]',
                   '[{"Ok":{"Id":true}}]', '[{"Ok":{"Id":1,"Id":2}}]',
                   '[{"Ok":{"Id":0,"value":NaN}}]', '[{"Ok":{"Id":0,"value":1e999}}]',
                   '[{"DeviceRemoved":{"Id":1,"DeviceIndex":1}}]',
                   '[{"DeviceRemoved":{"Id":0,"DeviceIndex":-1}}]',
                   '[{"ScanningFinished":{"Id":2}}]', '[]']
        for text in samples:
            client = actual_client()
            with self.subTest(text=text), self.assertRaises(buttplug.ConnectionClosed):
                client._handle(text)
            self.assertTrue(client.closed)

    def test_invalid_actuator_numbers_or_names_disconnect(self):
        for command, attrs in (("ScalarCmd", [{"ActuatorType": "Vibrate", "StepCount": True}]),
                               ("ScalarCmd", [{"ActuatorType": []}]),
                               ("ScalarCmd", [{}]), ("RotateCmd", [{"StepCount": -1}]),
                               ("LinearCmd", [{"StepCount": 4294967296}])):
            client = actual_client()
            d = device()
            d["DeviceMessages"][command] = attrs
            with self.subTest(command=command, attrs=attrs), self.assertRaises(buttplug.ConnectionClosed):
                client._add(d)
            self.assertTrue(client.closed)

    def test_delayed_device_list_keeps_negotiated_watchdog_alive(self):
        now = [0.]
        class WatchdogPeer(TextSocket):
            def __init__(self, url):
                super().__init__()
                self.ready = []
                self.last_ping = 0.
                self.list_id = None
            def send(self, text):
                super().send(text)
                message = json.loads(text)[0]
                kind, fields = next(iter(message.items()))
                if kind == "RequestServerInfo":
                    self.ready.append({"ServerInfo": {"Id": fields["Id"], "ServerName": "Delayed",
                                                       "MessageVersion": 3, "MaxPingTime": 1000}})
                elif kind == "RequestDeviceList": self.list_id = fields["Id"]
                elif kind == "Ping":
                    self.last_ping = now[0]
                    self.ready.append({"Ok": {"Id": fields["Id"]}})
            def recv(self, timeout):
                if self.ready: return json.dumps([self.ready.pop(0)])
                now[0] += min(timeout, max(0., 1.6 - now[0]))
                if now[0] - self.last_ping > 1:
                    raise buttplug.ConnectionClosed("watchdog expired during request")
                if now[0] >= 1.6 and self.list_id is not None:
                    ident, self.list_id = self.list_id, None
                    return json.dumps([{"DeviceList": {"Id": ident, "Devices": []}}])
                return None
        with patch.object(buttplug, "WebSocket", WatchdogPeer), \
                patch.object(buttplug.time, "monotonic", side_effect=lambda: now[0]):
            client = buttplug.Client("ws://127.0.0.1:12345")
        self.assertFalse(client.closed)
        self.assertGreaterEqual(sum("Ping" in m for m in client.ws.sent), 3)
        self.assertAlmostEqual(now[0], 1.6)

    def test_request_timeout_closes_and_clears_pending_match(self):
        client = actual_client()
        now = [0.]
        def receive(timeout):
            now[0] += timeout
            return None
        with patch.object(client.ws, "recv", side_effect=receive), \
                patch.object(buttplug.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "did not answer RequestDeviceList"):
                client.request("RequestDeviceList", {}, timeout=.1)
        self.assertTrue(client.closed)
        self.assertFalse(client.pending)

    def test_invalid_server_ping_information_or_duplicate_device_index_closes(self):
        for fields in ({"MessageVersion": 3.0}, {"MaxPingTime": True}, {"MaxPingTime": -1}, {"MaxPingTime": 4294967296},
                       {"ServerName": []}, {"duplicate": True}):
            peers = []
            class InfoPeer(TextSocket):
                def __init__(self, url):
                    super().__init__()
                    self.ready = []
                    peers.append(self)
                def send(self, text):
                    super().send(text)
                    kind, data = next(iter(json.loads(text)[0].items()))
                    if kind == "RequestServerInfo":
                        info = {"Id": data["Id"], "ServerName": "Test", "MessageVersion": 3, "MaxPingTime": 0}
                        info.update({k: v for k, v in fields.items() if k != "duplicate"})
                        self.ready.append({"ServerInfo": info})
                    elif kind == "RequestDeviceList":
                        self.ready.append({"DeviceList": {"Id": data["Id"], "Devices": [device(), device()]}})
                def recv(self, timeout): return json.dumps([self.ready.pop(0)]) if self.ready else None
            with self.subTest(fields=fields), patch.object(buttplug, "WebSocket", InfoPeer):
                with self.assertRaises(buttplug.ConnectionClosed):
                    buttplug.Client("ws://127.0.0.1:12345")
            self.assertTrue(peers[0].closed)


def frame(opcode, payload=b"", fin=True):
    header = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126: header += bytes([length])
    elif length < 65536: header += b"\x7e" + struct.pack(">H", length)
    else: header += b"\x7f" + struct.pack(">Q", length)
    return header + payload


class ByteSocket:
    def __init__(self, incoming=b""):
        self.incoming, self.sent, self.closed, self.reads = incoming, [], False, 0
    def recv(self, size):
        self.reads += 1
        data, self.incoming = self.incoming[:size], self.incoming[size:]
        return data
    def send(self, data): self.sent.append(data); return len(data)
    def close(self): self.closed = True


def websocket(buffer=b"", incoming=b""):
    result = buttplug.WebSocket.__new__(buttplug.WebSocket)
    result.sock, result.buffer = ByteSocket(incoming), buffer
    result.fragments, result.fragmenting = bytearray(), False
    return result


class WebSocketRegressions(unittest.TestCase):
    def test_fragmented_utf8_with_interleaved_ping_and_pong_is_valid(self):
        data = '"é"'.encode()
        ws = websocket(frame(1, data[:2], False) + frame(9, b"hello") + frame(10, b"ok") + frame(0, data[2:]))
        self.assertEqual(ws.recv(), '"é"')
        self.assertEqual(ws.sock.sent[0][0] & 15, 10)
        self.assertFalse(ws.fragmenting)

    def test_aggregate_fragment_size_is_rejected_before_waiting_for_payload(self):
        ws = websocket(frame(1, b"1234", False) + b"\x00\x05")
        with patch.object(buttplug, "MAX_MESSAGE", 8):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "Message too large"): ws.recv()
        self.assertTrue(ws.sock.closed)
        self.assertEqual(ws.fragments, bytearray(b"1234"))

    def test_invalid_frame_forms_close_connection(self):
        samples = [frame(0, b"orphan"), frame(1, b"unfinished", False) + frame(1, b"new"),
                   frame(9, b"fragmented", False), frame(9, b"x" * 126), b"\xc1\x00",
                   b"\x81\x80" + b"mask", frame(2, b"binary"), frame(8, b"x"),
                   b"\x81\x7e\x00\x01x", b"\x81\x7f" + struct.pack(">Q", 1) + b"x",
                   frame(1, b"\xff"), b"\x81\x7f" + struct.pack(">Q", buttplug.MAX_MESSAGE + 1)]
        for sample in samples:
            ws = websocket(sample)
            with self.subTest(frame=sample[:12]):
                with self.assertRaises(buttplug.ConnectionClosed): ws.recv()
                self.assertTrue(ws.sock.closed)

    def test_receive_work_yields_for_control_flood_without_losing_buffer(self):
        ws = websocket(frame(10) * 1000)
        with patch.object(buttplug.time, "monotonic", return_value=0):
            self.assertIsNone(ws.recv())
        self.assertGreater(len(ws.buffer), 0)
        self.assertLess(len(ws.buffer), 2000)
        self.assertFalse(ws.sock.closed)

    def test_continuous_partial_fragments_have_a_byte_budget(self):
        incoming = frame(1, b"x" * 65536, False) + frame(0, b"x" * 65536, False) * 20
        ws = websocket(incoming=incoming)
        with patch.object(buttplug.select, "select", return_value=([ws.sock], [], [])), \
                patch.object(buttplug.time, "monotonic", return_value=0):
            self.assertIsNone(ws.recv())
        self.assertLessEqual(ws.sock.reads, 4)
        self.assertGreater(len(ws.sock.incoming), 0)
        self.assertLessEqual(len(ws.buffer), buttplug.MAX_BUFFER)

    def test_receive_buffer_is_checked_before_append(self):
        ws = websocket(incoming=frame(1, b"x" * 100))
        with patch.object(buttplug, "MAX_BUFFER", 16), \
                patch.object(buttplug.select, "select", return_value=([ws.sock], [], [])):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "buffer too large"): ws.recv()
        self.assertEqual(ws.buffer, b"")
        self.assertTrue(ws.sock.closed)

    def test_absolute_handshake_deadline_rejects_continuous_drip(self):
        now = [0.]
        class Drip(ByteSocket):
            def sendall(self, data): pass
            def settimeout(self, timeout): self.timeout = timeout
            def recv(self, size):
                self.reads += 1
                now[0] += .2
                return b"H"
        sock = Drip()
        with patch.object(buttplug.socket, "create_connection", return_value=sock), \
                patch.object(buttplug.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(buttplug.ConnectionClosed, "handshake timed out"):
                buttplug.WebSocket("ws://127.0.0.1:12345", timeout=.5)
        self.assertTrue(sock.closed)
        self.assertLessEqual(sock.reads, 3)

    def test_valid_upgrade_query_and_connection_tokens_are_supported(self):
        class Handshake(ByteSocket):
            def settimeout(self, timeout): pass
            def setblocking(self, blocking): pass
            def sendall(self, data):
                self.request = data
                key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                self.incoming = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: WebSocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        sock = Handshake()
        with patch.object(buttplug.socket, "create_connection", return_value=sock):
            ws = buttplug.WebSocket("ws://[::1]:12345/control?mode=test")
        self.assertIn(b"GET /control?mode=test HTTP/1.1", sock.request)
        self.assertIn(b"Host: [::1]:12345", sock.request)
        self.assertFalse(sock.closed)


class HandshakeAuditRegressions(unittest.TestCase):
    def test_invalid_address_rejects_before_socket_connection(self):
        for url in ("ws://127.0.0.1:0/", "ws://user:password@127.0.0.1/", "ws://127.0.0.1/#ignored",
                    "ws://127.0.0.1/path\r\nHeader:value", "ws://127.0.0.1/path with spaces"):
            with self.subTest(url=url), patch.object(buttplug.socket, "create_connection") as connect:
                with self.assertRaises(ValueError): buttplug.WebSocket(url)
                connect.assert_not_called()

    def test_invalid_status_duplicate_accept_and_unrequested_subprotocol_reject(self):
        for extra in ("status", "duplicate", "subprotocol", "bad-header"):
            class Handshake(ByteSocket):
                def settimeout(self, timeout): pass
                def setblocking(self, blocking): pass
                def sendall(self, data):
                    key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    status = b"HTTP/1.1 101 Switching Protocols" if extra != "status" else b"garbage 101 Switching Protocols"
                    suffix = {"duplicate": b"Sec-WebSocket-Accept: " + accept + b"\r\n",
                              "subprotocol": b"Sec-WebSocket-Protocol: unexpected\r\n",
                              "bad-header": b"invalid line\r\n"}.get(extra, b"")
                    self.incoming = status + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n" + suffix + b"\r\n"
            sock = Handshake()
            with self.subTest(extra=extra), patch.object(buttplug.socket, "create_connection", return_value=sock):
                with self.assertRaises(buttplug.ConnectionClosed): buttplug.WebSocket("ws://127.0.0.1:12345")
            self.assertTrue(sock.closed)


if __name__ == "__main__":
    unittest.main()
