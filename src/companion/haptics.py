"""Night City Haptics: turns game events and levels into one output strength.

Pure Python, no I/O. The catalog (catalog.json) defines the interactions and
their feels; settings choose which interactions are on, their strength and feel.
"""
from __future__ import annotations

import copy
import math

EVENT_MAX_AGE = 1.5  # seconds; older events in a heartbeat are ignored


def clamp(value, low=0., high=1.):
    try:
        value = float(value)
    except (TypeError, ValueError):
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

    def rand(n):
        return (math.sin(n * 12.9898 + 78.233) * 43758.5453) % 1
    return rand(step) * (1 - blend) + rand(step + 1) * blend


def texture(name, t, level=1.):
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
    if name == "heartbeat":
        # Quickens from 60 to 110 bpm as the level rises.
        phase = (t / (1. - .45 * clamp(level))) % 1
        if phase < .12:
            return 1.
        if .24 <= phase < .34:
            return .7
        return 0.
    return 1.


def default_settings(catalog):
    settings = copy.deepcopy(catalog["defaults"])
    settings["interactions"] = {item["id"]: dict(item["default"]) for item in catalog["interactions"]}
    return settings


def normalize_settings(raw, catalog):
    """Fill in missing values and drop invalid ones; never raises."""
    settings = default_settings(catalog)
    if not isinstance(raw, dict):
        return settings
    feels = {kind: {f["id"] for f in items} for kind, items in catalog["feels"].items()}
    if isinstance(raw.get("enabled"), bool):
        settings["enabled"] = raw["enabled"]
    if "max_strength" in raw:
        settings["max_strength"] = clamp(raw["max_strength"])
    if isinstance(raw.get("start_intiface"), bool):
        settings["start_intiface"] = raw["start_intiface"]
    address = raw.get("intiface_address")
    if isinstance(address, str) and address.startswith("ws://") and len(address) < 200:
        settings["intiface_address"] = address
    for item in catalog["interactions"]:
        saved = (raw.get("interactions") or {}).get(item["id"])
        if not isinstance(saved, dict):
            continue
        target = settings["interactions"][item["id"]]
        if isinstance(saved.get("on"), bool):
            target["on"] = saved["on"]
        if "strength" in saved:
            target["strength"] = clamp(saved["strength"])
        if saved.get("feel") in feels[item["kind"]]:
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
    def __init__(self, catalog):
        self.catalog = catalog
        self.kinds = {item["id"]: item["kind"] for item in catalog["interactions"]}
        self.effects = {f["id"]: f for f in catalog["feels"]["event"]}
        self.voices = {}      # interaction id -> {start, magnitude, scale}
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
        voice = self.voices.get(name)
        if voice and voice.get("feel") == feel and self._voice_value(voice, now) > magnitude:
            return
        scale = 1.
        effect = self.effects.get(feel)
        if seconds and effect:
            scale = clamp(seconds, .05, 600) / max(.01, effect["points"][-1][0])
        self.voices[name] = {"start": now, "magnitude": magnitude, "feel": feel, "scale": scale}

    def _voice_value(self, voice, now):
        effect = self.effects.get(voice["feel"])
        if not effect:
            return 0.
        t = (now - voice["start"]) / voice["scale"]
        value = envelope(effect["points"], t)
        if effect.get("texture"):
            value *= texture(effect["texture"], now - voice["start"])
        return value * voice["magnitude"]

    def tick(self, now, levels, settings, extra_voices=()):
        """Return (output, {interaction: strength}) for this moment."""
        master = clamp(settings["max_strength"])
        rules = settings["interactions"]
        raw = {}
        for name, voice in list(self.voices.items()):
            effect = self.effects.get(voice["feel"])
            if not effect or (now - voice["start"]) / voice["scale"] > effect["points"][-1][0]:
                del self.voices[name]
                continue
            # "try:<id>" previews an interaction even while it is switched off;
            # "test" is the in-game test buzz.
            trying = name.startswith("try:")
            rule = rules.get(name[4:] if trying else name)
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
        self.contributions = {k: v for k, v in raw.items() if v > .001} if settings["enabled"] else {}
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
        self.last_event = -1
        self.changed = None
        self.state = {}

    def update(self, raw, now):
        """Return the list of new events [(id, magnitude, seconds)] in this heartbeat."""
        if not isinstance(raw, dict) or raw.get("v") != 2:
            return []
        session, seq = raw.get("session"), raw.get("seq")
        if not isinstance(session, str) or type(seq) is not int:
            return []
        events = [e for e in raw.get("events", []) if isinstance(e, list) and len(e) >= 4
                  and type(e[0]) is int and isinstance(e[1], str)]
        if session != self.session:
            self.session, self.seq, self.changed, self.state = session, seq, None, raw
            self.last_event = max([e[0] for e in events], default=-1)
            return []
        if seq <= self.seq:
            if seq < self.seq:  # rewritten file; treat as a fresh baseline
                self.seq, self.changed = seq, None
            return []
        self.seq, self.changed, self.state = seq, now, raw
        game_time = raw.get("t", 0)
        fresh = []
        for event in sorted(events, key=lambda e: e[0]):
            if event[0] <= self.last_event:
                continue
            self.last_event = event[0]
            if isinstance(game_time, (int, float)) and game_time - clamp(event[3], -1e9, 1e9) > EVENT_MAX_AGE:
                continue
            seconds = event[4] if len(event) > 4 and isinstance(event[4], (int, float)) else None
            fresh.append((event[1], clamp(event[2]), seconds))
        return fresh

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
