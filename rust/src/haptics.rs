//! Pure mixer and CET heartbeat boundary. The catalog remains runtime data.
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::f64::consts::PI;

pub const EVENT_MAX_AGE: f64 = 1.5;
pub const RELEASE: f64 = 1.5;
pub const CUE_RISE: f64 = 0.06;
pub const CUE_FALL: f64 = 0.64;
pub const CHANNELS: [&str; 7] = [
    "vibrate",
    "rotate",
    "oscillate",
    "stroke",
    "constrict",
    "heat",
    "light",
];

pub fn clamp_num(value: f64) -> f64 {
    clamp_num_range(value, 0., 1.)
}
pub fn clamp_num_range(value: f64, low: f64, high: f64) -> f64 {
    if value.is_finite() {
        value.min(high).max(low)
    } else {
        low
    }
}

/// Settings may contain numeric strings; booleans deliberately never act as numbers.
pub fn clamp(value: &Value) -> f64 {
    clamp_range(value, 0., 1.)
}
pub fn clamp_range(value: &Value, low: f64, high: f64) -> f64 {
    let number = match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => {
            let text: String = text.trim().chars().map(decimal_digit).collect();
            let valid_underscores = text.char_indices().all(|(i, ch)| {
                ch != '_'
                    || (i > 0
                        && text.as_bytes()[i - 1].is_ascii_digit()
                        && text.as_bytes().get(i + 1).is_some_and(u8::is_ascii_digit))
            });
            if valid_underscores {
                text.replace('_', "").parse::<f64>().ok()
            } else {
                None
            }
        }
        _ => None,
    };
    number.map_or(low, |number| clamp_num_range(number, low, high))
}

// Saved numeric settings accept Unicode decimal digits in numeric
// strings. Convert the ten-digit Nd blocks before applying its underscore rule.
fn decimal_digit(ch: char) -> char {
    const STARTS: &[u32] = &[
        0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66,
        0xDE6, 0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90,
        0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0,
        0xFF10, 0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0,
        0x11650, 0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60,
        0x16AC0, 0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0,
        0x1E950, 0x1FBF0,
    ];
    let code = ch as u32;
    STARTS
        .iter()
        .find(|&&start| (start..start + 10).contains(&code))
        .map_or(ch, |&start| (b'0' + (code - start) as u8) as char)
}

fn num(value: &Value) -> f64 {
    value.as_f64().unwrap_or(0.)
}
fn finite(value: &Value) -> bool {
    value.is_number() && value.as_f64().is_some_and(f64::is_finite)
}
fn integer(value: &Value) -> Option<i128> {
    value
        .as_i64()
        .map(i128::from)
        .or_else(|| value.as_u64().map(i128::from))
}
fn nonnegative_integer(value: &Value) -> Option<i128> {
    integer(value).filter(|v| *v >= 0)
}
fn string_length(value: &Value, max: usize) -> bool {
    value.as_str().is_some_and(|text| {
        let length = text.chars().count();
        length > 0 && length <= max
    })
}
fn effect_length(effect: &Value) -> f64 {
    effect["points"]
        .as_array()
        .and_then(|points| points.last())
        .map_or(0., |p| num(&p[0]))
}

pub fn envelope(points: &Value, t: f64) -> f64 {
    let Some(points) = points.as_array().filter(|p| !p.is_empty()) else {
        return 0.;
    };
    if t < 0. || t > num(&points.last().unwrap()[0]) {
        return 0.;
    }
    let mut previous = &points[0];
    for point in points {
        if t <= num(&point[0]) {
            let span = num(&point[0]) - num(&previous[0]);
            if span <= 0. {
                return num(&point[1]);
            }
            return num(&previous[1])
                + (num(&point[1]) - num(&previous[1])) * (t - num(&previous[0])) / span;
        }
        previous = point;
    }
    num(&previous[1])
}

