//! Buttplug v3 transport and output scheduling. Capability indices always refer
//! to the server's original arrays; desired output is never an acknowledgement.
use serde::de::{MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{json, Map, Number, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream, ToSocketAddrs};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use tungstenite::protocol::{Message, Role, WebSocketConfig};

pub const CHANNELS: [&str; 7] = [
    "vibrate",
    "rotate",
    "oscillate",
    "stroke",
    "constrict",
    "heat",
    "light",
];
const MAX_MESSAGE: usize = 4 * 1024 * 1024;
const MAX_BUFFER: usize = MAX_MESSAGE + 65536 + 14;
const RECV_BYTES: usize = 256 * 1024;
const RECV_FRAMES: usize = 64;
const RECV_WORK: f64 = 0.01;
pub const POLL_WORK: f64 = RECV_WORK;
const MAX_PENDING: usize = 512;
const BATTERY_EVERY: f64 = 60.;
const EXPIRED_OUTPUT: &str = "Game heartbeat expired during output";

fn check_output_deadline(deadline: f64) -> Result<(), String> {
    if !deadline.is_finite() || monotonic() >= deadline {
        return Err(EXPIRED_OUTPUT.into());
    }
    Ok(())
}

pub fn monotonic() -> f64 {
    static EPOCH: OnceLock<Instant> = OnceLock::new();
    EPOCH.get_or_init(Instant::now).elapsed().as_secs_f64()
}
fn uint(value: &Value) -> Option<u32> {
    value.as_u64().and_then(|n| n.try_into().ok())
}
fn output_command(kind: &str) -> bool {
    matches!(
        kind,
        "ScalarCmd" | "RotateCmd" | "LinearCmd" | "StopDeviceCmd" | "StopAllDevices"
    )
}
fn expected_reply(kind: &str) -> Option<&str> {
    match kind {
        "RequestServerInfo" => Some("ServerInfo"),
        "RequestDeviceList" => Some("DeviceList"),
        "SensorReadCmd" => Some("SensorReading"),
        "ScalarCmd" | "RotateCmd" | "LinearCmd" | "StopDeviceCmd" | "StopAllDevices" | "Ping"
        | "StartScanning" | "StopScanning" => Some("Ok"),
        _ => None,
    }
}
fn scalar_channel(kind: &str) -> Option<&'static str> {
    match kind {
        "Vibrate" => Some("vibrate"),
        "Rotate" => Some("rotate"),
        "Oscillate" => Some("oscillate"),
        "Position" => Some("stroke"),
        "Constrict" | "Inflate" => Some("constrict"),
        "Temperature" => Some("heat"),
        "Led" => Some("light"),
        _ => None,
    }
}

// serde_json's ordinary Value deserializer silently replaces duplicate fields.
// The wire protocol rejects them at every nesting level, including device attrs.
struct StrictValue(Value);
impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D: Deserializer<'de>>(de: D) -> Result<Self, D::Error> {
        struct StrictVisitor;
        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = StrictValue;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("finite JSON with unique fields")
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Bool(v)))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Number(v.into())))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Number(v.into())))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
                Number::from_f64(v)
                    .map(|n| StrictValue(Value::Number(n)))
                    .ok_or_else(|| E::custom("Nonfinite Intiface number"))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::String(v.to_owned())))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::String(v)))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Null))
            }
            fn visit_none<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(value) = seq.next_element::<StrictValue>()? {
                    values.push(value.0);
                }
                Ok(StrictValue(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut values = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(serde::de::Error::custom("Duplicate Intiface JSON field"));
                    }
                    values.insert(key, map.next_value::<StrictValue>()?.0);
                }
                Ok(StrictValue(Value::Object(values)))
            }
        }
        de.deserialize_any(StrictVisitor)
    }
}
pub fn parse_protocol(text: &str) -> Result<Value, String> {
    if text.len() > MAX_MESSAGE {
        return Err("Message too large".into());
    }
    // Bound integer conversion work even when a JSON implementation changes its
    // numeric representation. Strings are skipped rather than inspected as data.
    let mut string = false;
    let mut escape = false;
    let bytes = text.as_bytes();
    let mut at = 0;
    while at < bytes.len() {
        let b = bytes[at];
        if string {
            if escape {
                escape = false;
            } else if b == b'\\' {
                escape = true;
            } else if b == b'"' {
                string = false;
            }
            at += 1;
            continue;
        }
        if b == b'"' {
            string = true;
        } else if b == b'-' || b.is_ascii_digit() {
            let start = at;
            while at < bytes.len()
                && (bytes[at].is_ascii_digit()
                    || matches!(bytes[at], b'-' | b'+' | b'.' | b'e' | b'E'))
            {
                at += 1;
            }
            let number = &bytes[start..at];
            if !number.iter().any(|b| matches!(b, b'.' | b'e' | b'E')) && number.len() > 64 {
                return Err("Invalid Intiface JSON".into());
            }
            continue;
        }
        at += 1;
    }
    serde_json::from_str::<StrictValue>(text)
        .map(|v| v.0)
        .map_err(|_| "Invalid Intiface JSON".into())
}

pub struct WebSocket {
    socket: TcpStream,
    writer: tungstenite::WebSocket<TcpStream>,
    buffer: Vec<u8>,
    fragments: Vec<u8>,
    fragmenting: bool,
}
pub trait Wire: Send {
    fn send(&mut self, text: &str) -> Result<(), String>;
    fn send_before(&mut self, text: &str, deadline: f64) -> Result<(), String> {
        check_output_deadline(deadline)?;
        self.send(text)?;
        check_output_deadline(deadline)
    }
    fn recv(&mut self, timeout: f64) -> Result<Option<String>, String>;
    fn close(&mut self);
}
impl Wire for WebSocket {
    fn send(&mut self, text: &str) -> Result<(), String> {
        self.send(text)
    }
    fn send_before(&mut self, text: &str, deadline: f64) -> Result<(), String> {
        self.send_before(text, deadline)
    }
    fn recv(&mut self, timeout: f64) -> Result<Option<String>, String> {
        self.recv(timeout)
    }
    fn close(&mut self) {
        self.close()
    }
}
impl WebSocket {
    pub fn connect(address: &str) -> Result<Self, String> {
        if address.chars().any(|c| c < ' ' || c == '\u{7f}') {
            return Err("Invalid WebSocket address".into());
        }
        let url = url::Url::parse(address).map_err(|_| "Invalid WebSocket address")?;
        if url.scheme() != "ws"
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some_and(|fragment| !fragment.is_empty())
        {
            return Err("Intiface address must look like ws://127.0.0.1:12345".into());
        }
        // URL parsing percent-encodes spaces and Unicode paths. Preserve the
        // original path validation, which never permitted those request bytes.
        let authority_start = address.find("://").unwrap_or(0) + 3;
        let authority = address[authority_start..]
            .split(['/', '?', '#'])
            .next()
            .unwrap_or("");
        if authority.contains('@') {
            return Err("Intiface address must look like ws://127.0.0.1:12345".into());
        }
        if let Some(at) = address[authority_start..].find(['/', '?']) {
            if address[authority_start + at..]
                .bytes()
                .any(|b| !(33..=126).contains(&b))
            {
                return Err("Invalid WebSocket path".into());
            }
        }
        let port = url.port_or_known_default().unwrap_or(80);
        if port == 0 {
            return Err("Invalid WebSocket port or timeout".into());
        }
        let host = match url.host().unwrap() {
            url::Host::Domain(v) => v.to_owned(),
            url::Host::Ipv4(v) => v.to_string(),
            url::Host::Ipv6(v) => v.to_string(),
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        let addresses = (host.as_str(), port)
            .to_socket_addrs()
            .map_err(|e| e.to_string())?;
        let mut socket = None;
        let mut last = "Cannot connect to Intiface".to_owned();
        for addr in addresses {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            match TcpStream::connect_timeout(&addr, remaining) {
                Ok(v) => {
                    socket = Some(v);
                    break;
                }
                Err(e) => last = e.to_string(),
            }
        }
        let socket = socket.ok_or(last)?;
        socket.set_nodelay(true).map_err(|e| e.to_string())?;
        let key = tungstenite::handshake::client::generate_key();
        let host_header = if host.contains(':') {
            format!("[{host}]")
        } else {
            host
        };
        let path = if url.path().is_empty() {
            "/"
        } else {
            url.path()
        };
        let query = url.query().map(|v| format!("?{v}")).unwrap_or_default();
        let request = format!("GET {path}{query} HTTP/1.1\r\nHost: {host_header}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n");
        socket
            .set_write_timeout(Some(deadline.saturating_duration_since(Instant::now())))
            .map_err(|e| e.to_string())?;
        let mut socket = socket;
        socket
            .write_all(request.as_bytes())
            .map_err(|e| e.to_string())?;
        let mut response = Vec::new();
        let end = loop {
            if let Some(end) = response.windows(4).position(|v| v == b"\r\n\r\n") {
                break end;
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("WebSocket handshake timed out".into());
            }
            socket
                .set_read_timeout(Some(remaining))
                .map_err(|e| e.to_string())?;
            let mut chunk = [0u8; 4096];
            let n = socket.read(&mut chunk).map_err(|e| e.to_string())?;
            if n == 0 {
                return Err("Intiface closed the connection during the handshake".into());
            }
            response.extend_from_slice(&chunk[..n]);
            if response.len() > 65536 {
                return Err("Invalid handshake response".into());
            }
        };
        let header: String = response[..end].iter().map(|&b| char::from(b)).collect();
        let mut lines = header.split("\r\n");
        let status = lines.next().unwrap_or("");
        let fields: Vec<_> = status.splitn(3, ' ').collect();
        if fields.len() < 2 || fields[..2] != ["HTTP/1.1", "101"] {
            return Err(format!("Not a WebSocket server: {status}"));
        }
        let mut headers = BTreeMap::new();
        for line in lines {
            let Some((name, value)) = line.split_once(':') else {
                return Err("Invalid WebSocket response header".into());
            };
            if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
                return Err("Invalid WebSocket response header".into());
            }
            let name = name.to_ascii_lowercase();
            let value = value.trim().to_owned();
            if let Some(old) = headers.get_mut(&name) {
                if matches!(
                    name.as_str(),
                    "sec-websocket-accept"
                        | "sec-websocket-protocol"
                        | "sec-websocket-extensions"
                        | "upgrade"
                ) {
                    return Err("Duplicate WebSocket response header".into());
                }
                *old = format!("{old},{value}");
            } else {
                headers.insert(name, value);
            }
        }
        let header = |name: &str| headers.get(name).map(String::as_str).unwrap_or("");
        if header("sec-websocket-accept")
            != tungstenite::handshake::derive_accept_key(key.as_bytes())
            || !header("upgrade").eq_ignore_ascii_case("websocket")
            || !header("connection")
                .split(',')
                .any(|v| v.trim().eq_ignore_ascii_case("upgrade"))
            || !header("sec-websocket-extensions").is_empty()
            || !header("sec-websocket-protocol").is_empty()
        {
            return Err("WebSocket handshake failed".into());
        }
        let buffer = response[end + 4..].to_vec();
        socket.set_nonblocking(true).map_err(|e| e.to_string())?;
        let writer = tungstenite::WebSocket::from_raw_socket(
            socket.try_clone().map_err(|e| e.to_string())?,
            Role::Client,
            Some(
                WebSocketConfig::default()
                    .write_buffer_size(0)
                    .max_write_buffer_size(MAX_BUFFER)
                    .max_message_size(Some(MAX_MESSAGE))
                    .max_frame_size(Some(MAX_MESSAGE)),
            ),
        );
        Ok(Self {
            socket,
            writer,
            buffer,
            fragments: vec![],
            fragmenting: false,
        })
    }
    fn send_message(&mut self, message: Message, deadline: Instant) -> Result<(), String> {
        if Instant::now() >= deadline {
            return Err("Send timed out".into());
        }
        match self.writer.send(message) {
            Ok(()) => return Ok(()),
            Err(tungstenite::Error::Io(e)) if e.kind() == io::ErrorKind::WouldBlock => (),
            Err(e) => return Err(e.to_string()),
        }
        loop {
            if Instant::now() >= deadline {
                return Err("Send timed out".into());
            }
            match self.writer.flush() {
                Ok(()) => return Ok(()),
                Err(tungstenite::Error::Io(e)) if e.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(e) => return Err(e.to_string()),
            }
        }
    }
    pub fn send(&mut self, text: &str) -> Result<(), String> {
        self.send_message(
            Message::Text(text.to_owned().into()),
            Instant::now() + Duration::from_secs(2),
        )
    }
    fn send_before(&mut self, text: &str, deadline: f64) -> Result<(), String> {
        let began = Instant::now();
        let result = check_output_deadline(deadline).and_then(|_| {
            let remaining = (deadline - monotonic()).clamp(0., 2.);
            self.send_message(
                Message::Text(text.to_owned().into()),
                began + Duration::from_secs_f64(remaining),
            )?;
            check_output_deadline(deadline)
        });
        if result.is_err() {
            // A partially written positive frame cannot be replaced by a stop.
            // Shut down TCP before cleanup could flush tungstenite's old buffer.
            let _ = self.socket.shutdown(Shutdown::Both);
        }
        result
    }
    fn frame(&mut self) -> Result<Option<(bool, u8, Vec<u8>)>, String> {
        if self.buffer.len() < 2 {
            return Ok(None);
        }
        let fin = self.buffer[0] & 0x80 != 0;
        let opcode = self.buffer[0] & 15;
        if self.buffer[0] & 0x70 != 0
            || self.buffer[1] & 0x80 != 0
            || !matches!(opcode, 0 | 1 | 8 | 9 | 10)
        {
            return Err("Invalid WebSocket frame".into());
        }
        let marker = self.buffer[1] & 127;
        let (length, offset) = match marker {
            126 => {
                if self.buffer.len() < 4 {
                    return Ok(None);
                }
                (
                    u16::from_be_bytes(self.buffer[2..4].try_into().unwrap()) as u64,
                    4,
                )
            }
            127 => {
                if self.buffer.len() < 10 {
                    return Ok(None);
                }
                (
                    u64::from_be_bytes(self.buffer[2..10].try_into().unwrap()),
                    10,
                )
            }
            n => (n as u64, 2),
        };
        if marker == 126 && length < 126 || marker == 127 && length < 65536 {
            return Err("Invalid WebSocket frame length".into());
        }
        if length > MAX_MESSAGE as u64 {
            return Err("Frame too large".into());
        }
        let length = length as usize;
        if opcode >= 8 {
            if !fin || length > 125 || opcode == 8 && length == 1 {
                return Err("Invalid WebSocket control frame".into());
            }
        } else if opcode == 0 && !self.fragmenting {
            return Err("Unexpected WebSocket continuation".into());
        } else if opcode == 1 && self.fragmenting {
            return Err("Unfinished WebSocket message".into());
        }
        if opcode < 8 && self.fragments.len() + length > MAX_MESSAGE {
            return Err("Message too large".into());
        }
        if self.buffer.len() < offset + length {
            return Ok(None);
        }
        let payload = self.buffer[offset..offset + length].to_vec();
        self.buffer.drain(..offset + length);
        Ok(Some((fin, opcode, payload)))
    }
    pub fn recv(&mut self, timeout: f64) -> Result<Option<String>, String> {
        let result = self.recv_inner(timeout);
        if result.is_err() {
            let _ = self.socket.shutdown(Shutdown::Both);
        }
        result
    }
    fn recv_inner(&mut self, timeout: f64) -> Result<Option<String>, String> {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.));
        let work_deadline = Instant::now() + Duration::from_secs_f64(timeout.max(RECV_WORK));
        let (mut received, mut frames) = (0, 0);
        loop {
            if frames >= RECV_FRAMES || received >= RECV_BYTES || Instant::now() >= work_deadline {
                return Ok(None);
            }
            while let Some((fin, opcode, payload)) = self.frame()? {
                frames += 1;
                match opcode {
                    9 => self.send_message(Message::Pong(payload.into()), work_deadline)?,
                    8 => return Err("Intiface closed the connection".into()),
                    0 | 1 => {
                        self.fragments.extend(payload);
                        self.fragmenting = !fin;
                        if fin {
                            return String::from_utf8(std::mem::take(&mut self.fragments))
                                .map(Some)
                                .map_err(|_| "Invalid WebSocket text".into());
                        }
                    }
                    _ => (),
                }
                if frames >= RECV_FRAMES || Instant::now() >= work_deadline {
                    return Ok(None);
                }
            }
            let mut chunk = [0u8; 65536];
            let remaining = chunk.len().min(RECV_BYTES - received);
            match self.socket.read(&mut chunk[..remaining]) {
                Ok(0) => return Err("Intiface closed the connection".into()),
                Ok(n) => {
                    received += n;
                    if self.buffer.len() + n > MAX_BUFFER {
                        return Err("WebSocket receive buffer too large".into());
                    }
                    self.buffer.extend_from_slice(&chunk[..n]);
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return Ok(None);
                    }
                    std::thread::sleep(
                        Duration::from_millis(1)
                            .min(deadline.saturating_duration_since(Instant::now())),
                    );
                }
                Err(e) => return Err(e.to_string()),
            }
        }
    }
    pub fn close(&mut self) {
        let _ = self.send_message(
            Message::Close(None),
            Instant::now() + Duration::from_millis(100),
        );
        let _ = self.socket.shutdown(Shutdown::Both);
    }
}
impl Drop for WebSocket {
    fn drop(&mut self) {
        let _ = self.socket.shutdown(Shutdown::Both);
    }
}

