// Konduktor desktop shell (Tauri v2).
//
// The backend is the frozen Python sidecar (see backend/sidecar.py): on launch
// we spawn it, read the `KONDUKTOR_PORT=<n>` line it prints on stdout, wait for
// it to actually accept connections, then create the app window with an
// initialization script that hands the frontend its API base
// (`window.__KONDUKTOR_API__`). The sidecar is killed when the app exits.
//
// Quitting ASKS FIRST. Closing the window or Cmd/Ctrl+Q is prevented and
// becomes a `konduktor://quit-requested` event; the frontend (QuitGuard.tsx)
// acknowledges it, checks for unsaved changes and either quits (`quit_now`) or
// asks Save / Discard / Cancel. The sidecar must outlive that question — a
// Save or Discard runs in it — so it is killed only once the app really exits.
// If the webview does not acknowledge within ~2 s (hung, or not loaded yet),
// the app quits anyway: a broken page must never make the app unquittable.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Holds the running sidecar child so we can terminate it on exit.
struct Sidecar(Mutex<Option<CommandChild>>);

/// Quit handshake state: `allowed` once the frontend (or the timeout) has said
/// quit; `acked` once the frontend has seen the current request.
struct Quit {
    allowed: AtomicBool,
    acked: AtomicBool,
}

fn kill_sidecar(app: &AppHandle) {
    if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
        let _ = child.kill();
    }
}

fn exit_now(app: &AppHandle) {
    app.state::<Quit>().allowed.store(true, Ordering::SeqCst);
    kill_sidecar(app);
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

/// The frontend has seen the quit request (it may now take as long as it likes).
#[tauri::command]
fn quit_ack(app: AppHandle) {
    app.state::<Quit>().acked.store(true, Ordering::SeqCst);
}

/// The user chose Cancel: forget the request.
#[tauri::command]
fn quit_cancel(app: AppHandle) {
    app.state::<Quit>().acked.store(false, Ordering::SeqCst);
}

/// Quit for real (nothing unsaved, or Save / Discard has finished).
#[tauri::command]
fn quit_now(app: AppHandle) {
    exit_now(&app);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .manage(Quit { allowed: AtomicBool::new(false), acked: AtomicBool::new(false) })
        .invoke_handler(tauri::generate_handler![quit_ack, quit_cancel, quit_now])
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

            let handle = app.handle().clone();

            // Spawn the frozen backend. In dev/bundle the shell plugin resolves
            // the sidecar from `binaries/konduktor-sidecar-<target-triple>`.
            let (mut rx, child) = app
                .shell()
                .sidecar("konduktor-sidecar")
                .expect("sidecar binary not found (build it into src-tauri/binaries/)")
                .spawn()
                .expect("failed to spawn backend sidecar");
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            tauri::async_runtime::block_on(async move {
                // Read stdout until the backend announces its port (bounded so a
                // misbehaving sidecar can't hang the launch forever).
                let find_port = async {
                    while let Some(event) = rx.recv().await {
                        if let CommandEvent::Stdout(bytes) = event {
                            let text = String::from_utf8_lossy(&bytes);
                            for line in text.lines() {
                                if let Some(p) = line.trim().strip_prefix("KONDUKTOR_PORT=") {
                                    if let Ok(n) = p.trim().parse::<u16>() {
                                        return Some(n);
                                    }
                                }
                            }
                        }
                    }
                    None
                };
                let port = tokio::time::timeout(Duration::from_secs(20), find_port)
                    .await
                    .ok()
                    .flatten()
                    .expect("backend sidecar did not report a port");

                // Keep draining events so the sidecar's stdout/stderr pipes never
                // fill and block it; the task ends when the child exits.
                tauri::async_runtime::spawn(async move { while rx.recv().await.is_some() {} });

                // Wait for the socket to actually accept connections (the port
                // line is printed before uvicorn binds).
                for _ in 0..200 {
                    if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }

                let inject =
                    format!("window.__KONDUKTOR_API__ = 'http://127.0.0.1:{port}';");
                WebviewWindowBuilder::new(&handle, "main", WebviewUrl::App("index.html".into()))
                    .title("Konduktor")
                    .inner_size(1400.0, 900.0)
                    .min_inner_size(900.0, 600.0)
                    // On Windows the webview would otherwise be served from
                    // https://tauri.localhost, and fetching the sidecar over
                    // http://127.0.0.1 would be blocked as mixed content. Force
                    // the http custom-protocol scheme so both are http. (No-op on
                    // macOS, which uses the tauri:// scheme.)
                    .use_https_scheme(false)
                    .initialization_script(&inject)
                    .build()
                    .expect("failed to create the main window");
            });

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