fn rand(n: f64) -> f64 {
    ((n * 12.9898 + 78.233).sin() * 43758.5453).rem_euclid(1.)
}
fn noise(t: f64) -> f64 {
    let step = (t * 12.).floor();
    let blend = (1. - ((t * 12. - step) * PI).cos()) / 2.;
    rand(step) * (1. - blend) + rand(step + 1.) * blend
}
fn joytoy(t: f64, seed: f64) -> f64 {
    let (mut start, mut k) = (0., 0.);
    loop {
        let heavy = rand(seed * 7.31 + k) < 0.5;
        let length = if heavy { 0.3 } else { 0.18 };
        if t < start + length {
            return if heavy { 1. } else { 0.6 };
        }
        start += length + [0.5, 0.7, 0.9, 1.1][(rand(seed * 3.17 + k + 0.5) * 4.) as usize % 4];
        if t < start {
            return 0.;
        }
        k += 1.;
    }
}

pub fn texture(name: &str, t: f64, level: f64, seed: f64) -> f64 {
    match name {
        "rumble" => 0.55 + 0.45 * noise(t),
        "wave" => 0.25 + 0.75 * (1. - (t / 2.6 * 2. * PI).cos()) / 2.,
        "throb" => {
            if (t / 0.7).rem_euclid(1.) < 0.5 {
                1.
            } else {
                0.15
            }
        }
        "flutter" => {
            if (t / 0.25).rem_euclid(1.) < 0.5 {
                1.
            } else {
                0.15
            }
        }
        "footsteps" => {
            if (t / 0.36).rem_euclid(1.) < 0.3 {
                1.
            } else {
                0.
            }
        }
        "ring" => {
            let phase = t.rem_euclid(3.);
            if phase < 0.4 || (0.6..1.).contains(&phase) {
                1.
            } else {
                0.
            }
        }
        "glitch" => {
            let step = (t * 10.).floor();
            if rand(step) > 0.45 {
                0.5 + 0.5 * rand(step + 0.37)
            } else {
                0.
            }
        }
        "joytoy" => joytoy(t, seed),
        "pulse" => 0.2 + 0.8 * (1. - (t / 1.2 * 2. * PI).cos()) / 2.,
        "build" => (0.35 + 0.65 * t / 8.).min(1.),
        "static" => {
            let step = (t * 10.).floor();
            if rand(step + 0.13) > 0.3 {
                0.35 + 0.65 * rand(step + 0.71)
            } else {
                0.1
            }
        }
        "heartbeat" => {
            let phase = (t / (1. - 0.45 * clamp_num(level))).rem_euclid(1.);
            if phase < 0.12 {
                1.
            } else if (0.24..0.34).contains(&phase) {
                0.7
            } else {
                0.
            }
        }
        _ => 1.,
    }
}

pub fn feel_ids(catalog: &Value, item: &Value) -> HashSet<String> {
    let kind = item["kind"].as_str().unwrap_or("");
    catalog["feels"][kind]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|feel| {
            feel.get("for")
                .is_none_or(|ids| ids.as_array().is_some_and(|ids| ids.contains(&item["id"])))
        })
        .filter_map(|feel| feel["id"].as_str().map(str::to_owned))
        .collect()
}

pub fn default_settings(catalog: &Value) -> Value {
    let mut settings = catalog["defaults"].clone();
    let mut interactions = Map::new();
    for item in catalog["interactions"].as_array().into_iter().flatten() {
        if let Some(id) = item["id"].as_str() {
            interactions.insert(id.to_owned(), item["default"].clone());
        }
    }
    settings["interactions"] = Value::Object(interactions);
    settings
}