#[derive(Clone, Debug)]
struct Pending {
    deadline: f64,
    device: Option<u32>,
    kind: String,
}
pub struct Client {
    ws: Box<dyn Wire>,
    pub next_id: u32,
    pending: BTreeMap<u32, Pending>,
    pub devices: BTreeMap<u32, Value>,
    pub scanning: bool,
    pub scan_id: Option<u32>,
    pub closed: bool,
    pub errors: Vec<String>,
    pub ping_every: f64,
    pub next_ping: f64,
    pub server_name: String,
    pub battery: BTreeMap<u32, f64>,
    battery_due: BTreeMap<u32, f64>,
    battery_reads: BTreeMap<u32, (u32, f64)>,
    generations: BTreeMap<u32, u64>,
    generation: u64,
}
impl Client {
    pub fn connect(url: &str) -> Result<Self, String> {
        Self::connect_named(url, "Night City Haptics")
    }
    pub fn connect_named(url: &str, name: &str) -> Result<Self, String> {
        Self::connect_wire(Box::new(WebSocket::connect(url)?), name)
    }
    pub fn connect_wire(wire: Box<dyn Wire>, name: &str) -> Result<Self, String> {
        let mut client = Self::unconnected(wire);
        client.handshake(name)?;
        Ok(client)
    }
    fn unconnected(wire: Box<dyn Wire>) -> Self {
        Self {
            ws: wire,
            next_id: 0,
            pending: BTreeMap::new(),
            devices: BTreeMap::new(),
            scanning: false,
            scan_id: None,
            closed: false,
            errors: vec![],
            ping_every: 0.,
            next_ping: f64::INFINITY,
            server_name: "Intiface".into(),
            battery: BTreeMap::new(),
            battery_due: BTreeMap::new(),
            battery_reads: BTreeMap::new(),
            generations: BTreeMap::new(),
            generation: 0,
        }
    }
    fn handshake(&mut self, name: &str) -> Result<(), String> {
        let (_, info) = self.request(
            "RequestServerInfo",
            json!({"ClientName": name, "MessageVersion": 3}),
            3.,
        )?;
        if info["MessageVersion"].as_u64() != Some(3) {
            return self.fail("Intiface did not accept Buttplug message version 3");
        }
        if info.get("ServerName").is_some_and(|v| {
            !v.is_null() && (!v.is_string() || v.as_str().unwrap().chars().count() > 256)
        }) || uint(info.get("MaxPingTime").unwrap_or(&json!(0))).is_none()
        {
            return self.fail("Invalid Intiface server information");
        }
        self.server_name = info["ServerName"]
            .as_str()
            .filter(|v| !v.is_empty())
            .unwrap_or("Intiface")
            .to_owned();
        let max_ping = info["MaxPingTime"].as_u64().unwrap_or(0);
        if max_ping > 0 {
            self.ping_every = max_ping as f64 / 2000.;
            self.next_ping = monotonic() + self.ping_every;
        }
        let (_, data) = self.request("RequestDeviceList", json!({}), 3.)?;
        let Some(devices) = data["Devices"].as_array().filter(|d| d.len() <= 128) else {
            return self.fail("Invalid Intiface device list");
        };
        let mut listed = BTreeSet::new();
        for device in devices {
            self.add(device.clone())?;
            if !listed.insert(uint(&device["DeviceIndex"]).unwrap()) {
                return self.fail("Duplicate Intiface device index");
            }
        }
        Ok(())
    }
    fn fail<T>(&mut self, message: impl Into<String>) -> Result<T, String> {
        let message = message.into();
        if !self.closed {
            self.pending.clear();
            let _ = self.stop_all();
            self.close();
        }
        Err(message)
    }
    fn add(&mut self, device: Value) -> Result<(), String> {
        let Some(index) = uint(&device["DeviceIndex"]) else {
            return self.fail("Invalid Intiface device");
        };
        if !device.is_object()
            || device["DeviceName"]
                .as_str()
                .is_none_or(|v| v.chars().count() > 256)
            || !device["DeviceMessages"].is_object()
        {
            return self.fail("Invalid Intiface device");
        }
        if device
            .get("DeviceDisplayName")
            .is_some_and(|v| !v.is_null() && v.as_str().is_none_or(|v| v.chars().count() > 256))
        {
            return self.fail("Invalid Intiface device display name");
        }
        for kind in ["ScalarCmd", "RotateCmd", "LinearCmd"] {
            let empty = json!([]);
            let Some(attrs) = device["DeviceMessages"]
                .get(kind)
                .unwrap_or(&empty)
                .as_array()
                .filter(|v| v.len() <= 64)
            else {
                return self.fail("Invalid Intiface device capabilities");
            };
            for attr in attrs {
                if !attr.is_object() {
                    return self.fail("Invalid Intiface device capabilities");
                }
                if attr.get("StepCount").is_some_and(|v| uint(v).is_none())
                    || kind == "ScalarCmd" && attr.get("ActuatorType").is_none()
                    || attr
                        .get("ActuatorType")
                        .is_some_and(|v| v.as_str().is_none_or(|v| v.chars().count() > 64))
                {
                    return self.fail("Invalid Intiface actuator attributes");
                }
            }
        }
        let empty = json!([]);
        let Some(sensors) = device["DeviceMessages"]
            .get("SensorReadCmd")
            .unwrap_or(&empty)
            .as_array()
            .filter(|v| v.len() <= 64)
        else {
            return self.fail("Invalid Intiface sensor attributes");
        };
        if sensors.iter().any(|v| {
            !v.is_object()
                || v["SensorType"]
                    .as_str()
                    .is_none_or(|v| v.chars().count() > 64)
        }) {
            return self.fail("Invalid Intiface sensor attributes");
        }
        if uint(device.get("DeviceMessageTimingGap").unwrap_or(&json!(0))).is_none() {
            return self.fail("Invalid Intiface device timing gap");
        }
        if !self.devices.contains_key(&index) && self.devices.len() >= 128 {
            return self.fail("Too many Intiface devices");
        }
        self.generation = self.generation.wrapping_add(1);
        self.generations.insert(index, self.generation);
        self.devices.insert(index, device);
        Ok(())
    }
    pub fn send(&mut self, kind: &str, fields: Value, device: Option<u32>) -> Result<u32, String> {
        self.send_timeout(kind, fields, device, 2.)
    }
    fn send_timeout(
        &mut self,
        kind: &str,
        fields: Value,
        device: Option<u32>,
        timeout: f64,
    ) -> Result<u32, String> {
        self.send_bounded(kind, fields, device, timeout, None)
    }
    fn send_bounded(
        &mut self,
        kind: &str,
        mut fields: Value,
        device: Option<u32>,
        timeout: f64,
        deadline: Option<f64>,
    ) -> Result<u32, String> {
        if self.closed {
            return Err("Intiface connection is closed".into());
        }
        if self.pending.len() >= MAX_PENDING {
            return self.fail("Too many pending Intiface commands");
        }
        for _ in 0..=self.pending.len() {
            self.next_id = if self.next_id == u32::MAX {
                1
            } else {
                self.next_id + 1
            };
            if !self.pending.contains_key(&self.next_id) {
                break;
            }
        }
        let id = self.next_id;
        let Some(fields) = fields.as_object_mut() else {
            return Err("Invalid command fields".into());
        };
        fields.insert("Id".into(), json!(id));
        let mut command = Map::new();
        command.insert(kind.into(), Value::Object(fields.clone()));
        let text =
            serde_json::to_string(&vec![Value::Object(command)]).map_err(|e| e.to_string())?;
        // A wire failure is returned directly. The cleanup path calls send once
        // for its best-effort stop, so recursive failure handling must not occur.
        let result = match deadline {
            Some(deadline) => self.ws.send_before(&text, deadline),
            None => self.ws.send(&text),
        };
        if let Err(error) = result {
            if deadline.is_some() {
                self.close();
            }
            return Err(error);
        }
        self.pending.insert(
            id,
            Pending {
                deadline: monotonic() + timeout,
                device,
                kind: kind.into(),
            },
        );
        Ok(id)
    }
    fn handle(&mut self, text: &str) -> Result<BTreeMap<u32, (String, Value)>, String> {
        let batch = match parse_protocol(text) {
            Ok(v) => v,
            Err(e) => return self.fail(e),
        };
        let Some(batch) = batch.as_array().filter(|v| !v.is_empty() && v.len() <= 128) else {
            return self.fail("Invalid Intiface message batch");
        };
        let mut replies = BTreeMap::new();
        for message in batch {
            let Some(object) = message.as_object().filter(|v| v.len() == 1) else {
                return self.fail("Invalid Intiface message");
            };
            let (kind, data) = object.iter().next().unwrap();
            if !data.is_object() {
                return self.fail("Invalid Intiface message fields");
            }
            let Some(id) = uint(&data["Id"]) else {
                return self.fail("Invalid Intiface message id");
            };
            if matches!(
                kind.as_str(),
                "DeviceAdded" | "DeviceRemoved" | "ScanningFinished"
            ) && id != 0
            {
                return self.fail("Invalid Intiface system message id");
            }
            match kind.as_str() {
                "DeviceAdded" => self.add(data.clone())?,
                "DeviceRemoved" => {
                    let Some(index) = uint(&data["DeviceIndex"]) else {
                        return self.fail("Invalid Intiface device index");
                    };
                    self.devices.remove(&index);
                    self.generations.remove(&index);
                    self.battery.remove(&index);
                    self.battery_due.remove(&index);
                    self.pending.retain(|_, p| p.device != Some(index));
                }
                "ScanningFinished" => self.scanning = false,
                "Error" => {
                    let Some(error) = data["ErrorMessage"].as_str() else {
                        return self.fail("Invalid Intiface error");
                    };
                    let Some(code) = uint(&data["ErrorCode"]) else {
                        return self.fail("Invalid Intiface error");
                    };
                    let error: String = error.chars().take(200).collect();
                    if self
                        .pending
                        .get(&id)
                        .is_none_or(|p| p.kind != "SensorReadCmd")
                    {
                        self.errors.push(error.clone());
                        if self.errors.len() > 5 {
                            self.errors.remove(0);
                        }
                    }
                    if id == 0 || matches!(code, 1 | 2) {
                        return self.fail(error);
                    }
                }
                _ => (),
            }
            if let Some(pending) = self.pending.remove(&id) {
                if kind == "Error" {
                    if pending.kind == "StartScanning" && Some(id) == self.scan_id {
                        self.scanning = false;
                    }
                    if output_command(&pending.kind)
                        || matches!(pending.kind.as_str(), "Ping" | "StopScanning")
                    {
                        return self.fail(
                            data["ErrorMessage"]
                                .as_str()
                                .unwrap()
                                .chars()
                                .take(200)
                                .collect::<String>(),
                        );
                    }
                } else if pending.kind == "SensorReadCmd" {
                    if let Some((index, full)) = self.battery_reads.get(&id).copied() {
                        if kind == "SensorReading" && self.devices.contains_key(&index) {
                            if let Some(level) = data["Data"]
                                .as_array()
                                .and_then(|v| v.first())
                                .and_then(integer_f64)
                            {
                                self.battery.insert(index, (level / full).clamp(0., 1.));
                            }
                        }
                    }
                } else if expected_reply(&pending.kind).is_some_and(|expected| expected != kind) {
                    return self.fail(if output_command(&pending.kind) {
                        "Intiface did not acknowledge output".into()
                    } else {
                        format!("Invalid Intiface reply to {}", pending.kind)
                    });
                }
                self.battery_reads.remove(&id);
                replies.insert(id, (kind.clone(), data.clone()));
            }
        }
        Ok(replies)
    }
    pub fn refresh_batteries(&mut self, now: f64) -> Result<(), String> {
        self.refresh_batteries_before(now, None)
    }
    fn refresh_batteries_before(&mut self, now: f64, deadline: Option<f64>) -> Result<(), String> {
        let reads: Vec<_> = self
            .devices
            .iter()
            .filter_map(|(&index, d)| battery_sensor(d).map(|s| (index, s)))
            .collect();
        for (index, (sensor, full)) in reads {
            if now < *self.battery_due.get(&index).unwrap_or(&0.) {
                continue;
            }
            // Battery telemetry is optional. Defer unsent reads instead of
            // accumulating a separate transport timeout for every device.
            if deadline.is_some_and(|deadline| monotonic() >= deadline) {
                break;
            }
            self.battery_due.insert(index, now + BATTERY_EVERY);
            if self.battery_reads.values().any(|r| r.0 == index) {
                continue;
            }
            let id = self.send_bounded(
                "SensorReadCmd",
                json!({"DeviceIndex": index, "SensorIndex": sensor, "SensorType": "Battery"}),
                None,
                5.,
                deadline,
            )?;
            self.battery_reads.insert(id, (index, full));
        }
        Ok(())
    }
    fn maintain(&mut self, now: f64) -> Result<(), String> {
        self.maintain_before(now, None)
    }
    fn maintain_before(&mut self, now: f64, deadline: Option<f64>) -> Result<(), String> {
        let expired: Vec<_> = self
            .pending
            .iter()
            .filter(|(_, p)| now >= p.deadline)
            .map(|(&id, p)| (id, p.kind.clone()))
            .collect();
        for (id, kind) in expired {
            if kind == "SensorReadCmd" {
                self.pending.remove(&id);
                self.battery_reads.remove(&id);
            } else {
                return self.fail(format!("Intiface did not answer {kind}"));
            }
        }
        if now >= self.next_ping {
            self.send_bounded("Ping", json!({}), None, 2., deadline)?;
            self.next_ping = now + self.ping_every;
        }
        Ok(())
    }
    pub fn request(
        &mut self,
        kind: &str,
        fields: Value,
        timeout: f64,
    ) -> Result<(String, Value), String> {
        let deadline = monotonic() + timeout;
        let id = self.send_timeout(kind, fields, None, timeout)?;
        while monotonic() < deadline {
            let now = monotonic();
            self.maintain(now)?;
            let wake = self
                .pending
                .values()
                .fold(deadline.min(self.next_ping), |v, p| v.min(p.deadline));
            let text = match self.ws.recv((wake - now).max(0.)) {
                Ok(v) => v,
                Err(e) => return self.fail(e),
            };
            if let Some(text) = text {
                if let Some(reply) = self.handle(&text)?.remove(&id) {
                    if reply.0 == "Error" {
                        return self.fail(
                            reply.1["ErrorMessage"]
                                .as_str()
                                .unwrap_or("Intiface error")
                                .chars()
                                .take(200)
                                .collect::<String>(),
                        );
                    }
                    return Ok(reply);
                }
            }
        }
        self.fail(format!("Intiface did not answer {kind}"))
    }
    pub fn poll(&mut self) -> Result<(), String> {
        self.poll_before(monotonic() + POLL_WORK)
    }
    /// Bound background control writes together with receive work so the next
    /// heartbeat/Off gate cannot wait behind one battery timeout per device.
    pub fn poll_before(&mut self, deadline: f64) -> Result<(), String> {
        for _ in 0..64 {
            let text = match self.ws.recv(0.) {
                Ok(v) => v,
                Err(e) => return self.fail(e),
            };
            let Some(text) = text else {
                break;
            };
            self.handle(&text)?;
            if monotonic() >= deadline {
                break;
            }
        }
        let now = monotonic();
        self.maintain_before(now, Some(deadline))?;
        if !self.closed {
            self.refresh_batteries_before(now, Some(deadline))?;
        }
        Ok(())
    }
    pub fn busy(&self, index: u32) -> bool {
        self.pending.values().any(|p| p.device == Some(index))
    }
    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }
    pub fn start_scanning(&mut self) -> Result<(), String> {
        if !self.scanning {
            self.scan_id = Some(self.send("StartScanning", json!({}), None)?);
            self.scanning = true;
        }
        Ok(())
    }
    pub fn stop_scanning(&mut self) -> Result<(), String> {
        self.stop_scanning_bounded(None)
    }
    pub fn stop_scanning_before(&mut self, deadline: f64) -> Result<(), String> {
        self.stop_scanning_bounded(Some(deadline))
    }
    fn stop_scanning_bounded(&mut self, deadline: Option<f64>) -> Result<(), String> {
        if self.scanning {
            self.scan_id =
                Some(self.send_bounded("StopScanning", json!({}), None, 2., deadline)?);
            self.scanning = false;
        }
        Ok(())
    }
    pub fn stop_all(&mut self) -> Result<(), String> {
        self.send("StopAllDevices", json!({}), None).map(|_| ())
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.scanning = false;
        self.scan_id = None;
        self.pending.clear();
        self.battery_reads.clear();
        self.ws.close();
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        if !self.closed {
            let _ = self.stop_all();
            self.close();
        }
    }
}

