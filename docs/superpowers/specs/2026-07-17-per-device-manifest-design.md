# Per-device manifest (device-id scoped incremental export)

Date: 2026-07-17

## Problem

`export_sessions.py` runs incrementally, tracking each exported session in a
single `<vault>/.export_manifest.json`. The manifest key for a session is its
**local absolute path** (e.g. `key = jsonl` in `claude_code_tasks`,
`export_sessions.py:469`).

When several machines export into one **shared / synced output vault**, they all
read and write the same `.export_manifest.json`. On a second machine,
`run_tasks` only adds *that machine's* local session keys to the `seen` set, so
`prune_manifest` (`export_sessions.py:888`) treats the *other* machine's
sessions as "source vanished" and **deletes their output files**. The
vendor-level `vendor_source_present` guard does not help once both machines have
the same vendor installed. Net effect: devices clobber each other's exports, and
concurrent writes to the one manifest file also cause sync conflicts.

## Goal

Attribute every exported session to the **device** that produced it, so that
pruning and filename-collision handling are device-scoped. No device may delete
or overwrite another device's exports. Model the solution on the device-id
algorithm already used in `~/git/mdeditor`.

## Reference: mdeditor's algorithm

- **Device id** — a persisted random UUID. `crypto.randomUUID()` generated once
  and stored under `device.id` (`src/lib/settings.svelte.ts:167-172`,
  `getDeviceId()` at `:238`).
- **Device name** — `hostname()`, falling back to `Device-<id first 8 chars>`
  (`src/lib/recent-sync.svelte.ts:63`).
- **Per-device files** — each device writes only its own `${deviceId}.json` into
  a shared directory; on read it loads every `*.json` except its own and merges,
  skipping corrupt/partial files (`recent-sync.svelte.ts:41-53,69`).

We port this pattern directly.

## Design

### 1. Device identity

New helpers (in `export_sessions.py`):

- `device_id() -> str`
  - Read from a local, **non-synced, non-git** state file:
    `~/.config/codexclaudeappsessions/device_id`.
  - If absent: generate `uuid.uuid4()`, create the directory, write the file,
    return it. First-run generation only.
- `device_name() -> str`
  - `socket.gethostname()`, falling back to `Device-<device_id()[:8]>` on error
    or empty result.

The id file lives outside the shared vault and outside the git repo, so it is
never synced between machines — each machine keeps its own stable id.

### 2. Manifest layout

Replace the single file with a directory of per-device files:

```
<vault>/.export_manifest/<deviceid>.json
```

Each file keeps the existing shape plus two identity fields:

```json
{
  "version": 1,
  "deviceId": "<uuid>",
  "deviceName": "<hostname>",
  "sessions": { "<key>": { "version": "...", "vendor": "...", "row": { ... } } }
}
```

`MANIFEST_NAME` becomes the directory name `.export_manifest`.
`manifest_path(out_root)` returns
`<out_root>/.export_manifest/<device_id()>.json`.

### 3. Read / write responsibilities

- `load_manifest(out_root)` — load **only this device's** file; on missing or
  corrupt file return a fresh `{"version": 1, "deviceId": ..., "deviceName": ...,
  "sessions": {}}`.
- `load_all_manifests(out_root) -> list[dict]` — new. Load **every**
  `<vault>/.export_manifest/*.json`, skipping corrupt/partial files (mirrors
  mdeditor's tolerant read). Returns the parsed docs.
- `seed_used_from_manifest` — iterate the sessions of **all** device manifests,
  so a new session on device B never picks a filename already used by device A
  (prevents cross-device output overwrite via the existing `_USED` de-dup in
  `emit`).
- `manifest_rows` — iterate **all** device manifests (union), so `index.md` and
  the per-project `_project.md` files aggregate every device's sessions. Keep the
  existing "output file still exists on disk" filter.
- `save_manifest` — write **only this device's** file (create the
  `.export_manifest/` directory if needed), including `deviceId` / `deviceName`.
- `prune_manifest` — unchanged in logic; because the `manifest` it receives is
  this device's file only, it can never delete another device's entries. This is
  the fix.

`vendor_source_present` stays: it still correctly protects a single device that
has temporarily lost a vendor (e.g. uninstalled Codex) from self-pruning.

### 4. Migration from the legacy single file

On first run of the new code, if `<vault>/.export_manifest.json` (the old flat
file) exists:

1. Do **not** adopt its entries into this device's manifest (it may contain
   sessions produced by other devices, which we cannot attribute).
2. This device rebuilds its own manifest by re-scanning local sources normally.
   Output files already on disk are overwritten idempotently by `emit`, so this
   is a one-time full re-render on each device, then incremental thereafter.
3. Delete the legacy `.export_manifest.json` after a successful run so it is not
   mistaken for live state.

Other devices' output files that this device does not produce are simply never
referenced by this device's manifest, and `prune_manifest` only touches this
device's file — so they are left untouched.

### 5. `main()` wiring

- `manifest = load_manifest(output)` — this device's file (unchanged call site).
- `seed_used_from_manifest(output)` — now seeds from all device manifests
  (signature can drop the per-device `manifest` arg or ignore it; it reads all).
- `rows = manifest_rows(output)` — union across all device manifests.
- `save_manifest(output, manifest)` — this device's file.
- Legacy-file migration cleanup runs after `save_manifest`.

## Data flow

```
device_id()  ──> manifest_path(vault) = vault/.export_manifest/<id>.json
                     │
load_manifest ───────┘  (this device only)   ──> run_tasks / prune (this device)
load_all_manifests ─────> seed_used + manifest_rows (all devices)  ──> index.md, _project.md
save_manifest ──────────> vault/.export_manifest/<id>.json  (this device only)
```

## Error handling

- Missing/corrupt device manifest → treated as empty, skipped in the union read
  (never aborts the run).
- Cannot create `~/.config/codexclaudeappsessions/` or write the id file → fall
  back to an in-memory id derived from `device_name()` for this run and warn on
  stderr (degrades to hostname-scoped rather than crashing).
- `.export_manifest/` directory creation is `exist_ok=True`.

## Testing

- Unit: `device_id()` generates once and is stable across calls; `device_name()`
  falls back when hostname is empty.
- Two-device simulation: run with device id A (export sessions), then with device
  id B against the same vault; assert B's run does **not** delete A's output
  files and A's rows still appear in `index.md`.
- Filename collision: two devices that would emit the same output path get
  distinct files (suffix), not an overwrite.
- Migration: a pre-existing `.export_manifest.json` is consumed and removed;
  output files survive; the new `.export_manifest/<id>.json` is created.

## Out of scope

- Changing the session output directory structure or filenames.
- A UI/report of which device produced which session (deviceName is stored in the
  manifest for future use but not surfaced in `index.md` in this change).
