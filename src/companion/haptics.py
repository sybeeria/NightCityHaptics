"""Night City Haptics: turns game events and levels into one output strength.

Pure Python, no I/O. The catalog (catalog.json) defines the interactions and
their feels; settings choose which interactions are on, their strength and feel.
"""
from __future__ import annotations

import copy
import math

EVENT_MAX_AGE = 1.5  # seconds; older events in a heartbeat are ignored
RELEASE = 1.5        # seconds a scene track takes to fade when its scene ends
CUE_RISE, CUE_FALL = .06, .64  # shape of one cue pulse in a scene track


def clamp(value, low=0., high=1.):
    if isinstance(value, bool):
        return low
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return low
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def envelope(points, t):
    """Linear interpolation through [[time, value], ...]; zero outside."""
    if t < 0 or not points or t > points[-1][0]:
        return 0.
    previous = points[0]
    for point in points:
        if t <= point[0]:
            span = point[0] - previous[0]
            if span <= 0:
                return point[1]
            return previous[1] + (point[1] - previous[1]) * (t - previous[0]) / span
        previous = point
    return previous[1]


def _noise(t, rate=12.):
    # Deterministic smooth noise: a hash per step, cosine-blended between steps.
    step = math.floor(t * rate)
    blend = (1 - math.cos((t * rate - step) * math.pi)) / 2

    return _rand(step) * (1 - blend) + _rand(step + 1) * blend


def _rand(n):
    return (math.sin(n * 12.9898 + 78.233) * 43758.5453) % 1


def _joytoy(t, seed):
    # The joytoy scenes' own rumble loop: a heavy or light pulse, then a random
    # 0.5/0.7/0.9/1.1 s pause, again and again (reports/scene-research-20261010).
    start, k = 0., 0
    while True:
        heavy = _rand(seed * 7.31 + k) < .5
        length = .3 if heavy else .18
        if t < start + length:
            return 1. if heavy else .6
        start += length + (.5, .7, .9, 1.1)[int(_rand(seed * 3.17 + k + .5) * 4) % 4]
        if t < start:
            return 0.
        k += 1


def texture(name, t, level=1., seed=0.):
    """Continuous modulation in 0..1 for level interactions."""
    if name == "rumble":
        return .55 + .45 * _noise(t)
    if name == "wave":
        return .25 + .75 * (1 - math.cos(t / 2.6 * 2 * math.pi)) / 2
    if name == "throb":
        return 1. if (t / .7) % 1 < .5 else .15
    if name == "flutter":
        return 1. if (t / .2) % 1 < .5 else 0.
    if name == "footsteps":
        return 1. if (t / .36) % 1 < .3 else 0.
    if name == "ring":
        # A phone ringing: two buzzes, then a pause; a 3-second cycle.
        phase = t % 3
        return 1. if phase < .4 or .6 <= phase < 1. else 0.
    if name == "glitch":
        # Irregular stutter, like the relic's screen glitches.
        step = math.floor(t * 18)
        return .5 + .5 * _rand(step + .37) if _rand(step) > .45 else 0.
    if name == "joytoy":
        return _joytoy(t, seed)
    if name == "heartbeat":
        # Quickens from 60 to 110 bpm as the level rises.
        phase = (t / (1. - .45 * clamp(level))) % 1
        if phase < .12:
            return 1.
        if .24 <= phase < .34:
            return .7
        return 0.
    return 1.


def feel_ids(catalog, item):
    """The feels this interaction may use; a feel with "for" belongs to those interactions only."""
    return {f["id"] for f in catalog["feels"][item["kind"]] if item["id"] in f.get("for", [item["id"]])}


def default_settings(catalog):
    settings = copy.deepcopy(catalog["defaults"])
    settings["interactions"] = {item["id"]: dict(item["default"]) for item in catalog["interactions"]}
    return settings


