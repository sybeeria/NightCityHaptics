//! Steam lifecycle helper. Only a companion started here is stopped here.
#[cfg(unix)]
mod unix {
    use serde_json::Value;
    use std::{
        env,
        io::{Read, Write},
        net::{SocketAddr, TcpStream},
        os::unix::process::CommandExt,
        path::PathBuf,
        process::{Child, Command, Stdio},
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
        thread,
        time::{Duration, Instant},
    };

    fn running(port: u16, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let address = SocketAddr::from(([127, 0, 0, 1], port));
        let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
            return false;
        };
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return false;
        }
        let _ = stream.set_write_timeout(Some(remaining));
        let request = format!(
            "GET /api/ping HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n"
        );
        if stream.write_all(request.as_bytes()).is_err() {
            return false;
        }
        let mut response = Vec::new();
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            if stream.set_read_timeout(Some(remaining)).is_err() {
                return false;
            }
            let mut bytes = [0u8; 4096];
            let limit = bytes.len().min(65537 - response.len());
            match stream.read(&mut bytes[..limit]) {
                Ok(0) => break,
                Ok(count) => response.extend_from_slice(&bytes[..count]),
                Err(_) => return false,
            }
            if response.len() > 65536 {
                return false;
            }
        }
        let Some(boundary) = response.windows(4).position(|p| p == b"\r\n\r\n") else {
            return false;
        };
        if !response.starts_with(b"HTTP/1.1 200 ") && !response.starts_with(b"HTTP/1.0 200 ") {
            return false;
        }
        serde_json::from_slice::<Value>(&response[boundary + 4..])
            .ok()
            .is_some_and(|v| v["app"] == "night-city-haptics")
    }
    fn stop(child: &mut Child) {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        unsafe {
            libc::kill(child.id() as i32, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(25));
        }
        eprintln!(
            "Night City Haptics: companion did not stop within five seconds; stopping our child."
        );
        let _ = child.kill();
        let _ = child.wait();
    }
    pub fn run(args: &[String]) -> i32 {
        let mut path = env::current_exe().unwrap_or_else(|_| PathBuf::from("night-city-haptics"));
        let mut port = 19870;
        let mut offset = 0;
        while offset < args.len() && args[offset] != "--" {
            if offset + 1 >= args.len() {
                eprintln!("Expected a value before Steam's -- command");
                return 2;
            }
            match args[offset].as_str() {
                "--companion" => path = PathBuf::from(&args[offset + 1]),
                "--port" => match args[offset + 1].parse::<u16>() {
                    Ok(p) if p > 0 => port = p,
                    _ => return 2,
                },
                _ => {
                    eprintln!("Unknown Steam launcher option: {}", args[offset]);
                    return 2;
                }
            }
            offset += 2;
        }
        if args.get(offset).map(String::as_str) != Some("--") || offset + 1 == args.len() {
            eprintln!("Supply Steam's command after --");
            return 2;
        }
        let interrupted = Arc::new(AtomicUsize::new(0));
        let signal_events = Arc::new(AtomicUsize::new(0));
        let mut handlers = Vec::new();
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            let interrupted = interrupted.clone();
            let signal_events = signal_events.clone();
            // The handler only performs lock-free atomic operations. Count each
            // delivery so a second SIGTERM can reach a child that kept running.
            let handler = unsafe {
                signal_hook::low_level::register(signal, move || {
                    interrupted.store(signal as usize, Ordering::SeqCst);
                    signal_events.fetch_add(1, Ordering::SeqCst);
                })
            };
            match handler {
                Ok(id) => handlers.push(id),
                Err(e) => {
                    eprintln!("Cannot install signal handler: {e}");
                    return 1;
                }
            }
        }
        let mut companion = None;
        if !running(port, Duration::from_millis(200)) && interrupted.load(Ordering::SeqCst) == 0 {
            let mut command = Command::new(&path);
            command.args(["--no-browser", "--port", &port.to_string()]);
            if let Some(mod_dir) = path.parent().and_then(|p| p.parent()) {
                command.arg("--mod-dir").arg(mod_dir);
            }
            command
                .env_remove("LD_LIBRARY_PATH")
                .env_remove("LD_PRELOAD")
                .env_remove("NCH_STEAM_SAVED_LD_LIBRARY_PATH")
                .env_remove("NCH_STEAM_SAVED_LD_PRELOAD")
                .stdin(Stdio::null());
            match command.spawn() {
                Ok(mut child) => {
                    let deadline = Instant::now() + Duration::from_secs(2);
                    loop {
                        if child.try_wait().ok().flatten().is_some() {break;}
                        let remaining = deadline.saturating_duration_since(Instant::now());
                        if remaining.is_zero() || interrupted.load(Ordering::SeqCst) != 0 || running(port, Duration::from_millis(200).min(remaining)) {
                            companion = Some(child); break;
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
                Err(e) => eprintln!("Night City Haptics: could not start the companion ({e}); continuing with the game."),
            }
        }
        let signal = interrupted.load(Ordering::SeqCst);
        let result = if signal != 0 {
            128 + signal as i32
        } else {
            let mut command = Command::new(&args[offset + 1]);
            command.args(&args[offset + 2..]).process_group(0);
            for (name, saved) in [
                ("LD_LIBRARY_PATH", "NCH_STEAM_SAVED_LD_LIBRARY_PATH"),
                ("LD_PRELOAD", "NCH_STEAM_SAVED_LD_PRELOAD"),
            ] {
                if let Some(value) = env::var_os(saved) {
                    command.env(name, value);
                }
                command.env_remove(saved);
            }
            match command.spawn() {
                Ok(mut game) => {
                    let mut forwarded = 0;
                    loop {
                        let events = signal_events.load(Ordering::SeqCst);
                        let signal = interrupted.load(Ordering::SeqCst);
                        if signal != 0 && events != forwarded {
                            unsafe {
                                libc::kill(-(game.id() as i32), signal as i32);
                            }
                            forwarded = events;
                        }
                        match game.try_wait() {
                            Ok(Some(status)) => {
                                use std::os::unix::process::ExitStatusExt;
                                break status
                                    .code()
                                    .unwrap_or_else(|| 128 + status.signal().unwrap_or(0));
                            }
                            Err(e) => {
                                eprintln!("Cannot wait for Steam command: {e}");
                                break 1;
                            }
                            _ => thread::sleep(Duration::from_millis(20)),
                        }
                    }
                }
                Err(e) => {
                    eprintln!("Night City Haptics: could not start Steam's command: {e}");
                    if e.kind() == std::io::ErrorKind::NotFound {
                        127
                    } else {
                        126
                    }
                }
            }
        };
        if let Some(mut child) = companion {
            stop(&mut child);
        }
        for handler in handlers {
            signal_hook::low_level::unregister(handler);
        }
        result
    }
}
pub fn main_cli(args: &[String]) -> i32 {
    #[cfg(unix)]
    {
        unix::run(args)
    }
    #[cfg(not(unix))]
    {
        let _ = args;
        eprintln!("Steam lifecycle helper requires Linux/SteamOS");
        2
    }
}
