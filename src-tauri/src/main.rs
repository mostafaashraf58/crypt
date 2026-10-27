#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rand::RngCore;
use serde::Serialize;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc, Condvar, Mutex},
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, RunEvent, State};

#[derive(Clone, Serialize)]
struct EngineConnection {
    url: String,
    token: String,
}

#[derive(Default)]
struct EngineStatus {
    child: Option<Child>,
    job: Option<ProcessJob>,
    connection: Option<EngineConnection>,
    error: Option<String>,
    stopping: bool,
}

struct SharedEngine {
    status: Mutex<EngineStatus>,
    ready: Condvar,
}

struct LocalEngine {
    shared: Arc<SharedEngine>,
    bootstrap: Mutex<Option<thread::JoinHandle<()>>>,
}

// The Windows one-file Python bundle has a bootloader and a worker process.
// Keep both in a job whose closure terminates the complete process tree.
#[cfg(windows)]
struct ProcessJob { handle: isize }

#[cfg(windows)]
impl ProcessJob {
    fn new() -> std::io::Result<Self> {
        use windows_sys::Win32::System::JobObjects::*;
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle as isize == 0 { return Err(std::io::Error::last_os_error()); }
            let job = Self {handle: handle as isize};
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(handle, JobObjectExtendedLimitInformation, &info as *const _ as *const std::ffi::c_void, std::mem::size_of_val(&info) as u32) == 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(job)
        }
    }

    fn attach(&self, child: &Child) -> std::io::Result<()> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{Foundation::HANDLE, System::JobObjects::AssignProcessToJobObject};
        if unsafe {AssignProcessToJobObject(self.handle as HANDLE, child.as_raw_handle() as HANDLE)} == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
}

#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle as windows_sys::Win32::Foundation::HANDLE); }
    }
}

#[cfg(not(windows))]
struct ProcessJob;

#[cfg(not(windows))]
impl ProcessJob {
    fn new() -> std::io::Result<Self> { Ok(Self) }
    fn attach(&self, _child: &Child) -> std::io::Result<()> { Ok(()) }
}

impl LocalEngine {
    fn stop(&self) {
        let (child, job) = if let Ok(mut status) = self.shared.status.lock() {
            status.stopping = true;
            self.shared.ready.notify_all();
            (status.child.take(), status.job.take())
        } else { (None, None) };
        drop(job);
        if let Some(mut child) = child {
            // Windows and Unix both close the local service with the desktop.
            let _ = child.kill();
            let _ = child.wait();
        }
        if let Ok(mut bootstrap) = self.bootstrap.lock() {
            if let Some(handle) = bootstrap.take() {
                // Startup observes the stop flag on each readiness iteration.
                // Join it before normal exit so an early close leaves no child.
                if handle.thread().id() != thread::current().id() { let _ = handle.join(); }
            }
        }
    }
}

impl Drop for LocalEngine {
    fn drop(&mut self) { self.stop(); }
}

#[tauri::command]
async fn get_engine_connection(window: tauri::Window, state: State<'_, LocalEngine>) -> Result<EngineConnection, String> {
    if window.label() != "main" {
        return Err("Engine access is limited to the main application window".into());
    }
    let shared = state.shared.clone();
    // Startup and waiting run outside the UI thread. The frontend can render its
    // loading state immediately and this invocation resolves once Python is ready.
    tauri::async_runtime::spawn_blocking(move || {
        let guard = shared.status.lock().map_err(|_| "Engine state is unavailable".to_string())?;
        let (status, _) = shared.ready.wait_timeout_while(guard, Duration::from_secs(50), |s| {
            s.connection.is_none() && s.error.is_none() && !s.stopping
        }).map_err(|_| "Engine state is unavailable".to_string())?;
        if status.stopping { return Err("The local engine is shutting down".into()); }
        if let Some(error) = &status.error { return Err(error.clone()); }
        status.connection.clone().ok_or_else(|| "The local engine did not become ready".into())
    }).await.map_err(|_| "Unable to wait for the local engine".to_string())?
}

