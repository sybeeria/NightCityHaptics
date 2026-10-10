//! Development-only deterministic output probe. Reads one JSON scenario from
//! stdin; it has no network or device access.
use night_city_haptics::buttplug::{self, CommandSink, Outputs};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;

struct Recorder {
    devices: BTreeMap<u32, Value>,
    generations: BTreeMap<u32, u64>,
    busy: BTreeSet<u32>,
    now: f64,
    commands: Vec<Value>,
}
impl CommandSink for Recorder {
    fn devices(&self) -> &BTreeMap<u32, Value> {
        &self.devices
    }
    fn generation(&self, index: u32) -> u64 {
        *self.generations.get(&index).unwrap_or(&0)
    }
    fn busy(&self, index: u32) -> bool {
        self.busy.contains(&index)
    }
    fn send(&mut self, kind: &str, fields: Value, _: Option<u32>) -> Result<u32, String> {
        self.commands.push(json!([self.now, kind, fields]));
        Ok(self.commands.len() as u32)
    }
}
fn main() {
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text).unwrap();
    let scenario: Value = serde_json::from_str(&text).unwrap();
    let mut recorder = Recorder {
        devices: BTreeMap::new(),
        generations: BTreeMap::new(),
        busy: BTreeSet::new(),
        now: 0.,
        commands: vec![],
    };
    for device in scenario["devices"].as_array().unwrap() {
        recorder.devices.insert(
            device["DeviceIndex"].as_u64().unwrap() as u32,
            device.clone(),
        );
    }
    let mut outputs = Outputs::new();
    for step in scenario["steps"].as_array().unwrap() {
        recorder.now = step["now"].as_f64().unwrap();
        if let Some(devices) = step["devices"].as_array() {
            recorder.devices.clear();
            for device in devices {
                let index = device["DeviceIndex"].as_u64().unwrap() as u32;
                recorder.devices.insert(index, device.clone());
                *recorder.generations.entry(index).or_default() += 1;
            }
        }
        recorder.busy = step["busy"]
            .as_array()
            .map(|v| v.iter().map(|i| i.as_u64().unwrap() as u32).collect())
            .unwrap_or_default();
        if let Some(holds) = step["hold"].as_object() {
            for (index, hold) in holds {
                outputs.hold.insert(
                    index.parse().unwrap(),
                    (hold[0].as_f64().unwrap(), hold[1].as_f64().unwrap()),
                );
            }
        }
        let now = recorder.now;
        outputs
            .update(
                &mut recorder,
                step["strength"].as_f64().unwrap_or(0.),
                &step["settings"],
                now,
                step["quiet"].as_bool().unwrap_or(false),
            )
            .unwrap();
    }
    let mut devices = Map::new();
    for (index, device) in &recorder.devices {
        devices.insert(index.to_string(), json!({"summary": buttplug::summary(device), "key": buttplug::settings_key(device),
            "channels": buttplug::channels(device), "enabled": buttplug::enabled_channels(device, &json!({}))}));
    }
    println!(
        "{}",
        json!({"commands": recorder.commands, "devices": devices})
    );
}
