//! Steam lifecycle helper. Only a companion started here is stopped here.
use serde_json::Value;
use std::{
    env,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    time::{Duration, Instant},
};

fn instance(port: u16, timeout: Duration) -> Option<Value> {
    let deadline = Instant::now() + timeout;
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
        return None;
    };
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return None;
    }
    let _ = stream.set_write_timeout(Some(remaining));
    let request =
        format!("GET /api/ping HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return None;
    }
    let mut response = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return None;
        }
        if stream.set_read_timeout(Some(remaining)).is_err() {
            return None;
        }
        let mut bytes = [0u8; 4096];
        let limit = bytes.len().min(65537 - response.len());
        match stream.read(&mut bytes[..limit]) {
            Ok(0) => break,
            Ok(count) => response.extend_from_slice(&bytes[..count]),
            Err(_) => return None,
        }
        if response.len() > 65536 {
            return None;
        }
    }
    let Some(boundary) = response.windows(4).position(|p| p == b"\r\n\r\n") else {
        return None;
    };
    if !response.starts_with(b"HTTP/1.1 200 ") && !response.starts_with(b"HTTP/1.0 200 ") {
        return None;
    }
    serde_json::from_slice::<Value>(&response[boundary + 4..])
        .ok()
        .filter(|v| v["app"] == "night-city-haptics")
}

fn running(port: u16, timeout: Duration) -> bool {
    instance(port, timeout).is_some()
}

/// `[--companion PATH] [--port PORT] -- <Steam command...>` -> (companion, port, index of `--`).
fn options(args: &[String]) -> Result<(PathBuf, u16, usize), i32> {
    let mut path = env::current_exe().unwrap_or_else(|_| PathBuf::from("night-city-haptics"));
    let mut port = 19870;
    let mut offset = 0;
    while offset < args.len() && args[offset] != "--" {
        if offset + 1 >= args.len() {
            eprintln!("Expected a value before Steam's -- command");
            return Err(2);
        }
        match args[offset].as_str() {
            "--companion" => path = PathBuf::from(&args[offset + 1]),
            "--port" => match args[offset + 1].parse::<u16>() {
                Ok(p) if p > 0 => port = p,
                _ => return Err(2),
            },
            _ => {
                eprintln!("Unknown Steam launcher option: {}", args[offset]);
                return Err(2);
            }
        }
        offset += 2;
    }
    if args.get(offset).map(String::as_str) != Some("--") || offset + 1 == args.len() {
        eprintln!("Supply Steam's command after --");
        return Err(2);
    }
    Ok((path, port, offset))
}

#[cfg(any(windows, test))]
fn keep_waiting_for_game(seen: &mut bool, running: bool, now: Instant, deadline: Instant) -> bool {
    if running {
        *seen = true;
        true
    } else {
        !*seen && now < deadline
    }
}

#[cfg(unix)]
mod unix {
    use super::{options, running};
    use std::{
        env,
        os::unix::process::CommandExt,
        process::{Child, Command, Stdio},
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
        thread,
        time::{Duration, Instant},
    };

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
        let (path, port, offset) = match options(args) {
            Ok(parsed) => parsed,
            Err(code) => return code,
        };
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
/// Windows: Steam runs `night-city-haptics.exe --steam -- %command%`. There are no signals or
/// process groups, so the companion is asked to quit over its own API (stopping toys and the
/// engine it started), and the game is tracked by process name because CDPR's launcher can
/// exit while Cyberpunk2077.exe keeps running.
#[cfg(windows)]
mod windows {
    use super::{instance, keep_waiting_for_game, options, running};
    use std::{
        io::{Read, Write},
        net::{SocketAddr, TcpStream},
        os::windows::process::CommandExt,
        path::Path,
        process::{Child, Command, Stdio},
        thread,
        time::{Duration, Instant},
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
        System::{
            Console::FreeConsole,
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
                TH32CS_SNAPPROCESS,
            },
        },
    };

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const GAME: &str = "Cyberpunk2077.exe";
    /// How long a launcher may take to hand over to the game after it exits.
    const HANDOVER: Duration = Duration::from_secs(60);

