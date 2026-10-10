"""Compare the native pure mixer with the retained Python implementation.

Build the probe with `cargo build --manifest-path rust/Cargo.toml --example
haptics_probe`, then run this test. Python is an offline oracle only.
"""
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "tests/reference/haptics.py"
if not REFERENCE.exists():
    REFERENCE = ROOT / "src/companion/haptics.py"
spec = importlib.util.spec_from_file_location("haptics_oracle", REFERENCE)
haptics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(haptics)
CATALOG = json.loads((ROOT / "src/companion/catalog.json").read_text())
PROBE = Path(os.environ.get("NCH_HAPTICS_PROBE", str(ROOT / "rust/target/debug/examples/haptics_probe")))


def oracle(request):
    op = request["op"]
    if op == "settings":
        return haptics.normalize_settings(request["raw"], CATALOG)
    if op == "preview":
        return haptics.preview(CATALOG, request["kind"], request["feel"], request.get("samples", 48))
    if op == "texture":
        return haptics.texture(request["feel"], request["t"], request.get("level", 1.), request.get("seed", 0.))
    if op == "boundary":
        return haptics.GameLink._state(request["raw"])
    if op == "envelope":
        return haptics.envelope(request["points"], request["t"])
    if op == "blend":
        return haptics.blend(request["values"], request["cap"])
    if op == "mixer":
        mixer = haptics.Mixer(CATALOG)
        settings = haptics.normalize_settings(request.get("settings"), CATALOG)
        observations = []
        for step in request["steps"]:
            now, op = step.get("now", 0.), step["op"]
            if op in ("trigger", "event"):
                fn = mixer.trigger if op == "trigger" else mixer.game_event
                fn(step["name"], step["magnitude"], now, step["feel"], step.get("seconds"))
            elif op == "release":
                mixer.release(step["name"], now)
            elif op == "clear":
                mixer.clear(step.get("keep", ()))
            elif op == "settings":
                settings = haptics.normalize_settings(step["raw"], CATALOG)
            elif op == "effect":
                mixer.effects[step["name"]] = copy.deepcopy(step["effect"])
            elif op == "voice":
                mixer.voices[step["name"]] = copy.deepcopy(step["voice"])
            elif op == "tick":
                mixer.tick(now, step.get("levels"), settings)
            observations.append(copy.deepcopy({"voices": mixer.voices, "level_start": mixer.level_start,
                                               "output": mixer.output, "contributions": mixer.contributions}))
        return observations
    if op == "link":
        link = haptics.GameLink(request.get("timeout", 1.))
        observations = []
        for step in request["steps"]:
            now = step["now"]
            events = link.update(step["raw"], now) if "raw" in step else []
            observations.append(copy.deepcopy({"events": events, "session": link.session, "seq": link.seq,
                                               "clock": link.clock, "last_event": link.last_event,
                                               "changed": link.changed, "state": link.state, "live": link.live(now),
                                               "active": link.active(now), "levels": link.levels(now),
                                               "describe": link.describe(now)}))
        return observations
    raise AssertionError(op)


def heartbeat(seq=1, t=10., **fields):
    return {"v": 2, "session": "sample", "seq": seq, "t": t, "active": True,
            "events": [], "levels": {}, **fields}


class RustHapticsParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROBE.is_file():
            raise unittest.SkipTest("Rust haptics probe must be built first")

    def assert_nested(self, actual, expected, path="result"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(set(actual), set(expected), path)
            for key in expected:
                self.assert_nested(actual[key], expected[key], f"{path}.{key}")
        elif isinstance(expected, (tuple, list)):
            self.assertEqual(len(actual), len(expected), path)
            for index, (a, e) in enumerate(zip(actual, expected)):
                self.assert_nested(a, e, f"{path}[{index}]")
        elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
            self.assertIsInstance(actual, (float, int), path)
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9), f"{path}: {actual!r} != {expected!r}")
        else:
            self.assertEqual(actual, expected, path)

    def compare(self, requests):
        result = subprocess.run([str(PROBE)], input="".join(json.dumps(r, allow_nan=False) + "\n" for r in requests),
                                text=True, capture_output=True, timeout=60, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), len(requests), result.stderr)
        for index, (line, request) in enumerate(zip(lines, requests)):
            with self.subTest(case=index, op=request["op"]):
                self.assert_nested(json.loads(line), oracle(request))

    def test_every_feel_preview_and_texture(self):
        requests = []
        for kind, feels in CATALOG["feels"].items():
            for feel in feels:
                for samples in (1, 48, 113):
                    requests.append({"op": "preview", "kind": kind, "feel": feel["id"], "samples": samples})
                if kind == "level":
                    for t in (-.3, 0., .06, .1, .24, .34, .36, .55, .7, 1.2, 3., 15.71):
                        for level in (0., .3, 1.):
                            requests.append({"op": "texture", "feel": feel["id"], "t": t, "level": level, "seed": 3.})
        for seed in (0., 3., 9., 551.125):
            for t in [i / 100 for i in range(700)]:
                requests.append({"op": "texture", "feel": "joytoy", "t": t, "seed": seed})
        self.compare(requests)

    def test_settings_malformed_values_outputs_legacy_and_limit(self):
        raws = [None, [], True, {}, {"enabled": False}, {"intiface_address": "wss://wrong"}, {"intiface_address": "ws://" + "é" * 120}]
        for value in (True, False, None, {}, [], -.5, 1.3, "0.2", "NaN", "inf", "1_0", " .4 ", "bad", "０.５", "٠.٥", "𝟘.𝟝", "٠_١.٥", "1__2"):
            raws.append({"max_strength": value, "interactions": {i["id"]: {"on": value, "strength": value, "feel": "scene"} for i in CATALOG["interactions"]},
                         "devices": {"test": {"on": value, "strength": value, "all_motors": True, "outputs": {c: value for c in (*haptics.CHANNELS, "spray")}}}})
        raws.append({"devices": {f"device-{i}": {"strength": i / 100, "outputs": {"vibrate": i % 2 == 0, "heat": True}} for i in range(70)}})
        self.compare([{"op": "settings", "raw": raw} for raw in raws])

    def test_heartbeat_boundaries_and_sequence_transitions(self):
        valid = [heartbeat(), heartbeat(events={}, levels=[], scene=[]), heartbeat(v=2.), heartbeat(session="é" * 128),
                 heartbeat(scene={"name": "judy", "elapsed": 3600., "instance": 0, "extra": "dropped"})]
        invalid = [None, [], {}, heartbeat(v=True), heartbeat(session=""), heartbeat(session="a" * 129), heartbeat(seq=True),
                   heartbeat(seq=1.), heartbeat(seq=-1), heartbeat(t=-1), heartbeat(t="1"), heartbeat(active=1), heartbeat(paused=1),
                   heartbeat(events=[None] * 257), heartbeat(events="no"), heartbeat(levels={str(i): 0 for i in range(65)}),
                   heartbeat(levels={"engine": True}), heartbeat(levels={"engine": "1"}), heartbeat(scene=True),
                   heartbeat(scene={"name": "judy", "elapsed": 0, "instance": True}),
                   heartbeat(scene={"name": "judy", "elapsed": 3601, "instance": 0})]
        events = [None, {}, [], [True, "hurt", 1, 10], [-1, "hurt", 1, 10], [1, "", 1, 10], [2, "x" * 129, 1, 10],
                  [3, "hurt", True, 10], [4, "hurt", 1, -1], [5, "hurt", 1, 10, .049], [6, "hurt", 1, 10, 600.1],
                  [7, "hurt", 3, 10], [8, "hurt", .5, 10, .05], [9, "hurt", -.5, 10, 600]]
        self.compare([{"op": "boundary", "raw": raw} for raw in valid + invalid + [heartbeat(events=events)]])
        steps = [{"now": 0, "raw": heartbeat(events=[[5, "hurt", 1, 10]])},
                 {"now": .1, "raw": heartbeat(events=[[6, "kill", 1, 10]])},
                 {"now": .2, "raw": heartbeat(2, events=[[5, "hurt", 1, 10], [6, "kill", .5, 10]], levels={"engine": .5})},
                 {"now": 1.2}, {"now": 1.20001},
                 {"now": 1.3, "raw": heartbeat(3, t=12., events=[[9,"kill",1,10.49],[8,"jump",1,12.11],[7,"hurt",1,12]], paused=True)},
                 {"now": 1.4, "raw": heartbeat(4, t=-1, levels={"engine": 1})},
                 {"now": 1.5, "raw": heartbeat(5, t=1, events=[[10,"hurt",1,1]])},
                 {"now": 1.6, "raw": heartbeat(6, t=1.1, events=[[10,"hurt",1,1],[11,"kill",.6,1.1]])},
                 {"now": 1.7, "raw": heartbeat(2, t=1.2, events=[[20,"hurt",1,1.2]])},
                 {"now": 1.8, "raw": heartbeat(3, t=1.3, active=False, reason="loading" * 30)},
                 {"now": 1.9, "raw": heartbeat(1, t=0, session="next", events=[[1,"hurt",1,0]])},
                 {"now": 2., "raw": heartbeat(2, t=.1, session="next", events=[[1,"hurt",1,0],[2,"kill",.2,.1]])}]
        self.compare([{"op": "link", "steps": steps}])

    def test_all_event_feels_scene_variants_release_and_resume(self):
        requests = []
        for feel in CATALOG["feels"]["event"]:
            length = feel["points"][-1][0]
            steps = [{"op": "trigger", "name": "external", "now": 0., "magnitude": 1., "feel": feel["id"]}]
            for t in sorted(set((0., .01, .05, .1, .2, .5, 1., length / 2, length, length + .001))):
                steps.append({"op": "tick", "now": t, "levels": {}})
            requests.append({"op": "mixer", "steps": steps})
        # Check every knot and cue over each full timeline, including exact
        # rise/fall boundaries. Short tracks must not hide long-track errors.
        for track, effect in CATALOG["tracks"].items():
            if not isinstance(effect, dict):
                continue
            times = {0., effect["points"][-1][0] + .001}
            for time, _ in effect["points"]:
                times.update((max(0., time - .00001), time, time + .00001))
            for time, _ in effect.get("cues", []):
                times.update(time + delta for delta in (0., .03, .06, .4, .7, .70001))
            steps = [{"op": "trigger", "name": "external", "now": 0., "magnitude": 1., "feel": "track:" + track}]
            steps += [{"op": "tick", "now": time, "levels": {}} for time in sorted(times)]
            requests.append({"op": "mixer", "steps": steps})
        # Duration clamping and a weaker retrigger compare the scaled waveform,
        # then permit a fresh hit after the previous waveform has fallen away.
        for feel in ("kick", "swell", "slowbuild", "thud"):
            for seconds in (None, 0., -.5, .05, 6., 600., 601.):
                length = next(effect["points"][-1][0] for effect in CATALOG["feels"]["event"] if effect["id"] == feel)
                span = haptics.clamp(seconds, .05, 600.) if seconds else length
                requests.append({"op": "mixer", "steps": [
                    {"op": "trigger", "name": "external", "magnitude": 1., "now": 0., "feel": feel, "seconds": seconds},
                    {"op": "tick", "now": span / 2, "levels": {}},
                    {"op": "trigger", "name": "external", "magnitude": .2, "now": span / 2, "feel": feel, "seconds": seconds},
                    {"op": "tick", "now": span / 2, "levels": {}},
                    {"op": "trigger", "name": "external", "magnitude": .1, "now": span + .001, "feel": feel, "seconds": seconds},
                    {"op": "tick", "now": span + .001, "levels": {}}]})
        for track in CATALOG["tracks"]:
            steps = [{"op": "event", "name": "romance:" + track, "now": 100., "magnitude": 1., "feel": "scene", "seconds": 3}]
            for t in (100., 100.1, 103.7, 103.76):
                steps.append({"op": "tick", "now": t, "levels": {}})
            steps.extend([{"op": "event", "name": "romance:pulse", "now": 104., "magnitude": .7, "feel": "scene"},
                          {"op": "tick", "now": 104.05, "levels": {}},
                          {"op": "event", "name": "romance:end", "now": 105., "magnitude": 1, "feel": "scene"}])
            for t in (105., 105.1, 106.5, 106.50001):
                steps.append({"op": "tick", "now": t, "levels": {}})
            requests.append({"op": "mixer", "steps": steps})
        requests.append({"op": "mixer", "steps": [
            {"op": "event", "name": "romance:missing", "now": 0., "magnitude": 1., "feel": "scene"},
            {"op": "event", "name": "romance:pulse", "now": .1, "magnitude": 1., "feel": "slowbuild"},
            {"op": "voice", "name": "romance", "voice": {"start": -40., "magnitude": 1., "feel": "track:judy", "scale": 1.}},
            {"op": "tick", "now": 0., "levels": {}},
            {"op": "clear", "keep": ["try:", "test"]},
            {"op": "trigger", "name": "try:hurt", "now": 1., "magnitude": 1., "feel": "thud"},
            {"op": "trigger", "name": "test", "now": 1., "magnitude": 1., "feel": "double"},
            {"op": "clear", "keep": ["try:", "test"]},
            {"op": "settings", "raw": {"interactions": {"hurt": {"on": False}}}},
            {"op": "tick", "now": 1., "levels": {}}]})
        self.compare(requests)

    def test_random_overlaps_retriggers_levels_and_master_gates(self):
        rng = random.Random(84247)
        settings = haptics.default_settings(CATALOG)
        steps = []
        event_items = [item for item in CATALOG["interactions"] if item["kind"] == "event"]
        level_items = [item for item in CATALOG["interactions"] if item["kind"] == "level"]
        now = 0.
        for i in range(200):
            now += rng.uniform(.005, .15)
            item = rng.choice(event_items)
            feel = rng.choice(sorted(haptics.feel_ids(CATALOG, item)))
            steps.append({"op": "trigger", "name": item["id"], "now": now, "magnitude": rng.random(), "feel": feel, "seconds": rng.choice([None, .5, 4, 10])})
            if i % 13 == 0:
                settings["enabled"] = i % 26 != 0
                settings["max_strength"] = rng.random()
                steps.append({"op": "settings", "raw": copy.deepcopy(settings)})
            levels = {item["id"]: rng.random() for item in rng.sample(level_items, rng.randint(0, len(level_items)))}
            steps.append({"op": "tick", "now": now, "levels": levels})
        self.compare([{"op": "mixer", "steps": steps}])


if __name__ == "__main__":
    unittest.main()