/// Fill missing settings and discard invalid input. External values never panic.
pub fn normalize_settings(raw: &Value, catalog: &Value) -> Value {
    let mut settings = default_settings(catalog);
    if !raw.is_object() {
        return settings;
    }
    for key in ["enabled", "start_intiface"] {
        if raw[key].is_boolean() {
            settings[key] = raw[key].clone();
        }
    }
    if let Some(value) = raw.get("max_strength") {
        settings["max_strength"] = json!(clamp(value));
    }
    if raw["intiface_address"]
        .as_str()
        .is_some_and(|s| s.starts_with("ws://") && s.chars().count() < 200)
    {
        settings["intiface_address"] = raw["intiface_address"].clone();
    }
    for item in catalog["interactions"].as_array().into_iter().flatten() {
        let Some(id) = item["id"].as_str() else {
            continue;
        };
        let saved = &raw["interactions"][id];
        if !saved.is_object() {
            continue;
        }
        let target = &mut settings["interactions"][id];
        if saved["on"].is_boolean() {
            target["on"] = saved["on"].clone();
        }
        if let Some(value) = saved.get("strength") {
            target["strength"] = json!(clamp(value));
        }
        if saved["feel"]
            .as_str()
            .is_some_and(|feel| feel_ids(catalog, item).contains(feel))
        {
            target["feel"] = saved["feel"].clone();
        }
    }
    if let Some(devices) = raw["devices"].as_object() {
        for (name, options) in devices {
            if !options.is_object() || settings["devices"].as_object().map_or(64, Map::len) >= 64 {
                continue;
            }
            let mut device = json!({"on": options["on"] != Value::Bool(false), "strength": options.get("strength").map_or(1., clamp)});
            if options["all_motors"] == Value::Bool(true) {
                device["all_motors"] = json!(true);
            }
            if options["outputs"].is_object() {
                let mut outputs = Map::new();
                for channel in CHANNELS {
                    if options["outputs"][channel].is_boolean() {
                        outputs.insert(channel.to_owned(), options["outputs"][channel].clone());
                    }
                }
                device["outputs"] = Value::Object(outputs);
            }
            settings["devices"][name] = device;
        }
    }
    settings
}

pub fn blend(values: impl IntoIterator<Item = f64>, cap: f64) -> f64 {
    let cap = clamp_num(cap);
    if cap <= 0. {
        return 0.;
    }
    let active: Vec<_> = values
        .into_iter()
        .filter(|v| *v > 0.)
        .map(|v| v.min(cap))
        .collect();
    match active.len() {
        0 => 0.,
        1 => active[0],
        _ => {
            cap * (1.
                - active
                    .iter()
                    .fold(1., |remaining, value| remaining * (1. - value / cap)))
        }
    }
}