def normalize_settings(raw, catalog):
    """Fill in missing values and drop invalid ones; never raises."""
    settings = default_settings(catalog)
    if not isinstance(raw, dict):
        return settings
    if isinstance(raw.get("enabled"), bool):
        settings["enabled"] = raw["enabled"]
    if "max_strength" in raw:
        settings["max_strength"] = clamp(raw["max_strength"])
    if isinstance(raw.get("start_intiface"), bool):
        settings["start_intiface"] = raw["start_intiface"]
    address = raw.get("intiface_address")
    if isinstance(address, str) and address.startswith("ws://") and len(address) < 200:
        settings["intiface_address"] = address
    interactions = raw.get("interactions")
    if not isinstance(interactions, dict):
        interactions = {}
    for item in catalog["interactions"]:
        saved = interactions.get(item["id"])
        if not isinstance(saved, dict):
            continue
        target = settings["interactions"][item["id"]]
        if isinstance(saved.get("on"), bool):
            target["on"] = saved["on"]
        if "strength" in saved:
            target["strength"] = clamp(saved["strength"])
        if isinstance(saved.get("feel"), str) and saved["feel"] in feel_ids(catalog, item):
            target["feel"] = saved["feel"]
    devices = raw.get("devices")
    if isinstance(devices, dict):
        for name, options in devices.items():
            if isinstance(name, str) and isinstance(options, dict) and len(settings["devices"]) < 64:
                settings["devices"][name] = {
                    "on": options.get("on") is not False,
                    "strength": clamp(options.get("strength", 1.)),
                    "all_motors": options.get("all_motors") is True,
                }
    return settings


def blend(values, cap):
    """Overlaps add up gently and never pass the cap; one input passes unchanged."""
    cap = clamp(cap)
    active = [min(v, cap) for v in values if v > 0]
    if not active or cap <= 0:
        return 0.
    if len(active) == 1:
        return active[0]
    remaining = 1.
    for value in active:
        remaining *= 1 - value / cap
    return cap * (1 - remaining)


