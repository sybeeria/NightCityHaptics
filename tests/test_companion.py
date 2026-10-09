"""Companion checks. The companion itself is stdlib-only; these tests use the
`websockets` package (dev venv) as an independent server implementation.

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

SRC = Path(__file__).resolve().parents[1] / "src" / "companion"
sys.path.insert(0, str(SRC))
import buttplug  # noqa: E402
import haptics  # noqa: E402
import nch  # noqa: E402

CATALOG = json.loads((SRC / "catalog.json").read_text())


def wait_for(predicate, timeout=3.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("timed out")


class CatalogTests(unittest.TestCase):
    def test_every_default_feel_exists_for_its_kind(self):
        feels = {kind: {f["id"] for f in items} for kind, items in CATALOG["feels"].items()}
        categories = {c["id"] for c in CATALOG["categories"]}
        ids = [i["id"] for i in CATALOG["interactions"]]
        self.assertEqual(len(ids), len(set(ids)))
        for item in CATALOG["interactions"]:
            self.assertIn(item["default"]["feel"], haptics.feel_ids(CATALOG, item), item["id"])
            self.assertIn(item["default"]["feel"], feels[item["kind"]], item["id"])
            self.assertIn(item["category"], categories)
            self.assertTrue(0 < item["default"]["strength"] <= 1)
        for effect in CATALOG["feels"]["event"]:
            times = [p[0] for p in effect["points"]]
            self.assertEqual(times, sorted(times), effect["id"])
            self.assertEqual(effect["points"][-1][1], 0, effect["id"] + " must end silent")
        for name, track in CATALOG["tracks"].items():
            if not isinstance(track, dict):
                continue
            times = [p[0] for p in track["points"]]
            self.assertEqual(times, sorted(times), name)
            self.assertEqual(track["points"][-1][1], 0, name + " must end silent")
            for cue_time, strength in track.get("cues", []):
                self.assertTrue(0 <= cue_time <= times[-1] and 0 < strength <= 1, name)

    def test_every_scene_cet_can_start_has_a_track(self):
        lua = (SRC.parent / "cet/nightCityHaptics/init.lua").read_text()
        import re
        scenes = set(re.findall(r'(?:startScene|armScene)\((?:censored and )?"(\w+)"', lua))
        scenes |= set(re.findall(r'or "(\w+)", \d+\)', lua))
        self.assertTrue({"kerry", "kerryCensored", "judy", "judyCensored", "river", "stout", "panam", "panamCensored"} <= scenes, scenes)
        for scene in scenes:
            self.assertIn(scene, CATALOG["tracks"], scene)

    def test_scene_feel_belongs_to_scene_interactions_only(self):
        for item in CATALOG["interactions"]:
            self.assertEqual("scene" in haptics.feel_ids(CATALOG, item), item["id"] in ("romance", "joytoy"), item["id"])

    def test_cet_reports_every_interaction(self):
        lua = (SRC.parent / "cet/nightCityHaptics/init.lua").read_text()
        for item in CATALOG["interactions"]:
            needle = f'event("{item["id"]}' if item["kind"] == "event" else f'levels.{item["id"]}'
            self.assertIn(needle, lua, item["id"])


class MixerTests(unittest.TestCase):
    def setUp(self):
        self.settings = haptics.default_settings(CATALOG)
        self.mixer = haptics.Mixer(CATALOG)

    def test_envelope_interpolates_and_ends(self):
        points = [[0, 0], [1, 1], [2, 0]]
        self.assertAlmostEqual(haptics.envelope(points, .5), .5)
        self.assertAlmostEqual(haptics.envelope(points, 1.5), .5)
        self.assertEqual(haptics.envelope(points, 2.1), 0)
        self.assertEqual(haptics.envelope(points, -.1), 0)

    def test_blend_is_bounded_and_single_inputs_pass(self):
        self.assertAlmostEqual(haptics.blend([.3], .7), .3)
        both = haptics.blend([.5, .5], .7)
        self.assertTrue(.5 < both < .7)
        self.assertLessEqual(haptics.blend([.7] * 20, .7), .7)
        self.assertEqual(haptics.blend([], .7), 0)

    def test_event_strength_is_share_of_master(self):
        self.mixer.trigger("hurt", 1, 0, "thud")
        output, parts = self.mixer.tick(0, {}, self.settings)
        self.assertAlmostEqual(output, .9 * .7)
        self.assertIn("hurt", parts)
        self.assertEqual(self.mixer.tick(1, {}, self.settings)[0], 0, "thud ends after .5 s")

    def test_switched_off_interactions_are_silent_but_try_plays(self):
        self.settings["interactions"]["hurt"]["on"] = False
        self.mixer.trigger("hurt", 1, 0, "thud")
        self.assertEqual(self.mixer.tick(0, {}, self.settings)[0], 0)
        self.mixer.trigger("try:hurt", 1, 0, "thud")
        self.assertGreater(self.mixer.tick(0, {}, self.settings)[0], 0)

    def test_feedback_off_silences_everything(self):
        self.settings["enabled"] = False
        self.mixer.trigger("try:hurt", 1, 0, "thud")
        self.assertEqual(self.mixer.tick(0, {"engine": 1}, self.settings)[0], 0)

    def test_weaker_retrigger_does_not_cut_a_stronger_pulse(self):
        self.mixer.trigger("explosion", 1, 0, "burst")
        self.mixer.trigger("explosion", .1, .05, "burst")
        self.assertEqual(self.mixer.voices["explosion"]["magnitude"], 1)
        self.mixer.trigger("gunfire", .5, 0, "kick")
        self.mixer.trigger("gunfire", 1, .02, "kick")
        self.assertEqual(self.mixer.voices["gunfire"]["magnitude"], 1)

    def test_seconds_stretch_an_effect(self):
        self.mixer.trigger("external", 1, 0, "swell", seconds=6)
        self.assertGreater(self.mixer.tick(5, {}, self.settings)[0], 0)
        self.assertEqual(self.mixer.tick(6.1, {}, self.settings)[0], 0)

    def test_level_textures_follow_input(self):
        out_low = self.mixer.tick(0, {"engine": .15}, self.settings)[0]
        self.mixer.clear()
        out_high = self.mixer.tick(0, {"engine": 1}, self.settings)[0]
        self.assertGreater(out_high, out_low)
        self.assertLessEqual(out_high, .7)
        footsteps = [haptics.texture("footsteps", t / 100) for t in range(100)]
        self.assertIn(0., footsteps)
        self.assertIn(1., footsteps)

    def test_heartbeat_quickens_with_level(self):
        def beats(level):
            values = [haptics.texture("heartbeat", t / 100, level) for t in range(600)]
            return sum(1 for a, b in zip(values, values[1:]) if a == 0 and b == 1.)
        self.assertGreater(beats(1), beats(.1))

    def test_scene_events_follow_their_track(self):
        settings = self.settings
        self.mixer.game_event("romance:stout", 1, 0., "scene")
        self.assertEqual(self.mixer.voices["romance"]["feel"], "track:stout")
        early = self.mixer.tick(0.1, {}, settings)[0]
        cue = self.mixer.tick(3.76, {}, settings)[0]          # an "intense" moan cue at 3.7 s
        self.assertGreater(cue, early)
        self.mixer.game_event("romance:pulse", 1, 4., "scene")
        self.assertIn("romance#pulse", self.mixer.voices)
        _, parts = self.mixer.tick(4.05, {}, settings)
        self.assertEqual(set(parts), {"romance"}, "a scene's pulses show as the scene, not a separate entry")
        self.mixer.game_event("romance:end", 1, 5., "scene")
        self.assertGreater(self.mixer.tick(5.1, {}, settings)[0], 0, "ending fades rather than cuts")
        self.mixer.tick(5. + haptics.RELEASE + .1, {}, settings)
        self.assertEqual(self.mixer.voices, {}, "every voice of the scene is gone after the release")

    def test_scene_events_with_another_feel(self):
        self.mixer.game_event("romance:kerry", 1, 0., "slowbuild")
        self.assertEqual(self.mixer.voices["romance"]["feel"], "slowbuild")
        self.mixer.game_event("romance:pulse", 1, 1., "slowbuild")
        self.assertNotIn("romance#pulse", self.mixer.voices, "long feels are not repeated as pulses")
        self.mixer.game_event("romance:nosuchscene", 1, 2., "scene")
        self.assertEqual(self.mixer.voices["romance"]["feel"], "swell", "an unknown scene falls back to a swell")

    def test_joytoy_rhythm_is_the_games_pulse_and_pause(self):
        values = [haptics.texture("joytoy", i / 100, seed=3.) for i in range(700)]
        self.assertEqual(values[0] in (1., .6), True, "a clip starts on a pulse")
        gaps, run = [], 0
        for value in values:
            if value == 0:
                run += 1
            elif run:
                gaps.append(run / 100)
                run = 0
        self.assertTrue(gaps and all(.45 <= g <= 1.15 for g in gaps), gaps)
        self.assertNotEqual(values, [haptics.texture("joytoy", i / 100, seed=9.) for i in range(700)],
                            "each clip gets its own random rhythm")

    def test_ring_and_glitch_textures(self):
        self.assertEqual([haptics.texture("ring", t) for t in (.1, .5, .8, 2.)], [1., 0., 1., 0.])
        glitch = [haptics.texture("glitch", i / 50) for i in range(200)]
        self.assertTrue(0 < sum(1 for v in glitch if v) < 200)

    def test_normalize_keeps_valid_and_drops_invalid(self):
        settings = haptics.normalize_settings({
            "max_strength": 4, "enabled": "yes",
            "interactions": {"hurt": {"strength": .2, "feel": "nope"}, "bogus": {"on": True}},
            "devices": {"Lovense Hush": {"strength": .5, "on": False}}}, CATALOG)
        self.assertEqual(settings["max_strength"], 1)
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["interactions"]["hurt"], {"on": True, "strength": .2, "feel": "thud"})
        self.assertNotIn("bogus", settings["interactions"])
        self.assertEqual(settings["devices"]["Lovense Hush"], {"on": False, "strength": .5, "all_motors": False})


def heartbeat(session="s1", seq=1, active=True, events=(), levels=None, t=10., paused=False):
    return {"v": 2, "session": session, "seq": seq, "t": t, "active": active, "paused": paused,
            "levels": levels or {}, "events": [list(e) for e in events]}


class GameLinkTests(unittest.TestCase):
    def test_a_leftover_file_is_only_a_baseline(self):
        link = haptics.GameLink()
        self.assertEqual(link.update(heartbeat(events=[(5, "hurt", 1, 10)]), 0.), [])
        self.assertFalse(link.live(0.))
        self.assertEqual(link.update(heartbeat(seq=1, events=[(5, "hurt", 1, 10)]), .1), [], "same seq is not life")
        self.assertFalse(link.active(.1))
        self.assertEqual(link.update(heartbeat(seq=2, events=[(5, "hurt", 1, 10), (6, "kill", 1, 10)]), .2),
                         [("kill", 1., None)])
        self.assertTrue(link.active(.2))

    def test_events_play_once_and_old_ones_are_skipped(self):
        link = haptics.GameLink()
        link.update(heartbeat(seq=1), 0.)
        self.assertEqual(link.update(heartbeat(seq=2, events=[(1, "jump", .5, 10, 3)]), .1), [("jump", .5, 3)])
        self.assertEqual(link.update(heartbeat(seq=3, events=[(1, "jump", .5, 10)]), .2), [])
        self.assertEqual(link.update(heartbeat(seq=4, t=20, events=[(2, "jump", 1, 10)]), .3), [], "10 s old")

    def test_stale_paused_and_new_sessions(self):
        link = haptics.GameLink()
        link.update(heartbeat(seq=1), 0.)
        link.update(heartbeat(seq=2, levels={"engine": .5}), .1)
        self.assertEqual(link.levels(.5), {"engine": .5})
        self.assertFalse(link.active(1.2), "no new heartbeat for over a second")
        self.assertEqual(link.describe(1.2)[0], "offline")
        link.update(heartbeat(seq=3, paused=True, levels={"engine": .5}), 1.3)
        self.assertFalse(link.active(1.3))
        self.assertEqual(link.levels(1.3), {})
        link.update(heartbeat(session="s2", seq=1), 1.4)
        self.assertFalse(link.live(1.4), "new session waits for its sequence to advance")


def device(index=3, name="Lovense Edge", scalars=("Vibrate", "Constrict", "Vibrate"), **extra):
    messages = {"ScalarCmd": [{"ActuatorType": t, "StepCount": 20} for t in scalars], "StopDeviceCmd": {}}
    messages.update(extra)
    return {"DeviceIndex": index, "DeviceName": name, "DeviceMessages": messages, "DeviceMessageTimingGap": 0}


class FakeIntiface:
    """Speaks Buttplug v3 through the `websockets` library."""
    def __init__(self, devices=None, max_ping=0):
        self.devices = [device()] if devices is None else devices
        self.max_ping = max_ping
        self.received = []
        self.sockets = []
        self.server = serve(self.handle, "127.0.0.1", 0, ping_interval=.05)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, ws):
        self.sockets.append(ws)
        try:
            self._serve(ws)
        except ConnectionClosed:
            pass

    def _serve(self, ws):
        for frame in ws:
            for message in json.loads(frame):
                kind, data = next(iter(message.items()))
                self.received.append((kind, data))
                if kind == "RequestServerInfo":
                    reply = {"ServerInfo": {"Id": data["Id"], "ServerName": "Fake", "MessageVersion": 3, "MaxPingTime": self.max_ping}}
                elif kind == "RequestDeviceList":
                    reply = {"DeviceList": {"Id": data["Id"], "Devices": self.devices}}
                else:
                    reply = {"Ok": {"Id": data["Id"]}}
                ws.send(json.dumps([reply]))

    def push(self, message):
        for ws in self.sockets:
            ws.send(json.dumps([message]))

    def commands(self, kind="ScalarCmd"):
        return [data for k, data in self.received if k == kind]

    def close(self):
        self.server.shutdown()


class WebSocketTests(unittest.TestCase):
    def test_server_ping_watchdog_is_negotiated_and_satisfied(self):
        server = FakeIntiface(max_ping=3000)
        client = buttplug.Client(server.url)
        try:
            self.assertEqual(client.ping_every, 1.5)
            with mock.patch.object(buttplug.time, "monotonic", return_value=client.next_ping + .01):
                client.poll()
            self.assertTrue(wait_for(lambda: server.commands("Ping")))
        finally:
            client.close()
            server.close()

    def test_long_messages_and_server_pings(self):
        echoed = []

        def echo(ws):
            for message in ws:
                echoed.append(message)
                ws.send(message)
        server = serve(echo, "127.0.0.1", 0, ping_interval=.02, max_size=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            ws = buttplug.WebSocket(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
            for size in (10, 300, 70000):
                ws.send("x" * size)
                reply = None
                deadline = time.monotonic() + 2
                while reply is None and time.monotonic() < deadline:
                    reply = ws.recv(.1)
                self.assertEqual(len(reply or ""), size)
            # Keep answering pings for a while; the server would drop us otherwise.
            for _ in range(10):
                self.assertIsNone(ws.recv(.03))
            ws.send("still here")
            self.assertEqual(wait_for(lambda: ws.recv(.05)), "still here")
            ws.close()
        finally:
            server.shutdown()

    def test_rejects_non_websocket_servers(self):
        with self.assertRaises((buttplug.ConnectionClosed, OSError)):
            buttplug.WebSocket("ws://127.0.0.1:9")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeIntiface()
        self.client = buttplug.Client(self.server.url)
        self.outputs = buttplug.Outputs()

    def tearDown(self):
        self.client.close()
        self.server.close()

    def settle(self):
        wait_for(lambda: (self.client.poll(), not self.client.pending)[1])

    def test_vibration_uses_original_feature_indices(self):
        self.outputs.update(self.client, .4, {}, 0.)
        self.settle()
        command = self.server.commands()[-1]
        self.assertEqual([(s["Index"], s["Scalar"], s["ActuatorType"]) for s in command["Scalars"]],
                         [(0, .4, "Vibrate"), (2, .4, "Vibrate")])

    def test_all_motors_and_device_strength(self):
        self.outputs.update(self.client, .5, {"Lovense Edge": {"on": True, "strength": .5, "all_motors": True}}, 0.)
        self.settle()
        command = self.server.commands()[-1]
        self.assertEqual([s["Index"] for s in command["Scalars"]], [0, 1, 2])
        self.assertEqual({s["Scalar"] for s in command["Scalars"]}, {.25})

    def test_only_meaningful_changes_are_sent_and_zero_always_is(self):
        self.outputs.update(self.client, .4, {}, 0.)
        self.settle()
        self.outputs.update(self.client, .404, {}, .1)
        self.outputs.update(self.client, .4, {}, .2)
        self.settle()
        self.assertEqual(len(self.server.commands()), 1)
        self.outputs.update(self.client, 0., {}, .3)
        self.settle()
        self.assertEqual(self.server.commands()[-1]["Scalars"][0]["Scalar"], 0)

    def test_device_switched_off_gets_zero(self):
        self.outputs.update(self.client, .4, {}, 0.)
        self.settle()
        self.outputs.update(self.client, .4, {"Lovense Edge": {"on": False}}, .1)
        self.settle()
        self.assertEqual(self.server.commands()[-1]["Scalars"][0]["Scalar"], 0)

    def test_device_test_holds_then_returns(self):
        self.outputs.hold[3] = (.5, .8)
        self.outputs.update(self.client, 0., {}, 0.)
        self.settle()
        self.assertEqual(self.server.commands()[-1]["Scalars"][0]["Scalar"], .5)
        self.outputs.update(self.client, 0., {}, 1.)
        self.settle()
        self.assertEqual(self.server.commands()[-1]["Scalars"][0]["Scalar"], 0)

    def test_hotplug_and_scanning(self):
        self.client.start_scanning()
        self.server.push({"DeviceAdded": dict(device(9, "Satisfyer"), Id=0)})
        self.server.push({"ScanningFinished": {"Id": 0}})
        wait_for(lambda: (self.client.poll(), 9 in self.client.devices and not self.client.scanning)[1])
        self.assertEqual(buttplug.summary(self.client.devices[9])["name"], "Satisfyer")
        self.server.push({"DeviceRemoved": {"Id": 0, "DeviceIndex": 9}})
        wait_for(lambda: (self.client.poll(), 9 not in self.client.devices)[1])

    def test_strokers_alternate_with_strength(self):
        stroker = device(5, "Launch", scalars=(), LinearCmd=[{"StepCount": 99}])
        self.client.devices[5] = stroker
        del self.client.devices[3]
        options = {"Launch": {"all_motors": True}}
        self.outputs.update(self.client, .5, options, 0.)
        self.settle()
        self.outputs.update(self.client, .5, options, 1.)
        self.settle()
        positions = [c["Vectors"][0]["Position"] for c in self.server.commands("LinearCmd")]
        self.assertEqual(positions, [.5, .1])
        self.outputs.update(self.client, 0., options, 1.1)
        self.settle()
        self.assertTrue(self.server.commands("StopDeviceCmd"))


class AppTests(unittest.TestCase):
    """The whole companion against a fake Intiface and a fake game heartbeat."""
    def setUp(self):
        self.server = FakeIntiface()
        self.folder = tempfile.TemporaryDirectory()
        self.mod = Path(self.folder.name)
        self.settings = self.mod / "settings.json"
        self.settings.write_text(json.dumps({"intiface_address": self.server.url, "start_intiface": False}))
        self.app = nch.App(self.mod, self.settings)
        self.seq = 0

    def tearDown(self):
        self.app.disconnect("test over")
        self.server.close()
        self.folder.cleanup()

    def beat(self, **kwargs):
        self.seq += 1
        (self.mod / "state.json").write_text(json.dumps(heartbeat(seq=self.seq, **kwargs)))

    def run_for(self, seconds, start):
        now = start
        while now < start + seconds:
            self.app.tick(now)
            time.sleep(.005)
            now += 1 / 30
        return now

    def test_game_event_reaches_the_device(self):
        self.beat()
        now = self.run_for(.2, 100.)
        self.assertEqual(self.app.link, "connected")
        self.beat(events=[(1, "hurt", 1, 10)])
        self.app.tick(now)
        sent = wait_for(lambda: (self.app.client.poll(), [c for c in self.server.commands() if c["Scalars"][0]["Scalar"]])[1])
        self.assertAlmostEqual(sent[0]["Scalars"][0]["Scalar"], round(.9 * .7, 3), places=3)
        status = json.loads((self.mod / "status.json").read_text())
        self.assertEqual(status["devices"], ["Lovense Edge"])
        # The heartbeat stops (game closed): output must fall to zero within a second.
        self.run_for(1.3, now)
        wait_for(lambda: (self.app.client.poll(), self.server.commands()[-1]["Scalars"][0]["Scalar"] == 0)[1])

    def test_scene_events_reach_the_mixer(self):
        self.beat()
        now = self.run_for(.1, 100.)
        self.beat(events=[(1, "romance:judy", 1, 10), (2, "joytoy", 1, 10)])
        self.app.tick(now)
        self.assertEqual(self.app.mixer.voices["romance"]["feel"], "track:judy")
        self.assertEqual(self.app.mixer.voices["joytoy"]["feel"], "track:joytoy")
        self.beat(events=[(3, "joytoy:end", 1, 10)])
        self.app.tick(now + .1)
        self.assertIn("release", self.app.mixer.voices["joytoy"])

    def test_status_file_is_not_rewritten_needlessly(self):
        self.app.write_status(10.)
        first = (self.mod / "status.json").stat().st_mtime_ns
        time.sleep(.02)
        self.app.write_status(10.5)
        self.assertEqual((self.mod / "status.json").stat().st_mtime_ns, first, "unchanged status waits a second")
        self.app.write_status(11.1)
        self.assertNotEqual((self.mod / "status.json").stat().st_mtime_ns, first)

    def test_inactive_game_ignores_events_but_test_buzz_plays(self):
        self.beat()
        now = self.run_for(.1, 100.)
        self.beat(active=False, events=[(1, "hurt", 1, 10), (2, "test", 1, 10)])
        self.app.tick(now)
        self.assertEqual(set(self.app.mixer.contributions), {"test"})

    def test_http_api(self):
        server = nch.make_server(self.app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port
        try:
            def call(method, path, body=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request(method, path, json.dumps(body) if body is not None else None,
                                   dict({"Content-Type": "application/json"}, **(headers or {})))
                response = connection.getresponse()
                try:
                    return response.status, response.read()
                finally:
                    connection.close()
            status, page = call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"Night City", page)
            self.assertEqual(call("GET", "/", headers={"Host": "evil.example:80"})[0], 403)
            status, font = call("GET", "/fonts/archivo.woff2")
            self.assertEqual((status, font[:4]), (200, b"wOF2"), "the page's font is served locally")
            self.assertNotIn(b"https://", page, "the page loads nothing from the internet")
            self.assertEqual(call("POST", "/api/settings", {"max_strength": .2})[0], 403, "needs X-NCH")
            for path in ("/api/settings", "/api/try", "/api/test-device"):
                self.assertEqual(call("POST", path, [], {"X-NCH": "1"})[0], 400,
                                 "valid JSON with the wrong body type is rejected")
            self.assertEqual(call("POST", "/api/settings", {"max_strength": .2, "interactions": {"loot": {"on": True}}},
                                  {"X-NCH": "1"})[0], 200)
            saved = json.loads(self.settings.read_text())
            self.assertEqual(saved["max_strength"], .2)
            self.assertTrue(saved["interactions"]["loot"]["on"])
            self.assertEqual(saved["interactions"]["loot"]["strength"], .15, "partial updates keep other fields")
            self.assertEqual(call("POST", "/api/try", {"id": "kill"}, {"X-NCH": "1"})[0], 200)
            self.assertIn("try:kill", self.app.mixer.voices)
            self.assertEqual(call("POST", "/api/try", {"id": "nope"}, {"X-NCH": "1"})[0], 400)
            state = json.loads(call("GET", "/api/state", headers={"X-NCH-Catalog": "1"})[1])
            self.assertEqual(len(state["catalog"]["interactions"]), len(CATALOG["interactions"]))
            # Keep-alive: the page reuses one connection; a rejected POST must not poison it.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("POST", "/api/settings", json.dumps({"max_strength": .9}), {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual((response.status, response.version), (403, 11))
            response.read()
            connection.request("GET", "/api/ping")
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read())["app"], "night-city-haptics", "same connection still works")
            connection.close()
            self.assertEqual(call("POST", "/api/try", {"id": "romance"}, {"X-NCH": "1"})[0], 200)
            self.assertEqual(self.app.mixer.voices["try:romance"]["feel"], "track:stout")
            self.assertEqual(call("POST", "/api/reset", {}, {"X-NCH": "1"})[0], 200)
            self.assertEqual(json.loads(self.settings.read_text())["max_strength"], .2,
                             "interaction reset preserves the user's output cap")
            self.assertEqual(json.loads(self.settings.read_text())["intiface_address"], self.server.url,
                             "reset keeps the Intiface address")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
