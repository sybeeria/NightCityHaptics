"""Differential output scheduling against the retained Python 0.3 oracle.

The native probe never opens a socket and cannot communicate with hardware.
Build it with cargo build --manifest-path rust/Cargo.toml --example transport_probe.
"""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/reference"))
import buttplug


class Recorder:
    def __init__(self, devices):
        self.devices = {d["DeviceIndex"]: d for d in devices}
        self.commands = []
        self.now = 0
        self.blocked = set()

    def busy(self, index):
        return index in self.blocked

    def send(self, kind, fields, device=None):
        self.commands.append([self.now, kind, fields])


def oracle(scenario):
    recorder = Recorder(scenario["devices"])
    outputs = buttplug.Outputs()
    for step in scenario["steps"]:
        recorder.now = step["now"]
        if "devices" in step:
            recorder.devices = {d["DeviceIndex"]: d for d in step["devices"]}
        recorder.blocked = set(step.get("busy", []))
        for index, held in step.get("hold", {}).items():
            outputs.hold[int(index)] = tuple(held)
        outputs.update(recorder, step.get("strength", 0), step.get("settings", {}), recorder.now, step.get("quiet", False))
    devices = {str(index): {"summary": buttplug.summary(device), "key": buttplug.settings_key(device),
              "channels": {c: [list(route) for route in routes] for c, routes in buttplug.channels(device).items()},
              "enabled": sorted(buttplug.enabled_channels(device, {}))} for index, device in recorder.devices.items()}
    return {"commands": recorder.commands, "devices": devices}


def device(index=4, gap=0, linear=True, rotate=True, name="Synthetic fixture", display=None):
    kinds = ["Spray", "Vibrate", "Constrict", "Temperature", "Led", "Rotate", "Position", "Oscillate", "Inflate", "Vibrate"]
    messages = {"ScalarCmd": [{"ActuatorType": kind, "StepCount": 20} for kind in kinds], "StopDeviceCmd": {}}
    if linear:
        messages["LinearCmd"] = [{"StepCount": 99}, {"StepCount": 99}]
    if rotate:
        messages["RotateCmd"] = [{"StepCount": 20}, {"StepCount": 20}]
    result = {"DeviceIndex": index, "DeviceName": name, "DeviceMessageTimingGap": gap, "DeviceMessages": messages}
    if display:
        result["DeviceDisplayName"] = display
    return result


PROBE = Path(os.environ.get("NCH_RUST_TRANSPORT_PROBE", ROOT / "rust/target/debug/examples/transport_probe"))


@unittest.skipUnless(PROBE.is_file(), "build Rust transport_probe first")
class RustTransportParity(unittest.TestCase):
    def compare(self, scenario):
        actual = json.loads(subprocess.run([str(PROBE)], input=json.dumps(scenario), text=True, capture_output=True, check=True).stdout)
        self.assertEqual(actual, oracle(scenario))

    def test_all_channels_follower_release_hold_and_quiet(self):
        d = device()
        settings = {buttplug.settings_key(d): {"outputs": {c: True for c in buttplug.CHANNELS}}}
        steps = [{"now": i / 30, "strength": .8 if i < 300 else 0, "settings": settings} for i in range(600)]
        steps += [{"now": 20., "strength": 0, "settings": settings, "hold": {"4": [.5, 20.8]}, "quiet": True},
                  {"now": 20.5, "strength": 0, "settings": settings, "quiet": True},
                  {"now": 20.9, "strength": 0, "settings": settings, "quiet": True}]
        self.compare({"devices": [d], "steps": steps})

    def test_random_settings_capability_forms_and_backpressure(self):
        rng = random.Random(419773)
        for linear in (False, True):
            for rotate in (False, True):
                d = device(gap=100, linear=linear, rotate=rotate)
                steps = []
                settings = {}
                for i in range(600):
                    if i % 31 == 0:
                        settings = {buttplug.settings_key(d): {"on": rng.random() > .1, "strength": rng.random(),
                            "all_motors": rng.random() > .5, "outputs": {c: rng.random() > .3 for c in buttplug.CHANNELS}}}
                    steps.append({"now": i / 30, "strength": rng.random(), "settings": settings,
                                  "busy": [4] if rng.random() < .3 else [], "quiet": i % 97 == 0})
                steps.append({"now": 21., "strength": 0., "quiet": True})
                with self.subTest(linear=linear, rotate=rotate):
                    self.compare({"devices": [d], "steps": steps})

    def test_hotplug_capability_replacement_and_unicode_preferences(self):
        d = device(name="Éléphant 😀", display="Gauche 🦄", gap=100)
        replacement = device(name=d["DeviceName"], display=d["DeviceDisplayName"], gap=100, linear=False, rotate=False)
        settings = {buttplug.settings_key(d): {"all_motors": True, "strength": .5}}
        self.compare({"devices": [d], "steps": [
            {"now": 0, "strength": 1, "settings": settings, "hold": {"4": [.5, .8]}},
            {"now": .1, "strength": 1, "settings": settings},
            {"now": .11, "strength": 1, "settings": settings, "devices": [replacement]},
            {"now": .22, "strength": 1, "settings": settings},
            {"now": .3, "devices": []}, {"now": .4, "devices": [d]}, {"now": .6, "strength": .7, "settings": settings}]})

    def test_rounding_small_changes_and_direction_impacts(self):
        d = device(linear=False)
        settings = {buttplug.settings_key(d): {"outputs": {"rotate": True}}}
        values = [.0005, .0015, .0025, .0035, .3995, .4, .404, .4, .2, .25, .7, .72, 0]
        self.compare({"devices": [d], "steps": [{"now": i / 10, "strength": v, "settings": settings} for i, v in enumerate(values)]})


if __name__ == "__main__":
    unittest.main()
