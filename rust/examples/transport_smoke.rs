//! Connect and exercise negotiated pings without scanning or device output.
use night_city_haptics::buttplug::Client;
use std::{thread, time::{Duration, Instant}};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut client = Client::connect(args.get(1).expect("Pass a ws:// endpoint")).expect("Buttplug handshake");
    let seconds = args.get(2).and_then(|s| s.parse::<f64>().ok()).unwrap_or(3.0);
    let deadline = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < deadline {
        client.poll().expect("Buttplug ping watchdog");
        thread::sleep(Duration::from_millis(20));
    }
    let (kind, reply) = client.request("RequestDeviceList", serde_json::json!({}), 3.0).expect("Acknowledged request after ping deadlines");
    assert_eq!(kind, "DeviceList");
    assert!(reply["Devices"].as_array().expect("Device list").is_empty());
    println!("{}", serde_json::json!({"server":client.server_name,"devices":client.devices.len(),"closed":client.closed,"errors":client.errors}));
    client.close();
}
