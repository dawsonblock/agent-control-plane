# ACP Desktop (M12)

A Tauri-based desktop wrapper for the Agent Control Plane.

## How it works

The desktop app spawns `acp serve` as a background sidecar process on
startup, then opens a WebView pointing to `http://localhost:8000/ui/`
where the React dashboard is served. When the app window is closed,
the sidecar process is killed.

## Prerequisites

1. [Rust](https://rustup.rs/) (stable)
2. [Tauri CLI](https://tauri.app/v1/guides/getting-started/setup):
   ```bash
   cargo install tauri-cli --version "^2"
   ```
3. ACP installed and on PATH:
   ```bash
   uv sync --extra api --extra rag --extra memory
   ```

## Development

```bash
# Start the ACP server in one terminal
acp serve --config configs/repos/example.repo.yaml

# Run the Tauri dev build in another terminal
cd desktop/src-tauri
cargo tauri dev
```

## Build

```bash
cd desktop/src-tauri
cargo tauri build
```

This produces a platform-specific installer (`.dmg` on macOS, `.msi` on
Windows, `.AppImage` on Linux) in `desktop/src-tauri/target/release/bundle/`.

## Configuration

The sidecar is configured to use `configs/repos/example.repo.yaml` by
default. To use a different config, edit `src/main.rs` and change the
`--config` argument.
