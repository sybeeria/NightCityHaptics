//! JSON-lines oracle adapter for differential tests; never used by the runtime.
use night_city_haptics::haptics::{self, GameLink, Mixer, Voice};
use serde_json::{json, Value};
use std::io::{self, BufRead};

fn number(value: &Value) -> f64 {
    value.as_f64().unwrap_or(0.)
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}
fn run(request: &Value, catalog: &Value) -> Value {
    match text(&request["op"]) {
        "settings" => haptics::normalize_settings(&request["raw"], catalog),
        "preview" => json!(haptics::preview(
            catalog,
            text(&request["kind"]),
            text(&request["feel"]),
            request["samples"].as_u64().unwrap_or(48) as usize
        )),
        "texture" => json!(haptics::texture(
            text(&request["feel"]),
            number(&request["t"]),
            request.get("level").map_or(1., number),
            request.get("seed").map_or(0., number)
        )),
        "envelope" => json!(haptics::envelope(&request["points"], number(&request["t"]))),
        "blend" => json!(haptics::blend(
            request["values"].as_array().unwrap().iter().map(number),
            number(&request["cap"])
        )),
        "boundary" => GameLink::state(&request["raw"]).unwrap_or(Value::Null),
        "mixer" => {
            let mut mixer = Mixer::new(catalog);
            let mut settings = haptics::normalize_settings(&request["settings"], catalog);
            let mut observations = vec![];
            for step in request["steps"].as_array().unwrap() {
                let now = number(&step["now"]);
                let seconds = step.get("seconds").and_then(Value::as_f64);
                match text(&step["op"]) {
                    "trigger" => mixer.trigger(
                        text(&step["name"]),
                        number(&step["magnitude"]),
                        now,
                        text(&step["feel"]),
                        seconds,
                    ),
                    "event" => mixer.game_event(
                        text(&step["name"]),
                        number(&step["magnitude"]),
                        now,
                        text(&step["feel"]),
                        seconds,
                    ),
                    "release" => mixer.release(text(&step["name"]), now),
                    "clear" => mixer.clear(
                        &step["keep"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(Value::as_str)
                            .collect::<Vec<_>>(),
                    ),
                    "settings" => settings = haptics::normalize_settings(&step["raw"], catalog),
                    "effect" => {
                        mixer
                            .effects
                            .insert(text(&step["name"]).to_owned(), step["effect"].clone());
                    }
                    "voice" => {
                        mixer.voices.insert(
                            text(&step["name"]).to_owned(),
                            serde_json::from_value::<Voice>(step["voice"].clone()).unwrap(),
                        );
                    }
                    "tick" => {
                        mixer.tick(now, &step["levels"], &settings);
                    }
                    _ => panic!("unknown mixer operation"),
                }
                observations.push(json!({"voices":mixer.voices,"level_start":mixer.level_start,"output":mixer.output,"contributions":mixer.contributions}));
            }
            json!(observations)
        }
        "link" => {
            let mut link = GameLink::new(request.get("timeout").map_or(1., number));
            let mut observations = vec![];
            for step in request["steps"].as_array().unwrap() {
                let now = number(&step["now"]);
                let events = step
                    .get("raw")
                    .map_or_else(Vec::new, |raw| link.update(raw, now));
                observations.push(json!({"events":events,"session":link.session,"seq":link.seq,"clock":link.clock,"last_event":link.last_event,"changed":link.changed,"state":link.state,"live":link.live(now),"active":link.active(now),"levels":link.levels(now),"describe":link.describe(now)}));
            }
            json!(observations)
        }
        _ => panic!("unknown probe operation"),
    }
}
fn main() {
    let catalog: Value =
        serde_json::from_str(include_str!("../../src/companion/catalog.json")).unwrap();
    for line in io::stdin().lock().lines() {
        let request: Value = serde_json::from_str(&line.unwrap()).unwrap();
        println!("{}", run(&request, &catalog));
    }
}
