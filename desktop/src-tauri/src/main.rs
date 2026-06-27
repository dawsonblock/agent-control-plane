// ACP Desktop — Tauri main entry point.
//
// This wrapper spawns the ACP FastAPI server (`acp serve`) as a
// background sidecar process on startup, then points the WebView
// to localhost:8000/ui/ where the React dashboard is served.
//
// When the app window is closed, the sidecar process is killed so
// no orphaned server processes remain.
//
// v0.7.5: Config path is now configurable via ACP_CONFIG env var
// or --config CLI flag. Auto-update support via tauri-plugin-updater.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::process::{Child, Command};
use std::sync::Mutex;

struct SidecarState {
    child: Mutex<Option<Child>>,
}

/// Determine the config file path.
///
/// Priority:
/// 1. --config CLI flag passed to the desktop app
/// 2. ACP_CONFIG environment variable
/// 3. Default: configs/repos/example.repo.yaml
fn get_config_path() -> String {
    // Check CLI args for --config <path>
    let args: Vec<String> = env::args().collect();
    for i in 0..args.len() {
        if args[i] == "--config" && i + 1 < args.len() {
            return args[i + 1].clone();
        }
        // Also handle --config=path syntax
        if args[i].starts_with("--config=") {
            return args[i]["--config=".len()..].to_string();
        }
    }

    // Check environment variable
    if let Ok(config) = env::var("ACP_CONFIG") {
        if !config.is_empty() {
            return config;
        }
    }

    // Default
    "configs/repos/example.repo.yaml".to_string()
}

/// Wait for the ACP server to be ready by polling the health endpoint.
fn wait_for_server(timeout_secs: u64) {
    let start = std::time::Instant::now();
    let client = reqwest::blocking::Client::new();
    while start.elapsed().as_secs() < timeout_secs {
        if client
            .get("http://localhost:8000/health")
            .send()
            .is_ok()
        {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    // Timeout — proceed anyway; the WebView will show a connection error.
    eprintln!("ACP Desktop: server did not become ready within {}s", timeout_secs);
}

fn main() {
    let config_path = get_config_path();

    // Spawn the ACP server as a sidecar process.
    // The server serves the React UI at http://localhost:8000/ui/
    let child = Command::new("acp")
        .arg("serve")
        .arg("--config")
        .arg(&config_path)
        .spawn()
        .expect("failed to start acp serve sidecar");

    let sidecar = SidecarState {
        child: Mutex::new(Some(child)),
    };

    // Wait for the server to be ready before opening the WebView.
    // This prevents a flash of "connection refused" on startup.
    wait_for_server(30);

    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(sidecar)
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Kill the sidecar when the window is closed.
                let state: tauri::State<SidecarState> = window.app_handle().state();
                let mut child_guard = state.child.lock().unwrap();
                if let Some(mut child) = child_guard.take() {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running ACP Desktop");
}