pub fn features(device: &Value, message: &str, scalar_only: bool) -> Vec<usize> {
    device["DeviceMessages"][message]
        .as_array()
        .map(|items| {
            items
                .iter()
                .enumerate()
                .filter(|(_, attr)| {
                    attr.is_object()
                        && (!scalar_only
                            || attr["ActuatorType"]
                                .as_str()
                                .and_then(scalar_channel)
                                .is_some())
                })
                .map(|(i, _)| i)
                .collect()
        })
        .unwrap_or_default()
}
pub fn battery_sensor(device: &Value) -> Option<(usize, f64)> {
    device["DeviceMessages"]["SensorReadCmd"]
        .as_array()?
        .iter()
        .enumerate()
        .find_map(|(i, sensor)| {
            if sensor["SensorType"] != "Battery" {
                return None;
            }
            let full = sensor["SensorRange"]
                .as_array()
                .and_then(|v| v.first())
                .and_then(Value::as_array)
                .filter(|v| v.len() == 2)
                .and_then(|v| integer_f64(&v[1]))
                .filter(|&v| v > 0.)
                .unwrap_or(100.);
            Some((i, full))
        })
}
fn integer_f64(value: &Value) -> Option<f64> {
    value
        .as_i64()
        .map(|v| v as f64)
        .or_else(|| value.as_u64().map(|v| v as f64))
}
pub type Routes = BTreeMap<&'static str, Vec<(&'static str, usize)>>;
pub fn channels(device: &Value) -> Routes {
    let mut routes: Routes = BTreeMap::new();
    let rotations = features(device, "RotateCmd", false);
    let linear = features(device, "LinearCmd", false);
    if let Some(attrs) = device["DeviceMessages"]["ScalarCmd"].as_array() {
        for (index, attr) in attrs.iter().enumerate() {
            if let Some(channel) = attr["ActuatorType"].as_str().and_then(scalar_channel) {
                if channel == "rotate" && !rotations.is_empty()
                    || channel == "stroke" && !linear.is_empty()
                {
                    continue;
                }
                routes
                    .entry(channel)
                    .or_default()
                    .push(("ScalarCmd", index));
            }
        }
    }
    for i in rotations {
        routes.entry("rotate").or_default().push(("RotateCmd", i));
    }
    for i in linear {
        routes.entry("stroke").or_default().push(("LinearCmd", i));
    }
    routes
}
pub fn default_channels(available: &Routes) -> BTreeSet<&'static str> {
    let motion = if available.contains_key("vibrate") {
        &["vibrate"][..]
    } else {
        &["rotate", "oscillate", "stroke"][..]
    };
    motion
        .iter()
        .copied()
        .chain(["light"])
        .filter(|v| available.contains_key(v))
        .collect()
}
pub fn enabled_channels(device: &Value, options: &Value) -> BTreeSet<&'static str> {
    let available = channels(device);
    let base = if options["all_motors"] == true {
        available.keys().copied().filter(|c| *c != "heat").collect()
    } else {
        default_channels(&available)
    };
    available
        .keys()
        .copied()
        .filter(|c| options["outputs"][*c].as_bool().unwrap_or(base.contains(c)))
        .collect()
}
pub fn settings_key(device: &Value) -> String {
    let display = device["DeviceDisplayName"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(|s| json!(s))
        .unwrap_or_else(|| device["DeviceIndex"].clone());
    format!(
        "device:{}",
        ascii_json(&json!([
            device["DeviceName"].as_str().unwrap_or(""),
            display
        ]))
    )
}
fn ascii_json(value: &Value) -> String {
    let mut result = String::new();
    for c in value.to_string().chars() {
        if c <= '~' {
            result.push(c);
        } else {
            for code in c.encode_utf16(&mut [0u16; 2]) {
                result.push_str(&format!("\\u{code:04x}"));
            }
        }
    }
    result
}
pub fn device_options(device: &Value, settings: &Value) -> Value {
    let key = settings_key(device);
    let options = settings
        .get(&key)
        .or_else(|| settings.get(device["DeviceName"].as_str().unwrap_or("")));
    options
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}))
}
pub fn summary(device: &Value) -> Value {
    let mut motors: Vec<_> = device["DeviceMessages"]["ScalarCmd"]
        .as_array()
        .map(|v| {
            v.iter()
                .filter(|v| v.is_object())
                .map(|v| v["ActuatorType"].as_str().unwrap_or("").to_owned())
                .collect()
        })
        .unwrap_or_default();
    motors.extend(
        features(device, "RotateCmd", false)
            .iter()
            .map(|_| "Rotate".to_owned()),
    );
    motors.extend(
        features(device, "LinearCmd", false)
            .iter()
            .map(|_| "Linear".to_owned()),
    );
    let routes = channels(device);
    let mut counts = Map::new();
    for c in CHANNELS {
        if let Some(items) = routes.get(c) {
            counts.insert(c.into(), json!(items.len()));
        }
    }
    let name = device["DeviceDisplayName"]
        .as_str()
        .filter(|v| !v.is_empty())
        .or_else(|| device["DeviceName"].as_str())
        .unwrap_or("Device");
    json!({"index": device["DeviceIndex"], "name": name, "key": format!("device:{}", device["DeviceIndex"]), "settings_key": settings_key(device), "motors": motors, "channels": counts})
}

