//! Companion state, settings transactions, game gating, and owned engine lifecycle.
use crate::{buttplug, haptics};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const PORT: u16 = 19870;
pub const ENGINE_RELEASE: &str = "5.0.4";
const MAX_JSON: u64 = 1024 * 1024;
const MAX_DOWNLOAD: u64 = 64 * 1024 * 1024;
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

pub fn data_dir() -> PathBuf {
    let home = env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    if cfg!(windows) {
        env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData/Local"))
            .join("NightCityHaptics")
    } else {
        env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/share"))
            .join("night-city-haptics")
    }
}

pub fn read_json(path: &Path) -> Option<Value> {
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(MAX_JSON + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_JSON {
        return None;
    }
    serde_json::from_slice(&bytes).ok()
}

fn temporary_name(parent: &Path, name: &str) -> PathBuf {
    parent.join(format!(
        ".{name}.{}.{}",
        std::process::id(),
        TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ))
}

/// Create alongside the destination and rename only after a complete write.
pub fn write_json(path: &Path, value: &Value) -> io::Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let (temporary, mut output) = loop {
        let temporary = temporary_name(parent, &name);
        match OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
        {
            Ok(file) => break (temporary, file),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    };
    let result = (|| {
        serde_json::to_writer_pretty(&mut output, value)?;
        output.flush()?;
        drop(output);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return path
            .metadata()
            .map(|m| m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn engine_download(platform: &str) -> Option<(&'static str, &'static str)> {
    match platform {
        "linux-x86_64" => Some((
            "intiface-engine-v5.0.4-linux-x64.zip",
            "ea2b40eca8369e01edcaa00076315bde0d7352108d38c226b77f1818395d2e66",
        )),
        "linux-aarch64" => Some((
            "intiface-engine-v5.0.4-linux-arm64.zip",
            "01536003a3075ddb896552b70d2024f19992099b9ea1aef93459f9084aacb7e2",
        )),
        "windows-amd64" => Some((
            "intiface-engine-v5.0.4-win-x64.zip",
            "29ea7051b23f7a05124e65581df48c8600070c83adc0348f0738b9ba75b58782",
        )),
        "darwin-arm64" => Some((
            "intiface-engine-v5.0.4-macos-arm64.zip",
            "430fbea694cbbde60e567c2f8e01c297db040259f02e541a5bf3d2abdbdf3d0d",
        )),
        _ => None,
    }
}

fn platform_key() -> String {
    if cfg!(windows) {
        "windows-amd64".into()
    } else if cfg!(target_os = "macos") {
        format!(
            "darwin-{}",
            if cfg!(target_arch = "aarch64") {
                "arm64"
            } else {
                env::consts::ARCH
            }
        )
    } else {
        format!("linux-{}", env::consts::ARCH)
    }
}

pub struct Engine {
    pub process: Option<Child>,
    log: Option<File>,
    assets: PathBuf,
    download_busy: Arc<AtomicBool>,
    download_state: Arc<Mutex<String>>,
}

impl Engine {
    pub fn new(assets: PathBuf) -> Self {
        Self {
            process: None,
            log: None,
            assets,
            download_busy: Arc::new(AtomicBool::new(false)),
            download_state: Arc::new(Mutex::new(String::new())),
        }
    }
    pub fn download_state(&self) -> String {
        self.download_state
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }
    pub fn downloading(&self) -> bool {
        self.download_busy.load(Ordering::Acquire)
    }
    pub fn executable(&self) -> Option<PathBuf> {
        let name = if cfg!(windows) {
            "intiface-engine.exe"
        } else {
            "intiface-engine"
        };
        let mut candidates = Vec::new();
        if let Some(path) = env::var_os("NCH_INTIFACE_ENGINE") {
            if !path.is_empty() {
                candidates.push(PathBuf::from(path));
            }
        }
        candidates.push(
            data_dir()
                .join(format!("intiface-engine-{ENGINE_RELEASE}"))
                .join(name),
        );
        candidates.push(self.assets.join(name));
        if let Some(paths) = env::var_os("PATH") {
            candidates.extend(env::split_paths(&paths).map(|p| p.join(name)));
        }
        candidates.into_iter().find(|p| executable_file(p))
    }
    pub fn running(&mut self) -> bool {
        self.process
            .as_mut()
            .map(|p| matches!(p.try_wait(), Ok(None)))
            .unwrap_or(false)
    }
    pub fn start(&mut self, port: u16) -> Result<bool, String> {
        let Some(path) = self.executable() else {
            return Ok(false);
        };
        if self.running() || self.downloading() {
            return Ok(false);
        }
        fs::create_dir_all(data_dir()).map_err(|e| e.to_string())?;
        self.log = None;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(data_dir().join("intiface-engine.log"))
            .map_err(|e| e.to_string())?;
        let mut command = Command::new(&path);
        command
            .args([
                "--websocket-port",
                &port.to_string(),
                "--server-name",
                "Night City Haptics",
                "--max-ping-time",
                "3000",
                "--use-bluetooth-le",
                "--use-serial",
                "--use-lovense-dongle-hid",
                "--use-lovense-dongle-serial",
                "--log",
                "info",
            ])
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log.try_clone().map_err(|e| e.to_string())?);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        match command.spawn() {
            Ok(process) => {
                self.process = Some(process);
                self.log = Some(log);
                Ok(true)
            }
            Err(error) => {
                self.process = None;
                Err(error.to_string())
            }
        }
    }
    pub fn stop(&mut self) {
        if let Some(mut process) = self.process.take() {
            if matches!(process.try_wait(), Ok(None)) {
                #[cfg(unix)]
                unsafe {
                    libc::kill(process.id() as i32, libc::SIGTERM);
                }
                #[cfg(not(unix))]
                {
                    let _ = process.kill();
                }
                let deadline = Instant::now() + Duration::from_secs(3);
                while matches!(process.try_wait(), Ok(None)) && Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(30));
                }
                if matches!(process.try_wait(), Ok(None)) {
                    let _ = process.kill();
                }
                let _ = process.wait();
            }
        }
        self.log = None;
    }
    pub fn request_download(&mut self) -> Result<bool, String> {
        if self
            .download_busy
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Ok(false);
        }
        *self
            .download_state
            .lock()
            .unwrap_or_else(|p| p.into_inner()) = "Downloading Intiface Engine…".into();
        let running = self.running();
        let busy = Arc::clone(&self.download_busy);
        let state = Arc::clone(&self.download_state);
        let result = thread::Builder::new()
            .name("engine-download".into())
            .spawn(move || {
                let result = if running {
                    Err("Stop the running Intiface Engine before replacing it".into())
                } else {
                    download_engine(&platform_key())
                };
                *state.lock().unwrap_or_else(|p| p.into_inner()) = match result {
                    Ok(()) => String::new(),
                    Err(error) => error,
                };
                busy.store(false, Ordering::Release);
            });
        if let Err(error) = result {
            self.download_busy.store(false, Ordering::Release);
            *self
                .download_state
                .lock()
                .unwrap_or_else(|p| p.into_inner()) = "Download could not start".into();
            return Err(error.to_string());
        }
        Ok(true)
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.stop();
    }
}

struct TempDir(PathBuf);
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn temporary_dir(parent: &Path, prefix: &str) -> io::Result<TempDir> {
    loop {
        let path = temporary_name(parent, prefix);
        match fs::create_dir(&path) {
            Ok(()) => return Ok(TempDir(path)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
}

fn find_engine(root: &Path) -> io::Result<Option<PathBuf>> {
    let mut entries = fs::read_dir(root)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(|e| e.path());
    for entry in entries {
        let path = entry.path();
        if entry.file_type()?.is_dir() {
            if let Some(binary) = find_engine(&path)? {
                return Ok(Some(binary));
            }
        } else if entry.file_type()?.is_file() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with("intiface-engine")
                && path.extension().map(|e| e == "exe").unwrap_or(true)
            {
                return Ok(Some(path));
            }
        }
    }
    Ok(None)
}

/// Validate the archive before any entry is extracted. Trusted digest is checked by caller.
fn unpack_engine(archive: &Path, unpacked: &Path) -> Result<PathBuf, String> {
    let file = File::open(archive).map_err(|e| e.to_string())?;
    let mut bundle = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut total = 0u64;
    if bundle.len() > 4096 {
        return Err("unexpected file in the download".into());
    }
    for index in 0..bundle.len() {
        let member = bundle.by_index(index).map_err(|e| e.to_string())?;
        if member.enclosed_name().is_none()
            || member.name().contains('\\')
            || member.name().split('/').any(|component| component == "..")
            || member
                .name()
                .split('/')
                .next()
                .map(|component| component.contains(':'))
                .unwrap_or(false)
            || member
                .unix_mode()
                .map(|m| m & 0o170000 == 0o120000)
                .unwrap_or(false)
        {
            return Err("unexpected file in the download".into());
        }
        total = total
            .checked_add(member.size())
            .ok_or("engine download is too large")?;
        if total > 256 * 1024 * 1024 {
            return Err("engine download is too large".into());
        }
    }
    fs::create_dir(unpacked).map_err(|e| e.to_string())?;
    for index in 0..bundle.len() {
        let mut member = bundle.by_index(index).map_err(|e| e.to_string())?;
        let target = unpacked.join(
            member
                .enclosed_name()
                .ok_or("unexpected file in the download")?,
        );
        if member.is_dir() {
            fs::create_dir_all(&target).map_err(|e| e.to_string())?;
        } else {
            fs::create_dir_all(target.parent().ok_or("unexpected file in the download")?)
                .map_err(|e| e.to_string())?;
            let mut output = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
                .map_err(|e| e.to_string())?;
            io::copy(&mut member, &mut output).map_err(|e| e.to_string())?;
            #[cfg(unix)]
            if let Some(mode) = member.unix_mode() {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&target, fs::Permissions::from_mode(mode & 0o777))
                    .map_err(|e| e.to_string())?;
            }
        }
    }
    let binary = find_engine(unpacked)
        .map_err(|e| e.to_string())?
        .ok_or("the download did not contain intiface-engine")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&binary)
            .map_err(|e| e.to_string())?
            .permissions()
            .mode();
        fs::set_permissions(&binary, fs::Permissions::from_mode(mode | 0o100))
            .map_err(|e| e.to_string())?;
    }
    Ok(binary)
}