class Mixer:
    """Voices are keyed by interaction id; "<id>#<part>" is a second voice of the same
    interaction (a scene's live pulses) and "try:<id>" a preview from the page."""
    def __init__(self, catalog):
        self.catalog = catalog
        self.kinds = {item["id"]: item["kind"] for item in catalog["interactions"]}
        self.effects = {f["id"]: f for f in catalog["feels"]["event"]}
        for name, track in catalog.get("tracks", {}).items():
            if isinstance(track, dict):
                self.effects["track:" + name] = track
        self.voices = {}      # voice name -> {start, magnitude, feel, scale, release}
        self.level_start = {}  # interaction id -> time the level became active
        self.output = 0.
        self.contributions = {}

    def clear(self, keep=()):
        """Silence everything except voices whose name starts with a prefix in `keep`."""
        self.voices = {k: v for k, v in self.voices.items() if k.startswith(tuple(keep))} if keep else {}
        self.level_start.clear()
        self.output, self.contributions = 0., {}

    def trigger(self, name, magnitude, now, feel=None, seconds=None):
        """Start (or restart) an event voice. A weaker retrigger never cuts a stronger one."""
        magnitude = clamp(magnitude)
        if magnitude <= 0:
            return
        scale = 1.
        effect = self.effects.get(feel)
        if seconds and effect:
            scale = clamp(seconds, .05, 600) / max(.01, effect["points"][-1][0])
        candidate = {"start": now, "magnitude": magnitude, "feel": feel, "scale": scale}
        voice = self.voices.get(name)
        # Swells and scene tracks can start below their event magnitude. Compare
        # the waveforms at this instant so a weaker retrigger cannot cut a stronger voice.
        if (voice and voice.get("feel") == feel and magnitude < voice["magnitude"]
                and self._voice_value(voice, now) > self._voice_value(candidate, now)):
            return
        self.voices[name] = candidate

    def game_event(self, event_id, magnitude, now, feel, seconds=None):
        """Play one game event. "<id>:<variant>" picks a scene track when the feel is
        "scene"; "<id>:end" fades the interaction's voices; "<id>:pulse" is a live accent."""
        name, _, variant = event_id.partition(":")
        if variant == "end":
            self.release(name, now)
            return
        if variant == "pulse":
            effect = "track:pulse" if feel == "scene" else feel
            if effect in self.effects and not self.effects[effect]["points"][-1][0] > 10:
                self.trigger(name + "#pulse", magnitude, now, effect, seconds)
            return
        if feel == "scene":
            track = "track:" + (variant or name)
            feel = track if track in self.effects else "swell"
            seconds = None if track in self.effects else seconds
        self.trigger(name, magnitude, now, feel, seconds)

    def release(self, name, now):
        for key, voice in self.voices.items():
            if (key == name or key.startswith(name + "#")) and "release" not in voice:
                voice["release"] = now

    def _voice_value(self, voice, now):
        effect = self.effects.get(voice["feel"])
        if not effect:
            return 0.
        t = (now - voice["start"]) / voice["scale"]
        value = envelope(effect["points"], t)
        if effect.get("texture"):
            value *= texture(effect["texture"], now - voice["start"], seed=voice["start"])
        for cue_time, strength in effect.get("cues", ()):
            age = t - cue_time
            if 0 <= age < CUE_RISE:
                value = max(value, strength * age / CUE_RISE)
            elif CUE_RISE <= age < CUE_RISE + CUE_FALL:
                value = max(value, strength * (1 - (age - CUE_RISE) / CUE_FALL))
        if "release" in voice:
            value *= max(0., 1 - (now - voice["release"]) / RELEASE)
        return value * voice["magnitude"]

    def _finished(self, voice, now):
        effect = self.effects.get(voice["feel"])
        return (not effect or (now - voice["start"]) / voice["scale"] > effect["points"][-1][0]
                or now - voice.get("release", now) > RELEASE)

    def tick(self, now, levels, settings, extra_voices=()):
        """Return (output, {interaction: strength}) for this moment."""
        master = clamp(settings["max_strength"])
        rules = settings["interactions"]
        raw = {}
        for name, voice in list(self.voices.items()):
            if self._finished(voice, now):
                del self.voices[name]
                continue
            # "try:<id>" previews an interaction even while it is switched off;
            # "test" is the in-game test buzz.
            trying = name.startswith("try:")
            rule = rules.get((name[4:] if trying else name).split("#")[0])
            if name == "test":
                strength = .6
            elif rule and (rule["on"] or trying):
                strength = rule["strength"]
            else:
                continue
            raw[name] = self._voice_value(voice, now) * strength * master
        for name, level in (levels or {}).items():
            rule = rules.get(name)
            level = clamp(level)
            if not rule or self.kinds.get(name) != "level" or level <= 0 or not rule["on"]:
                self.level_start.pop(name, None)
                continue
            start = self.level_start.setdefault(name, now)
            value = level * texture(rule["feel"], now - start, level) * rule["strength"] * master
            raw[name] = max(raw.get(name, 0.), value)
        for name in list(self.level_start):
            if name not in (levels or {}):
                del self.level_start[name]
        self.output = blend(raw.values(), master) if settings["enabled"] else 0.
        contributions = {}
        if settings["enabled"]:
            for name, value in raw.items():
                if value > .001:
                    key = name.split("#")[0]
                    contributions[key] = max(contributions.get(key, 0.), value)
        self.contributions = contributions
        return self.output, self.contributions


