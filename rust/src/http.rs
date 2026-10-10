//! Small loopback-only HTTP/1.1 server. Every POST is explicitly framed and bounded.
use crate::{
    app::{App, VERSION},
    buttplug,
};
use serde_json::{json, Value};
use std::{
    fs,
    io::{self, BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const MAX_BODY: usize = 262144;
const MAX_HEADERS: usize = 65536;
const CSP: &str = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'";
pub type SharedApp = Arc<Mutex<App>>;

#[derive(Debug)]
struct Request {
    method: String,
    path: String,
    version: String,
    headers: Vec<(String, String)>,
}
impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, v)| v.as_str())
    }
    fn close(&self) -> bool {
        self.version != "HTTP/1.1"
            || self
                .header("connection")
                .map(|v| v.eq_ignore_ascii_case("close"))
                .unwrap_or(false)
    }
    fn local(&self, port: u16) -> bool {
        self.header("host")
            .map(|host| host == format!("127.0.0.1:{port}") || host == format!("localhost:{port}"))
            .unwrap_or(false)
    }
    fn length(&self) -> Result<usize, Response> {
        let lengths: Vec<_> = self
            .headers
            .iter()
            .filter(|(key, _)| key == "content-length")
            .map(|(_, value)| value)
            .collect();
        if self.header("transfer-encoding").is_some()
            || lengths.len() != 1
            || lengths[0].is_empty()
            || !lengths[0].bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err(Response::error(400, "A valid Content-Length is required").closing());
        }
        // Parse without overflowing, including arbitrarily many leading zeroes.
        let mut length = 0usize;
        for digit in lengths[0].bytes() {
            length = length
                .checked_mul(10)
                .and_then(|v| v.checked_add((digit - b'0') as usize))
                .ok_or_else(|| Response::error(413, "Request body is too large").closing())?;
            if length > MAX_BODY {
                return Err(Response::error(413, "Request body is too large").closing());
            }
        }
        Ok(length)
    }
}

#[derive(Debug)]
struct Response {
    code: u16,
    body: Vec<u8>,
    kind: &'static str,
    close: bool,
}
impl Response {
    fn json(code: u16, value: Value) -> Self {
        Self {
            code,
            body: serde_json::to_vec(&value).unwrap_or_else(|_| b"{}".to_vec()),
            kind: "application/json; charset=utf-8",
            close: false,
        }
    }
    fn bytes(body: Vec<u8>, kind: &'static str) -> Self {
        Self {
            code: 200,
            body,
            kind,
            close: false,
        }
    }
    fn error(code: u16, message: &str) -> Self {
        Self::json(code, json!({"error":message}))
    }
    fn closing(mut self) -> Self {
        self.close = true;
        self
    }
    fn write(&self, output: &mut impl Write, close: bool) -> io::Result<()> {
        let reason = match self.code {
            200 => "OK",
            400 => "Bad Request",
            403 => "Forbidden",
            404 => "Not Found",
            413 => "Request Entity Too Large",
            431 => "Request Header Fields Too Large",
            500 => "Internal Server Error",
            501 => "Not Implemented",
            _ => "Error",
        };
        write!(output,"HTTP/1.1 {} {}\r\nServer: NightCityHaptics/{}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: {}\r\n",self.code,reason,VERSION,self.kind,self.body.len(),CSP)?;
        if close {
            output.write_all(b"Connection: close\r\n")?;
        }
        output.write_all(b"\r\n")?;
        output.write_all(&self.body)?;
        output.flush()
    }
}

// read_until would allocate without a bound before noticing an oversized line.
fn bounded_line(reader: &mut impl BufRead, limit: usize) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    loop {
        let buffer = reader.fill_buf()?;
        if buffer.is_empty() {
            return Ok(line);
        }
        let count = buffer
            .iter()
            .position(|b| *b == b'\n')
            .map(|p| p + 1)
            .unwrap_or(buffer.len());
        if count > limit.saturating_sub(line.len()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "HTTP line is too large",
            ));
        }
        line.extend_from_slice(&buffer[..count]);
        let finished = buffer[count - 1] == b'\n';
        reader.consume(count);
        if finished {
            return Ok(line);
        }
    }
}

