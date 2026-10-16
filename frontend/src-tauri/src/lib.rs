// Konduktor desktop shell (Tauri v2).
//
// The backend is the frozen Python sidecar (see backend/sidecar.py), a
// PyInstaller ONEDIR folder bundled as the `sidecar/` resource — not an
// `externalBin`, which must be one file. A one-file build unpacked ~390 native
// libraries into a new temp folder on every launch, and macOS scans each
// library it has not seen before as it loads, so the window took ~10 s to
// appear; installed as a folder they are scanned once.
//
// The window opens AT ONCE and shows the startup screen (StartupScreen.tsx)
// while the backend starts: a first launch after an install or update still
// waits for that one-time scan, and a window that appears seconds late looks
// like an app that failed to open. Meanwhile we spawn the sidecar, read the
// `KONDUKTOR_PORT=<n>` line it prints on stdout and wait for the socket to
// accept connections. The page asks `backend_status` and listens for
// `konduktor://backend` (starting / ready + url / stopped + the output's last
// lines); `backend_restart` is its Try again. There is no timeout here — a
// fresh install can take a minute — so the page decides when to ask the user.
// The sidecar is killed when the app exits.
//
// Quitting ASKS FIRST. Closing the window or Cmd/Ctrl+Q is prevented and
// becomes a `konduktor://quit-requested` event; the frontend (QuitGuard.tsx,
// or the startup screen before the app has loaded) acknowledges it, checks for
// unsaved changes and either quits (`quit_now`) or asks Save / Discard /
// Cancel. The sidecar must outlive that question — a Save or Discard runs in
// it — so it is killed only once the app really exits. If the webview does not
// acknowledge within ~2 s (hung, or not loaded yet), the app quits anyway: a
// broken page must never make the app unquittable.
//
// UPDATING rides on that same handshake (UpdateDialog.tsx). The page finds the
// release (its own GitHub check); `update_download` fetches THAT release's
// `latest.json`, downloads + verifies the package with tauri-plugin-updater and
// STAGES it on disk; `update_restart` then asks to quit like Cmd+Q does. The
// package is installed only in `exit_now`, after the sidecar is dead: on macOS
// the sidecar lazily loads native libraries from inside the bundle being
// replaced, and on Windows its .exe in the install folder is locked while it
// runs. Cancel in the Save / Discard prompt keeps the update staged, so the
// next ordinary quit installs it (without relaunching). A failed install is
// written to `failed.json` for the next launch's dialog to report.
use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::window::Color;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::{Update, UpdaterExt};

/// Holds the running sidecar child so we can terminate it on exit.
struct Sidecar(Mutex<Option<CommandChild>>);

/// How many of the sidecar's last output lines a "couldn't start" report keeps.
const LOG_LINES: usize = 30;

/// What the startup screen shows. Serialised as `{"state": "starting"}`,
/// `{"state": "ready", "url": …}` or `{"state": "stopped", "code", "signal", "log"}`.
#[derive(Clone, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum BackendStatus {
    Starting,
    Ready { url: String },
    Stopped { code: Option<i32>, signal: Option<i32>, log: Vec<String> },
}

/// The backend's current status, plus a generation counter: each spawn bumps
/// it, so events from a sidecar that was replaced (Try again) are ignored.
struct Backend {
    status: Mutex<BackendStatus>,
    generation: AtomicU64,
}

/// Quit handshake state: `allowed` once the frontend (or the timeout) has said
/// quit; `acked` once the frontend has seen the current request.
struct Quit {
    allowed: AtomicBool,
    acked: AtomicBool,
}

/// A downloaded, verified update waiting for the app to exit.
struct StagedUpdate {
    update: Update,
    /// The package on disk (~150 MB: not worth holding in memory all session).
    path: PathBuf,
    /// Relaunch once installed: set by `update_restart`, cleared by Cancel.
    relaunch: bool,
}

struct Staged(Mutex<Option<StagedUpdate>>);

/// Download progress for the update dialog.
#[derive(Clone, Serialize)]
struct UpdateProgress {
    downloaded: u64,
    total: Option<u64>,
}

/// An install that failed at exit, reported by the next launch.
#[derive(Serialize, serde::Deserialize)]
struct UpdateFailure {
    version: String,
    error: String,
}

const REPO: &str = "MrPatben8/konduktor";