fn token() -> String {
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn python_command(project: &PathBuf) -> Command {
    if let Some(configured) = std::env::var_os("AXIOM_PYTHON") {
        return Command::new(configured);
    }
    let local = if cfg!(windows) {
        project.join(".venv/Scripts/python.exe")
    } else {
        project.join(".venv/bin/python")
    };
    Command::new(if local.exists() {local} else {PathBuf::from(if cfg!(windows) {"python"} else {"python3"})})
}

fn start_engine(app: &tauri::AppHandle, shared: &SharedEngine) -> Result<(Child, EngineConnection, ProcessJob), Box<dyn std::error::Error>> {
    let data_dir = app.path().app_local_data_dir()?;
    fs::create_dir_all(&data_dir)?;
    // Python binds and retains its own ephemeral listening socket. Read the
    // chosen port only from this child's private stdout pipe, avoiding a local
    // reserve/release race and any unauthenticated network discovery endpoint.
    let session_token = token();
    let mut command;
    if cfg!(debug_assertions) {
        let project = PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
        command = python_command(&project);
        command.args(["-m", "engine"]).current_dir(project);
    } else {
        let resource_dir = app.path().resource_dir()?;
        let executable = resource_dir.join(if cfg!(windows) {"axiom-engine.exe"} else {"axiom-engine"});
        if !executable.is_file() {
            return Err("The bundled local engine is missing. Reinstall Axiom Workstation.".into());
        }
        command = Command::new(executable);
    }
    command.args(["--port", "0", "--data-dir"])
        .arg(data_dir)
        .arg("--token-stdin")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    if shared.status.lock().map(|s| s.stopping).unwrap_or(true) {
        return Err("Local engine startup was cancelled".into());
    }
    let secret_input = serde_json::to_vec(&serde_json::json!({"token": &session_token}))?;
    let job = ProcessJob::new()?;
    let mut child = command.spawn()?;
    if let Err(error) = job.attach(&child) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error.into());
    }
    if let Some(mut stdin) = child.stdin.take() {
        if let Err(error) = stdin.write_all(&secret_input).and_then(|_| stdin.write_all(b"\n")) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error.into());
        }
    }
    let stdout = child.stdout.take().ok_or("The local engine startup pipe is missing")?;
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout.take(1024)).read_line(&mut line)
            .map_err(|_| "Unable to read the local engine startup message".to_string())
            .and_then(|_| {
                let payload = line.strip_prefix("AXIOM_READY ").ok_or_else(|| "Invalid local engine startup message".to_string())?;
                let value: serde_json::Value = serde_json::from_str(payload).map_err(|_| "Invalid local engine port metadata".to_string())?;
                let port = value.get("port").and_then(serde_json::Value::as_u64).filter(|p| *p > 0 && *p <= 65535)
                    .ok_or_else(|| "Invalid local engine port".to_string())?;
                Ok(port as u16)
            });
        let _ = sender.send(result);
    });
    let mut port = None;
    let deadline = Instant::now() + Duration::from_secs(45);
    while Instant::now() < deadline {
        if shared.status.lock().map(|s| s.stopping).unwrap_or(true) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Local engine startup was cancelled".into());
        }
        match child.try_wait() {
            Ok(Some(_)) => return Err("The local engine exited during startup. Check the Python environment in development, or reinstall the packaged app.".into()),
            Ok(None) => {},
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error.into());
            }
        }
        if port.is_none() {
            match receiver.try_recv() {
                Ok(Ok(value)) => port = Some(value),
                Ok(Err(error)) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(error.into());
                }
                Err(mpsc::TryRecvError::Empty) => {},
                Err(mpsc::TryRecvError::Disconnected) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("The local engine startup channel closed".into());
                }
            }
        }
        if let Some(port) = port {
            if authenticated_probe(port, &session_token) {
                return Ok((child, EngineConnection {url: format!("http://127.0.0.1:{port}"), token: session_token}, job));
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
    Err("The local engine did not become ready within 45 seconds.".into())
}

fn authenticated_probe(port: u16, token: &str) -> bool {
    let address = format!("127.0.0.1:{port}").parse().unwrap();
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(200)) else { return false; };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let request = format!("GET /snapshot HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() { return false; }
    let mut bytes = [0u8; 256];
    match stream.read(&mut bytes) {
        Ok(count) => std::str::from_utf8(&bytes[..count]).unwrap_or("").starts_with("HTTP/1.1 200"),
        Err(_) => false,
    }
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let shared = Arc::new(SharedEngine {
                status: Mutex::new(EngineStatus::default()),
                ready: Condvar::new(),
            });
            app.manage(LocalEngine {shared: shared.clone(), bootstrap: Mutex::new(None)});
            let handle = app.handle().clone();
            let bootstrap = thread::Builder::new().name("axiom-engine-bootstrap".into()).spawn(move || {
                let result = start_engine(&handle, &shared);
                if let Ok(mut status) = shared.status.lock() {
                    match result {
                        Ok((mut child, connection, job)) => {
                            if status.stopping {
                                drop(job);
                                let _ = child.kill();
                                let _ = child.wait();
                            } else {
                                status.child = Some(child);
                                status.job = Some(job);
                                status.connection = Some(connection);
                            }
                        }
                        Err(error) => status.error = Some(error.to_string()),
                    }
                    shared.ready.notify_all();
                } else if let Ok((mut child, _, job)) = result {
                    drop(job);
                    let _ = child.kill();
                    let _ = child.wait();
                };
            })?;
            if let Ok(mut guard) = app.state::<LocalEngine>().bootstrap.lock() { *guard = Some(bootstrap); }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_engine_connection])
        .build(tauri::generate_context!())
        .expect("Unable to start Axiom Workstation");
    app.run(|app, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            if let Some(engine) = app.try_state::<LocalEngine>() { engine.stop(); }
        }
    });
}