fn read_request(reader: &mut impl BufRead) -> Result<Option<Request>, Response> {
    let line = match bounded_line(reader, 8192) {
        Ok(line) if line.is_empty() => return Ok(None),
        Ok(line) => line,
        Err(_) => return Err(Response::error(400, "Invalid request line").closing()),
    };
    let text = std::str::from_utf8(&line)
        .map_err(|_| Response::error(400, "Invalid request line").closing())?;
    let fields: Vec<_> = text.split_whitespace().collect();
    if fields.len() != 3
        || !matches!(fields[2], "HTTP/1.1" | "HTTP/1.0")
        || !fields[0].bytes().all(|b| b.is_ascii_uppercase())
        || !fields[1].is_ascii()
    {
        return Err(Response::error(400, "Invalid request line").closing());
    }
    let mut request = Request {
        method: fields[0].into(),
        path: fields[1].into(),
        version: fields[2].into(),
        headers: Vec::new(),
    };
    let mut size = line.len();
    loop {
        let line = bounded_line(reader, MAX_HEADERS.saturating_sub(size))
            .map_err(|_| Response::error(431, "Request headers are too large").closing())?;
        size += line.len();
        if line == b"\r\n" || line == b"\n" {
            break;
        }
        if line.is_empty() || request.headers.len() >= 100 || !line.ends_with(b"\n") {
            return Err(Response::error(400, "Invalid request headers").closing());
        }
        let text: String = line.iter().map(|b| *b as char).collect();
        let Some((key, value)) = text.trim_end_matches(['\r', '\n']).split_once(':') else {
            return Err(Response::error(400, "Invalid request headers").closing());
        };
        if key.is_empty()
            || !key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
        {
            return Err(Response::error(400, "Invalid request headers").closing());
        }
        let value = value.trim_matches([' ', '\t']);
        if value
            .chars()
            .any(|c| (c as u32) < 32 && c != '\t' || c == '\u{7f}')
        {
            return Err(Response::error(400, "Invalid request headers").closing());
        }
        request
            .headers
            .push((key.to_ascii_lowercase(), value.into()));
    }
    Ok(Some(request))
}

fn get(request: &Request, app: &SharedApp, port: u16) -> Response {
    if !request.local(port) {
        return Response::error(403, "Open the page from 127.0.0.1");
    }
    // The single-instance probe must answer even while the engine handshake is busy.
    if request.path == "/api/ping" {
        return Response::json(200, json!({"app":"night-city-haptics","version":VERSION}));
    }
    let app = app.lock().unwrap_or_else(|p| p.into_inner());
    match request.path.as_str() {
        "/" | "/index.html" => match fs::read(app.assets.join("ui.html")) {
            Ok(bytes) => Response::bytes(bytes, "text/html; charset=utf-8"),
            Err(_) => Response::error(500, "Could not load control page"),
        },
        "/fonts/archivo.woff2" => match fs::read(app.assets.join("fonts/archivo.woff2")) {
            Ok(bytes) => Response::bytes(bytes, "font/woff2"),
            Err(_) => Response::error(500, "Could not load font"),
        },
        "/api/state" => {
            let mut state = app.snapshot(buttplug::monotonic());
            state["catalog"] = if request
                .header("x-nch-catalog")
                .map(|h| !h.is_empty())
                .unwrap_or(false)
            {
                let mut catalog = app.catalog.clone();
                catalog["previews"] = app.previews.clone();
                catalog
            } else {
                Value::Null
            };
            Response::json(200, state)
        }
        _ => Response::error(404, "Not found"),
    }
}

fn post(request: &Request, reader: &mut impl Read, app: &SharedApp, port: u16) -> Response {
    let length = match request.length() {
        Ok(length) => length,
        Err(response) => return response,
    };
    let mut bytes = vec![0u8; length];
    if reader.read_exact(&mut bytes).is_err() {
        return Response::error(400, "Incomplete request body").closing();
    }
    if !request.local(port) || request.header("x-nch") != Some("1") {
        return Response::error(403, "Rejected");
    }
    let body: Value = match serde_json::from_slice(if bytes.is_empty() { b"{}" } else { &bytes }) {
        Ok(body) => body,
        Err(error) => return Response::error(400, &error.to_string()),
    };
    if !body.is_object() {
        return Response::error(400, "Request body must be an object");
    }
    if !request.path.starts_with("/api/") || request.path.matches('/').count() != 2 {
        return Response::error(404, "Unknown action");
    }
    let mut app = app.lock().unwrap_or_else(|p| p.into_inner());
    let now = buttplug::monotonic();
    let result = match &request.path[5..] {
        "settings" => app.save(&body),
        "reset" => app.reset(),
        "try" => app.try_interaction(body["id"].as_str().unwrap_or(""), now),
        "test-device" => app.test_device(body["key"].as_str().unwrap_or(""), now),
        "scan" => app.scan(now),
        "download-engine" => app.engine.request_download().map(|_| ()),
        "reconnect" => {
            app.disconnect("Reconnecting");
            app.next_connect = 0.;
            Ok(())
        }
        "quit" => {
            app.stopping = true;
            Ok(())
        }
        _ => return Response::error(404, "Unknown action"),
    };
    match result {
        Ok(()) => Response::json(200, json!({"ok":true})),
        Err(error) => Response::error(400, &error),
    }
}