/// The frozen backend's executable, in the bundled `sidecar/` folder (staged
/// into src-tauri/binaries/ by backend/build_sidecar.*).
fn sidecar_exe(app: &AppHandle) -> Result<PathBuf, String> {
    let exe = if cfg!(windows) { "konduktor-sidecar.exe" } else { "konduktor-sidecar" };
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("sidecar").join(exe))
}

fn kill_sidecar(app: &AppHandle) {
    if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
        let _ = child.kill();
    }
}

/// Windows only: `kill` asks the OS to end the process and returns at once,
/// and the installer must not find the sidecar's .exe still locked. Opening a
/// running executable for writing is refused, so poll that (up to ~5 s).
fn wait_for_sidecar_exit(app: &AppHandle) {
    if !cfg!(windows) {
        return;
    }
    let Ok(exe) = sidecar_exe(app) else { return };
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if std::fs::OpenOptions::new().write(true).open(&exe).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn update_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_cache_dir().ok().map(|dir| dir.join("update"))
}

/// Install a staged update, if any. Runs at exit with the sidecar already dead.
/// On Windows a successful install never returns (the updater starts the
/// installer and exits the process); on macOS the new bundle is in place and
/// a requested relaunch starts it.
fn install_staged(app: &AppHandle) {
    let Some(staged) = app.state::<Staged>().0.lock().unwrap().take() else { return };
    let StagedUpdate { update, path, relaunch } = staged;
    let version = update.version.clone();
    wait_for_sidecar_exit(app);
    // Not re-verified: `download` checked the signature before staging, and
    // whoever could alter the app's cache folder could alter the app itself.
    let result = std::fs::read(&path).map_err(|e| e.to_string()).and_then(|bytes| {
        update.restart_after_install(relaunch).install(bytes).map_err(|e| e.to_string())
    });
    let _ = std::fs::remove_file(&path);
    match result {
        Ok(()) if relaunch => app.restart(),
        Ok(()) => {}
        Err(error) => {
            log::error!("update to {version} failed to install: {error}");
            if let Some(dir) = update_dir(app) {
                let failure = UpdateFailure { version, error };
                if let Ok(json) = serde_json::to_vec(&failure) {
                    let _ = std::fs::write(dir.join("failed.json"), json);
                }
            }
            if relaunch {
                app.restart(); // back into the version that is still installed
            }
        }
    }
}

fn exit_now(app: &AppHandle) {
    app.state::<Quit>().allowed.store(true, Ordering::SeqCst);
    kill_sidecar(app);
    install_staged(app);
    app.exit(0);
}

/// Hand the quit decision to the frontend, with a deadline for it to answer.
fn request_quit(app: &AppHandle) {
    app.state::<Quit>().acked.store(false, Ordering::SeqCst);
    if app.emit("konduktor://quit-requested", ()).is_err() {
        exit_now(app);
        return;
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(2)).await;
        if !handle.state::<Quit>().acked.load(Ordering::SeqCst) {
            exit_now(&handle);
        }
    });
}

/// Record a new status for generation `gen` (ignored if a newer sidecar has
/// been spawned since) and tell the page.
fn set_status(app: &AppHandle, gen: u64, status: BackendStatus) {
    let backend = app.state::<Backend>();
    if backend.generation.load(Ordering::SeqCst) != gen {
        return;
    }
    *backend.status.lock().unwrap() = status.clone();
    let _ = app.emit("konduktor://backend", status);
}

fn push_lines(log: &mut VecDeque<String>, bytes: &[u8]) {
    for line in String::from_utf8_lossy(bytes).lines() {
        if log.len() == LOG_LINES {
            log.pop_front();
        }
        log.push_back(line.to_string());
    }
}

