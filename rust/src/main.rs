use night_city_haptics::{
    app::{self, App},
    buttplug, http,
};
use std::{
    env, fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

struct Args {
    mod_dir: Option<PathBuf>,
    settings: Option<PathBuf>,
    port: u16,
    no_browser: bool,
}
fn parse(args: &[String]) -> Result<Option<Args>, String> {
    let mut options = Args {
        mod_dir: None,
        settings: None,
        port: app::PORT,
        no_browser: false,
    };
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        let (name, value) = arg
            .split_once('=')
            .map(|(a, b)| (a, Some(b)))
            .unwrap_or((arg.as_str(), None));
        match name {
            "--help" | "-h" => {
                println!("Night City Haptics {}\n\nUsage: night-city-haptics [--mod-dir PATH] [--settings PATH] [--port PORT] [--no-browser]\n\nThe native companion connects Cyberpunk 2077 to Intiface and opens its local control page.\n--mod-dir PATH   nightCityHaptics CET folder (default: binary's parent folder)\n--settings PATH  settings file (default: companion/settings.json)\n--port PORT      control page port (default: {})\n--no-browser     do not open the control page",app::VERSION,app::PORT);
                return Ok(None);
            }
            "--version" => {
                println!("{}", app::VERSION);
                return Ok(None);
            }
            "--no-browser" if value.is_none() => options.no_browser = true,
            "--mod-dir" | "--settings" | "--port" => {
                let value = if let Some(value) = value {
                    value
                } else {
                    index += 1;
                    args.get(index)
                        .map(String::as_str)
                        .ok_or_else(|| format!("{name} requires a value"))?
                };
                match name {
                    "--mod-dir" => options.mod_dir = Some(PathBuf::from(value)),
                    "--settings" => options.settings = Some(PathBuf::from(value)),
                    _ => {
                        options.port = value
                            .parse::<u16>()
                            .map_err(|_| "port must be between 1 and 65535")?;
                        if options.port == 0 {
                            return Err("port must be between 1 and 65535".into());
                        }
                    }
                }
            }
            _ => return Err(format!("Unknown argument: {arg}")),
        }
        index += 1;
    }
    Ok(Some(options))
}
fn absolute(path: &Path) -> Result<PathBuf, String> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir().map_err(|e| e.to_string())?.join(path)
    };
    Ok(fs::canonicalize(&path).unwrap_or(path))
}
fn browser(url: &str) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("rundll32.exe")
            .args(["url.dll,FileProtocolHandler", url])
            .creation_flags(0x08000000)
            .spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(url).spawn();
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        let _ = Command::new("xdg-open").arg(url).spawn();
    }
}
fn log(message: &str) {
    eprintln!("{message}");
    if let Ok(mut log) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(app::data_dir().join("companion.log"))
    {
        let _ = writeln!(log, "{message}");
    }
}
fn run(args: &[String]) -> Result<(), String> {
    let Some(args) = parse(args)? else {
        return Ok(());
    };
    fs::create_dir_all(app::data_dir()).map_err(|e| e.to_string())?;
    let url = format!("http://127.0.0.1:{}/", args.port);
    if http::already_running(args.port) {
        println!("Night City Haptics is already running: {url}");
        if !args.no_browser {
            browser(&url);
        }
        return Ok(());
    }
    let executable = env::current_exe().map_err(|e| e.to_string())?;
    let here = executable
        .parent()
        .ok_or("Could not locate companion binary")?;
    let mod_dir = absolute(
        args.mod_dir
            .as_deref()
            .unwrap_or_else(|| here.parent().unwrap_or(here)),
    )?;
    let assets = if args.mod_dir.is_some() {
        mod_dir.join("companion")
    } else {
        here.to_path_buf()
    };
    let settings = args.settings.map(|p| absolute(&p)).transpose()?;
    let app = Arc::new(Mutex::new(App::new(mod_dir, assets, settings)?));
    let mut server = match http::Server::start(Arc::clone(&app), args.port) {
        Ok(server) => server,
        Err(error)
            if error.kind() == std::io::ErrorKind::AddrInUse
                && http::already_running(args.port) =>
        {
            println!("Night City Haptics is already running: {url}");
            if !args.no_browser {
                browser(&url);
            }
            return Ok(());
        }
        Err(error) => {
            return Err(format!(
                "Could not open control page port {}: {error}",
                args.port
            ))
        }
    };
    let stopping = Arc::new(AtomicBool::new(false));
    let signal = Arc::clone(&stopping);
    ctrlc::set_handler(move || signal.store(true, Ordering::Release)).map_err(|e| e.to_string())?;
    log(&format!("Night City Haptics {}: {url}", app::VERSION));
    if !args.no_browser {
        let url = url.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            browser(&url);
        });
    }
    loop {
        let started = buttplug::monotonic();
        let period = {
            let mut app = app.lock().unwrap_or_else(|p| p.into_inner());
            if stopping.load(Ordering::Acquire) || app.stopping {
                break;
            }
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| app.tick(started)));
            match result {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    log(&format!(
                        "Unexpected error; stopping devices and disconnecting: {error}"
                    ));
                    app.processing_error(started);
                }
                Err(_) => {
                    log("Unexpected processing error; stopping devices and disconnecting");
                    app.processing_error(started);
                }
            }
            if app.busy(started) {
                1. / 30.
            } else {
                1. / 10.
            }
        };
        thread::sleep(Duration::from_secs_f64(
            (period - (buttplug::monotonic() - started)).max(0.),
        ));
    }
    app.lock().unwrap_or_else(|p| p.into_inner()).shutdown();
    server.stop();
    log("Stopped");
    Ok(())
}
fn main() {
    let args: Vec<String> = env::args().collect();
    if args.get(1).map(String::as_str) == Some("--steam") {
        std::process::exit(night_city_haptics::steam::main_cli(&args[2..]));
    }
    if let Err(error) = run(&args[1..]) {
        eprintln!("Night City Haptics: {error}");
        std::process::exit(1);
    }
}