fn connection(mut stream: TcpStream, app: SharedApp, port: u16, stopping: Arc<AtomicBool>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_nodelay(true);
    let Ok(input) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(input);
    while !stopping.load(Ordering::Acquire) {
        let request = match read_request(&mut reader) {
            Ok(Some(request)) => request,
            Ok(None) => return,
            Err(response) => {
                let _ = response.write(&mut stream, true);
                return;
            }
        };
        let response = match request.method.as_str() {
            "GET" => get(&request, &app, port),
            "POST" => post(&request, &mut reader, &app, port),
            _ => Response::error(501, "Unsupported method").closing(),
        };
        let close = request.close() || response.close;
        if response.write(&mut stream, close).is_err() || close {
            return;
        }
    }
}

pub struct Server {
    pub port: u16,
    stopping: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}
impl Server {
    pub fn start(app: SharedApp, port: u16) -> io::Result<Self> {
        let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port))?;
        let port = listener.local_addr()?.port();
        listener.set_nonblocking(true)?;
        let stopping = Arc::new(AtomicBool::new(false));
        let signal = Arc::clone(&stopping);
        let count = Arc::new(AtomicUsize::new(0));
        let worker = thread::Builder::new()
            .name("control-page".into())
            .spawn(move || {
                while !signal.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            if count.load(Ordering::Acquire) >= 128 {
                                continue;
                            }
                            count.fetch_add(1, Ordering::AcqRel);
                            let app = Arc::clone(&app);
                            let stopping = Arc::clone(&signal);
                            let connections = Arc::clone(&count);
                            let spawn = thread::Builder::new()
                                .name("control-request".into())
                                .spawn(move || {
                                    struct ConnectionCount(Arc<AtomicUsize>);
                                    impl Drop for ConnectionCount {
                                        fn drop(&mut self) {
                                            self.0.fetch_sub(1, Ordering::AcqRel);
                                        }
                                    }
                                    let _count = ConnectionCount(connections);
                                    connection(stream, app, port, stopping);
                                });
                            if spawn.is_err() {
                                count.fetch_sub(1, Ordering::AcqRel);
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(15))
                        }
                        Err(_) => break,
                    }
                }
            })?;
        Ok(Self {
            port,
            stopping,
            worker: Some(worker),
        })
    }
    pub fn stop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Apply the same deadline to every read/write rather than resetting it when
/// a peer supplies another byte. A slow unrelated listener must not hold startup.
struct ProbeStream {
    socket: TcpStream,
    deadline: Instant,
}
impl ProbeStream {
    fn remaining(&self) -> io::Result<Duration> {
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "Instance probe timed out"))
    }
}
impl Read for ProbeStream {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.socket.set_read_timeout(Some(self.remaining()?))?;
        self.socket.read(bytes)
    }
}
impl Write for ProbeStream {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.socket.set_write_timeout(Some(self.remaining()?))?;
        self.socket.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.remaining()?;
        self.socket.flush()
    }
}

