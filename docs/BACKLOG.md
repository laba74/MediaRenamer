# MediaRenamer — Backlog

Epics and user stories derived from `docs/PLAN.MD`. Each epic ends with a **Done** section: the checks that must all pass before the epic is closed. All commands use `uv` (see `AGENTS.md`).

Global Definition of Done (applies to every story):

- Every function has a Sphinx docstring in English and a pytest unit test.
- Identifiers, comments and docs are in English; 2 blank lines between functions.
- `uv run pytest` passes.

---

## Epic 1 — Configuration and `--check`

Goal: load and validate `conf/config.json` / `conf/devices.json`, locate ExifTool, expose `--check` and `--help`.

- **1.1 CLI skeleton** — As a user, I get `--help` and all options from the plan (`--source`, `--dest`, `--apply`, `--recursive`, `--device`, `--tz`, `--check`) parsed with `argparse`.
- **1.1b Default and personal config files** — As a user, my personal `conf/config.json` and `conf/devices.json` are never versioned and are created from `conf/config.default.json` / `conf/devices.default.json` on first run (message shown, existing files never overwritten). Writes to `devices.json` are atomic (temporary file + replace) and preceded by a `devices.json.bak` copy.
- **1.2 Load `conf/config.json`** — Validate filename pattern, extensions, `dry_run_by_default`, `log_level`.
- **1.3 Filename pattern validation** — Accept only known tokens (`{YYYY}`, `{MM}`, `{DD}`, `{HH}`, `{MIN}`, `{SEC}`, `{DEVICE}`, `{ORIGINAL}`); reject invalid characters and unexpected paths.
- **1.4 Load `conf/devices.json`** — Validate JSON, duplicate `id`s, `match` criteria, signed `HH:MM:SS` offsets, optional IANA timezone.
- **1.5 ExifTool discovery** — Search `bin/` (including versioned subfolders such as `bin/exiftool-13.62_64/`), then `PATH`, then `EXIFTOOL_PATH`; clear error if absent.
- **1.6 `--check`** — Validate configuration and print ExifTool version and location; non-zero exit on failure.

### Done — Epic 1
- [x] `uv run python media_renamer.py --help` lists every option of the plan.
- [x] `conf/config.json` and `conf/devices.json` are git-ignored; the `*.default.json` templates are tracked.
- [x] On first run with missing personal files, they are created from the templates; existing files are left untouched (tested).
- [x] `--check` succeeds with the default `conf/config.json`, empty `conf/devices.json` and ExifTool installed.
- [x] `--check` fails with an explicit message for: invalid JSON, unknown token, bad offset, duplicate device id, missing ExifTool.
- [x] Unit tests cover each validation rule (valid and invalid cases).

---

## Epic 2 — Metadata extraction and device identification

Goal: read metadata through ExifTool and match each file to a device.

