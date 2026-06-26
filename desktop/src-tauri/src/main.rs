// ACP Desktop — Tauri main entry point.
//
// This wrapper spawns the ACP FastAPI server (`acp serve`) as a
// background sidecar process on startup, then points the WebView
// to localhost:8000/ui/ where the React dashboard is served.
//
// When the app window is closed, the sidecar process is killed so
// no orphaned server processes remain.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;

struct SidecarState {
    child: Mutex<Option<Child>>,
}

fn main() {
    // Spawn the ACP server as a sidecar process.
    // The server serves the React UI at http://localhost:8000/ui/
    let child = Command::new("acp")
        .arg("serve")
        .arg("--config")
        .arg("configs/repos/example.repo.yaml")
        .spawn()
        .expect("failed to start acp serve sidecar");

    let sidecar = SidecarState {
        child: Mutex::new(Some(child)),
    };

    tauri::Builder::default()
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
