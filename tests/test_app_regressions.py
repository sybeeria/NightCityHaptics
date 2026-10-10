"""Offline regressions for file validation, scene resume and fail-closed output.

These tests use temporary files and a command recorder, never an engine/device.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src" / "companion"
sys.path.insert(0, str(Path(__file__).resolve().parent / "reference"))
import buttplug
import haptics
import nch

CATALOG = json.loads((SRC / "catalog.json").read_text())


def heartbeat(seq=1, **changes):
    state = {"v": 2, "session": "regression", "seq": seq, "t": 10.,
             "active": True, "paused": False, "events": [], "levels": {}}
    state.update(changes)
    return state


def device(index=1, display="Fixture"):
    return {"DeviceIndex": index, "DeviceName": "Same model", "DeviceDisplayName": display,
            "DeviceMessages": {"ScalarCmd": [{"ActuatorType": "Vibrate", "StepCount": 20}],
                               "StopDeviceCmd": {}}, "DeviceMessageTimingGap": 0}


class RecordingClient:
    def __init__(self):
        self.devices = {1: device()}
        self.commands = []
        self.scanning = False
        self.errors = []
        self.server_name = "Offline fixture"
        self.closed = False
        self.battery = {}

    def send(self, kind, fields, device=None):
        self.commands.append((kind, fields))
        return len(self.commands)

    def busy(self, index):
        return False

    def poll(self):
        pass

    def stop_all(self):
        self.send("StopAllDevices", {})

    def close(self):
        self.closed = True


class ValidationRegressions(unittest.TestCase):
    def test_malformed_nested_settings_keep_valid_limits_and_recover_defaults(self):
        for malformed in (True, "bad", [1], 42):
            with self.subTest(interactions=malformed):
                settings = haptics.normalize_settings({"enabled": False, "max_strength": .05,
                                                       "interactions": malformed}, CATALOG)
                self.assertFalse(settings["enabled"])
                self.assertEqual(settings["max_strength"], .05)
                self.assertEqual(settings["interactions"], haptics.default_settings(CATALOG)["interactions"])
        for feel in ([], {}, True, 4):
            with self.subTest(feel=feel):
                settings = haptics.normalize_settings({"interactions": {"hurt": {"feel": feel,
                                                                               "strength": .2}}}, CATALOG)
                self.assertEqual(settings["interactions"]["hurt"]["feel"], "thud")
                self.assertEqual(settings["interactions"]["hurt"]["strength"], .2)
        self.assertEqual(haptics.clamp(10 ** 1000), 0.)

    def test_malformed_heartbeat_silences_previous_live_levels_immediately(self):
        malformed_fields = [{"events": None}, {"events": True}, {"events": {"bad": 1}},
                            {"levels": [1]}, {"levels": {"engine": float("nan")}},
                            {"t": float("inf")}, {"t": 10 ** 1000}, {"active": "yes"},
                            {"scene": {"name": "judy", "elapsed": float("nan"), "instance": 1}}]
        for fields in malformed_fields:
            with self.subTest(fields=fields):
                link = haptics.GameLink()
                link.update(heartbeat(), 0.)
                link.update(heartbeat(2, levels={"engine": .5}), .1)
                self.assertTrue(link.active(.1))
                self.assertEqual(link.update(heartbeat(3, **fields), .11), [])
                self.assertFalse(link.active(.11))
                self.assertEqual(link.levels(.11), {})
                link.update(heartbeat(4, levels={"engine": .2}), .2)
                self.assertEqual(link.levels(.2), {"engine": .2})

    def test_bad_event_numbers_are_discarded_without_poisoning_valid_events(self):
        link = haptics.GameLink()
        link.update(heartbeat(), 0.)
        events = [[1, "external", 1, 10, float("nan")], [2, "hurt", 1, float("inf")],
                  [3, "hurt", float("nan"), 10], [4, "hurt", 1, 11], [5, "kill", .5, 10]]
        self.assertEqual(link.update(heartbeat(2, events=events), .1), [("kill", .5, None)])
        self.assertTrue(link.active(.1))

    def test_cet_empty_tables_allow_live_inactive_heartbeats(self):
        link = haptics.GameLink()
        link.update(heartbeat(active=False, levels=[], scene=[]), 0.)
        self.assertEqual(link.update(heartbeat(2, active=False, levels=[], scene=[],
                                              events=[[1, "test", 1., 10.]]), .1),
                         [("test", 1., None)])
        self.assertTrue(link.live(.1))
        self.assertFalse(link.active(.1))
        self.assertEqual(link.levels(.1), {})
        self.assertIsNone(link.state["scene"])

    def test_rewritten_sequence_rebaselines_event_counter(self):
        link = haptics.GameLink()
        link.update(heartbeat(10, events=[[10, "hurt", 1, 10]]), 0.)
        link.update(heartbeat(11), .1)
        self.assertEqual(link.update(heartbeat(1, events=[[1, "hurt", 1, 10]]), .2), [])
        self.assertFalse(link.live(.2))
        self.assertEqual(link.update(heartbeat(2, events=[[2, "kill", 1, 10]]), .3),
                         [("kill", 1., None)])


class AppRegressions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.mod = Path(self.folder.name)
        self.path = self.mod / "settings.json"
        self.path.write_text(json.dumps({"start_intiface": False}))
        self.app = nch.App(self.mod, self.path)
        self.client = RecordingClient()
        self.app.client = self.client
        self.app.link = "connected"

    def tearDown(self):
        self.folder.cleanup()

    def test_device_output_switches_merge_validate_and_show_in_the_snapshot(self):
        key = buttplug.settings_key(self.client.devices[1])
        self.client.devices[1]["DeviceMessages"]["ScalarCmd"].append({"ActuatorType": "Temperature", "StepCount": 1})
        self.app.save({"devices": {key: {"outputs": {"heat": True}}}})
        self.app.save({"devices": {key: {"outputs": {"vibrate": False}}}})
        self.assertEqual(self.app.settings["devices"][key]["outputs"], {"vibrate": False, "heat": True})
        for bad in ({"spray": True}, {"heat": "on"}, ["heat"]):
            with self.subTest(outputs=bad), self.assertRaises(ValueError):
                self.app.save({"devices": {key: {"outputs": bad}}})
        self.client.battery[1] = .5
        info = self.app.snapshot()["devices"][0]
        self.assertEqual(info["channels"], {"vibrate": 1, "heat": 1})
        self.assertEqual(info["settings"]["outputs"], {"vibrate": False, "heat": True})
        self.assertEqual(info["battery"], .5)

    def test_nothing_playing_stops_slow_outputs_at_once(self):
        key = buttplug.settings_key(self.client.devices[1])
        self.client.devices[1]["DeviceMessages"]["ScalarCmd"] = [{"ActuatorType": "Constrict", "StepCount": 20}]
        self.app.save({"devices": {key: {"outputs": {"constrict": True}}}})
        self.app.try_interaction("explosion")
        start = self.app.mixer.voices["try:explosion"]["start"]
        for i in range(40):
            self.app.tick(start + i / 30)
        squeezed = [s["Scalar"] for kind, f in self.client.commands if kind == "ScalarCmd" for s in f["Scalars"]]
        self.assertGreater(max(squeezed), 0)
        for i in range(40, 80):
            self.app.tick(start + i / 30)
        self.assertEqual(self.client.commands[-2][0], "StopDeviceCmd", "the preview ended, nothing else can play")

    def beat(self, seq, now, **fields):
        (self.mod / "state.json").write_text(json.dumps(heartbeat(seq, **fields)))
        self.app.tick(now)

    def test_startup_recovers_from_semantically_corrupt_settings(self):
        self.path.write_text(json.dumps({"enabled": False, "max_strength": .1,
                                        "interactions": True, "devices": []}))
        app = nch.App(self.mod, self.path)
        self.assertFalse(app.settings["enabled"])
        self.assertEqual(app.settings["max_strength"], .1)
        self.assertEqual(len(app.settings["interactions"]), len(app.catalog["interactions"]))

    def test_corrupt_state_after_positive_output_sends_zero(self):
        self.beat(1, 0.)
        self.beat(2, .1, levels={"engine": 1.})
        self.assertGreater(self.client.commands[-1][1]["Scalars"][0]["Scalar"], 0)
        self.beat(3, .11, events=None, levels={"engine": 1.})
        self.assertEqual(self.app.output, 0.)
        self.assertTrue(self.client.commands[-1][0] in ("StopDeviceCmd", "ScalarCmd"))
        if self.client.commands[-1][0] == "ScalarCmd":
            self.assertEqual(self.client.commands[-1][1]["Scalars"][0]["Scalar"], 0.)
        for seq, now in ((4, 2.), (5, 10.)):
            self.beat(seq, now, events=None)
            self.assertEqual(self.app.output, 0.)

    def test_unexpected_run_error_attempts_stop_and_closes_connection(self):
        self.beat(1, 0.)
        self.beat(2, .1, levels={"engine": 1.})
        self.app.outputs.hold[1] = (.5, 10.)
        self.app.mixer.trigger("try:hurt", 1., .1, "thud")
        def after_error(_):
            self.assertIn("StopAllDevices", [kind for kind, _ in self.client.commands])
            self.assertTrue(self.client.closed)
            self.assertIsNone(self.app.client)
            self.assertEqual(self.app.output, 0.)
            self.assertEqual(self.app.outputs.hold, {})
            self.assertEqual(self.app.mixer.voices, {})
            self.assertFalse(self.app.game.live(1.))
            self.app.stopping = True
        with mock.patch.object(self.app, "tick", side_effect=RuntimeError("fixture failure")), \
             mock.patch.object(nch.time, "sleep", side_effect=after_error), \
             mock.patch.object(self.app.engine, "stop"), self.assertLogs(level="ERROR"):
            self.app.run()

    def test_disconnect_closes_even_if_stop_send_fails_and_retains_error(self):
        self.client.errors = ["Fixture rejected device stop"]
        self.app.outputs.hold[1] = (.5, 10.)
        with mock.patch.object(self.client, "stop_all", side_effect=OSError("send failed")):
            self.app.disconnect("Connection lost")
        self.assertTrue(self.client.closed)
        self.assertEqual(self.app.outputs.hold, {})
        self.assertEqual(self.app.snapshot()["device_errors"], ["Fixture rejected device stop"])

    def test_reset_restores_only_interactions_preserving_user_output_policy(self):
        self.app.save({"enabled": False, "max_strength": .05, "start_intiface": False,
                       "devices": {"Fixture": {"on": False, "strength": .2}},
                       "interactions": {"hurt": {"on": False, "strength": .1}}})
        before = copy.deepcopy(self.app.settings)
        self.app.reset()
        self.assertEqual(self.app.settings["interactions"], haptics.default_settings(CATALOG)["interactions"])
        for key in ("enabled", "max_strength", "devices", "intiface_address", "start_intiface"):
            self.assertEqual(self.app.settings[key], before[key], key)
        self.assertEqual(json.loads(self.path.read_text()), self.app.settings)

    def test_failed_save_is_transactional_including_connection(self):
        self.app.save({"enabled": False, "max_strength": .05})
        before, disk = copy.deepcopy(self.app.settings), self.path.read_text()
        with mock.patch.object(nch, "write_json", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                self.app.save({"enabled": True, "max_strength": .9,
                               "intiface_address": "ws://127.0.0.1:12346"})
        self.assertEqual(self.app.settings, before)
        self.assertEqual(self.path.read_text(), disk)
        self.assertIs(self.app.client, self.client)
        self.assertFalse(self.client.closed)

    def test_failed_reset_keeps_live_interactions(self):
        self.app.save({"interactions": {"hurt": {"on": False}}})
        before = copy.deepcopy(self.app.settings)
        with mock.patch.object(nch, "write_json", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                self.app.reset()
        self.assertEqual(self.app.settings, before)

    def test_invalid_settings_patch_is_rejected_without_resetting_interactions(self):
        before = copy.deepcopy(self.app.settings)
        for changes in ([], {"interactions": True}, {"devices": {"fixture": []}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.app.save(changes)
            self.assertEqual(self.app.settings, before)

    def test_steady_status_has_advancing_heartbeat_only_on_successful_writes(self):
        self.app.write_status(10.)
        first = json.loads((self.mod / "status.json").read_text())
        self.app.write_status(10.5)
        self.assertEqual(json.loads((self.mod / "status.json").read_text()), first)
        self.app.write_status(11.1)
        second = json.loads((self.mod / "status.json").read_text())
        self.assertEqual(second["heartbeat"], first["heartbeat"] + 1)
        self.assertFalse(second["active"])
        with mock.patch.object(nch, "write_json", side_effect=PermissionError("read only")):
            self.app.write_status(12.2)
        self.app.write_status(12.3)
        self.assertEqual(json.loads((self.mod / "status.json").read_text())["heartbeat"], second["heartbeat"] + 1)

    def test_duplicate_model_test_targets_exact_live_device_and_own_strength(self):
        self.client.devices[2] = device(2, "Right")
        first, second = map(buttplug.summary, self.client.devices.values())
        self.assertNotEqual(first["key"], second["key"])
        self.assertNotEqual(first["settings_key"], second["settings_key"])
        self.app.save({"devices": {first["settings_key"]: {"strength": .2},
                                   second["settings_key"]: {"strength": .8, "on": False}}})
        with mock.patch.object(nch.time, "monotonic", return_value=10.):
            self.app.test_device(second["key"])
        self.assertEqual(self.app.outputs.hold, {2: (.4, 10.8)})
        rows = self.app.snapshot()["devices"]
        self.assertEqual([row["settings"]["strength"] for row in rows], [.2, .8])

    def test_romance_tracks_resume_at_reported_simulation_offset_after_each_gate(self):
        tracks = ("judy", "judyCensored", "kerry", "kerryCensored", "river", "stout", "panam", "panamCensored")
        for name in tracks:
            for gate in ("menu", "cet_pause"):
                with self.subTest(name=name, gate=gate):
                    self.app.game = haptics.GameLink()
                    self.app.mixer.clear()
                    self.app.scene_key = None
                    scene = {"name": name, "elapsed": 1., "instance": 1}
                    self.beat(1, 20., scene=scene)
                    self.assertNotIn("romance", self.app.mixer.voices, "leftover scene only baselines")
                    self.beat(2, 20.1, scene=scene)
                    self.assertAlmostEqual(20.1 - self.app.mixer.voices["romance"]["start"], 1.)
                    self.beat(3, 21., active=gate != "menu", paused=gate == "cet_pause",
                              scene=dict(scene, elapsed=2.))
                    self.assertNotIn("romance", self.app.mixer.voices)
                    self.assertEqual(self.app.output, 0.)
                    # Native menu freezes simulation; CET Pause may let it run.
                    elapsed = 2. if gate == "menu" else 6.
                    self.beat(4, 25., scene=dict(scene, elapsed=elapsed))
                    voice = self.app.mixer.voices["romance"]
                    self.assertEqual(voice["feel"], "track:" + name)
                    self.assertAlmostEqual(25. - voice["start"], elapsed)
                    self.assertGreater(self.app.output, 0.)

    def test_scene_instance_changes_restart_and_end_fades_without_restart(self):
        scene = {"name": "judy", "elapsed": 3., "instance": 1}
        self.beat(1, 10., scene=scene)
        self.beat(2, 10.1, scene=scene, events=[[1, "romance:pulse", 1, 10]])
        self.assertIn("romance#pulse", self.app.mixer.voices, "fresh Panam/Judy accent survives initial sync")
        self.beat(3, 11., scene=dict(scene, instance=2, elapsed=.5))
        self.assertAlmostEqual(self.app.mixer.voices["romance"]["start"], 10.5)
        self.beat(4, 11.1, scene=None)
        self.assertIn("release", self.app.mixer.voices["romance"])
        self.beat(5, 12.7, scene=None)
        self.assertNotIn("romance", self.app.mixer.voices)

    def test_custom_romance_envelope_resumes_remaining_time_and_never_restarts_finished(self):
        self.app.save({"interactions": {"romance": {"feel": "slowbuild"}}})
        scene = {"name": "judy", "elapsed": 1., "instance": 1}
        self.beat(1, 10., scene=scene)
        self.beat(2, 10.1, scene=scene)
        self.assertEqual(self.app.mixer.voices["romance"]["feel"], "slowbuild")
        self.beat(3, 11., paused=True, scene=dict(scene, elapsed=2.))
        self.assertEqual(self.app.output, 0.)
        self.assertNotIn("romance", self.app.mixer.voices)
        self.beat(4, 15., scene=dict(scene, elapsed=6.))
        self.assertAlmostEqual(15. - self.app.mixer.voices["romance"]["start"], 6.)
        self.assertGreater(self.app.output, 0.)
        duration = self.app.mixer.effects["slowbuild"]["points"][-1][0]
        self.beat(5, 25., scene=dict(scene, elapsed=duration + .1),
                  events=[[1, "romance:judy", 1., 10.]])
        self.assertNotIn("romance", self.app.mixer.voices)
        self.assertEqual(self.app.output, 0., "even a repeated anchor cannot restart a finished envelope")
        self.beat(6, 26., paused=True, scene=dict(scene, elapsed=duration + 1.))
        self.beat(7, 27., scene=dict(scene, elapsed=duration + 2.))
        self.assertNotIn("romance", self.app.mixer.voices)
        self.assertEqual(self.app.output, 0.)

    def test_managed_engine_has_negotiated_ping_watchdog(self):
        process = mock.Mock()
        process.poll.return_value = None
        engine = nch.Engine()
        with mock.patch.object(engine, "executable", return_value=self.mod / "fixture-engine"), \
             mock.patch.object(nch, "data_dir", return_value=self.mod), \
             mock.patch.object(nch.subprocess, "Popen", return_value=process) as launch:
            self.assertTrue(engine.start(12345))
            args = launch.call_args[0][0]
            self.assertEqual(args[args.index("--max-ping-time") + 1], "3000")
            engine.stop()


if __name__ == "__main__":
    unittest.main()
