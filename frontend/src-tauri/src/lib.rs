// Konduktor desktop shell (Tauri v2).
//
// The backend is the frozen Python sidecar (see backend/sidecar.py): on launch
// we spawn it, read the `KONDUKTOR_PORT=<n>` line it prints on stdout, wait for
// it to actually accept connections, then create the app window with an
// initialization script that hands the frontend its API base
// (`window.__KONDUKTOR_API__`). The sidecar is killed when the app exits.
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Holds the running sidecar child so we can terminate it on exit.
struct Sidecar(Mutex<Option<CommandChild>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
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
        .run(|app_handle, event| {
            // Terminate the sidecar when the app is quitting.
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(child) = app_handle.state::<Sidecar>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