- **2.1 File discovery** — List files filtered by configured extensions, case-insensitive; with `--recursive`, scan all subdirectories (keeping each file's relative path); without it, scan only the source folder. Skip `.logs/` and the destination if it lies inside the source.
- **2.2 ExifTool wrapper** — Run ExifTool via `subprocess` (JSON output), batch calls, handle errors and timeouts.
- **2.3 Date source selection** — Choose and document the EXIF date tag priority (e.g. `DateTimeOriginal`, `CreateDate`, `MediaCreateDate`); report files with missing/unreadable date and skip them (never use the system date).
- **2.4 Device matching** — Case-insensitive match on `Make`, `Model`, optional `SerialNumber` (empty serial is not a criterion); first matching entry wins; `--device` forces the device.
- **2.5 Unknown device handling** — Interactive prompt for label and offset, saved to `conf/devices.json`, reused for all matching files of the batch; resolved before any file modification; clear error in non-interactive mode.

### Done — Epic 2
- [x] On a sample folder, discovery returns the expected files (flat and recursive, nested subfolders, relative paths preserved).
- [x] Metadata extracted from real JPG, MP4 and RAW samples; missing-date file reported and skipped.
- [x] Matching tests: case-insensitivity, serial optional, first match wins, `--device` override.
- [x] Unknown device: answer persisted in `conf/devices.json`, no second prompt for the same device, non-interactive run exits with a clear error.
- [x] No source file is modified during this epic.

---

## Epic 3 — Dates, time zones and offsets

Goal: compute the effective date used in names.

- **3.1 Offset parsing and application** — Apply signed `HH:MM:SS` offsets to the retained date; never write to original metadata.
- **3.2 Time zone policy** — Normalize to UTC only when the zone is known or explicit (device IANA zone or `--tz`); otherwise keep the naive local time.
- **3.3 Ambiguous / non-existent times** — Detect DST ambiguities and gaps, warn instead of silently converting.
- **3.4 Date source documentation** — Document the chosen tags and the limits of zone-less metadata.

### Done — Epic 3
- [x] Tests: positive/negative offsets, day/month/year rollover, leap day.
- [x] Tests: known zone, `--tz`, no zone; DST ambiguous and gap cases produce warnings.
- [x] Original files' metadata verified unchanged after a run.
- [x] Documentation section on date sources and limitations written.

---

## Epic 4 — Deterministic rename plan

Goal: build the target names once, shared by preview and apply.

- **4.1 Name builder** — Render the pattern tokens, sanitize invalid characters deterministically.
- **4.2 GoPro auxiliary files** — Link `.LRV` / `.THM` to the main video with the same base name in the same folder; reuse its date and device, keep extension; warn and skip if the main file is absent from that folder.
- **4.3 Collision resolution** — Unique targets with `_01`, `_02`… before the extension, resolved per target directory; account for existing files and case-insensitive file systems.
- **4.4 Plan model** — Immutable plan entries (source, target, action, warnings) produced by a single function.

### Done — Epic 4
- [x] Same input always yields the same plan (determinism test).
- [x] Tests: collisions inside the batch, with existing files, case-only differences.
- [x] Tests: `.LRV`/`.THM` follow the main video; orphan auxiliary is warned and not renamed.
- [x] Tests: same base name in two different subfolders yields no cross-folder pairing and no cross-folder collision suffix.
- [x] Plan generation touches no file.

---

## Epic 5 — Preview and apply

Goal: execute the plan safely.

- **5.1 Preview mode** — Default mode; print the plan, modify nothing.
- **5.2 In-place rename** — With `--apply` and no `--dest`, rename sources; each file stays in its own subfolder.
- **5.3 Copy to destination** — With `--dest`, copy under the computed name and leave sources intact; create the destination if needed. With `--recursive`, recreate the original relative tree under the destination, creating only directories that receive at least one file (no empty directories, also in preview).
- **5.4 Overwrite protection** — Never overwrite an existing file; fail or skip with a clear message.
- **5.5 Config default** — Honor `dry_run_by_default` from `conf/config.json`; `--apply` forces execution and `--dry-run` forces a preview (mutually exclusive).

### Done — Epic 5
- [x] Preview and apply produce identical target names (same plan function).
- [x] Integration test with `tmp_path`: in-place rename, copy with sources intact, no overwrite.
- [x] Integration test with `tmp_path` and `--recursive`: in-place rename keeps subfolders; copy to `--dest` recreates the source tree and creates no empty directory (including subfolders with no supported file).
- [x] Unknown devices are resolved before the first file operation.
- [ ] Manual run on a real sample folder gives the expected result.

---

## Epic 6 — Reporting and logging

Goal: clear run summary and a safe log.

- **6.1 Run summary** — Counts of renamed/copied/skipped/errors/warnings, with reasons.
- **6.2 Log file** — Write `.logs/rename.log` using the configured `log_level`.
- **6.3 Privacy** — Do not expose unnecessary personal metadata (GPS, serial numbers, etc.) in output or log.

### Done — Epic 6
- [ ] Summary matches the actual operations in an integration test.
- [ ] `.logs/rename.log` created, `.logs/` stays git-ignored.
- [ ] Test asserting no GPS/serial values appear in logs.

---

## Epic 7 — Documentation and troubleshooting

Goal: maintainable, English documentation.

- **7.1 README** — Installation (`uv sync`), ExifTool setup, usage, options, examples.
- **7.1b Backup warning** — README section explaining that `conf/config.json` and `conf/devices.json` are personal, not in git, rewritten by the tool, and must be backed up outside the repository (and how to restore or reset from the `*.default.json` templates).
- **7.2 Troubleshooting guide** — Verify ExifTool, inspect a file's tags, fix a device entry, adjust an offset, diagnose missing date, time zones and collisions.
- **7.3 Licensing** — Keep `LICENSE` (MIT) and `THIRD-PARTY-NOTICES` accurate.

### Done — Epic 7
- [ ] A new user can install and run a preview following only the README.
- [ ] The README tells the user to back up `conf/config.json` and `conf/devices.json`.
- [ ] Each troubleshooting topic of the plan is covered.
- [ ] Licensing statement consistent with the plan (MIT for original code, ExifTool under its own license).

---

## Epic 8 — Quality and hardening

Goal: validate error cases and platforms.

- **8.1 Error-case tests** — Corrupt files, unreadable metadata, invalid configs, permission errors.
- **8.2 Date format tests** — Variants of EXIF/QuickTime date strings, missing sub-seconds.
- **8.3 File system tests** — Case-insensitive collisions, long names, special characters (Windows, macOS, Linux).
- **8.4 Coverage check** — Verify every function has a unit test.

### Done — Epic 8
- [ ] `uv run pytest` is green on Windows and one POSIX system.
- [ ] Every function in `media_renamer.py` has at least one test.
- [ ] End-to-end run on a mixed sample (photos, videos, GoPro files, unknown device) validated manually.