/// Spawn the sidecar and follow it until it is serving (ready) or exits (stopped).
fn start_backend(app: &AppHandle) {
    kill_sidecar(app);
    let gen = app.state::<Backend>().generation.fetch_add(1, Ordering::SeqCst) + 1;
    set_status(app, gen, BackendStatus::Starting);

    let spawned = sidecar_exe(app).and_then(|program| {
        app.shell()
            .command(&program)
            .spawn()
            .map_err(|e| format!("{}: {e}", program.display()))
    });
    let (mut rx, child) = match spawned {
        Ok(pair) => pair,
        Err(e) => {
            let log = vec![format!("Could not start the library service: {e}")];
            set_status(app, gen, BackendStatus::Stopped { code: None, signal: None, log });
            return;
        }
    };
    app.state::<Sidecar>().0.lock().unwrap().replace(child);

    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut log = VecDeque::with_capacity(LOG_LINES);
        let mut announced = false;
        // Keep draining to the end, so the sidecar's pipes never fill and block it.
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    if !announced {
                        let text = String::from_utf8_lossy(&bytes);
                        let port = text.lines().find_map(|line| {
                            line.trim().strip_prefix("KONDUKTOR_PORT=")?.trim().parse::<u16>().ok()
                        });
                        if let Some(port) = port {
                            announced = true;
                            wait_until_serving(handle.clone(), gen, port);
                        }
                    }
                    push_lines(&mut log, &bytes);
                }
                CommandEvent::Stderr(bytes) => push_lines(&mut log, &bytes),
                CommandEvent::Error(e) => push_lines(&mut log, e.as_bytes()),
                CommandEvent::Terminated(payload) => {
                    let status = BackendStatus::Stopped {
                        code: payload.code,
                        signal: payload.signal,
                        log: log.iter().cloned().collect(),
                    };
                    set_status(&handle, gen, status);
                }
                _ => {}
            }
        }
    });
}

/// The port line is printed before uvicorn binds: report ready once the socket
/// actually accepts connections (or give up quietly if this sidecar is gone).
fn wait_until_serving(app: AppHandle, gen: u64, port: u16) {
    tauri::async_runtime::spawn(async move {
        loop {
            let backend = app.state::<Backend>();
            let current = backend.generation.load(Ordering::SeqCst) == gen
                && matches!(*backend.status.lock().unwrap(), BackendStatus::Starting);
            if !current {
                return;
            }
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                let url = format!("http://127.0.0.1:{port}");
                set_status(&app, gen, BackendStatus::Ready { url });
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    });
}

/// Where the backend is: the startup screen asks once on load, then listens.
#[tauri::command]
fn backend_status(app: AppHandle) -> BackendStatus {
    app.state::<Backend>().status.lock().unwrap().clone()
}

/// The startup screen's Try again.
#[tauri::command]
fn backend_restart(app: AppHandle) {
    start_backend(&app);
}

/// The frontend has seen the quit request (it may now take as long as it likes).
#[tauri::command]
fn quit_ack(app: AppHandle) {
    app.state::<Quit>().acked.store(true, Ordering::SeqCst);
}

/// The user chose Cancel: forget the request. A staged update stays staged
/// but no longer relaunches; the next ordinary quit installs it.
#[tauri::command]
fn quit_cancel(app: AppHandle) {
    app.state::<Quit>().acked.store(false, Ordering::SeqCst);
    if let Some(staged) = app.state::<Staged>().0.lock().unwrap().as_mut() {
        staged.relaunch = false;
        let _ = app.emit("konduktor://update-deferred", ());
    }
}

/// Quit for real (nothing unsaved, or Save / Discard has finished).
#[tauri::command]
fn quit_now(app: AppHandle) {
    exit_now(&app);
}

/// Refuse up front what the macOS install could not do: replace a bundle on
/// a read-only volume (run from the DMG), a translocated copy, or a folder this
/// user cannot write. (The updater's own fallback for the last asks for an
/// admin password from the main thread, which the quit path may be blocking.)
#[cfg(target_os = "macos")]
fn check_installable() -> Result<(), String> {
    const MOVE_IT: &str = "Move Konduktor to your Applications folder, open it from there, then try again.";
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let bundle = tauri_plugin_updater::extract_path_from_executable(&exe).map_err(|e| e.to_string())?;
    if bundle.to_string_lossy().contains("/AppTranslocation/") {
        return Err(MOVE_IT.into());
    }
    let parent = bundle.parent().ok_or_else(|| MOVE_IT.to_string())?;
    let probe = parent.join(format!(".konduktor-update-check-{}", std::process::id()));
    match std::fs::File::create(&probe) {
        Ok(_) => {
            let _ = std::fs::remove_file(&probe);
            Ok(())
        }
        Err(_) => Err(MOVE_IT.into()),
    }
}

#[cfg(not(target_os = "macos"))]
fn check_installable() -> Result<(), String> {
    Ok(())
}

/// Download the update published with release `tag`, verify it and stage it
/// for installation at exit. Returns the version staged.
#[tauri::command]
async fn update_download(
    app: AppHandle,
    tag: String,
    on_progress: Channel<UpdateProgress>,
) -> Result<String, String> {
    check_installable()?;
    let manifest = format!("https://github.com/{REPO}/releases/download/{tag}/latest.json");
    let url = manifest.parse().map_err(|e| format!("{manifest}: {e}"))?;
    let update = app
        .updater_builder()
        .endpoints(vec![url])
        .and_then(|b| b.build())
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| format!("{tag} is not newer than this version."))?;

    let mut downloaded = 0u64;
    let mut last_sent = Instant::now();
    let bytes = update
        .download(
            |chunk, total| {
                downloaded += chunk as u64;
                // Chunks arrive every few KB: a few updates a second is plenty.
                if last_sent.elapsed() >= Duration::from_millis(100) {
                    last_sent = Instant::now();
                    let _ = on_progress.send(UpdateProgress { downloaded, total });
                }
            },
            || {},
        )
        .await
        .map_err(|e| e.to_string())?;

    let dir = update_dir(&app).ok_or("No cache folder to download into.")?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = dir.join(format!("{}.pkg", update.version));
    std::fs::write(&path, &bytes).map_err(|e| e.to_string())?;
    drop(bytes);

    let version = update.version.clone();
    let previous = app
        .state::<Staged>()
        .0
        .lock()
        .unwrap()
        .replace(StagedUpdate { update, path: path.clone(), relaunch: false });
    if let Some(old) = previous.filter(|old| old.path != path) {
        let _ = std::fs::remove_file(old.path);
    }
    Ok(version)
}