class GameLink:
    """Reads the CET heartbeat (state.json) and decides whether output may flow.

    Output needs a heartbeat whose sequence advanced within `timeout` seconds.
    A file left over from an earlier session is only ever a baseline: events
    already in it are skipped and nothing plays until the sequence advances.
    """
    def __init__(self, timeout=1.):
        self.timeout = timeout
        self.session = None
        self.seq = -1
        self.clock = None
        self.last_event = -1
        self.changed = None
        self.state = {}

    def update(self, raw, now):
        """Return the list of new events [(id, magnitude, seconds)] in this heartbeat."""
        state = self._state(raw)
        if state is None:
            # A corrupt/part-written heartbeat must not leave the previous live
            # levels flowing while we wait for the normal timeout.
            self.changed, self.state = None, {}
            return []
        session, seq = state["session"], state["seq"]
        events = state["events"]
        if session != self.session:
            self.session, self.seq, self.changed, self.state = session, seq, None, state
            self.clock = state["t"]
            self.last_event = max([e[0] for e in events], default=-1)
            return []
        rewound = self.clock is not None and state["t"] < self.clock
        if seq <= self.seq or rewound:
            if seq < self.seq or rewound:  # rewritten file; treat as a fresh baseline
                self.seq, self.changed, self.state = seq, None, state
                self.clock = state["t"]
                self.last_event = max([e[0] for e in events], default=-1)
            return []
        self.seq, self.changed, self.state = seq, now, state
        self.clock = game_time = state["t"]
        fresh = []
        for event in sorted(events, key=lambda e: e[0]):
            if event[0] <= self.last_event:
                continue
            self.last_event = event[0]
            if game_time - event[3] > EVENT_MAX_AGE or event[3] > game_time + .1:
                continue
            seconds = event[4] if len(event) > 4 else None
            fresh.append((event[1], clamp(event[2]), seconds))
        return fresh

    @staticmethod
    def _state(raw):
        """Validate the file boundary before updating any heartbeat state.

        Unknown fields are retained for diagnostics. Malformed collections and
        scene metadata fail closed; a malformed individual event is discarded.
        """
        def finite(value):
            try:
                return type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                return False

        if not isinstance(raw, dict) or raw.get("v") != 2:
            return None
        if not isinstance(raw.get("session"), str) or not 0 < len(raw["session"]) <= 128:
            return None
        if (type(raw.get("seq")) is not int or raw["seq"] < 0
                or not finite(raw.get("t")) or raw["t"] < 0):
            return None
        if type(raw.get("active")) is not bool or type(raw.get("paused", False)) is not bool:
            return None
        events, levels = raw.get("events", []), raw.get("levels", {})
        # CET's JSON encoder emits an empty Lua table as []. Empty levels/scene
        # therefore have an array representation while populated ones are objects.
        if levels == []:
            levels = {}
        if events == {}:
            events = []
        if not isinstance(events, list) or len(events) > 256 or not isinstance(levels, dict) or len(levels) > 64:
            return None
        if any(not isinstance(key, str) or not finite(value) for key, value in levels.items()):
            return None
        state = dict(raw)
        state["levels"] = {key: clamp(value) for key, value in levels.items()}
        state["events"] = []
        for event in events:
            if (not isinstance(event, list) or not 4 <= len(event) <= 5
                    or type(event[0]) is not int or event[0] < 0
                    or not isinstance(event[1], str) or not 0 < len(event[1]) <= 128
                    or not finite(event[2]) or not finite(event[3]) or event[3] < 0):
                continue
            if len(event) == 5 and (not finite(event[4]) or not .05 <= event[4] <= 600):
                continue
            state["events"].append(event)
        if "scene" in raw:
            scene = raw["scene"]
            if scene is None or scene == {} or scene == []:
                state["scene"] = None
            elif (isinstance(scene, dict) and isinstance(scene.get("name"), str)
                  and 0 < len(scene["name"]) <= 64 and finite(scene.get("elapsed"))
                  and 0 <= scene["elapsed"] <= 3600 and type(scene.get("instance")) is int
                  and scene["instance"] >= 0):
                state["scene"] = {key: scene[key] for key in ("name", "elapsed", "instance")}
            else:
                return None
        return state

    def live(self, now):
        return self.changed is not None and now - self.changed <= self.timeout

    def active(self, now):
        return self.live(now) and self.state.get("active") is True and self.state.get("paused") is not True

    def levels(self, now):
        if not self.active(now):
            return {}
        levels = self.state.get("levels")
        return {k: clamp(v) for k, v in levels.items() if isinstance(k, str)} if isinstance(levels, dict) else {}

    def describe(self, now):
        if self.changed is None:
            return "waiting", "Load a save in Cyberpunk"
        if not self.live(now):
            return "offline", "Game closed or loading"
        if self.state.get("paused"):
            return "paused", "Paused from the CET overlay"
        if not self.state.get("active"):
            return "idle", str(self.state.get("reason") or "Not in gameplay")[:60]
        return "live", "In gameplay"