fn round3(value: f64) -> f64 {
    format!("{value:.3}").parse().unwrap_or(0.)
}
pub fn preview(catalog: &Value, kind: &str, feel: &str, samples: usize) -> Vec<f64> {
    if kind == "level" {
        return (0..samples)
            .map(|i| round3(texture(feel, i as f64 * 3. / samples as f64, 0.7, 1.)))
            .collect();
    }
    let Some(effect) = catalog["feels"]["event"]
        .as_array()
        .and_then(|feels| feels.iter().find(|f| f["id"] == feel))
    else {
        return vec![0.; samples];
    };
    let length = effect_length(effect);
    let span = if feel == "scene" {
        40.
    } else if length <= 1.5 {
        1.5
    } else {
        length.min(40.)
    };
    let effect = if feel == "scene" {
        &catalog["tracks"]["judy"]
    } else {
        effect
    };
    let mut mixer = Mixer::new(catalog);
    mixer.effects.insert("_preview".to_owned(), effect.clone());
    let voice = Voice::new(0., 1., "_preview", 1.);
    (0..samples)
        .map(|i| {
            round3(
                (0..4)
                    .map(|j| {
                        mixer
                            .voice_value(&voice, (i as f64 + j as f64 / 4.) * span / samples as f64)
                    })
                    .fold(f64::NEG_INFINITY, f64::max),
            )
        })
        .collect()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Voice {
    pub start: f64,
    pub magnitude: f64,
    pub feel: String,
    pub scale: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release: Option<f64>,
}
impl Voice {
    pub fn new(start: f64, magnitude: f64, feel: &str, scale: f64) -> Self {
        Self {
            start,
            magnitude,
            feel: feel.to_owned(),
            scale,
            release: None,
        }
    }
}

pub struct Mixer {
    pub catalog: Value,
    pub kinds: HashMap<String, String>,
    pub effects: HashMap<String, Value>,
    pub voices: HashMap<String, Voice>,
    pub level_start: HashMap<String, f64>,
    pub output: f64,
    pub contributions: HashMap<String, f64>,
}
impl Mixer {
    pub fn new(catalog: &Value) -> Self {
        let kinds = catalog["interactions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|item| {
                Some((
                    item["id"].as_str()?.to_owned(),
                    item["kind"].as_str()?.to_owned(),
                ))
            })
            .collect();
        let mut effects: HashMap<String, Value> = catalog["feels"]["event"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|effect| Some((effect["id"].as_str()?.to_owned(), effect.clone())))
            .collect();
        if let Some(tracks) = catalog["tracks"].as_object() {
            for (name, track) in tracks {
                if track.is_object() {
                    effects.insert(format!("track:{name}"), track.clone());
                }
            }
        }
        Self {
            catalog: catalog.clone(),
            kinds,
            effects,
            voices: HashMap::new(),
            level_start: HashMap::new(),
            output: 0.,
            contributions: HashMap::new(),
        }
    }
    pub fn clear(&mut self, keep: &[&str]) {
        self.voices
            .retain(|name, _| keep.iter().any(|prefix| name.starts_with(prefix)));
        self.level_start.clear();
        self.output = 0.;
        self.contributions.clear();
    }
    pub fn trigger(
        &mut self,
        name: &str,
        magnitude: f64,
        now: f64,
        feel: &str,
        seconds: Option<f64>,
    ) {
        let magnitude = clamp_num(magnitude);
        if magnitude <= 0. {
            return;
        }
        let mut scale = 1.;
        if let (Some(seconds), Some(effect)) =
            (seconds.filter(|v| *v != 0.), self.effects.get(feel))
        {
            scale = clamp_num_range(seconds, 0.05, 600.) / effect_length(effect).max(0.01);
        }
        let candidate = Voice::new(now, magnitude, feel, scale);
        if self.voices.get(name).is_some_and(|voice| {
            voice.feel == feel
                && magnitude < voice.magnitude
                && self.voice_value(voice, now) > self.voice_value(&candidate, now)
        }) {
            return;
        }
        self.voices.insert(name.to_owned(), candidate);
    }
    pub fn game_event(
        &mut self,
        event_id: &str,
        magnitude: f64,
        now: f64,
        feel: &str,
        seconds: Option<f64>,
    ) {
        let (name, variant) = event_id.split_once(':').unwrap_or((event_id, ""));
        if variant == "end" {
            self.release(name, now);
            return;
        }
        if variant == "pulse" {
            let effect = if feel == "scene" { "track:pulse" } else { feel };
            if self
                .effects
                .get(effect)
                .is_some_and(|effect| effect_length(effect) <= 10.)
            {
                self.trigger(&format!("{name}#pulse"), magnitude, now, effect, seconds);
            }
            return;
        }
        if feel == "scene" {
            let track = format!("track:{}", if variant.is_empty() { name } else { variant });
            if self.effects.contains_key(&track) {
                self.trigger(name, magnitude, now, &track, None);
            } else {
                self.trigger(name, magnitude, now, "swell", seconds);
            }
        } else {
            self.trigger(name, magnitude, now, feel, seconds);
        }
    }
    pub fn release(&mut self, name: &str, now: f64) {
        let prefix = format!("{name}#");
        for (key, voice) in &mut self.voices {
            if (key == name || key.starts_with(&prefix)) && voice.release.is_none() {
                voice.release = Some(now);
            }
        }
    }
    pub fn voice_value(&self, voice: &Voice, now: f64) -> f64 {
        let Some(effect) = self.effects.get(&voice.feel) else {
            return 0.;
        };
        let t = (now - voice.start) / voice.scale;
        let mut value = envelope(&effect["points"], t);
        if let Some(name) = effect["texture"].as_str().filter(|name| !name.is_empty()) {
            value *= texture(name, now - voice.start, 1., voice.start);
        }
        for cue in effect["cues"].as_array().into_iter().flatten() {
            let age = t - num(&cue[0]);
            let strength = num(&cue[1]);
            if (0. ..CUE_RISE).contains(&age) {
                value = value.max(strength * age / CUE_RISE);
            } else if (CUE_RISE..CUE_RISE + CUE_FALL).contains(&age) {
                value = value.max(strength * (1. - (age - CUE_RISE) / CUE_FALL));
            }
        }
        if let Some(release) = voice.release {
            value *= (1. - (now - release) / RELEASE).max(0.);
        }
        value * voice.magnitude
    }
    pub fn finished(&self, voice: &Voice, now: f64) -> bool {
        self.effects
            .get(&voice.feel)
            .is_none_or(|effect| (now - voice.start) / voice.scale > effect_length(effect))
            || voice.release.is_some_and(|release| now - release > RELEASE)
    }
    pub fn tick(
        &mut self,
        now: f64,
        levels: &Value,
        settings: &Value,
    ) -> (f64, HashMap<String, f64>) {
        let master = clamp(&settings["max_strength"]);
        let rules = &settings["interactions"];
        let mut raw: HashMap<String, f64> = HashMap::new();
        let finished: Vec<_> = self
            .voices
            .iter()
            .filter(|(_, voice)| self.finished(voice, now))
            .map(|(name, _)| name.clone())
            .collect();
        for name in finished {
            self.voices.remove(&name);
        }
        for (name, voice) in &self.voices {
            let trying = name.starts_with("try:");
            let rule_id = name
                .strip_prefix("try:")
                .unwrap_or(name)
                .split('#')
                .next()
                .unwrap();
            let rule = &rules[rule_id];
            let strength = if name == "test" {
                0.6
            } else if rule.is_object() && (rule["on"] == true || trying) {
                num(&rule["strength"])
            } else {
                continue;
            };
            raw.insert(
                name.clone(),
                self.voice_value(voice, now) * strength * master,
            );
        }
        if let Some(levels) = levels.as_object() {
            for (name, value) in levels {
                let rule = &rules[name];
                let level = clamp(value);
                if !rule.is_object()
                    || self.kinds.get(name).is_none_or(|kind| kind != "level")
                    || level <= 0.
                    || rule["on"] != true
                {
                    self.level_start.remove(name);
                    continue;
                }
                let start = *self.level_start.entry(name.clone()).or_insert(now);
                let value = level
                    * texture(rule["feel"].as_str().unwrap_or(""), now - start, level, 0.)
                    * num(&rule["strength"])
                    * master;
                let entry = raw.entry(name.clone()).or_insert(0.);
                *entry = entry.max(value);
            }
        }
        self.level_start.retain(|name, _| {
            levels
                .as_object()
                .is_some_and(|levels| levels.contains_key(name))
        });
        let enabled = settings["enabled"] == true;
        self.output = if enabled {
            blend(raw.values().copied(), master)
        } else {
            0.
        };
        self.contributions.clear();
        if enabled {
            for (name, value) in raw {
                if value > 0.001 {
                    let entry = self
                        .contributions
                        .entry(name.split('#').next().unwrap().to_owned())
                        .or_insert(0.);
                    *entry = entry.max(value);
                }
            }
        }
        (self.output, self.contributions.clone())
    }
}