/// Quit, install the staged update and relaunch. Goes through the ordinary
/// quit request, so unsaved changes are asked about first.
#[tauri::command]
fn update_restart(app: AppHandle) -> Result<(), String> {
    match app.state::<Staged>().0.lock().unwrap().as_mut() {
        Some(staged) => staged.relaunch = true,
        None => return Err("No update has been downloaded.".into()),
    }
    request_quit(&app);
    Ok(())
}

/// The last update's install failure, if any — reported once.
#[tauri::command]
fn update_take_failure(app: AppHandle) -> Option<UpdateFailure> {
    let path = update_dir(&app)?.join("failed.json");
    let bytes = std::fs::read(&path).ok()?;
    let _ = std::fs::remove_file(&path);
    serde_json::from_slice(&bytes).ok()
}

/// Packages left by a run that never exited normally (crash, force quit).
fn clear_stale_updates(app: &AppHandle) {
    let Some(dir) = update_dir(app) else { return };
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        if entry.path().extension().is_some_and(|ext| ext == "pkg") {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Sidecar(Mutex::new(None)))
        .manage(Staged(Mutex::new(None)))
        .manage(Backend { status: Mutex::new(BackendStatus::Starting), generation: AtomicU64::new(0) })
        .manage(Quit { allowed: AtomicBool::new(false), acked: AtomicBool::new(false) })
        .invoke_handler(tauri::generate_handler![
            backend_status,
            backend_restart,
            quit_ack,
            quit_cancel,
            quit_now,
            update_download,
            update_restart,
            update_take_failure
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                if !app.state::<Quit>().allowed.load(Ordering::SeqCst) {
                    api.prevent_close();
                    request_quit(app);
                }
            }
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            clear_stale_updates(app.handle());
            start_backend(app.handle());

            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Konduktor")
                .inner_size(1400.0, 900.0)
                .min_inner_size(900.0, 600.0)
                // The page's own base colour (ink-950), so the first frame —
                // before any CSS has loaded — is dark rather than white.
                .background_color(Color(5, 6, 10, 255))
                // On Windows the webview would otherwise be served from
                // https://tauri.localhost, and fetching the sidecar over
                // http://127.0.0.1 would be blocked as mixed content. Force
                // the http custom-protocol scheme so both are http. (No-op on
                // macOS, which uses the tauri:// scheme.)
                .use_https_scheme(false)
                .build()?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tauri application")
        .run(|app_handle, event| match event {
            // Cmd/Ctrl+Q, the app menu's Quit, the last window closing: ask
            // first, unless the answer is already in.
            RunEvent::ExitRequested { api, .. } => {
                if !app_handle.state::<Quit>().allowed.load(Ordering::SeqCst) {
                    api.prevent_exit();
                    request_quit(app_handle);
                }
            }
            // Really exiting: only now may the backend go.
            RunEvent::Exit => kill_sidecar(app_handle),
            _ => {}
        });
}