/// Separates deterministic scheduling from the network for differential tests
/// and for the optional in-process server. Device generations detect replacement
/// even if a hotplug reuses an index and advertises identical capabilities.
pub trait CommandSink {
    fn devices(&self) -> &BTreeMap<u32, Value>;
    fn generation(&self, index: u32) -> u64;
    fn busy(&self, index: u32) -> bool;
    /// A live transport clock measures work inside a supplied scheduler tick.
    /// Deterministic sinks keep the caller's clock without real-time drift.
    fn clock(&self) -> Option<f64> {
        None
    }
    fn send(&mut self, kind: &str, fields: Value, device: Option<u32>) -> Result<u32, String>;
    fn send_before(
        &mut self,
        kind: &str,
        fields: Value,
        device: Option<u32>,
        deadline: f64,
    ) -> Result<u32, String> {
        check_output_deadline(deadline)?;
        let id = self.send(kind, fields, device)?;
        check_output_deadline(deadline)?;
        Ok(id)
    }
}
fn tick_time<C: CommandSink>(client: &C, now: f64, began: Option<f64>) -> f64 {
    match (began, client.clock()) {
        (Some(began), Some(finished)) => now + (finished - began).max(0.),
        _ => now,
    }
}
impl CommandSink for Client {
    fn devices(&self) -> &BTreeMap<u32, Value> {
        &self.devices
    }
    fn generation(&self, index: u32) -> u64 {
        *self.generations.get(&index).unwrap_or(&0)
    }
    fn busy(&self, index: u32) -> bool {
        self.busy(index)
    }
    fn clock(&self) -> Option<f64> {
        Some(monotonic())
    }
    fn send(&mut self, kind: &str, fields: Value, device: Option<u32>) -> Result<u32, String> {
        self.send(kind, fields, device)
    }
    fn send_before(
        &mut self,
        kind: &str,
        fields: Value,
        device: Option<u32>,
        deadline: f64,
    ) -> Result<u32, String> {
        self.send_bounded(kind, fields, device, 2., Some(deadline))
    }
}
#[derive(Clone, Debug)]
struct Queued {
    key: &'static str,
    kind: &'static str,
    fields: Value,
    motion: Option<(bool, u32)>,
}
#[derive(Clone, Debug)]
enum Sent {
    Zero,
    Values(BTreeMap<&'static str, f64>),
}
pub struct Outputs {
    sent: BTreeMap<u32, (Sent, f64)>,
    stroke: BTreeMap<u32, (bool, f64)>,
    pub hold: BTreeMap<u32, (f64, f64)>,
    selection: BTreeMap<u32, BTreeSet<&'static str>>,
    queues: BTreeMap<u32, VecDeque<Queued>>,
    last_send: BTreeMap<u32, f64>,
    identity: BTreeMap<u32, (u64, Value)>,
    slow: BTreeMap<u32, BTreeMap<&'static str, f64>>,
    ticked: BTreeMap<u32, f64>,
    clockwise: BTreeMap<u32, bool>,
}
impl Default for Outputs {
    fn default() -> Self {
        Self::new()
    }
}
impl Outputs {
    pub fn new() -> Self {
        Self {
            sent: BTreeMap::new(),
            stroke: BTreeMap::new(),
            hold: BTreeMap::new(),
            selection: BTreeMap::new(),
            queues: BTreeMap::new(),
            last_send: BTreeMap::new(),
            identity: BTreeMap::new(),
            slow: BTreeMap::new(),
            ticked: BTreeMap::new(),
            clockwise: BTreeMap::new(),
        }
    }
    pub fn reset(&mut self) {
        *self = Self::new();
    }
    pub fn stop_device<C: CommandSink>(
        &mut self,
        client: &mut C,
        index: u32,
        now: f64,
    ) -> Result<(), String> {
        let device = client.devices().get(&index).cloned();
        let result = match device {
            Some(device) => self.stop(client, &device, now),
            None => Ok(()),
        };
        let stopped_at = self.last_send.get(&index).copied();
        // Retire the target even when a write fails: its previous Test must
        // never survive an Off action or restart on a later connection.
        self.forget(index);
        if result.is_ok() {
            self.sent.insert(index, (Sent::Zero, now));
            if let Some(stopped_at) = stopped_at {
                self.last_send.insert(index, stopped_at);
            }
        }
        result
    }
    fn forget(&mut self, index: u32) {
        self.sent.remove(&index);
        self.stroke.remove(&index);
        self.hold.remove(&index);
        self.selection.remove(&index);
        self.queues.remove(&index);
        self.last_send.remove(&index);
        self.identity.remove(&index);
        self.slow.remove(&index);
        self.ticked.remove(&index);
        self.clockwise.remove(&index);
    }
    fn values(
        &mut self,
        index: u32,
        selection: &BTreeSet<&'static str>,
        value: f64,
        holding: bool,
        quiet: bool,
        now: f64,
    ) -> BTreeMap<&'static str, f64> {
        let dt = (now - self.ticked.get(&index).copied().unwrap_or(now)).clamp(0., 1.);
        self.ticked.insert(index, now);
        let slow = self.slow.entry(index).or_default();
        if quiet {
            slow.clear();
        }
        let mut values = BTreeMap::new();
        for &channel in selection {
            let timings = match channel {
                "constrict" => Some((1.2, 2.5)),
                "heat" => Some((6., 15.)),
                _ => None,
            };
            if let Some((attack, release)) = timings {
                let mut level = *slow.get(channel).unwrap_or(&0.);
                if !holding {
                    level += (value - level)
                        * (1. - (-dt / if value > level { attack } else { release }).exp());
                    if value <= 0. && level < 0.03 {
                        level = 0.;
                    }
                }
                slow.insert(channel, level);
                values.insert(channel, (level * 20.).round_ties_even() / 20.);
            } else {
                values.insert(channel, value);
            }
        }
        slow.retain(|channel, _| selection.contains(channel));
        values
    }
    pub fn update<C: CommandSink>(
        &mut self,
        client: &mut C,
        strength: f64,
        settings: &Value,
        now: f64,
        quiet: bool,
    ) -> Result<(), String> {
        self.update_before(client, strength, settings, now, quiet, None)
    }
    /// Positive game output must finish while the heartbeat that authorized it
    /// is fresh. Stops retain their own transport budget after that deadline.
    pub fn update_before<C: CommandSink>(
        &mut self,
        client: &mut C,
        strength: f64,
        settings: &Value,
        now: f64,
        quiet: bool,
        deadline: Option<f64>,
    ) -> Result<(), String> {
        let gate_clock = client.clock();
        let removed: BTreeSet<_> = self
            .identity
            .keys()
            .chain(self.hold.keys())
            .filter(|i| !client.devices().contains_key(i))
            .copied()
            .collect();
        for index in removed {
            self.forget(index);
        }
        let devices = client.devices().clone();
        for (index, device) in devices {
            let generation = client.generation(index);
            if self
                .identity
                .get(&index)
                .is_some_and(|(old_generation, old)| {
                    *old_generation != generation || old != &device
                })
            {
                self.stop(client, &device, tick_time(client, now, gate_clock))?;
                let stopped_at = self.last_send.get(&index).copied().unwrap_or(now);
                self.forget(index);
                self.last_send.insert(index, stopped_at);
            }
            self.identity.insert(index, (generation, device.clone()));
            let options = device_options(&device, settings);
            let mut value = if options["on"] == false {
                0.
            } else {
                strength * options["strength"].as_f64().unwrap_or(1.)
            };
            let mut holding = false;
            if let Some(&(held, until)) = self.hold.get(&index) {
                if now < until {
                    value = held;
                    holding = true;
                } else {
                    self.hold.remove(&index);
                }
            }
            value = python_round(value.clamp(0., 1.), 3);
            let selection = enabled_channels(&device, &options);
            let values = self.values(
                index,
                &selection,
                value,
                holding,
                (quiet || options["on"] == false) && !holding,
                now,
            );
            // Slow heat/squeeze release may outlive the motion target. A stroke
            // waiting for an acknowledgement must not start after motion is zero.
            if values.get("stroke").is_none_or(|value| *value <= 0.) {
                if let Some(queue) = self.queues.get_mut(&index) {
                    queue.retain(|command| command.motion.is_none());
                }
            }
            let previous = self.sent.get(&index).cloned();
            let (mut old, last) = match previous {
                Some((Sent::Values(v), t)) => (v, t),
                _ => (BTreeMap::new(), 0.),
            };
            let gap = device["DeviceMessageTimingGap"]
                .as_f64()
                .filter(|v| v.is_finite() && *v > 0.)
                .unwrap_or(0.)
                / 1000.;
            let changed_selection = self.selection.get(&index).is_some_and(|v| *v != selection);
            let mut stopped = false;
            if changed_selection {
                self.stop(client, &device, tick_time(client, now, gate_clock))?;
                stopped = true;
                self.sent.remove(&index);
                old.clear();
            }
            self.selection.insert(index, selection.clone());
            if !values.values().any(|v| *v > 0.) {
                if !matches!(self.sent.get(&index), Some((Sent::Zero, _))) {
                    if !stopped {
                        self.stop(client, &device, tick_time(client, now, gate_clock))?;
                    }
                    self.sent.insert(index, (Sent::Zero, now));
                }
                continue;
            }
            let mut changed = values != old;
            if changed
                && !old.is_empty()
                && values.keys().eq(old.keys())
                && now - last < 1.
                && !changed_selection
                && values.iter().all(|(c, v)| (v - old[c]).abs() < 0.01)
            {
                changed = false;
            }
            let due = selection.contains("stroke")
                && now >= self.stroke.get(&index).map(|v| v.1).unwrap_or(0.);
            if changed || changed_selection || due {
                self.queue(
                    &device,
                    if changed || old.is_empty() {
                        &values
                    } else {
                        &old
                    },
                    &selection,
                    changed,
                    due,
                    &old,
                );
                if changed {
                    self.sent.insert(index, (Sent::Values(values), now));
                }
            }
            while self.queues.get(&index).is_some_and(|q| !q.is_empty()) && !client.busy(index) {
                if now
                    - self
                        .last_send
                        .get(&index)
                        .copied()
                        .unwrap_or(f64::NEG_INFINITY)
                    < gap
                {
                    break;
                }
                let command = self.queues.get_mut(&index).unwrap().pop_front().unwrap();
                if let Some(deadline) = deadline {
                    check_output_deadline(deadline)?;
                    client.send_before(command.kind, command.fields, Some(index), deadline)?;
                } else {
                    client.send(command.kind, command.fields, Some(index))?;
                }
                let sent_at = tick_time(client, now, gate_clock);
                self.last_send.insert(index, sent_at);
                if let Some((up, duration)) = command.motion {
                    self.stroke
                        .insert(index, (up, sent_at + duration as f64 / 1000.));
                }
            }
        }
        Ok(())
    }
    fn stop<C: CommandSink>(
        &mut self,
        client: &mut C,
        device: &Value,
        now: f64,
    ) -> Result<(), String> {
        let began = client.clock();
        let index = uint(&device["DeviceIndex"]).ok_or("Invalid Intiface device")?;
        self.queues.remove(&index);
        self.stroke.remove(&index);
        client.send("StopDeviceCmd", json!({"DeviceIndex": index}), Some(index))?;
        let scalars: Vec<_> = features(device, "ScalarCmd", true).into_iter().map(|i| json!({"Index": i, "Scalar": 0., "ActuatorType": device["DeviceMessages"]["ScalarCmd"][i]["ActuatorType"]})).collect();
        if !scalars.is_empty() {
            client.send(
                "ScalarCmd",
                json!({"DeviceIndex": index, "Scalars": scalars}),
                Some(index),
            )?;
        }
        let sent_at = tick_time(client, now, began);
        self.last_send.insert(index, sent_at);
        Ok(())
    }
    fn enqueue(&mut self, index: u32, command: Queued) {
        let queue = self.queues.entry(index).or_default();
        if let Some(old) = queue.iter_mut().find(|q| q.key == command.key) {
            *old = command;
        } else {
            queue.push_back(command);
        }
    }
    fn queue(
        &mut self,
        device: &Value,
        values: &BTreeMap<&'static str, f64>,
        selection: &BTreeSet<&'static str>,
        changed: bool,
        due: bool,
        old: &BTreeMap<&'static str, f64>,
    ) {
        let index = uint(&device["DeviceIndex"]).unwrap();
        let routes = channels(device);
        if changed {
            let mut scalars = Vec::new();
            // Preserve CHANNELS traversal, then sort by original feature index.
            for channel in CHANNELS {
                if channel == "stroke" || !selection.contains(channel) {
                    continue;
                }
                for &(kind, i) in routes.get(channel).into_iter().flatten() {
                    if kind == "ScalarCmd" {
                        scalars.push(json!({"Index": i, "Scalar": values[channel], "ActuatorType": device["DeviceMessages"]["ScalarCmd"][i]["ActuatorType"]}));
                    }
                }
            }
            scalars.sort_by_key(|v| v["Index"].as_u64().unwrap());
            if !scalars.is_empty() {
                self.enqueue(
                    index,
                    Queued {
                        key: "ScalarCmd",
                        kind: "ScalarCmd",
                        fields: json!({"DeviceIndex": index, "Scalars": scalars}),
                        motion: None,
                    },
                );
            }
            if selection.contains("rotate") {
                let rotations: Vec<_> = routes
                    .get("rotate")
                    .into_iter()
                    .flatten()
                    .filter(|r| r.0 == "RotateCmd")
                    .map(|r| r.1)
                    .collect();
                if !rotations.is_empty() {
                    if values["rotate"] - old.get("rotate").copied().unwrap_or(0.) >= 0.25 {
                        let direction = !self.clockwise.get(&index).copied().unwrap_or(true);
                        self.clockwise.insert(index, direction);
                    }
                    let direction = self.clockwise.get(&index).copied().unwrap_or(true);
                    let rotations: Vec<_> = rotations.into_iter().map(|i| json!({"Index": i, "Speed": values["rotate"], "Clockwise": direction})).collect();
                    self.enqueue(
                        index,
                        Queued {
                            key: "RotateCmd",
                            kind: "RotateCmd",
                            fields: json!({"DeviceIndex": index, "Rotations": rotations}),
                            motion: None,
                        },
                    );
                }
            }
        }
        if !selection.contains("stroke") {
            return;
        }
        let stroke = values.get("stroke").copied().unwrap_or(0.);
        let linear: Vec<_> = routes
            .get("stroke")
            .into_iter()
            .flatten()
            .filter(|r| r.0 == "LinearCmd")
            .map(|r| r.1)
            .collect();
        let positional: Vec<_> = routes
            .get("stroke")
            .into_iter()
            .flatten()
            .filter(|r| r.0 == "ScalarCmd")
            .map(|r| r.1)
            .collect();
        if (!linear.is_empty() || !positional.is_empty()) && due && stroke > 0. {
            let up = !self.stroke.get(&index).map(|v| v.0).unwrap_or(false);
            let position = if up { 0.1 + 0.8 * stroke } else { 0.1 };
            let duration = (900. - 600. * stroke) as u32;
            let (key, kind, fields) = if !linear.is_empty() {
                let vectors: Vec<_> = linear
                    .into_iter()
                    .map(|i| json!({"Index": i, "Duration": duration, "Position": position}))
                    .collect();
                (
                    "LinearCmd",
                    "LinearCmd",
                    json!({"DeviceIndex": index, "Vectors": vectors}),
                )
            } else {
                let scalars: Vec<_> = positional.into_iter().map(|i| json!({"Index": i, "Scalar": position, "ActuatorType": device["DeviceMessages"]["ScalarCmd"][i]["ActuatorType"]})).collect();
                (
                    "Position",
                    "ScalarCmd",
                    json!({"DeviceIndex": index, "Scalars": scalars}),
                )
            };
            self.enqueue(
                index,
                Queued {
                    key,
                    kind,
                    fields,
                    motion: Some((up, duration)),
                },
            );
        }
    }
}
// Decimal rounding uses the exact floating-point input, not the product
// x*1000 (which can cross a half boundary). Formatting uses the same tie rule.
fn python_round(value: f64, places: usize) -> f64 {
    format!("{value:.places$}").parse().unwrap_or(0.)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct WireState {
        sent: Vec<Value>,
        incoming: VecDeque<String>,
        closed: bool,
        fail_send: bool,
    }
    struct MockWire(Arc<Mutex<WireState>>);
    impl Wire for MockWire {
        fn send(&mut self, text: &str) -> Result<(), String> {
            let mut state = self.0.lock().unwrap();
            if state.fail_send {
                return Err("wire write failed".into());
            }
            state
                .sent
                .extend(serde_json::from_str::<Vec<Value>>(text).unwrap());
            Ok(())
        }
        fn recv(&mut self, _: f64) -> Result<Option<String>, String> {
            Ok(self.0.lock().unwrap().incoming.pop_front())
        }
        fn close(&mut self) {
            self.0.lock().unwrap().closed = true;
        }
    }
    fn rich(index: u32) -> Value {
        json!({"DeviceIndex": index, "DeviceName": "Synthetic fixture", "DeviceMessageTimingGap": 0,
            "DeviceMessages": {"ScalarCmd": [
                {"ActuatorType": "Vibrate", "StepCount": 20}, {"ActuatorType": "Constrict", "StepCount": 20},
                {"ActuatorType": "Temperature", "StepCount": 10}, {"ActuatorType": "Led", "StepCount": 10},
                {"ActuatorType": "Spray", "StepCount": 10}, {"ActuatorType": "Rotate", "StepCount": 20},
                {"ActuatorType": "Position", "StepCount": 99}, {"ActuatorType": "Oscillate", "StepCount": 20},
                {"ActuatorType": "Vibrate", "StepCount": 20}],
                "RotateCmd": [{"StepCount": 20}], "LinearCmd": [{"StepCount": 99}],
                "SensorReadCmd": [{"SensorType": "Pressure"}, {"SensorType": "Battery", "SensorRange": [[0, 100]]}]}})
    }
    fn mock_client() -> (Client, Arc<Mutex<WireState>>) {
        let state = Arc::new(Mutex::new(WireState::default()));
        let mut client = Client::unconnected(Box::new(MockWire(state.clone())));
        client.add(rich(1)).unwrap();
        (client, state)
    }
    #[test]
    fn poll_control_work_does_not_accumulate_per_device_send_timeouts() {
        struct SlowControlWire {
            sent: usize,
        }
        impl Wire for SlowControlWire {
            fn send(&mut self, _: &str) -> Result<(), String> {
                std::thread::sleep(Duration::from_millis(25));
                self.sent += 1;
                Ok(())
            }
            fn recv(&mut self, _: f64) -> Result<Option<String>, String> {
                Ok(None)
            }
            fn close(&mut self) {}
        }
        let mut client = Client::unconnected(Box::new(SlowControlWire { sent: 0 }));
        for index in 0..64 {
            client.add(rich(index)).unwrap();
        }
        let began = Instant::now();
        let _ = client.poll();
        assert!(
            began.elapsed() < Duration::from_millis(100),
            "optional control writes blocked the next safety gate for {:?}",
            began.elapsed()
        );
        assert!(
            client.closed,
            "a blocked in-flight write must close the connection"
        );
        assert!(client.battery_due.len() <= 1);
    }
    #[test]
    fn poll_defers_optional_reads_without_marking_them_sent() {
        let (mut client, state) = mock_client();
        client.add(rich(2)).unwrap();
        client.poll_before(monotonic() - 1.).unwrap();
        assert!(client.battery_due.is_empty());
        assert!(state.lock().unwrap().sent.is_empty());
        assert!(!client.closed);
        client.poll_before(monotonic() + 1.).unwrap();
        assert_eq!(client.battery_due.len(), 2);
        assert_eq!(state.lock().unwrap().sent.len(), 2);
    }
    #[test]
    fn scheduled_ping_and_scan_stop_share_poll_write_budget() {
        let (mut client, state) = mock_client();
        client.next_ping = 0.;
        assert!(client.poll_before(monotonic() - 1.).is_err());
        assert!(client.closed);
        assert!(state.lock().unwrap().sent.is_empty());
        let (mut client, state) = mock_client();
        client.scanning = true;
        assert!(client.stop_scanning_before(monotonic() - 1.).is_err());
        assert!(client.closed);
        assert!(state.lock().unwrap().sent.is_empty());
    }
    #[test]
    fn heartbeat_deadline_rejects_positive_output_but_allows_stop_and_zero() {
        let (mut client, state) = mock_client();
        let mut outputs = Outputs::new();
        let deadline = Some(monotonic() - 1.);
        assert_eq!(
            outputs
                .update_before(&mut client, 0.5, &json!({}), 0., false, deadline)
                .unwrap_err(),
            EXPIRED_OUTPUT
        );
        assert!(state.lock().unwrap().sent.is_empty());
        outputs
            .update_before(&mut client, 0., &json!({}), 0.1, true, deadline)
            .unwrap();
        let state = state.lock().unwrap();
        assert!(state
            .sent
            .iter()
            .any(|command| command.get("StopDeviceCmd").is_some()));
        assert!(state
            .sent
            .iter()
            .filter_map(|command| command["ScalarCmd"]["Scalars"].as_array())
            .flatten()
            .all(|scalar| scalar["Scalar"] == 0.));
    }
    #[test]
    #[cfg(unix)]
    fn expired_partial_websocket_write_aborts_before_cleanup_can_flush_it() {
        use std::net::TcpListener;
        use std::os::fd::AsRawFd;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let socket = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut peer, _) = listener.accept().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let receiver = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(60));
            let mut bytes = Vec::new();
            peer.read_to_end(&mut bytes).unwrap();
            bytes
        });
        let size: libc::c_int = 4096;
        assert_eq!(
            unsafe {
                libc::setsockopt(
                    socket.as_raw_fd(),
                    libc::SOL_SOCKET,
                    libc::SO_SNDBUF,
                    (&size as *const libc::c_int).cast(),
                    std::mem::size_of_val(&size) as libc::socklen_t,
                )
            },
            0
        );
        socket.set_nonblocking(true).unwrap();
        let writer = tungstenite::WebSocket::from_raw_socket(
            socket.try_clone().unwrap(),
            Role::Client,
            Some(
                WebSocketConfig::default()
                    .write_buffer_size(0)
                    .max_write_buffer_size(MAX_BUFFER),
            ),
        );
        let mut ws = WebSocket {
            socket,
            writer,
            buffer: vec![],
            fragments: vec![],
            fragmenting: false,
        };
        let text = "x".repeat(1024 * 1024);
        assert!(ws.send_before(&text, monotonic() + 0.02).is_err());
        ws.close();
        let received = receiver.join().unwrap();
        assert!(
            !received.is_empty(),
            "fixture must exercise a partial write"
        );
        assert!(
            received.len() < text.len(),
            "cleanup flushed the expired positive frame"
        );
    }
    #[test]
    fn original_indices_and_all_seven_channels() {
        let device = rich(1);
        let routes = channels(&device);
        assert_eq!(routes["vibrate"], [("ScalarCmd", 0), ("ScalarCmd", 8)]);
        assert_eq!(routes["rotate"], [("RotateCmd", 0)]);
        assert_eq!(routes["stroke"], [("LinearCmd", 0)]);
        assert_eq!(routes.len(), 7);
        assert!(routes
            .values()
            .flatten()
            .all(|(kind, i)| *kind != "ScalarCmd" || *i != 4));
        assert_eq!(
            enabled_channels(&device, &json!({})),
            BTreeSet::from(["vibrate", "light"])
        );
        assert!(!enabled_channels(&device, &json!({"all_motors": true})).contains("heat"));
        assert!(enabled_channels(&device, &json!({"outputs": {"heat": true}})).contains("heat"));
    }
    #[test]
    fn existing_unicode_device_settings_keys_are_retained() {
        let device = json!({"DeviceIndex": 17, "DeviceName": "Éléphant 😀", "DeviceDisplayName": "Gauche 🦄"});
        assert_eq!(
            settings_key(&device),
            r#"device:["\u00c9l\u00e9phant \ud83d\ude00","Gauche \ud83e\udd84"]"#
        );
        let settings =
            json!({settings_key(&device): {"strength": 0.2}, "Éléphant 😀": {"strength": 0.4}});
        assert_eq!(device_options(&device, &settings)["strength"], 0.2);
        let mut other = device.clone();
        other["DeviceIndex"] = json!(88);
        assert_eq!(settings_key(&device), settings_key(&other));
        other.as_object_mut().unwrap().remove("DeviceDisplayName");
        assert_eq!(device_options(&other, &settings)["strength"], 0.4);
    }
    #[test]
    fn acknowledgement_controls_backpressure_and_failed_zero_closes() {
        let (mut client, state) = mock_client();
        let mut outputs = Outputs::new();
        outputs
            .update(&mut client, 0.5, &json!({}), 0., false)
            .unwrap();
        assert!(client.busy(1));
        client.handle(r#"[{"Ok":{"Id":1}}]"#).unwrap();
        assert!(!client.busy(1));
        outputs
            .update(&mut client, 0., &json!({}), 0.1, true)
            .unwrap();
        let zero_id = client.next_id;
        assert_eq!(client.handle(&json!([{"Error": {"Id": zero_id, "ErrorCode": 4, "ErrorMessage": "zero failed"}}]).to_string()).unwrap_err(), "zero failed");
        assert!(client.closed);
        assert_eq!(client.pending_count(), 0);
        let state = state.lock().unwrap();
        assert!(state.closed);
        assert!(state.sent.last().unwrap().get("StopAllDevices").is_some());
    }
    fn acknowledge_pending(client: &mut Client) {
        let replies: Vec<_> = client
            .pending
            .keys()
            .map(|id| json!({"Ok":{"Id":id}}))
            .collect();
        if !replies.is_empty() {
            client.handle(&json!(replies).to_string()).unwrap();
        }
    }
    fn scheduler_snapshot(outputs: &Outputs, index: u32) -> String {
        format!(
            "{:?}",
            (
                outputs.sent.get(&index),
                outputs.stroke.get(&index),
                outputs.hold.get(&index),
                outputs.selection.get(&index),
                outputs.queues.get(&index),
                outputs.last_send.get(&index),
                outputs.identity.get(&index),
                outputs.slow.get(&index),
                outputs.ticked.get(&index),
                outputs.clockwise.get(&index),
            )
        )
    }
    fn seed_scheduler(outputs: &mut Outputs, index: u32) {
        outputs.sent.insert(
            index,
            (Sent::Values(BTreeMap::from([("vibrate", 0.8)])), 1.),
        );
        outputs.stroke.insert(index, (false, 50.));
        outputs.hold.insert(index, (0.8, 50.));
        outputs
            .selection
            .insert(index, BTreeSet::from(["vibrate", "heat", "stroke"]));
        outputs.queues.insert(index, VecDeque::from([Queued { key: "LinearCmd", kind: "LinearCmd",
            fields: json!({"DeviceIndex":index,"Vectors":[{"Index":0,"Position":0.1,"Duration":100}]}), motion: Some((false, 100)) }]));
        outputs.last_send.insert(index, 1.);
        outputs.identity.insert(index, (1, rich(index)));
        outputs
            .slow
            .insert(index, BTreeMap::from([("heat", 0.8), ("constrict", 0.7)]));
        outputs.ticked.insert(index, 1.);
        outputs.clockwise.insert(index, false);
    }
    #[test]
    fn stopping_one_device_retires_only_its_test_and_scheduler_then_allows_fresh_test() {
        let (mut client, state) = mock_client();
        client.add(rich(2)).unwrap();
        let mut outputs = Outputs::new();
        seed_scheduler(&mut outputs, 1);
        seed_scheduler(&mut outputs, 2);
        let other = scheduler_snapshot(&outputs, 2);
        outputs.stop_device(&mut client, 1, 2.).unwrap();
        assert_eq!(scheduler_snapshot(&outputs, 2), other);
        let mut expected = Outputs::new();
        expected.sent.insert(1, (Sent::Zero, 2.));
        expected.last_send.insert(1, outputs.last_send[&1]);
        assert!(outputs.last_send[&1] >= 2.);
        assert_eq!(
            scheduler_snapshot(&outputs, 1),
            scheduler_snapshot(&expected, 1)
        );
        {
            let sent = &state.lock().unwrap().sent;
            assert_eq!(sent.len(), 2);
            assert_eq!(sent[0]["StopDeviceCmd"]["DeviceIndex"], 1);
            assert_eq!(sent[1]["ScalarCmd"]["DeviceIndex"], 1);
            assert!(sent[1]["ScalarCmd"]["Scalars"]
                .as_array()
                .unwrap()
                .iter()
                .all(|scalar| scalar["Scalar"] == 0.));
        }
        acknowledge_pending(&mut client);
        state.lock().unwrap().sent.clear();
        outputs.hold.insert(1, (0.5, 3.));
        outputs
            .update(
                &mut client,
                0.,
                &json!({"Synthetic fixture":{"on":false,"outputs":{"stroke":true}}}),
                2.1,
                true,
            )
            .unwrap();
        acknowledge_pending(&mut client);
        outputs
            .update(
                &mut client,
                0.,
                &json!({"Synthetic fixture":{"on":false,"outputs":{"stroke":true}}}),
                2.2,
                true,
            )
            .unwrap();
        let sent = &state.lock().unwrap().sent;
        assert!(sent
            .iter()
            .any(|command| command["ScalarCmd"]["DeviceIndex"] == 1
                && command["ScalarCmd"]["Scalars"]
                    .as_array()
                    .is_some_and(|scalars| scalars.iter().any(|scalar| scalar["Scalar"] == 0.5))));
        assert!(sent
            .iter()
            .any(|command| command["LinearCmd"]["DeviceIndex"] == 1
                && command["LinearCmd"]["Vectors"][0]["Position"] == 0.5));
    }
    #[test]
    fn stopping_one_device_retires_its_scheduler_even_if_transport_fails() {
        let (mut client, state) = mock_client();
        client.add(rich(2)).unwrap();
        let mut outputs = Outputs::new();
        seed_scheduler(&mut outputs, 1);
        seed_scheduler(&mut outputs, 2);
        let other = scheduler_snapshot(&outputs, 2);
        state.lock().unwrap().fail_send = true;
        assert!(outputs.stop_device(&mut client, 1, 2.).is_err());
        assert_eq!(
            scheduler_snapshot(&outputs, 1),
            scheduler_snapshot(&Outputs::new(), 1)
        );
        assert_eq!(scheduler_snapshot(&outputs, 2), other);
    }
    #[test]
    fn motion_duration_and_message_gap_start_when_slow_successful_write_finishes() {
        struct DelayedSink {
            devices: BTreeMap<u32, Value>,
            elapsed: f64,
            sent: Vec<Value>,
        }
        impl CommandSink for DelayedSink {
            fn devices(&self) -> &BTreeMap<u32, Value> {
                &self.devices
            }
            fn generation(&self, _: u32) -> u64 {
                0
            }
            fn busy(&self, _: u32) -> bool {
                false
            }
            fn clock(&self) -> Option<f64> {
                Some(self.elapsed)
            }
            fn send(&mut self, kind: &str, fields: Value, _: Option<u32>) -> Result<u32, String> {
                self.elapsed += 0.2;
                self.sent.push(json!({kind:fields}));
                Ok(self.sent.len() as u32)
            }
        }
        let device = json!({"DeviceIndex":1,"DeviceName":"fixture","DeviceMessages":{"LinearCmd":[{"StepCount":99}]}});
        let mut sink = DelayedSink {
            devices: BTreeMap::from([(1, device)]),
            elapsed: 0.,
            sent: vec![],
        };
        let mut outputs = Outputs::new();
        outputs
            .update(&mut sink, 0.5, &json!({}), 10., false)
            .unwrap();
        assert!((outputs.stroke[&1].1 - 10.8).abs() < 1e-12);
        outputs
            .update(&mut sink, 0.5, &json!({}), 10.65, false)
            .unwrap();
        assert_eq!(
            sink.sent.len(),
            1,
            "a stroke is still moving after the slow send"
        );
        outputs
            .update(&mut sink, 0.5, &json!({}), 10.81, false)
            .unwrap();
        assert_eq!(sink.sent.len(), 2);
        let device = json!({"DeviceIndex":1,"DeviceName":"fixture","DeviceMessageTimingGap":250,"DeviceMessages":{"ScalarCmd":[{"ActuatorType":"Vibrate","StepCount":20}]}});
        let mut sink = DelayedSink {
            devices: BTreeMap::from([(1, device)]),
            elapsed: 0.,
            sent: vec![],
        };
        let mut outputs = Outputs::new();
        outputs
            .update(&mut sink, 0.5, &json!({}), 10., false)
            .unwrap();
        outputs
            .update(&mut sink, 0.8, &json!({}), 10.3, false)
            .unwrap();
        assert_eq!(sink.sent.len(), 1, "message gap starts after completion");
        outputs
            .update(&mut sink, 0.8, &json!({}), 10.46, false)
            .unwrap();
        assert_eq!(sink.sent.len(), 2);
        // Device 1 consumes 200 ms before device 2 stops. Its StopDevice
        // and scalar zero consume another 400 ms, so the latter's gap must
        // begin at tick+600 ms rather than tick+400 ms.
        let devices = (1..=2).map(|index| (index, json!({"DeviceIndex":index,"DeviceName":format!("fixture{index}"),"DeviceMessageTimingGap":250,"DeviceMessages":{"ScalarCmd":[{"ActuatorType":"Vibrate","StepCount":20}]}}))).collect();
        let mut sink = DelayedSink {
            devices,
            elapsed: 0.,
            sent: vec![],
        };
        let mut outputs = Outputs::new();
        outputs
            .update(&mut sink, 0.5, &json!({}), 10., false)
            .unwrap();
        outputs
            .update(
                &mut sink,
                0.8,
                &json!({"fixture2":{"on":false}}),
                11.,
                false,
            )
            .unwrap();
        assert!(
            (outputs.last_send[&2] - 11.6).abs() < 1e-12,
            "later-device stop omitted preceding work: {}",
            outputs.last_send[&2]
        );
        let sent = sink.sent.len();
        outputs
            .update(&mut sink, 0.8, &json!({}), 11.66, false)
            .unwrap();
        assert_eq!(
            sink.sent.len(),
            sent,
            "only 60 ms elapsed after device 2's stop"
        );
        outputs
            .update(&mut sink, 0.8, &json!({}), 11.86, false)
            .unwrap();
        assert_eq!(sink.sent.len(), sent + 1);
    }
    #[test]
    fn disabling_a_device_stops_warm_followers_and_explicit_test_still_bypasses_off() {
        let (mut client, state) = mock_client();
        let mut outputs = Outputs::new();
        let settings = json!({"Synthetic fixture":{"outputs":{"vibrate":false,"light":false,"constrict":true,"heat":true}}});
        for now in [0., 1., 2., 3.] {
            outputs
                .update(&mut client, 1., &settings, now, false)
                .unwrap();
            acknowledge_pending(&mut client);
        }
        assert!(state
            .lock()
            .unwrap()
            .sent
            .iter()
            .any(|command| command["ScalarCmd"]["Scalars"]
                .as_array()
                .is_some_and(|scalars| scalars
                    .iter()
                    .any(|scalar| scalar["Scalar"].as_f64().is_some_and(|value| value > 0.)))));
        state.lock().unwrap().sent.clear();
        let mut disabled = settings.clone();
        disabled["Synthetic fixture"]["on"] = json!(false);
        outputs
            .update(&mut client, 1., &disabled, 3.5, false)
            .unwrap();
        {
            let sent = &state.lock().unwrap().sent;
            assert!(sent
                .iter()
                .any(|command| command.get("StopDeviceCmd").is_some()));
            assert!(!sent.iter().any(|command| command["ScalarCmd"]["Scalars"]
                .as_array()
                .is_some_and(|scalars| scalars
                    .iter()
                    .any(|scalar| scalar["Scalar"].as_f64().is_some_and(|value| value > 0.)))));
        }
        acknowledge_pending(&mut client);
        state.lock().unwrap().sent.clear();
        let settings = json!({"Synthetic fixture":{"on":false}});
        outputs.hold.insert(1, (0.5, 5.));
        outputs
            .update(&mut client, 0., &settings, 4., true)
            .unwrap();
        acknowledge_pending(&mut client);
        outputs
            .update(&mut client, 0., &settings, 4.1, true)
            .unwrap();
        assert!(state
            .lock()
            .unwrap()
            .sent
            .iter()
            .any(|command| command["ScalarCmd"]["Scalars"]
                .as_array()
                .is_some_and(|scalars| scalars.iter().any(|scalar| scalar["Scalar"] == 0.5))));
        acknowledge_pending(&mut client);
        state.lock().unwrap().sent.clear();
        outputs
            .update(&mut client, 1., &settings, 5.1, false)
            .unwrap();
        assert!(state
            .lock()
            .unwrap()
            .sent
            .iter()
            .any(|command| command.get("StopDeviceCmd").is_some()));
    }
    #[test]
    fn zero_motion_target_cancels_queued_strokes_while_heat_releases() {
        for scalar_position in [false, true] {
            let (mut client, state) = mock_client();
            let device = if scalar_position {
                json!({"DeviceIndex":1,"DeviceName":"Synthetic fixture","DeviceMessages":{"ScalarCmd":[
                    {"ActuatorType":"Temperature","StepCount":20},{"ActuatorType":"Position","StepCount":99}],"StopDeviceCmd":{}}})
            } else {
                json!({"DeviceIndex":1,"DeviceName":"Synthetic fixture","DeviceMessages":{"ScalarCmd":[
                    {"ActuatorType":"Temperature","StepCount":20}],"LinearCmd":[{"StepCount":99}],"StopDeviceCmd":{}}})
            };
            client.add(device).unwrap();
            client
                .send("ScalarCmd", json!({"DeviceIndex":1,"Scalars":[]}), Some(1))
                .unwrap();
            let mut outputs = Outputs::new();
            let settings = json!({"Synthetic fixture":{"outputs":{"heat":true,"stroke":true}}});
            outputs
                .update(&mut client, 0.8, &settings, 0., false)
                .unwrap();
            outputs
                .update(&mut client, 0.8, &settings, 1., false)
                .unwrap();
            assert!(outputs.queues[&1]
                .iter()
                .any(|command| command.motion.is_some()));
            acknowledge_pending(&mut client);
            state.lock().unwrap().sent.clear();
            outputs
                .update(&mut client, 0., &settings, 1.1, false)
                .unwrap();
            assert!(outputs
                .queues
                .get(&1)
                .is_none_or(|queue| queue.iter().all(|command| command.motion.is_none())));
            let sent = &state.lock().unwrap().sent;
            assert!(sent.iter().any(|command| command["ScalarCmd"]["Scalars"]
                .as_array()
                .is_some_and(|scalars| scalars
                    .iter()
                    .any(|scalar| scalar["ActuatorType"] == "Temperature"
                        && scalar["Scalar"].as_f64().is_some_and(|value| value > 0.)))));
            assert!(
                !sent.iter().any(|command| command.get("LinearCmd").is_some()
                    || command["ScalarCmd"]["Scalars"]
                        .as_array()
                        .is_some_and(|scalars| scalars
                            .iter()
                            .any(|scalar| scalar["ActuatorType"] == "Position")))
            );
        }
    }
    #[test]
    fn unanswered_outputs_close_and_final_stop_write_failure_does_not_recurse() {
        let (mut client, state) = mock_client();
        client
            .send("StopDeviceCmd", json!({"DeviceIndex": 1}), Some(1))
            .unwrap();
        state.lock().unwrap().fail_send = true;
        assert!(client
            .maintain(1e9)
            .unwrap_err()
            .contains("did not answer StopDeviceCmd"));
        assert!(client.closed);
        assert!(state.lock().unwrap().closed);
    }
    #[test]
    fn batteries_keep_original_sensor_index_and_fail_silently() {
        let (mut client, state) = mock_client();
        client.refresh_batteries(0.).unwrap();
        let id = client.next_id;
        assert_eq!(
            state.lock().unwrap().sent[0]["SensorReadCmd"]["SensorIndex"],
            1
        );
        client
            .handle(&json!([{"SensorReading": {"Id": id, "Data": [42]}}]).to_string())
            .unwrap();
        assert_eq!(client.battery[&1], 0.42);
        client.refresh_batteries(30.).unwrap();
        assert_eq!(client.next_id, id);
        client.refresh_batteries(61.).unwrap();
        client.handle(&json!([{"Error": {"Id": client.next_id, "ErrorCode": 4, "ErrorMessage": "unsupported"}}]).to_string()).unwrap();
        assert!(!client.closed);
        assert!(client.errors.is_empty());
        client.refresh_batteries(122.).unwrap();
        client.maintain(1e9).unwrap();
        assert!(!client.closed);
        assert_eq!(client.pending_count(), 0);
    }
    #[test]
    fn scans_are_retryable_and_stale_scan_errors_do_not_clear_current_scan() {
        let (mut client, _) = mock_client();
        client.start_scanning().unwrap();
        let old = client.next_id;
        client.start_scanning().unwrap();
        assert_eq!(old, client.next_id);
        client.handle(r#"[{"ScanningFinished":{"Id":0}}]"#).unwrap();
        client.start_scanning().unwrap();
        client
            .handle(
                &json!([{"Error": {"Id": old, "ErrorCode": 4, "ErrorMessage": "old failed"}}])
                    .to_string(),
            )
            .unwrap();
        assert!(client.scanning);
        client.handle(&json!([{"Error": {"Id": client.next_id, "ErrorCode": 4, "ErrorMessage": "new failed"}}]).to_string()).unwrap();
        assert!(!client.scanning);
        assert!(!client.closed);
    }
    #[test]
    fn rejected_stop_scanning_and_wrong_ping_acknowledgement_close() {
        let (mut client, _) = mock_client();
        client.start_scanning().unwrap();
        client.handle(r#"[{"Ok":{"Id":1}}]"#).unwrap();
        client.stop_scanning().unwrap();
        assert!(client
            .handle(r#"[{"Error":{"Id":2,"ErrorCode":4,"ErrorMessage":"stop failed"}}]"#)
            .is_err());
        assert!(client.closed);
        let (mut client, _) = mock_client();
        let id = client.send("Ping", json!({}), None).unwrap();
        assert!(client
            .handle(&json!([{"DeviceList": {"Id": id, "Devices": []}}]).to_string())
            .unwrap_err()
            .contains("reply to Ping"));
    }
    #[test]
    fn pending_limit_wrap_and_hotplug_removal_are_bounded() {
        let (mut client, state) = mock_client();
        client.send("Ping", json!({}), None).unwrap();
        client.next_id = u32::MAX;
        assert_eq!(client.send("StartScanning", json!({}), None).unwrap(), 2);
        let id = client
            .send(
                "ScalarCmd",
                json!({"DeviceIndex": 1, "Scalars": []}),
                Some(1),
            )
            .unwrap();
        client
            .handle(r#"[{"DeviceRemoved":{"Id":0,"DeviceIndex":1}}]"#)
            .unwrap();
        assert!(!client.pending.contains_key(&id));
        assert!(!client.devices.contains_key(&1));
        for _ in 0..MAX_PENDING - 2 {
            client.send("Ping", json!({}), None).unwrap();
        }
        assert!(client
            .send("Ping", json!({}), None)
            .unwrap_err()
            .contains("Too many pending"));
        assert!(client.closed);
        assert!(state
            .lock()
            .unwrap()
            .sent
            .last()
            .unwrap()
            .get("StopAllDevices")
            .is_some());
    }
    #[test]
    fn strict_json_ids_numbers_nested_fields_and_capabilities() {
        let samples = [
            r#"[{"Ok":{"Id":-1}}]"#,
            r#"[{"Ok":{"Id":4294967296}}]"#,
            r#"[{"Ok":{"Id":true}}]"#,
            r#"[{"Ok":{"Id":1,"Id":2}}]"#,
            r#"[{"Ok":{"Id":0,"data":{"x":1,"x":2}}}]"#,
            r#"[{"Ok":{"Id":0,"value":NaN}}]"#,
            r#"[{"Ok":{"Id":0,"value":1e999}}]"#,
            r#"[{"DeviceRemoved":{"Id":1,"DeviceIndex":1}}]"#,
            r#"[{"ScanningFinished":{"Id":2}}]"#,
            "[]",
        ];
        for sample in samples {
            let (mut client, _) = mock_client();
            assert!(client.handle(sample).is_err(), "{sample}");
            assert!(client.closed);
        }
        assert!(parse_protocol(&format!(
            r#"[{{"Ok":{{"Id":0,"unknown":{}}}}}]"#,
            "1".repeat(100000)
        ))
        .is_err());
        assert!(parse_protocol(&format!("{}{}", "[".repeat(2000), "]".repeat(2000))).is_err());
        for attr in [
            json!({"ActuatorType": "Vibrate", "StepCount": true}),
            json!({"ActuatorType": []}),
            json!({}),
            json!({"ActuatorType": "Vibrate", "StepCount": -1}),
        ] {
            let (mut client, _) = mock_client();
            let mut d = rich(2);
            d["DeviceMessages"]["ScalarCmd"] = json!([attr]);
            assert!(client.add(d).is_err());
            assert!(client.closed);
        }
    }
    struct HandshakePeer {
        state: Arc<Mutex<WireState>>,
        overrides: Value,
        duplicate: bool,
        list: Option<(u32, Instant)>,
        delay: Duration,
    }
    impl Wire for HandshakePeer {
        fn send(&mut self, text: &str) -> Result<(), String> {
            let batch: Vec<Value> = serde_json::from_str(text).unwrap();
            let (kind, data) = batch[0].as_object().unwrap().iter().next().unwrap();
            let mut state = self.state.lock().unwrap();
            state.sent.extend(batch.clone());
            match kind.as_str() {
                "RequestServerInfo" => {
                    let mut info = json!({"Id": data["Id"], "ServerName": "Synthetic fixture", "MessageVersion": 3, "MaxPingTime": 200});
                    if let Some(overrides) = self.overrides.as_object() {
                        for (k, v) in overrides {
                            info[k] = v.clone();
                        }
                    }
                    state
                        .incoming
                        .push_back(json!([{"ServerInfo": info}]).to_string());
                }
                "RequestDeviceList" => {
                    self.list = Some((
                        data["Id"].as_u64().unwrap() as u32,
                        Instant::now() + self.delay,
                    ))
                }
                "Ping" => state
                    .incoming
                    .push_back(json!([{"Ok": {"Id": data["Id"]}}]).to_string()),
                _ => (),
            }
            Ok(())
        }
        fn recv(&mut self, timeout: f64) -> Result<Option<String>, String> {
            if let Some(text) = self.state.lock().unwrap().incoming.pop_front() {
                return Ok(Some(text));
            }
            if let Some((id, due)) = self.list {
                let wait = Duration::from_secs_f64(timeout.max(0.))
                    .min(due.saturating_duration_since(Instant::now()));
                std::thread::sleep(wait);
                if Instant::now() >= due {
                    self.list = None;
                    let devices = if self.duplicate {
                        json!([rich(1), rich(1)])
                    } else {
                        json!([])
                    };
                    return Ok(Some(
                        json!([{"DeviceList": {"Id": id, "Devices": devices}}]).to_string(),
                    ));
                }
            }
            Ok(None)
        }
        fn close(&mut self) {
            self.state.lock().unwrap().closed = true;
        }
    }
    fn handshake_peer(
        overrides: Value,
        duplicate: bool,
        delay: Duration,
    ) -> (HandshakePeer, Arc<Mutex<WireState>>) {
        let state = Arc::new(Mutex::new(WireState::default()));
        (
            HandshakePeer {
                state: state.clone(),
                overrides,
                duplicate,
                list: None,
                delay,
            },
            state,
        )
    }
    #[test]
    fn delayed_device_list_keeps_negotiated_ping_watchdog_alive() {
        let (peer, state) = handshake_peer(json!({}), false, Duration::from_millis(360));
        let client = Client::connect_wire(Box::new(peer), "Night City Haptics").unwrap();
        assert!(!client.closed);
        assert_eq!(client.ping_every, 0.1);
        assert!(
            state
                .lock()
                .unwrap()
                .sent
                .iter()
                .filter(|m| m.get("Ping").is_some())
                .count()
                >= 3
        );
    }
    #[test]
    fn invalid_server_information_and_duplicate_devices_are_rejected() {
        for overrides in [
            json!({"MessageVersion": 3.0}),
            json!({"MaxPingTime": true}),
            json!({"MaxPingTime": -1}),
            json!({"MaxPingTime": 4294967296u64}),
            json!({"ServerName": []}),
        ] {
            let (peer, state) = handshake_peer(overrides, false, Duration::ZERO);
            assert!(Client::connect_wire(Box::new(peer), "Night City Haptics").is_err());
            assert!(state.lock().unwrap().closed);
        }
        let (peer, state) = handshake_peer(json!({}), true, Duration::ZERO);
        assert!(Client::connect_wire(Box::new(peer), "Night City Haptics")
            .err()
            .unwrap()
            .contains("Duplicate Intiface device index"));
        assert!(state.lock().unwrap().closed);
    }
    fn frame(opcode: u8, data: &[u8], final_frame: bool) -> Vec<u8> {
        let mut result = vec![opcode | if final_frame { 0x80 } else { 0 }];
        if data.len() < 126 {
            result.push(data.len() as u8);
        } else if data.len() < 65536 {
            result.push(126);
            result.extend_from_slice(&(data.len() as u16).to_be_bytes());
        } else {
            result.push(127);
            result.extend_from_slice(&(data.len() as u64).to_be_bytes());
        }
        result.extend_from_slice(data);
        result
    }
    fn raw_websocket(buffer: Vec<u8>) -> (WebSocket, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let socket = listener.accept().unwrap().0;
        socket.set_nonblocking(true).unwrap();
        let writer = tungstenite::WebSocket::from_raw_socket(
            socket.try_clone().unwrap(),
            Role::Client,
            None,
        );
        (
            WebSocket {
                socket,
                writer,
                buffer,
                fragments: vec![],
                fragmenting: false,
            },
            peer,
        )
    }
    #[test]
    fn fragmented_utf8_interleaved_controls_and_receive_budget() {
        let data = "\"é\"".as_bytes();
        let buffer = [
            frame(1, &data[..2], false),
            frame(9, b"hello", true),
            frame(10, b"ok", true),
            frame(0, &data[2..], true),
        ]
        .concat();
        let (mut ws, mut peer) = raw_websocket(buffer);
        assert_eq!(ws.recv(0.).unwrap(), Some("\"é\"".into()));
        assert!(!ws.fragmenting);
        let mut pong = [0u8; 64];
        peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let n = peer.read(&mut pong).unwrap();
        assert!(n >= 6);
        assert_eq!(pong[0] & 15, 10);
        assert_ne!(pong[1] & 128, 0);
        ws.buffer = frame(10, &[], true).repeat(1000);
        assert_eq!(ws.recv(0.).unwrap(), None);
        assert!(!ws.buffer.is_empty());
        assert!(ws.buffer.len() < 2000);
    }
    #[test]
    fn invalid_frame_forms_and_aggregate_fragments_close() {
        let mut huge = vec![0x81, 127];
        huge.extend_from_slice(&((MAX_MESSAGE + 1) as u64).to_be_bytes());
        let samples = vec![
            frame(0, b"orphan", true),
            [frame(1, b"old", false), frame(1, b"new", true)].concat(),
            frame(9, b"fragmented", false),
            frame(9, &[b'x'; 126], true),
            vec![0xc1, 0],
            vec![0x81, 0x80],
            frame(2, b"binary", true),
            frame(8, b"x", true),
            vec![0x81, 126, 0, 1, b'x'],
            vec![0x81, 127, 0, 0, 0, 0, 0, 0, 0, 1, b'x'],
            frame(1, &[255], true),
            huge,
        ];
        for sample in samples {
            let (mut ws, mut peer) = raw_websocket(sample);
            assert!(ws.recv(0.).is_err());
            let mut b = [0u8];
            assert_eq!(peer.read(&mut b).unwrap(), 0);
        }
        let (mut ws, _) = raw_websocket(frame(0, &[], false));
        ws.fragments = vec![b'x'; MAX_MESSAGE - 1];
        ws.fragmenting = true;
        ws.buffer = vec![0, 2];
        assert_eq!(ws.recv(0.).unwrap_err(), "Message too large");
    }
    #[test]
    fn partial_fragment_read_has_byte_budget() {
        let (mut ws, mut peer) = raw_websocket(vec![]);
        let first = frame(1, &[b'x'; 65536], false);
        let more = frame(0, &[b'x'; 65536], false).repeat(20);
        let worker = std::thread::spawn(move || {
            let _ = peer.write_all(&[first, more].concat());
        });
        let start = Instant::now();
        assert_eq!(ws.recv(0.02).unwrap(), None);
        assert!(start.elapsed() < Duration::from_millis(200));
        assert!(ws.fragments.len() + ws.buffer.len() <= RECV_BYTES);
        ws.close();
        worker.join().unwrap();
    }
    #[test]
    fn invalid_addresses_are_rejected_before_connection() {
        for address in [
            "ws://127.0.0.1:0/",
            "ws://u:p@127.0.0.1/",
            "ws://127.0.0.1/#fragment",
            "ws://127.0.0.1/a\r\nInjected:value",
            "ws://127.0.0.1/path with spaces",
            "wss://127.0.0.1/",
        ] {
            assert!(WebSocket::connect(address).is_err(), "{address}");
        }
    }
    #[test]
    fn real_socket_handshake_preserves_early_frame_and_connection_tokens() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let worker = std::thread::spawn(move || {
            let (mut peer, _) = listener.accept().unwrap();
            let mut request = vec![];
            let mut b = [0u8; 2048];
            while !request.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = peer.read(&mut b).unwrap();
                request.extend_from_slice(&b[..n]);
            }
            let request = String::from_utf8(request).unwrap();
            assert!(request.starts_with("GET /control?mode=test HTTP/1.1\r\n"));
            let key = request
                .split("Sec-WebSocket-Key: ")
                .nth(1)
                .unwrap()
                .split("\r\n")
                .next()
                .unwrap();
            let accept = tungstenite::handshake::derive_accept_key(key.as_bytes());
            let head = format!("HTTP/1.1 101 Switching Protocols\r\nUpgrade: WebSocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n");
            peer.write_all(&[head.as_bytes(), &frame(1, b"early", true)].concat())
                .unwrap();
            let _ = peer.read(&mut b);
        });
        let mut ws = WebSocket::connect(&format!("ws://{addr}/control?mode=test")).unwrap();
        assert_eq!(ws.recv(0.1).unwrap(), Some("early".into()));
        ws.close();
        worker.join().unwrap();
    }
}