fn replace_engine(binary: &Path, target: &Path, previous: &Path) -> Result<(), String> {
    let existed = target.exists();
    if existed {
        fs::rename(target, previous).map_err(|e| e.to_string())?;
    }
    if let Err(error) = fs::rename(binary.parent().ok_or("missing engine directory")?, target) {
        if existed {
            if let Err(restore) = fs::rename(previous, target) {
                return Err(format!(
                    "{error}; restoring previous engine failed: {restore}"
                ));
            }
        }
        return Err(error.to_string());
    }
    Ok(())
}

fn download_engine(platform: &str) -> Result<(), String> {
    let Some((name, digest)) = engine_download(platform) else {
        return Err(format!(
            "No Intiface Engine build for {platform}; install Intiface Central instead"
        ));
    };
    let result = (|| -> Result<(), String> {
        let parent = data_dir();
        fs::create_dir_all(&parent).map_err(|e| e.to_string())?;
        let temporary = temporary_dir(&parent, "engine-download").map_err(|e| e.to_string())?;
        let archive = temporary.0.join(name);
        let address = format!("https://github.com/buttplugio/buttplug/releases/download/intiface-engine-{ENGINE_RELEASE}/{name}");
        let agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(60)))
            .build()
            .new_agent();
        let mut response = agent.get(&address).call().map_err(|e| e.to_string())?;
        let mut reader = response.body_mut().as_reader().take(MAX_DOWNLOAD + 1);
        let mut output = File::create(&archive).map_err(|e| e.to_string())?;
        let mut hash = Sha256::new();
        let mut size = 0u64;
        let mut buffer = [0u8; 65536];
        loop {
            let count = reader.read(&mut buffer).map_err(|e| e.to_string())?;
            if count == 0 {
                break;
            }
            size += count as u64;
            if size > MAX_DOWNLOAD {
                return Err("engine download is too large".into());
            }
            output
                .write_all(&buffer[..count])
                .map_err(|e| e.to_string())?;
            hash.update(&buffer[..count]);
        }
        drop(output);
        if format!("{:x}", hash.finalize()) != digest {
            return Err("download failed its checksum".into());
        }
        let binary = unpack_engine(&archive, &temporary.0.join("engine"))?;
        replace_engine(
            &binary,
            &parent.join(format!("intiface-engine-{ENGINE_RELEASE}")),
            &temporary.0.join("previous"),
        )
    })();
    result.map_err(|e| format!("Download failed: {e}"))
}

pub struct App {
    pub mod_dir: PathBuf,
    pub assets: PathBuf,
    pub settings_path: PathBuf,
    pub catalog: Value,
    pub previews: Value,
    pub settings: Value,
    pub mixer: haptics::Mixer,
    pub game: haptics::GameLink,
    pub outputs: buttplug::Outputs,
    pub engine: Engine,
    pub client: Option<buttplug::Client>,
    pub device_errors: Vec<String>,
    pub link: String,
    pub link_detail: String,
    pub next_connect: f64,
    pub scan_until: f64,
    pub scan_count: usize,
    pub next_status: f64,
    pub started_engine_at: Option<f64>,
    pub history: Vec<(f64, f64)>,
    pub status_written: (Option<Value>, f64),
    pub status_heartbeat: u64,
    pub scene_key: Option<(Option<String>, Value, String)>,
    pub stopping: bool,
    pub output: f64,
}

impl App {
    pub fn new(
        mod_dir: PathBuf,
        assets: PathBuf,
        settings_path: Option<PathBuf>,
    ) -> Result<Self, String> {
        let catalog = read_json(&assets.join("catalog.json"))
            .ok_or("Could not load companion/catalog.json")?;
        if !catalog["feels"].is_object() || !catalog["interactions"].is_array() {
            return Err("Invalid haptics catalog".into());
        }
        let mut previews = Map::new();
        for (kind, feels) in catalog["feels"].as_object().unwrap() {
            let mut shapes = Map::new();
            for feel in feels.as_array().ok_or("Invalid haptics catalog feels")? {
                let id = feel["id"].as_str().ok_or("Invalid haptics catalog feel")?;
                shapes.insert(id.into(), json!(haptics::preview(&catalog, kind, id, 48)));
            }
            previews.insert(kind.clone(), Value::Object(shapes));
        }
        let settings_path = settings_path.unwrap_or_else(|| assets.join("settings.json"));
        let raw = read_json(&settings_path).unwrap_or(Value::Null);
        let settings = haptics::normalize_settings(&raw, &catalog);
        let mixer = haptics::Mixer::new(&catalog);
        Ok(Self {
            mod_dir,
            settings_path,
            catalog,
            previews: Value::Object(previews),
            settings,
            mixer,
            game: haptics::GameLink::default(),
            outputs: buttplug::Outputs::new(),
            engine: Engine::new(assets.clone()),
            assets,
            client: None,
            device_errors: Vec::new(),
            link: "connecting".into(),
            link_detail: String::new(),
            next_connect: 0.,
            scan_until: 0.,
            scan_count: 0,
            next_status: 0.,
            started_engine_at: None,
            history: Vec::new(),
            status_written: (None, 0.),
            status_heartbeat: 0,
            scene_key: None,
            stopping: false,
            output: 0.,
        })
    }