    fn game_running() -> bool {
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if snapshot == INVALID_HANDLE_VALUE {
                return false;
            }
            let mut entry: PROCESSENTRY32W = std::mem::zeroed();
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            let mut found = false;
            let mut more = Process32FirstW(snapshot, &mut entry) != 0;
            while more {
                let len = entry
                    .szExeFile
                    .iter()
                    .position(|&c| c == 0)
                    .unwrap_or(entry.szExeFile.len());
                if String::from_utf16_lossy(&entry.szExeFile[..len]).eq_ignore_ascii_case(GAME) {
                    found = true;
                    break;
                }
                more = Process32NextW(snapshot, &mut entry) != 0;
            }
            CloseHandle(snapshot);
            found
        }
    }

    fn quit(port: u16, expected_pid: u32) {
        // Older companions have no guarded Quit contract. Require their
        // instance identity first, then let the current server recheck it in
        // the atomic Quit request if the port changes after this probe.
        if instance(port, Duration::from_millis(200)).and_then(|reply| reply["pid"].as_u64())
            != Some(expected_pid as u64)
        {
            return;
        }
        let address = SocketAddr::from(([127, 0, 0, 1], port));
        let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_secs(1)) else {
            return;
        };
        let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let body = format!("{{\"expected_pid\":{expected_pid}}}");
        let request = format!(
            "POST /api/quit HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-NCH: 1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()
        );
        if stream.write_all(request.as_bytes()).is_ok() {
            let _ = stream.read(&mut [0u8; 1024]);
        }
    }

    fn stop(child: &mut Child, port: u16) {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        // The port can be rebound while our child starts or stops. The server
        // checks this PID in the same request that stops it, so only our child
        // can accept the supervisor's Quit.
        quit(port, child.id());
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        eprintln!(
            "Night City Haptics: companion did not quit within ten seconds; stopping our child."
        );
        let _ = child.kill();
        let _ = child.wait();
    }

    pub fn run(args: &[String]) -> i32 {
        // Steam starts this console binary from a GUI; drop the console window it gets.
        unsafe {
            FreeConsole();
        }
        let (path, port, offset) = match options(args) {
            Ok(parsed) => parsed,
            Err(code) => return code,
        };
        let mut companion = None;
        if !running(port, Duration::from_millis(200)) {
            let mut command = Command::new(&path);
            command.args(["--no-browser", "--port", &port.to_string()]);
            if let Some(mod_dir) = path.parent().and_then(|p| p.parent()) {
                command.arg("--mod-dir").arg(mod_dir);
            }
            command
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            match command.spawn() {
                Ok(mut child) => {
                    let deadline = Instant::now() + Duration::from_secs(2);
                    loop {
                        if child.try_wait().ok().flatten().is_some() {
                            break;
                        }
                        let remaining = deadline.saturating_duration_since(Instant::now());
                        if remaining.is_zero() || running(port, Duration::from_millis(200).min(remaining)) {
                            companion = Some(child);
                            break;
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
                Err(e) => eprintln!("Night City Haptics: could not start the companion ({e}); continuing with the game."),
            }
        }
        let program = &args[offset + 1];
        let mut seen = false;
        let mut launched = false;
        let result = match Command::new(program).args(&args[offset + 2..]).spawn() {
            Ok(mut game) => {
                launched = true;
                loop {
                    // A launcher may wait until its game has already ended.
                    // Observe during that wait so we do not start a new,
                    // unnecessary handover timeout after the launcher exits.
                    if companion.is_some() && game_running() {
                        seen = true;
                    }
                    match game.try_wait() {
                        Ok(Some(status)) => break status.code().unwrap_or(1),
                        Err(e) => {
                            eprintln!("Cannot wait for Steam command: {e}");
                            break 1;
                        }
                        _ => thread::sleep(Duration::from_millis(100)),
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
        };
        if companion.is_some() && launched {
            // A launcher may exit before or after starting the game: keep the companion while
            // Cyberpunk2077.exe runs, and give a launcher time to hand over first.
            let direct = Path::new(program)
                .file_name()
                .map(|n| n.to_string_lossy().eq_ignore_ascii_case(GAME))
                .unwrap_or(false);
            let handover = Instant::now() + if direct { Duration::ZERO } else { HANDOVER };
            while keep_waiting_for_game(&mut seen, game_running(), Instant::now(), handover) {
                thread::sleep(Duration::from_millis(100));
            }
        }
        if let Some(mut child) = companion {
            stop(&mut child, port);
        }
        result
    }
}

pub fn main_cli(args: &[String]) -> i32 {
    #[cfg(unix)]
    {
        unix::run(args)
    }
    #[cfg(windows)]
    {
        windows::run(args)
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = args;
        eprintln!("Steam lifecycle helper requires Linux/SteamOS or Windows");
        2
    }
}

#[cfg(test)]
mod tests {
    use super::{keep_waiting_for_game, options};
    use std::time::{Duration, Instant};
    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }
    #[test]
    fn options_take_companion_port_and_steam_command() {
        let (path, port, offset) = options(&args(&[
            "--companion",
            "c",
            "--port",
            "19991",
            "--",
            "game",
            "-x",
        ]))
        .unwrap();
        assert_eq!((path.to_str(), port, offset), (Some("c"), 19991, 4));
        assert_eq!(options(&args(&["--", "game"])).unwrap().1, 19870);
    }
    #[test]
    fn options_reject_missing_command_bad_port_and_unknown_flags() {
        for bad in [
            &["--"][..],
            &["game"],
            &["--port", "0", "--", "g"],
            &["--port"],
            &["--what", "x", "--", "g"],
        ] {
            assert_eq!(options(&args(bad)).err(), Some(2), "{bad:?}");
        }
    }
    #[test]
    fn completed_game_does_not_restart_launcher_handover_window() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs(60);
        let mut seen_during_launcher = true;
        assert!(!keep_waiting_for_game(
            &mut seen_during_launcher,
            false,
            now,
            deadline
        ));
    }
    #[test]
    fn launcher_handover_waits_for_late_game_then_stops_when_it_finishes() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs(60);
        let mut seen = false;
        assert!(keep_waiting_for_game(&mut seen, false, now, deadline));
        assert!(keep_waiting_for_game(&mut seen, true, deadline, deadline));
        assert!(!keep_waiting_for_game(&mut seen, false, deadline, deadline));
        let mut never_started = false;
        assert!(!keep_waiting_for_game(
            &mut never_started,
            false,
            deadline,
            deadline
        ));
    }
}
