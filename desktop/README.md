# ACP Desktop (M12)

A Tauri-based desktop wrapper for the Agent Control Plane.

## How it works

The desktop app spawns `acp serve` as a background sidecar process on
startup, waits for the server to be ready (polls `/health`), then opens
a WebView pointing to `http://localhost:8000/ui/` where the React
dashboard is served. When the app window is closed, the sidecar process
is killed so no orphaned server processes remain.

## Prerequisites

1. [Rust](https://rustup.rs/) (stable)
2. [Tauri CLI](https://tauri.app/v2/guides/getting-started/setup):
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

The sidecar config path is determined in priority order:

1. `--config <path>` CLI flag passed to the desktop app
2. `ACP_CONFIG` environment variable
3. Default: `configs/repos/example.repo.yaml`

```bash
# Use a custom config via env var
ACP_CONFIG=configs/repos/my.repo.yaml cargo tauri dev

# Or via CLI flag
cargo tauri dev -- --config configs/repos/my.repo.yaml
```

## Auto-Update (v0.7.5)

The app includes the Tauri updater plugin. When a new release is published
to GitHub Releases with a `latest.json` manifest, the app will check for
updates on startup and prompt the user to install.

To enable signed updates, generate a keypair and set the `pubkey` in
`tauri.conf.json`:

```bash
cargo tauri signer generate -w ~/.tauri/acp.key
# Copy the public key into tauri.conf.json plugins.updater.pubkey
# Set TAURI_SIGNING_PRIVATE_KEY env var when building releases
```

## Code Signing

### macOS

Set `APPLE_SIGNING_IDENTITY` env var and update `tauri.conf.json`:

```json
"macOS": {
  "signingIdentity": "Developer ID Application: Your Name (XXXXXXXXXX)"
}
```

### Windows

Set `TAURI_SIGNING_PRIVATE_KEY` and update `tauri.conf.json`:

```json
"windows": {
  "certificateThumbprint": "ABC123..."
}
```