    pub fn save(&mut self, changes: &Value) -> Result<(), String> {
        let changes = changes.as_object().ok_or("Settings must be an object")?;
        let stop_requested = changes.get("enabled") == Some(&Value::Bool(false));
        validate_options(
            changes,
            &[
                "enabled",
                "start_intiface",
                "max_strength",
                "intiface_address",
                "interactions",
                "devices",
            ],
        )?;
        if let Some(address) = changes.get("intiface_address") {
            validate_address(address)?;
        }
        let mut merged = self.settings.clone();
        for (key, value) in changes {
            if key == "interactions" || key == "devices" {
                let entries = value
                    .as_object()
                    .ok_or_else(|| format!("{key} must be an object"))?;
                for (name, options) in entries {
                    let options = options
                        .as_object()
                        .ok_or_else(|| format!("{key} entries must be named objects"))?;
                    validate_options(
                        options,
                        if key == "interactions" {
                            &["on", "strength", "feel"]
                        } else {
                            &["on", "strength", "all_motors", "outputs"]
                        },
                    )?;
                    if key == "interactions" {
                        let item = self.catalog["interactions"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .find(|item| item["id"] == *name)
                            .ok_or_else(|| format!("Unknown interaction: {name}"))?;
                        if let Some(feel) = options.get("feel") {
                            if !feel
                                .as_str()
                                .map(|id| haptics::feel_ids(&self.catalog, item).contains(id))
                                .unwrap_or(false)
                            {
                                return Err(format!("Invalid feel for {name}"));
                            }
                        }
                    }
                    let target = merged[key]
                        .as_object_mut()
                        .ok_or("Invalid normalized settings")?
                        .entry(name.clone())
                        .or_insert_with(|| json!({}));
                    for (option, value) in options {
                        if option == "outputs" {
                            if !target[option].is_object() {
                                target[option] = json!({});
                            }
                            for (channel, on) in value.as_object().unwrap() {
                                target[option][channel] = on.clone();
                            }
                        } else {
                            target[option] = value.clone();
                        }
                    }
                }
            } else {
                merged[key] = value.clone();
            }
        }
        if merged["devices"]
            .as_object()
            .is_some_and(|devices| devices.len() > 64)
        {
            return Err("At most 64 device settings can be saved".into());
        }
        let settings = haptics::normalize_settings(&merged, &self.catalog);
        validate_settings_size(&settings)?;
        let address_changed = settings["intiface_address"] != self.settings["intiface_address"];
        if let Err(error) = write_json(&self.settings_path, &settings) {
            // An explicit Off remains effective even if its persistence failed.
            // Do not publish the other settings from this failed transaction.
            if stop_requested {
                self.settings["enabled"] = json!(false);
                self.stop_feedback();
            }
            return Err(error.to_string());
        }
        self.settings = settings;
        if address_changed {
            self.disconnect("Address changed");
            self.engine.stop();
            self.started_engine_at = None;
            self.next_connect = 0.;
        } else if stop_requested {
            self.stop_feedback();
        }
        Ok(())
    }
    fn stop_feedback(&mut self) {
        // Cancel tests already running when Off was requested. A subsequent
        // explicit device Test can still start independently of the master mix.
        self.outputs.reset();
        if let Some(client) = &mut self.client {
            if let Err(error) = client.stop_all() {
                self.disconnect(&format!("Connection lost while stopping: {error}"));
                self.next_connect = buttplug::monotonic() + 1.;
            }
        }
    }
    pub fn reset(&mut self) -> Result<(), String> {
        let mut settings = self.settings.clone();
        settings["interactions"] = haptics::default_settings(&self.catalog)["interactions"].clone();
        validate_settings_size(&settings)?;
        write_json(&self.settings_path, &settings).map_err(|e| e.to_string())?;
        self.settings = settings;
        Ok(())
    }
    pub fn port(&self) -> u16 {
        self.settings["intiface_address"]
            .as_str()
            .and_then(|s| url::Url::parse(s).ok())
            .map(|address| address.port().unwrap_or(80))
            .unwrap_or(12345)
    }
    pub fn local_engine_address(&self) -> bool {
        self.settings["intiface_address"]
            .as_str()
            .and_then(|s| url::Url::parse(s).ok())
            .map(|address| {
                address.scheme() == "ws"
                    && matches!(address.host_str(), Some("127.0.0.1" | "localhost"))
                    && address.username().is_empty()
                    && address.password().is_none()
            })
            .unwrap_or(false)
    }
    pub fn disconnect(&mut self, reason: &str) {
        if let Some(mut client) = self.client.take() {
            self.device_errors.extend(client.errors.iter().cloned());
            if self.device_errors.len() > 5 {
                self.device_errors.drain(..self.device_errors.len() - 5);
            }
            let _ = client.stop_all();
            client.close();
        }
        self.outputs.reset();
        self.mixer.clear(&[]);
        self.output = 0.;
        self.scene_key = None;
        self.link = "connecting".into();
        self.link_detail = reason.into();
    }
    pub fn connect(&mut self, now: f64) -> Result<(), String> {
        if now < self.next_connect {
            return Ok(());
        }
        let address = self.settings["intiface_address"]
            .as_str()
            .unwrap_or("ws://127.0.0.1:12345")
            .to_string();
        match buttplug::Client::connect(&address) {
            Ok(client) => {
                self.link = "connected".into();
                self.link_detail = client.server_name.clone();
                let scan = client.devices.is_empty();
                self.client = Some(client);
                if scan {
                    self.scan(now)?;
                }
            }
            Err(error) => {
                self.client = None;
                self.next_connect = now + 2.;
                if self.engine.running() {
                    self.link = "starting".into();
                    self.link_detail = "Starting Intiface Engine…".into();
                    if self
                        .started_engine_at
                        .map(|start| now - start > 20.)
                        .unwrap_or(false)
                    {
                        self.link = "error".into();
                        self.link_detail =
                            "Intiface Engine did not start; see intiface-engine.log".into();
                    }
                } else if self.settings["start_intiface"].as_bool().unwrap_or(false)
                    && self.local_engine_address()
                    && self.engine.executable().is_some()
                    && !self.engine.downloading()
                {
                    if self.engine.start(self.port())? {
                        self.started_engine_at = Some(now);
                        self.link = "starting".into();
                        self.link_detail = "Starting Intiface Engine…".into();
                        self.next_connect = now + 0.7;
                    }
                } else if self.engine.download_state().starts_with("Downloading") {
                    self.link = "starting".into();
                    self.link_detail = self.engine.download_state();
                } else if self.settings["start_intiface"].as_bool().unwrap_or(false)
                    && self.local_engine_address()
                    && self.engine.executable().is_none()
                {
                    self.link = "missing".into();
                    self.link_detail = self.engine.download_state();
                    if self.link_detail.is_empty() {
                        self.link_detail = "Intiface Engine is not installed yet".into();
                    }
                } else {
                    self.link = "error".into();
                    self.link_detail = format!("No Intiface server at {address} ({error})");
                }
            }
        }
        Ok(())
    }
    pub fn scan(&mut self, now: f64) -> Result<(), String> {
        if let Some(client) = &mut self.client {
            client.start_scanning()?;
            self.scan_until = now + 30.;
            self.scan_count = client.devices.len();
        }
        Ok(())
    }
    pub fn tick(&mut self, mut now: f64) -> Result<(), String> {
        if self.client.is_none() && now >= self.next_connect {
            let began = buttplug::monotonic();
            self.connect(now)?;
            now += buttplug::monotonic() - began;
        }
        // Advance the gate clock after network work. A slow handshake or poll
        // must never make an old game heartbeat appear live when output is sent.
        if let Some(client) = &mut self.client {
            let began = buttplug::monotonic();
            let result = client.poll().and_then(|_| {
                if client.scanning
                    && (now > self.scan_until || client.devices.len() > self.scan_count)
                {
                    client.stop_scanning()
                } else {
                    Ok(())
                }
            });
            now += buttplug::monotonic() - began;
            if let Err(error) = result {
                self.disconnect(&format!("Connection lost: {error}"));
                self.next_connect = now + 1.;
            }
        }
        let gate_clock = buttplug::monotonic();
        let raw = read_json(&self.mod_dir.join("state.json")).unwrap_or(Value::Null);
        let events = self.game.update(&raw, now);
        let active = self.game.active(now);
        let live = self.game.live(now) && self.game.state["paused"] != true;
        for (name, magnitude, seconds) in events {
            let root = name.split(':').next().unwrap_or(&name);
            if name == "test" && live {
                self.mixer.trigger("test", 1., now, "double", None);
            } else if active {
                if let Some(feel) = self.settings["interactions"][root]["feel"].as_str() {
                    self.mixer.game_event(&name, magnitude, now, feel, seconds);
                }
            }
        }
        if !active {
            self.mixer.clear(&["try:", "test"]);
        } else {
            self.sync_scene(now);
        }
        self.output = self
            .mixer
            .tick(now, &self.game.levels(now), &self.settings)
            .0;
        self.history.push((now, rounded(self.output, 3)));
        let excess = self.history.len().saturating_sub(300);
        self.history.drain(..excess);
        self.history.retain(|(stamp, _)| now - stamp <= 5.);
        if let Some(client) = &mut self.client {
            let quiet =
                self.settings["enabled"] != true || (!active && self.mixer.voices.is_empty());
            // Anchor the remaining CET freshness to the real clock. Standalone
            // page Try/device Test works without CET; mixing Try with game
            // output must never remove the game's write deadline.
            let deadline = (active || self.mixer.voices.contains_key("test"))
                .then(|| gate_clock + self.game.changed.unwrap_or(now) + self.game.timeout - now);
            let began = buttplug::monotonic();
            let result = self.outputs.update_before(
                client,
                self.output,
                &self.settings["devices"],
                now,
                quiet,
                deadline,
            );
            now += buttplug::monotonic() - began;
            if let Err(error) = result {
                self.disconnect(&format!("Connection lost: {error}"));
                self.next_connect = now + 1.;
            }
        }
        if now >= self.next_status {
            self.next_status = now + 0.2;
            self.write_status(now);
        }
        Ok(())
    }
    pub fn processing_error(&mut self, now: f64) {
        self.disconnect("Unexpected processing error; devices stopped");
        self.game = haptics::GameLink::default();
        self.next_connect = now + 1.;
        self.write_status(now);
    }
    pub fn busy(&self, now: f64) -> bool {
        self.game.live(now) || !self.mixer.voices.is_empty() || self.output > 0.
    }
    pub fn sync_scene(&mut self, now: f64) {
        let Some(scene) = self.game.state.get("scene") else {
            return;
        };
        if scene.is_null() {
            if self.scene_key.is_some() {
                self.mixer.release("romance", now);
            }
            self.scene_key = None;
            return;
        }
        let name = scene["name"].as_str().unwrap_or("");
        let key = (
            self.game.session.clone(),
            scene["instance"].clone(),
            name.to_owned(),
        );
        let rule_feel = self.settings["interactions"]["romance"]["feel"]
            .as_str()
            .unwrap_or("");
        let track = format!("track:{name}");
        let feel = if rule_feel == "scene" {
            track.clone()
        } else {
            rule_feel.into()
        };
        let Some(effect) = self.mixer.effects.get(&feel) else {
            self.scene_key = Some(key);
            return;
        };
        if !self.mixer.effects.contains_key(&track) {
            self.scene_key = Some(key);
            return;
        }
        let changed = self.game.changed.unwrap_or(now);
        let start = changed - scene["elapsed"].as_f64().unwrap_or(0.);
        let end = effect["points"]
            .as_array()
            .and_then(|p| p.last())
            .and_then(|p| p[0].as_f64())
            .unwrap_or(0.);
        if now - start > end {
            self.mixer.voices.remove("romance");
            self.scene_key = Some(key);
            return;
        }
        let changed_key = self.scene_key.as_ref() != Some(&key);
        let changed_voice = self
            .mixer
            .voices
            .get("romance")
            .map(|v| v.feel != feel)
            .unwrap_or(true);
        if changed_key || changed_voice {
            if changed_key
                && self
                    .mixer
                    .voices
                    .get("romance#pulse")
                    .map(|p| p.start < changed)
                    .unwrap_or(false)
            {
                self.mixer.voices.remove("romance#pulse");
            }
            self.mixer.voices.insert(
                "romance".into(),
                haptics::Voice {
                    start,
                    magnitude: 1.,
                    feel,
                    scale: 1.,
                    release: None,
                },
            );
        } else if let Some(voice) = self.mixer.voices.get_mut("romance") {
            if voice.release.is_none() {
                voice.start = start;
            }
        }
        self.scene_key = Some(key);
    }
    pub fn write_status(&mut self, now: f64) {
        let devices: Vec<Value> = self
            .client
            .as_ref()
            .map(|c| {
                c.devices
                    .values()
                    .map(|d| buttplug::summary(d)["name"].clone())
                    .collect()
            })
            .unwrap_or_default();
        let contributions: Map<String, Value> = self
            .mixer
            .contributions
            .iter()
            .map(|(name, value)| (name.clone(), json!(rounded(*value, 2))))
            .collect();
        let status = json!({"v":2,"running":!self.stopping,"link":self.link,"devices":devices,"output":rounded(self.output,2),
            "enabled":self.settings["enabled"],"active":self.game.active(now),"contributions":contributions});
        if self.status_written.0.as_ref() == Some(&status) && now - self.status_written.1 < 1. {
            return;
        }
        let mut payload = status.clone();
        payload["heartbeat"] = json!(self.status_heartbeat + 1);
        if write_json(&self.mod_dir.join("status.json"), &payload).is_ok() {
            self.status_heartbeat += 1;
            self.status_written = (Some(status), now);
        }
    }
    pub fn output_history(&self, now: f64) -> Vec<f64> {
        let mut values = vec![0_f64; 150];
        for (stamp, value) in &self.history {
            let age = now - stamp;
            if (0. ..5.).contains(&age) {
                let index = (((stamp - (now - 5.)) * 30.) as usize).min(149);
                values[index] = values[index].max(*value);
            }
        }
        values
    }
    pub fn snapshot(&self, now: f64) -> Value {
        let (game, game_detail) = self.game.describe(now);
        let mut devices = Vec::new();
        let mut errors = self.device_errors.clone();
        if let Some(client) = &self.client {
            errors.extend(client.errors.iter().cloned());
            for (index, device) in &client.devices {
                devices.push(device_snapshot(
                    device,
                    &self.settings["devices"],
                    client.battery.get(index).copied(),
                ));
            }
        }
        if errors.len() > 5 {
            errors.drain(..errors.len() - 5);
        }
        json!({"version":VERSION,"settings":self.settings,"output":self.output,"contributions":self.mixer.contributions,
            "history":self.output_history(now),"link":self.link,"link_detail":self.link_detail,
            "scanning":self.client.as_ref().map(|c|c.scanning).unwrap_or(false),"devices":devices,"device_errors":errors,
            "game":game,"game_detail":game_detail,"engine_path":self.engine.executable().map(|p|p.to_string_lossy().into_owned()).unwrap_or_default(),
            "engine_download":self.engine.download_state()})
    }
    pub fn try_interaction(&mut self, name: &str, now: f64) -> Result<(), String> {
        let rule = self.settings["interactions"]
            .get(name)
            .ok_or("Unknown interaction")?;
        let kind = self.mixer.kinds.get(name).ok_or("Unknown interaction")?;
        let mut feel = rule["feel"].as_str().unwrap_or("").to_string();
        if kind == "event" {
            let mut seconds = if feel == "slowbuild" { Some(4.) } else { None };
            if feel == "scene" {
                feel = format!("track:{}", if name == "romance" { "stout" } else { name });
                seconds = if name == "romance" { Some(8.) } else { None };
            }
            self.mixer
                .trigger(&format!("try:{name}"), 1., now, &feel, seconds);
        } else {
            let effect = format!("_level:{name}");
            self.mixer.effects.insert(
                effect.clone(),
                json!({"points":[[0,1],[2.5,1],[2.6,0]],"texture":feel}),
            );
            self.mixer
                .trigger(&format!("try:{name}"), 1., now, &effect, None);
        }
        Ok(())
    }
    pub fn test_device(&mut self, key: &str, now: f64) -> Result<(), String> {
        let client = self.client.as_ref().ok_or("Intiface is not connected")?;
        for (index, device) in &client.devices {
            if buttplug::summary(device)["key"] == key {
                let strength = buttplug::device_options(device, &self.settings["devices"])
                    ["strength"]
                    .as_f64()
                    .unwrap_or(1.);
                self.outputs
                    .hold
                    .insert(*index, (rounded(0.5 * strength, 3), now + 0.8));
                return Ok(());
            }
        }
        Err("Device not found".into())
    }
    pub fn shutdown(&mut self) {
        self.stopping = true;
        self.disconnect("Stopped");
        self.engine.stop();
        let _ = write_json(
            &self.mod_dir.join("status.json"),
            &json!({"v":2,"running":false,"link":"stopped","devices":[],
            "output":0,"enabled":false,"active":false,"contributions":{},"heartbeat":self.status_heartbeat+1}),
        );
    }
}

fn validate_settings_size(settings: &Value) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(settings).map_err(|error| error.to_string())?;
    if bytes.len() as u64 > MAX_JSON {
        return Err("Settings exceed the 1 MiB persistence limit".into());
    }
    Ok(())
}