/// Probe directly over a loopback socket, with no environment proxy, unbounded
/// body, or response trickle that extends the one-second startup deadline.
pub fn already_running(port: u16) -> bool {
    already_running_with_timeout(port, Duration::from_secs(1))
}
fn already_running_with_timeout(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let Ok(stream) = TcpStream::connect_timeout(
        &SocketAddrV4::new(Ipv4Addr::LOCALHOST, port).into(),
        timeout,
    ) else {
        return false;
    };
    let mut stream = ProbeStream {
        socket: stream,
        deadline,
    };
    if write!(
        stream,
        "GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    )
    .is_err()
    {
        return false;
    }
    let mut reader = BufReader::new(stream);
    let Ok(status) = bounded_line(&mut reader, 8192) else {
        return false;
    };
    if !status.starts_with(b"HTTP/1.1 200 ") && !status.starts_with(b"HTTP/1.0 200 ") {
        return false;
    }
    let mut length = None;
    let mut headers = 0usize;
    loop {
        let Ok(line) = bounded_line(&mut reader, MAX_HEADERS.saturating_sub(headers)) else {
            return false;
        };
        headers += line.len();
        if line == b"\r\n" || line == b"\n" {
            break;
        }
        if line.is_empty() {
            return false;
        }
        let Ok(text) = std::str::from_utf8(&line) else {
            return false;
        };
        if let Some((name, value)) = text.trim().split_once(':') {
            if name.eq_ignore_ascii_case("transfer-encoding") {
                return false;
            }
            if name.eq_ignore_ascii_case("content-length") {
                if length.is_some() {
                    return false;
                }
                length = value.trim().parse::<usize>().ok();
                if length.is_none() {
                    return false;
                }
            }
        }
    }
    let mut bytes = Vec::new();
    if let Some(length) = length {
        if length > 65536 {
            return false;
        }
        bytes.resize(length, 0);
        if reader.read_exact(&mut bytes).is_err() {
            return false;
        }
    } else if reader.take(65537).read_to_end(&mut bytes).is_err() || bytes.len() > 65536 {
        return false;
    }
    if Instant::now() >= deadline {
        return false;
    }
    serde_json::from_slice::<Value>(&bytes)
        .map(|value| value.is_object() && value["app"] == "night-city-haptics")
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn read_probe_request(stream: &mut TcpStream) {
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut byte = [0; 1];
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
            assert!(request.len() <= 1024);
        }
    }
    fn parse(bytes: &[u8]) -> Request {
        read_request(&mut io::Cursor::new(bytes))
            .unwrap_or_else(|_| panic!("invalid fixture"))
            .unwrap()
    }
    #[test]
    fn framing_rejects_duplicates_transfer_encoding_and_unicode() {
        for headers in [
            "Content-Length: 0\r\nContent-Length: 0",
            "Content-Length: +1",
            "Content-Length: -1",
            "Content-Length: 1,1",
            "Transfer-Encoding: chunked\r\nContent-Length: 0",
            "Transfer-Encoding:\r\nContent-Length: 0",
            "Content-Length: ٢",
        ] {
            let raw =
                format!("POST /api/quit HTTP/1.1\r\nHost: localhost:19870\r\n{headers}\r\n\r\n");
            let response = parse(raw.as_bytes()).length().err().unwrap();
            assert_eq!(response.code, 400);
            assert!(response.close);
        }
        assert_eq!(
            parse(b"POST /api/quit HTTP/1.1\r\nContent-Length: 262145\r\n\r\n")
                .length()
                .err()
                .unwrap()
                .code,
            413
        );
        assert_eq!(
            parse(b"POST /api/quit HTTP/1.1\r\nContent-Length: 000262144\r\n\r\n")
                .length()
                .unwrap(),
            MAX_BODY
        );
    }
    #[test]
    fn origin_gate_and_keep_alive_are_exact() {
        let local = parse(b"GET /api/ping HTTP/1.1\r\nHost: localhost:19870\r\n\r\n");
        assert!(local.local(19870));
        assert!(!local.local(19871));
        assert!(!local.close());
        let remote=parse(b"GET /api/ping HTTP/1.1\r\nHost: localhost.attacker:19870\r\nConnection: close\r\n\r\n");
        assert!(!remote.local(19870));
        assert!(remote.close());
    }
    #[test]
    fn oversized_headers_are_bounded() {
        let raw = format!(
            "GET / HTTP/1.1\r\nX-Large: {}\r\n\r\n",
            "a".repeat(MAX_HEADERS)
        );
        assert_eq!(
            read_request(&mut io::Cursor::new(raw)).err().unwrap().code,
            431
        );
    }
    #[test]
    fn instance_probe_has_one_deadline_for_slow_status_headers_and_body() {
        let body = b"{\"app\":\"night-city-haptics\"}";
        let status = b"HTTP/1.1 200 OK\r\n";
        let headers = format!("Content-Length: {}\r\n\r\n", body.len());
        for phase in 0..3 {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
            let port = listener.local_addr().unwrap().port();
            let (prefix, trickle) = match phase {
                0 => (
                    Vec::new(),
                    [status.as_slice(), headers.as_bytes(), body.as_slice()].concat(),
                ),
                1 => (
                    status.to_vec(),
                    [headers.as_bytes(), body.as_slice()].concat(),
                ),
                _ => (
                    [status.as_slice(), headers.as_bytes()].concat(),
                    body.to_vec(),
                ),
            };
            let worker = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(1)))
                    .unwrap();
                read_probe_request(&mut stream);
                if stream.write_all(&prefix).is_err() {
                    return;
                }
                for byte in trickle {
                    thread::sleep(Duration::from_millis(40));
                    if stream.write_all(&[byte]).is_err() {
                        break;
                    }
                }
            });
            let began = Instant::now();
            assert!(!already_running_with_timeout(
                port,
                Duration::from_millis(120)
            ));
            assert!(
                began.elapsed() < Duration::from_millis(750),
                "phase {phase} extended the deadline"
            );
            worker.join().unwrap();
        }
    }
    #[test]
    fn instance_probe_accepts_a_complete_response_within_the_deadline() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let worker = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            read_probe_request(&mut stream);
            let body = "{\"app\":\"night-city-haptics\"}";
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        assert!(already_running_with_timeout(port, Duration::from_secs(1)));
        worker.join().unwrap();
    }
}