/// The first accepted file of each CET session establishes a baseline only.
pub struct GameLink {
    pub timeout: f64,
    pub session: Option<String>,
    pub seq: i128,
    pub clock: Option<f64>,
    pub last_event: i128,
    pub changed: Option<f64>,
    pub state: Value,
}
impl Default for GameLink {
    fn default() -> Self {
        Self::new(1.)
    }
}
impl GameLink {
    pub fn new(timeout: f64) -> Self {
        Self {
            timeout,
            session: None,
            seq: -1,
            clock: None,
            last_event: -1,
            changed: None,
            state: json!({}),
        }
    }
    pub fn update(&mut self, raw: &Value, now: f64) -> Vec<(String, f64, Option<f64>)> {
        let Some(state) = Self::state(raw) else {
            self.changed = None;
            self.state = json!({});
            return vec![];
        };
        let session = state["session"].as_str().unwrap().to_owned();
        let seq = integer(&state["seq"]).unwrap();
        let clock = num(&state["t"]);
        let events = state["events"].as_array().unwrap();
        let rewound = self.clock.is_some_and(|previous| clock < previous);
        if self.session.as_ref() != Some(&session) || seq < self.seq || rewound {
            self.session = Some(session);
            self.seq = seq;
            self.changed = None;
            self.clock = Some(clock);
            self.last_event = events
                .iter()
                .filter_map(|event| integer(&event[0]))
                .max()
                .unwrap_or(-1);
            self.state = state;
            return vec![];
        }
        if seq <= self.seq {
            return vec![];
        }
        self.seq = seq;
        self.changed = Some(now);
        self.clock = Some(clock);
        let mut events: Vec<_> = events.iter().collect();
        events.sort_by_key(|event| integer(&event[0]).unwrap());
        let mut fresh = vec![];
        for event in events {
            let seq = integer(&event[0]).unwrap();
            if seq <= self.last_event {
                continue;
            }
            self.last_event = seq;
            if clock - num(&event[3]) > EVENT_MAX_AGE || num(&event[3]) > clock + 0.1 {
                continue;
            }
            fresh.push((
                event[1].as_str().unwrap().to_owned(),
                clamp(&event[2]),
                event.get(4).map(num),
            ));
        }
        self.state = state;
        fresh
    }
    /// Unknown diagnostic fields survive, but malformed collections and scenes fail closed.
    pub fn state(raw: &Value) -> Option<Value> {
        if !raw.is_object()
            || raw["v"].as_f64() != Some(2.)
            || !string_length(&raw["session"], 128)
            || nonnegative_integer(&raw["seq"]).is_none()
            || !finite(&raw["t"])
            || num(&raw["t"]) < 0.
            || !raw["active"].is_boolean()
            || raw.get("paused").is_some_and(|value| !value.is_boolean())
        {
            return None;
        }
        let mut events = raw.get("events").cloned().unwrap_or_else(|| json!([]));
        let mut levels = raw.get("levels").cloned().unwrap_or_else(|| json!({}));
        if levels == json!([]) {
            levels = json!({});
        }
        if events == json!({}) {
            events = json!([]);
        }
        let events = events.as_array().filter(|events| events.len() <= 256)?;
        let levels = levels.as_object().filter(|levels| levels.len() <= 64)?;
        if levels.values().any(|value| !finite(value)) {
            return None;
        }
        let mut state = raw.clone();
        state["levels"] = Value::Object(
            levels
                .iter()
                .map(|(key, value)| (key.clone(), json!(clamp(value))))
                .collect(),
        );
        state["events"] = Value::Array(
            events
                .iter()
                .filter(|event| {
                    event.as_array().is_some_and(|event| {
                        (4..=5).contains(&event.len())
                            && nonnegative_integer(&event[0]).is_some()
                            && string_length(&event[1], 128)
                            && finite(&event[2])
                            && finite(&event[3])
                            && num(&event[3]) >= 0.
                            && (event.len() == 4
                                || finite(&event[4]) && (0.05..=600.).contains(&num(&event[4])))
                    })
                })
                .cloned()
                .collect(),
        );
        if let Some(scene) = raw.get("scene") {
            if scene.is_null() || scene == &json!({}) || scene == &json!([]) {
                state["scene"] = Value::Null;
            } else if scene.is_object()
                && string_length(&scene["name"], 64)
                && finite(&scene["elapsed"])
                && (0. ..=3600.).contains(&num(&scene["elapsed"]))
                && nonnegative_integer(&scene["instance"]).is_some()
            {
                state["scene"] = json!({"name": scene["name"], "elapsed": scene["elapsed"], "instance": scene["instance"]});
            } else {
                return None;
            }
        }
        Some(state)
    }
    pub fn live(&self, now: f64) -> bool {
        self.changed
            .is_some_and(|changed| now - changed <= self.timeout)
    }
    pub fn active(&self, now: f64) -> bool {
        self.live(now) && self.state["active"] == true && self.state["paused"] != true
    }
    pub fn levels(&self, now: f64) -> Value {
        if !self.active(now) {
            return json!({});
        }
        Value::Object(
            self.state["levels"]
                .as_object()
                .into_iter()
                .flatten()
                .map(|(key, value)| (key.clone(), json!(clamp(value))))
                .collect(),
        )
    }
    pub fn describe(&self, now: f64) -> (String, String) {
        let (status, text) = if self.changed.is_none() {
            ("waiting", "Load a save in Cyberpunk".to_owned())
        } else if !self.live(now) {
            ("offline", "Game closed or loading".to_owned())
        } else if self.state["paused"] == true {
            ("paused", "Paused from the CET overlay".to_owned())
        } else if self.state["active"] != true {
            (
                "idle",
                if truthy(&self.state["reason"]) {
                    python_str(&self.state["reason"]).chars().take(60).collect()
                } else {
                    "Not in gameplay".to_owned()
                },
            )
        } else {
            ("live", "In gameplay".to_owned())
        };
        (status.to_owned(), text)
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.to_owned(),
        Value::Number(value) => value.to_string(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!("{}: {}", python_repr(&json!(key)), python_repr(value)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}
fn python_repr(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        format!(
            "'{}'",
            text.replace('\\', "\\\\")
                .replace('\'', "\\'")
                .replace('\n', "\\n")
                .replace('\r', "\\r")
                .replace('\t', "\\t")
        )
    } else {
        python_str(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn catalog() -> Value {
        serde_json::from_str(include_str!("../../src/companion/catalog.json")).unwrap()
    }
    fn beat(seq: i64, t: f64, events: Value) -> Value {
        json!({"v":2,"session":"one","seq":seq,"t":t,"active":true,"events":events,"levels":{"engine":1.}})
    }
    #[test]
    fn baseline_corruption_rewind_and_timeout_never_replay() {
        let mut link = GameLink::default();
        assert!(link
            .update(&beat(1, 10., json!([[1, "hurt", 1., 10.]])), 0.)
            .is_empty());
        assert!(!link.active(0.));
        assert_eq!(
            link.update(
                &beat(
                    2,
                    10.1,
                    json!([[1, "hurt", 1., 10.], [2, "kill", 0.5, 10.1]])
                ),
                0.1
            ),
            vec![("kill".to_owned(), 0.5, None)]
        );
        assert!(link.active(0.1));
        assert!(!link.active(1.2));
        link.update(&beat(3, -1., json!([])), 0.2);
        assert!(!link.active(0.2));
        assert_eq!(link.clock, Some(10.1));
        assert!(link
            .update(&beat(4, 0., json!([[3, "hurt", 1., 0.]])), 0.3)
            .is_empty());
        assert!(!link.active(0.3));
        assert_eq!(
            link.update(
                &beat(5, 0.1, json!([[3, "hurt", 1., 0.], [4, "kill", 0.8, 0.1]])),
                0.4
            )
            .len(),
            1
        );
    }
    #[test]
    fn malformed_collections_silence_but_bad_events_are_discarded() {
        let mut raw = beat(
            1,
            1.,
            json!([
                [true, "hurt", 1., 1.],
                [1, "hurt", 1., 1.],
                [2, "hurt", 1., 1., 0.01]
            ]),
        );
        assert_eq!(
            GameLink::state(&raw).unwrap()["events"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        raw["levels"]["engine"] = json!(true);
        assert!(GameLink::state(&raw).is_none());
        raw["levels"] = json!([]);
        raw["scene"] = json!({"name":"judy","elapsed":1.,"instance":false});
        assert!(GameLink::state(&raw).is_none());
        raw["scene"] = json!([]);
        assert!(GameLink::state(&raw).unwrap()["scene"].is_null());
    }
    #[test]
    fn weaker_retrigger_compares_actual_waveform_and_scene_release_fades_every_part() {
        let catalog = catalog();
        let settings = default_settings(&catalog);
        let mut mixer = Mixer::new(&catalog);
        mixer.trigger("external", 1., 0., "swell", Some(6.));
        let before = mixer.voices["external"].clone();
        mixer.trigger("external", 0.1, 2., "swell", Some(6.));
        assert_eq!(mixer.voices["external"], before);
        mixer.game_event("romance:stout", 1., 0., "scene", None);
        mixer.game_event("romance:pulse", 1., 4., "scene", None);
        assert_eq!(mixer.voices["romance"].feel, "track:stout");
        mixer.game_event("romance:end", 1., 5., "scene", None);
        assert_eq!(mixer.voices["romance#pulse"].release, Some(5.));
        assert!(mixer.tick(5.1, &json!({}), &settings).0 > 0.);
        mixer.tick(6.6, &json!({}), &settings);
        assert!(!mixer.voices.contains_key("romance"));
        assert!(!mixer.voices.contains_key("romance#pulse"));
    }
    #[test]
    fn settings_migrate_outputs_preserve_booleans_and_restrict_feels() {
        let settings = normalize_settings(
            &json!({"max_strength":true,"devices":{"one":{"on":0,"all_motors":true,"outputs":{"vibrate":true,"heat":"yes","spray":true}}},"interactions":{"hurt":{"feel":"scene","strength":".2"}}}),
            &catalog(),
        );
        assert_eq!(settings["max_strength"], 0.);
        assert_eq!(
            settings["devices"]["one"],
            json!({"on":true,"strength":1.,"all_motors":true,"outputs":{"vibrate":true}})
        );
        assert_ne!(settings["interactions"]["hurt"]["feel"], "scene");
        assert_eq!(settings["interactions"]["hurt"]["strength"], 0.2);
    }
}