fn rounded(number: f64, digits: usize) -> f64 {
    // Decimal formatting rounds the original binary float. Multiplication by
    // 10^digits can erase the small error in the original binary floating-point value.
    format!("{number:.digits$}").parse().unwrap_or(0.)
}

fn device_snapshot(device: &Value, settings: &Value, battery: Option<f64>) -> Value {
    let mut info = buttplug::summary(device);
    let options = buttplug::device_options(device, settings);
    let enabled = buttplug::enabled_channels(device, &options);
    let outputs: Map<String, Value> = info["channels"]
        .as_object()
        .into_iter()
        .flat_map(|items| items.keys())
        .map(|name| (name.clone(), json!(enabled.contains(name.as_str()))))
        .collect();
    info["settings"] = json!({"on": options["on"] != false, "strength": options["strength"].as_f64().unwrap_or(1.), "outputs":outputs});
    info["battery"] = battery.map(|level| json!(level)).unwrap_or(Value::Null);
    info
}

fn validate_options(options: &Map<String, Value>, allowed: &[&str]) -> Result<(), String> {
    for (key, value) in options {
        if !allowed.contains(&key.as_str()) {
            return Err(format!("Unknown setting: {key}"));
        }
        if matches!(
            key.as_str(),
            "enabled" | "start_intiface" | "on" | "all_motors"
        ) && !value.is_boolean()
        {
            return Err(format!("{key} must be true or false"));
        }
        if key == "outputs"
            && !value
                .as_object()
                .map(|items| {
                    items.iter().all(|(name, on)| {
                        matches!(
                            name.as_str(),
                            "vibrate"
                                | "rotate"
                                | "oscillate"
                                | "stroke"
                                | "constrict"
                                | "heat"
                                | "light"
                        ) && on.is_boolean()
                    })
                })
                .unwrap_or(false)
        {
            return Err("outputs must map device outputs to true or false".into());
        }
        if matches!(key.as_str(), "max_strength" | "strength")
            && !value
                .as_f64()
                .map(|v| v.is_finite() && (0. ..=1.).contains(&v))
                .unwrap_or(false)
        {
            return Err(format!("{key} must be a finite number between 0 and 1"));
        }
    }
    Ok(())
}

fn validate_address(value: &Value) -> Result<(), String> {
    let address = value.as_str().ok_or("Invalid Intiface address")?;
    if !address.starts_with("ws://")
        || address.chars().count() >= 200
        || address.chars().any(|c| (c as u32) < 33 || c == '\u{7f}')
    {
        return Err("Invalid Intiface address".into());
    }
    let parsed = url::Url::parse(address).map_err(|_| "Invalid Intiface address")?;
    // Inspect the original authority too: URL normalization removes explicit :80
    // and percent-encodes paths, whereas the API requires printable ASCII paths.
    let rest = &address[5..];
    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..authority_end];
    let suffix = &rest[authority_end..];
    if parsed.scheme() != "ws"
        || parsed.host_str().is_none()
        || authority.contains('@')
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().map(|s| !s.is_empty()).unwrap_or(false)
        || parsed.port() == Some(0)
    {
        return Err("Invalid Intiface address".into());
    }
    if suffix.chars().any(|c| (c as u32) > 126) {
        return Err("Invalid Intiface path".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct SimulatedWire {
        sent: Arc<Mutex<Vec<Value>>>,
        replies: VecDeque<String>,
        fail_writes: Arc<AtomicBool>,
    }
    impl buttplug::Wire for SimulatedWire {
        fn send(&mut self, text: &str) -> Result<(), String> {
            if self.fail_writes.load(Ordering::Acquire) {
                return Err("Simulated write failure".into());
            }
            let batch: Value = serde_json::from_str(text).unwrap();
            for command in batch.as_array().unwrap() {
                self.sent.lock().unwrap().push(command.clone());
                let (kind, data) = command.as_object().unwrap().iter().next().unwrap();
                let reply = match kind.as_str() {
                    "RequestServerInfo" => {
                        json!({"ServerInfo":{"Id":data["Id"], "ServerName":"Offline test", "MessageVersion":3, "MaxPingTime":0}})
                    }
                    "RequestDeviceList" => {
                        json!({"DeviceList":{"Id":data["Id"], "Devices":[{"DeviceIndex":0,"DeviceName":"Offline test device","DeviceMessages":{"ScalarCmd":[{"ActuatorType":"Vibrate","StepCount":20}],"StopDeviceCmd":{}}}]}})
                    }
                    _ => json!({"Ok":{"Id":data["Id"]}}),
                };
                self.replies.push_back(json!([reply]).to_string());
            }
            Ok(())
        }
        fn recv(&mut self, _: f64) -> Result<Option<String>, String> {
            Ok(self.replies.pop_front())
        }
        fn close(&mut self) {}
    }
    fn simulated_client(app: &mut App) -> (Arc<Mutex<Vec<Value>>>, Arc<AtomicBool>) {
        let sent = Arc::new(Mutex::new(Vec::new()));
        let fail_writes = Arc::new(AtomicBool::new(false));
        app.client = Some(
            buttplug::Client::connect_wire(
                Box::new(SimulatedWire {
                    sent: Arc::clone(&sent),
                    replies: VecDeque::new(),
                    fail_writes: Arc::clone(&fail_writes),
                }),
                "Offline test",
            )
            .unwrap(),
        );
        sent.lock().unwrap().clear();
        (sent, fail_writes)
    }
    struct DelayedWire {
        inner: SimulatedWire,
        delayed: Arc<AtomicBool>,
        closed: Arc<AtomicBool>,
    }
    impl buttplug::Wire for DelayedWire {
        fn send(&mut self, text: &str) -> Result<(), String> {
            let batch: Value = serde_json::from_str(text).unwrap();
            let positive = batch.as_array().unwrap().iter().any(|command| {
                command["ScalarCmd"]["Scalars"]
                    .as_array()
                    .is_some_and(|scalars| {
                        scalars
                            .iter()
                            .any(|scalar| scalar["Scalar"].as_f64().unwrap_or(0.) > 0.)
                    })
            });
            if positive && !self.delayed.swap(true, Ordering::AcqRel) {
                thread::sleep(Duration::from_millis(80));
            }
            self.inner.send(text)
        }
        fn recv(&mut self, timeout: f64) -> Result<Option<String>, String> {
            self.inner.recv(timeout)
        }
        fn close(&mut self) {
            self.closed.store(true, Ordering::Release);
        }
    }
    fn delayed_client(app: &mut App) -> (Arc<Mutex<Vec<Value>>>, Arc<AtomicBool>, Arc<AtomicBool>) {
        let sent = Arc::new(Mutex::new(Vec::new()));
        let delayed = Arc::new(AtomicBool::new(false));
        let closed = Arc::new(AtomicBool::new(false));
        let mut client = buttplug::Client::connect_wire(
            Box::new(DelayedWire {
                inner: SimulatedWire {
                    sent: Arc::clone(&sent),
                    replies: VecDeque::new(),
                    fail_writes: Arc::new(AtomicBool::new(false)),
                },
                delayed: Arc::clone(&delayed),
                closed: Arc::clone(&closed),
            }),
            "Offline delayed fixture",
        )
        .unwrap();
        let mut second = client.devices[&0].clone();
        second["DeviceIndex"] = json!(1);
        client.devices.insert(1, second);
        app.client = Some(client);
        sent.lock().unwrap().clear();
        (sent, delayed, closed)
    }
    fn positive_devices(sent: &Arc<Mutex<Vec<Value>>>) -> Vec<u64> {
        sent.lock()
            .unwrap()
            .iter()
            .filter_map(|command| {
                command["ScalarCmd"]["Scalars"]
                    .as_array()
                    .filter(|scalars| {
                        scalars
                            .iter()
                            .any(|scalar| scalar["Scalar"].as_f64().unwrap_or(0.) > 0.)
                    })
                    .map(|_| command["ScalarCmd"]["DeviceIndex"].as_u64().unwrap())
            })
            .collect()
    }
    #[test]
    fn slow_output_cannot_send_to_later_devices_after_heartbeat_expiry_even_with_try() {
        for preview in [false, true] {
            let (_temp, mut app) = fixture();
            let (sent, delayed, closed) = delayed_client(&mut app);
            app.game.timeout = 0.03;
            for seq in [1, 2] {
                if seq == 2 && preview {
                    app.try_interaction("levelUp", 20.1).unwrap();
                }
                write_json(
                    &app.mod_dir.join("state.json"),
                    &json!({
                        "v":2,"session":"offline-slow-output","seq":seq,"t":20. + seq as f64 / 10.,
                        "active":true,"paused":false,"levels":{"burning":1.},"events":[]
                    }),
                )
                .unwrap();
                app.tick(20. + seq as f64 / 10.).unwrap();
            }
            assert!(delayed.load(Ordering::Acquire));
            assert_eq!(positive_devices(&sent), vec![0], "preview={preview}");
            assert!(closed.load(Ordering::Acquire));
            assert!(app.client.is_none());
            assert_eq!(app.output, 0.);
        }
    }
    #[test]
    fn standalone_try_and_device_tests_do_not_require_a_game_heartbeat_deadline() {
        for device_test in [false, true] {
            let (_temp, mut app) = fixture();
            let (sent, delayed, closed) = delayed_client(&mut app);
            app.game.timeout = 0.03;
            if device_test {
                for key in ["device:0", "device:1"] {
                    app.test_device(key, 20.).unwrap();
                }
            } else {
                app.try_interaction("levelUp", 20.).unwrap();
            }
            app.tick(20.1).unwrap();
            assert!(delayed.load(Ordering::Acquire));
            assert_eq!(
                positive_devices(&sent),
                vec![0, 1],
                "device_test={device_test}"
            );
            assert!(!closed.load(Ordering::Acquire));
            assert!(app.client.is_some());
        }
    }
    #[test]
    fn status_and_test_strength_rounding_matches_python_binary_float_cases() {
        assert_eq!(rounded(0.225, 2), 0.23);
        assert_eq!(rounded(2.675, 2), 2.67);
        assert_eq!(rounded(0.5625, 3), 0.562);
    }
    #[test]
    fn device_snapshot_reports_real_channel_switches_and_saved_policy() {
        let device = json!({"DeviceIndex":7,"DeviceName":"Offline fixture","DeviceIdentifier":"fixture-only",
            "DeviceMessages":{"ScalarCmd":[{"ActuatorType":"Vibrate","StepCount":20},{"ActuatorType":"Temperature","StepCount":20},
                {"ActuatorType":"Constrict","StepCount":20},{"ActuatorType":"Led","StepCount":20}],"StopDeviceCmd":{}}});
        let defaults = device_snapshot(&device, &json!({}), None);
        assert_eq!(
            defaults["settings"]["outputs"],
            json!({"vibrate":true,"heat":false,"constrict":false,"light":true})
        );
        assert_eq!(defaults["battery"], Value::Null);
        let key = buttplug::settings_key(&device);
        let settings =
            json!({key:{"on":false,"strength":0.2,"outputs":{"vibrate":false,"heat":true}}});
        let row = device_snapshot(&device, &settings, Some(0.75));
        assert_eq!(
            row["settings"],
            json!({"on":false,"strength":0.2,"outputs":{"vibrate":false,"heat":true,"constrict":false,"light":true}})
        );
        assert_eq!(row["battery"], 0.75);
    }
    #[test]
    #[cfg(unix)]
    fn owned_engine_shutdown_reaps_only_its_child_and_blocks_replacement_while_running() {
        struct Stub(Child);
        impl Drop for Stub {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        fn stub() -> Child {
            Command::new("/bin/sh")
                .args(["-c", "trap 'exit 0' TERM; while :; do sleep 0.01; done"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap()
        }
        let (temp, _app) = fixture();
        let mut unowned = Stub(stub());
        let mut engine = Engine::new(temp.0.clone());
        engine.process = Some(stub());
        assert!(engine.running());
        assert!(engine.request_download().unwrap());
        let deadline = Instant::now() + Duration::from_secs(2);
        while engine.downloading() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(!engine.downloading());
        assert!(engine
            .download_state()
            .contains("Stop the running Intiface Engine"));
        engine.stop();
        assert!(engine.process.is_none());
        assert!(!engine.running());
        assert!(unowned.0.try_wait().unwrap().is_none());
    }
    fn fixture() -> (TempDir, App) {
        let temporary = temporary_dir(&env::temp_dir(), "nch-app-test").unwrap();
        let assets = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src/companion");
        let path = temporary.0.join("settings.json");
        write_json(&path, &json!({"start_intiface":false})).unwrap();
        let mut app = App::new(temporary.0.clone(), assets, Some(path)).unwrap();
        app.next_connect = f64::INFINITY; // This fixture must never contact an engine or device.
        (temporary, app)
    }
    fn beat(app: &mut App, seq: u64, now: f64, active: bool, paused: bool, scene: Value) {
        write_json(
            &app.mod_dir.join("state.json"),
            &json!({"v":2,"session":"offline-fixture","seq":seq,"t":now,
            "active":active,"paused":paused,"levels":{},"events":[],"scene":scene}),
        )
        .unwrap();
        app.tick(now).unwrap();
    }
    #[test]
    fn invalid_changes_and_failed_writes_keep_live_policy_and_connection_state() {
        let (_temp, mut app) = fixture();
        app.save(
            &json!({"enabled":false,"max_strength":0.05,"interactions":{"hurt":{"on":false}}}),
        )
        .unwrap();
        let before = app.settings.clone();
        let bytes = fs::read(&app.settings_path).unwrap();
        for patch in [
            json!([]),
            json!({"enabled":"false"}),
            json!({"start_intiface":1}),
            json!({"max_strength":true}),
            json!({"max_strength":-1}),
            json!({"max_strength":2}),
            json!({"interactions":{"hurt":{"feel":[]}}}),
            json!({"interactions":{"unknown":{"on":true}}}),
            json!({"devices":{"fixture":{"all_motors":"false"}}}),
            json!({"devices":{"fixture":{"outputs":{"spray":true}}}}),
            json!({"devices":{"fixture":{"outputs":{"stroke":1}}}}),
            json!({"intiface_address":"ws://localhost:99999"}),
            json!({"intiface_address":"ws://localhost:0"}),
            json!({"intiface_address":"ws://user:pass@localhost:12345"}),
            json!({"intiface_address":"ws://localhost:12345\r\n"}),
            json!({"intiface_address":"WS://localhost:23456"}),
            json!({"intiface_address":"ws://localhost:23456/a b"}),
            json!({"intiface_address":"ws://localhost:23456/日本語"}),
        ] {
            assert!(app.save(&patch).is_err(), "accepted {patch}");
            assert_eq!(app.settings, before);
            assert_eq!(fs::read(&app.settings_path).unwrap(), bytes);
        }
        app.settings_path = app.mod_dir.join("missing/settings.json");
        app.started_engine_at = Some(10.);
        app.outputs.hold.insert(1, (0.5, 20.));
        assert!(app
            .save(&json!({"enabled":true,"intiface_address":"ws://localhost:12346"}))
            .is_err());
        assert_eq!(app.settings, before);
        assert_eq!(app.started_engine_at, Some(10.));
        assert_eq!(app.outputs.hold.len(), 1);
        assert!(app.reset().is_err());
        assert_eq!(app.settings, before);
    }
    #[test]
    fn reset_and_device_output_patches_preserve_other_user_policy() {
        let (_temp, mut app) = fixture();
        app.save(&json!({"enabled":false,"max_strength":0.05,"devices":{"fixture":{"on":false,"strength":0.2,"outputs":{"heat":true}}},
            "interactions":{"hurt":{"on":false}}})).unwrap();
        app.save(&json!({"devices":{"fixture":{"outputs":{"vibrate":false}}}}))
            .unwrap();
        assert_eq!(
            app.settings["devices"]["fixture"]["outputs"],
            json!({"heat":true,"vibrate":false})
        );
        let before = app.settings.clone();
        app.reset().unwrap();
        assert_eq!(
            app.settings["interactions"],
            haptics::default_settings(&app.catalog)["interactions"]
        );
        for key in [
            "enabled",
            "max_strength",
            "start_intiface",
            "intiface_address",
            "devices",
        ] {
            assert_eq!(app.settings[key], before[key]);
        }
        assert_eq!(read_json(&app.settings_path).unwrap(), app.settings);
    }
    #[test]
    fn master_off_stops_existing_device_test_and_later_explicit_test_still_works() {
        let (_temp, mut app) = fixture();
        let (sent, _fail) = simulated_client(&mut app);
        let key = buttplug::summary(&app.client.as_ref().unwrap().devices[&0])["key"]
            .as_str()
            .unwrap()
            .to_owned();
        app.test_device(&key, 10.).unwrap();
        app.tick(10.1).unwrap();
        assert!(sent
            .lock()
            .unwrap()
            .iter()
            .any(|command| command.get("ScalarCmd").is_some()));
        sent.lock().unwrap().clear();
        app.save(&json!({"enabled":false})).unwrap();
        assert!(app.outputs.hold.is_empty());
        assert!(sent
            .lock()
            .unwrap()
            .iter()
            .any(|command| command.get("StopAllDevices").is_some()));
        app.tick(10.2).unwrap();
        assert_eq!(app.output, 0.);
        sent.lock().unwrap().clear();
        app.test_device(&key, 10.3).unwrap();
        app.tick(10.4).unwrap();
        assert!(sent
            .lock()
            .unwrap()
            .iter()
            .any(|command| command["ScalarCmd"]["Scalars"][0]["Scalar"] == 0.5));
        assert_eq!(app.settings["enabled"], false);
    }
    #[test]
    fn master_off_retires_queued_motion_and_warm_followers_before_a_new_test() {
        struct CaptureSink {
            devices: std::collections::BTreeMap<u32, Value>,
            blocked: bool,
            commands: Vec<Value>,
        }
        impl buttplug::CommandSink for CaptureSink {
            fn devices(&self) -> &std::collections::BTreeMap<u32, Value> {
                &self.devices
            }
            fn generation(&self, _: u32) -> u64 {
                1
            }
            fn busy(&self, _: u32) -> bool {
                self.blocked
            }
            fn send(&mut self, kind: &str, fields: Value, _: Option<u32>) -> Result<u32, String> {
                let mut command = Map::new();
                command.insert(kind.to_owned(), fields);
                self.commands.push(Value::Object(command));
                Ok(1)
            }
        }
        let (_temp, mut app) = fixture();
        let (sent, _fail) = simulated_client(&mut app);
        let device = json!({"DeviceIndex":0,"DeviceName":"Offline test device", "DeviceMessages":{
            "ScalarCmd":[{"ActuatorType":"Vibrate","StepCount":20},{"ActuatorType":"Constrict","StepCount":20},{"ActuatorType":"Temperature","StepCount":20}],
            "RotateCmd":[{"StepCount":20}], "LinearCmd":[{"StepCount":20}], "StopDeviceCmd":{}}});
        app.client
            .as_mut()
            .unwrap()
            .devices
            .insert(0, device.clone());
        let preference = buttplug::settings_key(&device);
        app.save(&json!({"devices":{preference:{"outputs":{"vibrate":true,"rotate":true,"stroke":true,"constrict":true,"heat":true}}}})).unwrap();
        let mut sink = CaptureSink {
            devices: std::collections::BTreeMap::from([(0, device)]),
            blocked: false,
            commands: Vec::new(),
        };
        // Warm followers, flip rotation, and run strokes before leaving pending
        // scalar/rotation/stroke commands blocked behind backpressure.
        for (now, strength) in [(0., 1.), (0.1, 0.1), (0.3, 1.), (1., 0.9)] {
            app.outputs
                .update(&mut sink, strength, &app.settings["devices"], now, false)
                .unwrap();
        }
        assert!(sink
            .commands
            .iter()
            .any(|command| command["ScalarCmd"]["Scalars"]
                .as_array()
                .is_some_and(|scalars| scalars
                    .iter()
                    .any(|scalar| scalar["ActuatorType"] == "Temperature"
                        && scalar["Scalar"].as_f64().unwrap_or(0.) > 0.))));
        sink.blocked = true;
        sink.commands.clear();
        app.outputs
            .update(&mut sink, 0.9, &app.settings["devices"], 1.4, false)
            .unwrap();
        assert!(sink.commands.is_empty());
        app.outputs.hold.insert(0, (0.9, 10.));
        app.save(&json!({"enabled":false})).unwrap();
        assert!(app.outputs.hold.is_empty());
        assert!(sent
            .lock()
            .unwrap()
            .iter()
            .any(|command| command.get("StopAllDevices").is_some()));
        // Start immediately, with no quiet update between Off and Test.
        app.test_device("device:0", 1.45).unwrap();
        sink.blocked = false;
        let mut expected_sink = CaptureSink {
            devices: sink.devices.clone(),
            blocked: false,
            commands: Vec::new(),
        };
        let mut expected = buttplug::Outputs::new();
        expected.hold.insert(0, (0.5, 2.25));
        for now in [1.46, 1.7, 2.1, 2.3] {
            app.outputs
                .update(&mut sink, 0., &app.settings["devices"], now, true)
                .unwrap();
            expected
                .update(&mut expected_sink, 0., &app.settings["devices"], now, true)
                .unwrap();
        }
        assert!(expected_sink
            .commands
            .iter()
            .any(|command| command["LinearCmd"]["Vectors"][0]["Position"] == 0.5));
        assert_eq!(sink.commands, expected_sink.commands);
    }
    #[test]
    fn master_off_remains_effective_when_persistence_or_stop_transport_fails() {
        let (_temp, mut app) = fixture();
        let (sent, fail) = simulated_client(&mut app);
        app.outputs.hold.insert(0, (0.5, 20.));
        app.settings_path = app.mod_dir.join("missing/settings.json");
        assert!(app
            .save(&json!({"enabled":false,"max_strength":0.1}))
            .is_err());
        assert_eq!(app.settings["enabled"], false);
        assert_eq!(app.settings["max_strength"], 0.7);
        assert!(app.outputs.hold.is_empty());
        assert!(sent
            .lock()
            .unwrap()
            .iter()
            .any(|command| command.get("StopAllDevices").is_some()));
        app.settings_path = app.mod_dir.join("settings.json");
        app.save(&json!({"enabled":true})).unwrap();
        app.outputs.hold.insert(0, (0.5, 20.));
        fail.store(true, Ordering::Release);
        app.save(&json!({"enabled":false})).unwrap();
        assert_eq!(app.settings["enabled"], false);
        assert_eq!(read_json(&app.settings_path).unwrap()["enabled"], false);
        assert!(app.outputs.hold.is_empty());
        assert!(app.client.is_none());
    }
    #[test]
    fn device_limit_rejects_new_preferences_and_retains_existing_updates() {
        let (_temp, mut app) = fixture();
        let devices: Map<String, Value> = (0..64)
            .map(|index| (format!("device-{index}"), json!({"on":false})))
            .collect();
        app.save(&json!({"enabled":false,"devices":devices}))
            .unwrap();
        let saved = fs::read(&app.settings_path).unwrap();
        let before = app.settings.clone();
        assert!(app
            .save(&json!({"devices":{"new-device":{"on":false}}}))
            .is_err());
        assert_eq!(fs::read(&app.settings_path).unwrap(), saved);
        assert_eq!(app.settings, before);
        app.save(&json!({"devices":{"device-63":{"strength":0.2}}}))
            .unwrap();
        let reloaded = App::new(
            app.mod_dir.clone(),
            app.assets.clone(),
            Some(app.settings_path.clone()),
        )
        .unwrap();
        assert_eq!(reloaded.settings, app.settings);
        assert_eq!(reloaded.settings["enabled"], false);
        assert_eq!(reloaded.settings["devices"]["device-63"]["strength"], 0.2);
    }
    #[test]
    fn oversized_settings_transaction_preserves_roundtrip_and_disabled_master() {
        let (_temp, mut app) = fixture();
        app.save(&json!({"enabled":false})).unwrap();
        for batch in 0..7 {
            let devices: Map<String, Value> = (0..8)
                .map(|index| {
                    (
                        format!("{:02}:{}", batch * 8 + index, "a".repeat(17000)),
                        json!({"on":false}),
                    )
                })
                .collect();
            app.save(&json!({"devices":devices})).unwrap();
        }
        let saved = fs::read(&app.settings_path).unwrap();
        let before = app.settings.clone();
        assert!(saved.len() as u64 <= MAX_JSON);
        let devices: Map<String, Value> = (56..64)
            .map(|index| {
                (
                    format!("{index:02}:{}", "a".repeat(17000)),
                    json!({"on":false}),
                )
            })
            .collect();
        let error = app.save(&json!({"devices":devices})).unwrap_err();
        assert!(error.contains("persistence limit"));
        assert_eq!(fs::read(&app.settings_path).unwrap(), saved);
        assert_eq!(app.settings, before);
        let reloaded = App::new(
            app.mod_dir.clone(),
            app.assets.clone(),
            Some(app.settings_path.clone()),
        )
        .unwrap();
        assert_eq!(reloaded.settings, app.settings);
        assert_eq!(reloaded.settings["enabled"], false);
    }
    #[test]
    fn every_scene_resumes_after_gameplay_and_pause_gates_without_replaying_leftovers() {
        for name in [
            "judy",
            "judyCensored",
            "kerry",
            "kerryCensored",
            "river",
            "stout",
            "panam",
            "panamCensored",
        ] {
            for pause in [false, true] {
                let (_temp, mut app) = fixture();
                let scene = json!({"name":name,"instance":1,"elapsed":1.});
                beat(&mut app, 1, 20., true, false, scene.clone());
                assert!(!app.mixer.voices.contains_key("romance"));
                assert_eq!(app.output, 0.);
                beat(&mut app, 2, 20.1, true, false, scene.clone());
                assert!((20.1 - app.mixer.voices["romance"].start - 1.).abs() < 1e-12);
                beat(
                    &mut app,
                    3,
                    21.,
                    pause,
                    pause,
                    json!({"name":name,"instance":1,"elapsed":2.}),
                );
                assert!(!app.mixer.voices.contains_key("romance"));
                assert_eq!(app.output, 0.);
                let elapsed = if pause { 6. } else { 2. };
                beat(
                    &mut app,
                    4,
                    25.,
                    true,
                    false,
                    json!({"name":name,"instance":1,"elapsed":elapsed}),
                );
                let voice = &app.mixer.voices["romance"];
                assert_eq!(voice.feel, format!("track:{name}"));
                assert!((25. - voice.start - elapsed).abs() < 1e-12);
                assert!(app.output > 0., "{name} gate {pause}");
                app.tick(26.01).unwrap();
                assert_eq!(app.output, 0., "stale scene must stop");
            }
        }
    }
    #[test]
    fn scene_end_releases_and_custom_envelopes_never_restart_after_completion() {
        let (_temp, mut app) = fixture();
        beat(
            &mut app,
            1,
            10.,
            true,
            false,
            json!({"name":"judy","instance":1,"elapsed":3.}),
        );
        beat(
            &mut app,
            2,
            10.1,
            true,
            false,
            json!({"name":"judy","instance":1,"elapsed":3.}),
        );
        beat(
            &mut app,
            3,
            11.,
            true,
            false,
            json!({"name":"judy","instance":2,"elapsed":0.5}),
        );
        assert!((app.mixer.voices["romance"].start - 10.5).abs() < 1e-12);
        beat(&mut app, 4, 11.1, true, false, Value::Null);
        assert!(app.mixer.voices["romance"].release.is_some());
        beat(&mut app, 5, 12.7, true, false, Value::Null);
        assert!(!app.mixer.voices.contains_key("romance"));
        app.save(&json!({"interactions":{"romance":{"feel":"slowbuild"}}}))
            .unwrap();
        let end = app.mixer.effects["slowbuild"]["points"]
            .as_array()
            .unwrap()
            .last()
            .unwrap()[0]
            .as_f64()
            .unwrap();
        beat(
            &mut app,
            6,
            20.,
            true,
            false,
            json!({"name":"judy","instance":3,"elapsed":1.}),
        );
        assert!(app.mixer.voices.contains_key("romance"));
        beat(
            &mut app,
            7,
            25.,
            false,
            true,
            json!({"name":"judy","instance":3,"elapsed":2.}),
        );
        beat(
            &mut app,
            8,
            26.,
            true,
            false,
            json!({"name":"judy","instance":3,"elapsed":end+1.}),
        );
        assert!(!app.mixer.voices.contains_key("romance"));
        assert_eq!(app.output, 0.);
    }
    #[test]
    fn idle_status_heartbeat_advances_only_after_successful_atomic_writes() {
        let (_temp, mut app) = fixture();
        app.write_status(10.);
        let first = read_json(&app.mod_dir.join("status.json")).unwrap();
        app.write_status(10.5);
        assert_eq!(read_json(&app.mod_dir.join("status.json")).unwrap(), first);
        app.write_status(11.1);
        assert_eq!(app.status_heartbeat, 2);
        let saved = app.mod_dir.clone();
        app.mod_dir = app.mod_dir.join("missing");
        app.write_status(12.2);
        assert_eq!(app.status_heartbeat, 2);
        app.mod_dir = saved;
        app.write_status(12.3);
        assert_eq!(app.status_heartbeat, 3);
        app.shutdown();
        assert_eq!(
            read_json(&app.mod_dir.join("status.json")).unwrap()["running"],
            false
        );
    }
    #[test]
    fn graph_uses_elapsed_time_at_both_idle_and_busy_rates() {
        let (_temp, mut app) = fixture();
        app.history = vec![(10., 0.9), (15.1, 0.4), (19.95, 0.2)];
        let graph = app.output_history(20.);
        assert_eq!(graph.len(), 150);
        assert!(!graph.contains(&0.9));
        assert_eq!(graph[148], 0.2);
        assert_eq!(graph.iter().copied().fold(0., f64::max), 0.4);
    }
    #[test]
    fn read_json_bounds_files_and_invalid_depth() {
        let (temp, _app) = fixture();
        let path = temp.0.join("boundary.json");
        fs::write(&path, vec![b' '; MAX_JSON as usize + 1]).unwrap();
        assert!(read_json(&path).is_none());
        fs::write(&path, format!("{}0{}", "[".repeat(2000), "]".repeat(2000))).unwrap();
        assert!(read_json(&path).is_none());
    }
    #[test]
    fn archive_validation_and_failed_replacement_preserve_owned_engine() {
        let (temp, _app) = fixture();
        let archive = temp.0.join("bad.zip");
        let mut bundle = zip::ZipWriter::new(File::create(&archive).unwrap());
        bundle
            .start_file(
                "folder/../intiface-engine",
                zip::write::SimpleFileOptions::default(),
            )
            .unwrap();
        bundle.write_all(b"fixture").unwrap();
        bundle.finish().unwrap();
        assert!(unpack_engine(&archive, &temp.0.join("unpacked")).is_err());
        assert!(!temp.0.join("unpacked").exists());
        let target = temp.0.join("existing");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("intiface-engine"), b"old").unwrap();
        assert!(replace_engine(
            &temp.0.join("missing/intiface-engine"),
            &target,
            &temp.0.join("previous")
        )
        .is_err());
        assert_eq!(fs::read(target.join("intiface-engine")).unwrap(), b"old");
    }
}
