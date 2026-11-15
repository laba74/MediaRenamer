# MediaRenamer

MediaRenamer is a configurable command-line tool that renames photos and videos using metadata, device-specific names, and time offsets.

```text
IMG_20220125_151850_01.jpg  ->  2022-01-25_15-18-42_TelEric.jpg
GH011892.MP4                ->  2025-07-14_17-19-24_GoPro1.MP4
GL011892.LRV                ->  2025-07-14_17-19-24_GoPro1.LRV   (follows its main video)
```

- Reads capture dates and camera make/model through [ExifTool](https://exiftool.org/), run as an external program.
- Gives each camera a short name (its `id`, used in file names), an optional description and a clock correction, stored in `conf/devices.json`; asks for them the first time a new camera is seen.
- Previews by default: nothing is changed until you pass `--apply`.
- Renames in place, or copies to a destination folder and leaves the sources untouched.
- Never writes metadata, never overwrites a file, never uses the file system date, never flattens folders, and does not deduplicate content.

Contents: [Installation](#installation) · [Offline installation](#offline-installation-windows) · [Configuration](#configuration) · [Back up your personal files](#back-up-your-personal-files) · [Usage](#usage) · [Output and log](#output-and-log) · [How files are analyzed](#how-files-are-analyzed) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [License](#license)

## Installation

### 1. Requirements

- Python 3.12 or later.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which manages the Python environment. Every command below goes through `uv`.
- ExifTool (step 3).

### 2. Get the code and its environment

```sh
git clone https://github.com/laba74/MediaRenamer.git
cd MediaRenamer
uv sync
```

`uv sync` creates `.venv/` and installs the development tools (`pytest`) and, on Windows only, the optional `tzdata` time zone database. The program itself only uses the Python standard library and needs no package: without `tzdata`, only the time zone features are disabled (see [Time zone database](#effective-date-clock-offset-and-time-zone)).

### 3. Install ExifTool

ExifTool is not shipped with MediaRenamer. It is looked for, in this order:

1. in `bin/` and its direct subfolders (e.g. `bin/exiftool-13.62_64/`; when several versions are present, the highest version-like folder name wins);
2. on the `PATH`;
3. at the location given by the `EXIFTOOL_PATH` environment variable (the executable itself or its folder).

**Windows.** Download the 64-bit Windows package from [exiftool.org](https://exiftool.org/) and extract it into `bin/`, which gives for example:

```text
bin/exiftool-13.62_64/
├── exiftool(-k).exe      <- rename to exiftool.exe
└── exiftool_files/       <- keep next to the executable
```

Rename `exiftool(-k).exe` to `exiftool.exe`: the `(-k)` variant waits for a key press and would block the program. Keep `exiftool_files/` next to it.

**macOS.** `brew install exiftool`, or the macOS package from exiftool.org.

**Linux.** Install your distribution's package, e.g. `sudo apt install libimage-exiftool-perl` (Debian/Ubuntu) or `sudo dnf install perl-Image-ExifTool` (Fedora).

`bin/` is git-ignored: ExifTool keeps its own license (see [License](#license)).

### 4. Check the installation

```sh
uv run python media_renamer.py --check
```

```text
Created D:\...\MediaRenamer\conf\config.json from its template. Back it up: it is not versioned.
Created D:\...\MediaRenamer\conf\devices.json from its template. Back it up: it is not versioned.
[OK] Configuration: D:\...\MediaRenamer\conf\config.json
     filename_pattern: {YYYY}-{MM}-{DD}_{HH}-{MIN}-{SEC}_{DEVICE}
[OK] Devices: D:\...\MediaRenamer\conf\devices.json (0 device(s))
[OK] Time zone database: 598 zone(s)
[OK] ExifTool 13.62: D:\...\MediaRenamer\bin\exiftool-13.62_64\exiftool.exe
```

On the first run, the personal configuration files are created from their templates (see [Configuration](#configuration)). `--check` exits with a non-zero code and an explicit message when something is wrong.

### 5. Run a first preview

```sh
uv run python media_renamer.py --source "path/to/photos"
```

This only prints what would be done. When a camera is not yet in `conf/devices.json`, the program asks for its id, an optional label and its clock offset first (see [Unknown devices](#unknown-devices)). Run the same command with `--apply` once the plan looks right.

### Offline installation (Windows)

To run MediaRenamer on a Windows machine without internet access, download its Python packages as wheels, together with the standalone `uv` executable, on a connected machine, then install them from that folder. The offline machine only needs Python 3.12 or later; `uv` does not have to be installed there.

**On the connected machine** (with `uv`), from the repository root:

```powershell
# Runtime packages only (tzdata), into wheels\
.\scripts\build_offline_wheels.ps1

# Also the test tools (pytest), to run the test suite offline
.\scripts\build_offline_wheels.ps1 -IncludeDev

# Into another folder (relative to the repository root, or absolute)
.\scripts\build_offline_wheels.ps1 -OutputDir D:\transfer\wheels

# For an ARM64 offline machine (only changes the bundled uv executable)
.\scripts\build_offline_wheels.ps1 -UvArchitecture aarch64

# Without the uv executable, when the offline machine already has uv
.\scripts\build_offline_wheels.ps1 -SkipUv
```

The script:

1. exports the exact versions and hashes locked in `uv.lock` to `wheels\requirements.txt`;
2. downloads the matching wheels next to it (previous wheels in that folder are removed first);
3. downloads the official standalone `uv` archive from the [uv releases](https://github.com/astral-sh/uv/releases), in the same version as the local `uv`, checks it against its published SHA-256, and extracts `uv.exe` into `wheels\uv\`. It is a single executable that runs without installation.

If PowerShell refuses to run the script, start it with `powershell -ExecutionPolicy Bypass -File .\scripts\build_offline_wheels.ps1`. `wheels/` is git-ignored.

Copy to the offline machine:

- the whole repository folder, `wheels\` included (plus your backed-up `conf\config.json` and `conf\devices.json`, see [Back up your personal files](#back-up-your-personal-files));
- ExifTool, extracted into `bin\` as described in [Install ExifTool](#3-install-exiftool);
- the Python 3.12 or later installer (from [python.org](https://www.python.org/downloads/windows/)), if the offline machine does not have Python yet. `uv` cannot download Python without internet: it uses the Python already installed.

**On the offline machine**, in PowerShell, from the repository root:

```powershell
# 0. Use the bundled uv for this PowerShell session (repeat in each new session)
$env:PATH = "$PWD\wheels\uv;$env:PATH"

# 1. Once: create .venv\ and install the wheels, from the local folder only
uv venv --offline
uv pip install --offline --no-index --find-links wheels --requirement wheels\requirements.txt

# 2. Check the installation
uv run --offline --no-sync python media_renamer.py --check

# 3. Preview, then apply
uv run --offline --no-sync python media_renamer.py --source "D:\Photos\2025"
uv run --offline --no-sync python media_renamer.py --source "D:\Photos\2025" --apply

# Tests (only with wheels built with -IncludeDev)
uv run --offline --no-sync pytest
```

`--offline` forbids any network access and `--no-sync` stops `uv run` from syncing the project with `uv.lock`, which would need the packages that were left out (such as `pytest`) and the network. `uv sync` is not used for the same reason. Every option described in [Usage](#usage) works the same way: insert `--offline --no-sync` after `uv run`. To avoid typing them, set them once per PowerShell session with `$env:UV_OFFLINE = "1"; $env:UV_NO_SYNC = "1"`, then use the usual `uv run python media_renamer.py ...` commands.

Without step 0, call the executable by its path instead of `uv`, e.g. `.\wheels\uv\uv.exe run --offline --no-sync python media_renamer.py --check`.

The wheels are platform-specific in general: build them on Windows for a Windows machine. When `uv.lock` changes (new dependency or version), run the script again and copy the new `wheels\` folder.

## Configuration

The configuration lives in `conf/`:

| File | Versioned | Role |
| --- | --- | --- |
| `config.default.json` | yes | Template of `config.json`; never modified by the program. |
| `devices.default.json` | yes | Template of `devices.json` (empty list); never modified by the program. |
| `config.json` | **no** | Your settings. Created from the template on the first run if missing. |
| `devices.json` | **no** | Your cameras. Created from the template on the first run; rewritten each time a new device is registered. |
| `devices.json.bak` | **no** | Copy of `devices.json` taken before the first rewrite of a run. |

An existing personal file is never overwritten by a template. `devices.json` is rewritten atomically (temporary file, then replace), after a `devices.json.bak` copy, so an interruption cannot corrupt it. Both files are validated at every start; any error stops the run before a file is touched.

### `conf/config.json`

```json
{
  "filename_pattern": "{YYYY}-{MM}-{DD}_{HH}-{MIN}-{SEC}_{DEVICE}",
  "supported_extensions": [".mp4", ".mov", ".lrv", ".lrf", ".thm", ".wav", ".mp3", ".jpg", ".jpeg", ".png", ".heic", ".nef", ".arw", ".cr2", ".cr3", ".dng"],
  "dry_run_by_default": true,
  "log_level": "INFO"
}
```

- `filename_pattern` — target name **without extension** (the original extension is kept as is). Allowed tokens:

  | Token | Value |
  | --- | --- |
  | `{YYYY}` `{MM}` `{DD}` | Year (4 digits), month, day of the effective date |
  | `{HH}` `{MIN}` `{SEC}` | Hour (24 h), minute, second of the effective date |
  | `{DEVICE}` | The device `id` |
  | `{ORIGINAL}` | The original file name, without extension |

  Unknown tokens, path separators, characters invalid in file names (`< > : " / \ | ? *`), a leading `.` and a trailing `.` or space are rejected at startup. Device ids must be portable too: no character forbidden on Windows, macOS, Linux or Android memory cards (`< > : " / \ | ? *`, control characters, DEL), and no leading or trailing space or dot. In the rendered name, invalid characters coming from `{ORIGINAL}` are replaced by `_`, the name is normalized to Unicode NFC, and it is cut to 200 UTF-8 bytes (whole characters only) so that, with the collision suffix and the extension, it stays within the 255-byte file name limit of common file systems.
- `supported_extensions` — extensions to process, each starting with a dot; compared case-insensitively. Other files are ignored.
- `dry_run_by_default` — `true` (recommended): preview unless `--apply` is given. `false`: apply unless `--dry-run` is given.
- `log_level` — `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, for `.logs/rename.log`.

### `conf/devices.json`

```json
{
  "devices": [
    {
      "id": "GoPro1",
      "label": "My first GoPro",
      "match": { "model": "HERO9 Black" },
      "offset": "+00:00:00"
    },
    {
      "id": "A7III",
      "label": "Sony Alpha 7 III",
      "match": { "make": "SONY", "model": "ILCE-7M3", "serial_number": "" },
      "offset": "-00:02:30",
      "timezone": "Europe/Paris"
    }
  ]
}
```

- `id` — unique short name (case-insensitive), inserted in file names by `{DEVICE}` and used by `--device`. It must be valid in file names (see above).
- `label` — optional free description for you (e.g. `"Old family phone"`); never used in file names, only shown in messages. May be empty or omitted.
- `match` — any of `make`, `model`, `serial_number`, as reported by ExifTool (`Make`, `Model`, `SerialNumber`). See [Device matching](#device-matching).
- `offset` — signed `HH:MM:SS` added to the file's date to correct the camera clock (`+00:00:00` when the clock is right). See [Effective date](#effective-date-clock-offset-and-time-zone).
- `timezone` — optional IANA zone (e.g. `Europe/Paris`, `UTC`) of the camera clock.

You normally do not edit this file by hand: new cameras are added interactively. Editing it is useful to rename an id or a label, fix an offset or reorder entries; run `--check` afterwards.

## Back up your personal files

> **Warning.** `conf/config.json` and `conf/devices.json` are **not in git** (`.gitignore`) and `devices.json` is **rewritten by the program** each time a new device is registered. They are personal: they hold your camera makes, models, possibly serial numbers, and the clock offsets you worked out. If they are lost, every device and offset must be entered again. A `git clean -x`, a fresh clone or deleting the folder loses them.

Copy them regularly **outside the repository** (backup drive, cloud folder, password manager for serial numbers…), for example:

```sh
# macOS / Linux
cp conf/config.json conf/devices.json ~/Backups/MediaRenamer/
```

```powershell
# Windows (PowerShell)
Copy-Item conf\config.json, conf\devices.json -Destination $HOME\Backups\MediaRenamer\
```

**Restore.** Copy the saved files back into `conf/`, then run `uv run python media_renamer.py --check`.

**Undo the last device change.** `conf/devices.json.bak` holds `devices.json` as it was before the last run that registered devices (all the devices added by that run are undone): copy it over `devices.json`.

**Reset.** Delete (or move away) `conf/config.json` and/or `conf/devices.json`; the next run recreates them from `conf/config.default.json` and `conf/devices.default.json`. Never edit the `*.default.json` templates for your own settings: they are versioned and shared.

## Usage

```text
uv run python media_renamer.py [--source PATH] [--dest PATH] [--apply | --dry-run]
                               [--recursive] [--device ID] [--tz TIMEZONE] [--check]
```

| Option | Effect |
| --- | --- |
| `--source PATH` | Folder to process. Required unless `--check`. |
| `--dest PATH` | Copy the files, under their new names, into this folder (created if needed); sources are left intact. Without it, files are renamed in place. |
| `--apply` | Perform the renames or copies. Without it (and with `dry_run_by_default: true`), only a preview is shown. |
| `--dry-run` | Force a preview, even when `dry_run_by_default` is `false`. Cannot be combined with `--apply`. |
| `--recursive` | Also process every subfolder. Each file stays in its own subfolder; with `--dest`, the relative tree is recreated. |
| `--device ID` | Use the device `ID` of `conf/devices.json` for every file, whatever its metadata. |
| `--tz TIMEZONE` | Reference IANA time zone of the batch (e.g. `Europe/Paris`); dates are then normalized to UTC. |
| `--check` | Validate the configuration, show the ExifTool version and location, and exit. |
| `-h`, `--help` | Show the help. |

The exit code is `0` on success, non-zero when the configuration is invalid, ExifTool is missing, unknown devices cannot be registered, or a file operation failed.

### Examples

```sh
# Preview the renames of one folder (nothing is changed)
uv run python media_renamer.py --source ~/Pictures/2025-summer

# Rename in place, subfolders included
uv run python media_renamer.py --source ~/Pictures/2025-summer --recursive --apply

# Copy to another folder under the new names, keeping the folder tree; sources untouched
uv run python media_renamer.py --source /media/sdcard/DCIM --dest ~/Pictures/import --recursive --apply

# Files from one camera only, whose clock was set to Paris time
uv run python media_renamer.py --source ./gopro --device gopro1 --tz Europe/Paris
```

On Windows, quote paths containing spaces: `--source "D:\My Photos\2025"`.

### What happens to each file

- **Rename in place** (no `--dest`): the file gets its new name in its own folder.
- **Copy** (`--dest`): the file is copied under its new name; with `--recursive`, into the same relative subfolder under the destination. Only folders that receive at least one file are created (no empty folders). A destination inside the source is not scanned.
- **Unchanged**: the file already has its target name.
- **Skipped**: no valid date, no device, orphan auxiliary file… The reason is printed.
- **Collisions**: when two files would get the same name in the same folder, or the name is already taken there, `_01`, `_02`… is added before the extension. Names are compared case-insensitively and regardless of Unicode normalization (`é` composed or decomposed), so the result is safe on Windows and macOS.
- **No overwrite**: an existing file is never replaced; if a target appears between the plan and the operation, the file is reported as an error and left alone.

### Auxiliary files (GoPro, DJI)

`.LRV` (GoPro low-resolution video), `.LRF` (DJI low-resolution video), `.THM` (thumbnail) and camera `.WAV` files (GoPro RAW audio, DJI separate audio track) are not dated on their own: they take the name, date and device of their main video (`.MP4`/`.MOV`) in the **same folder**, and keep their extension. They are matched by identical base name (`GH011892.MP4` / `GH011892.THM`, `DJI_20260307110012_0426_D.MP4` / `DJI_20260307110012_0426_D.WAV`), or by the camera file id that ends the name (`GL011892.LRV` follows `GH011892.MP4`, also when another tool added a prefix to the names). A `.WAV` file is auxiliary only when its name ends with a GoPro or DJI file id; other `.wav` files are regular media. Without a main video in its folder, an auxiliary file is reported and not renamed.

## Output and log

The program first lists each file with its analysis, then the plan, then a summary:

```text
[SKIP] IMG_20220117_202623.jpg  no valid capture date (checked DateTimeOriginal, CreateDate, MediaCreateDate, TrackCreateDate)
[OK]   IMG_20220125_151850_01.jpg  2022-01-25 15:18:42 local  device 'TelEric'  (DateTimeOriginal 2022-01-25 15:18:42)
2 file(s): 1 ready, 0 auxiliary, 1 skipped, 0 warning(s).
Rename plan:
[SKIP]   IMG_20220117_202623.jpg  (no valid capture date (checked DateTimeOriginal, CreateDate, MediaCreateDate, TrackCreateDate))
[RENAME] IMG_20220125_151850_01.jpg -> 2022-01-25_15-18-42_TelEric.jpg
Summary (preview): 1 to rename, 0 to copy, 0 unchanged, 1 skipped, 0 warning(s).
  skipped (1): no valid capture date (checked DateTimeOriginal, CreateDate, MediaCreateDate, TrackCreateDate)
Preview only: no file was changed. Use --apply to perform these operations.
```

Preview and `--apply` use the same plan, so the names applied are exactly those previewed (as long as the folders do not change in between).

Each run is appended to `.logs/rename.log` in the project folder, at the configured `log_level`. `.logs/` is git-ignored and is never scanned. Neither the console nor the log shows GPS data. Serial numbers are always masked: only their last 4 characters are shown, as `***4391`, in the console and in the log; a full serial number never appears.

## How files are analyzed

### File discovery

Only files whose extension is listed in `supported_extensions` (case-insensitive) are read. Without `--recursive`, only the source folder itself is scanned; with it, every subfolder is scanned and each file keeps its relative path. Folders named `.logs` and the `--dest` folder (when it lies inside the source) are skipped.

### Capture date source

ExifTool is asked for these tags only (no GPS or other personal data is read). The first tag holding a valid date wins:

1. `DateTimeOriginal` — when the photo was taken (EXIF).
2. `CreateDate` — EXIF digitization date, or QuickTime creation date for videos.
3. `MediaCreateDate` — QuickTime media creation date.
4. `TrackCreateDate` — QuickTime track creation date.

Sub-seconds are ignored. Zero or impossible dates (e.g. `0000:00:00 00:00:00`) are treated as missing. A file without any valid date is reported and skipped: the file system date is never used.

For `DateTimeOriginal` and `CreateDate`, the EXIF 2.31 offset tags `OffsetTimeOriginal` and `OffsetTimeDigitized` are also read: when present and valid, they give the date's UTC offset.

### Effective date: clock offset and time zone

The date used in file names (the *effective date*) is computed in two steps. The original metadata is never written.

1. **Clock offset.** The device's `offset` (signed `HH:MM:SS`, e.g. `-01:00:00`) is added to the date read from the file. It corrects a camera clock that was wrong (late, early, or left on another zone); it does not replace the time zone. Rollovers to the previous/next day, month, year and leap days are handled.
2. **Time zone.** The corrected date is normalized to UTC **only when its zone is known**, in this order:
   1. a UTC offset stored in the file (`+02:00`, `Z`, or an `OffsetTime*` tag);
   2. `--tz ZONE`, the explicit policy for the whole batch;
   3. the device's optional `timezone` in `conf/devices.json`.

   Otherwise the naive local time of the camera is kept, unchanged. The preview shows `UTC` or `local` after each date, and the zone used with its source.

**Daylight saving time.** When a zone is applied, a local time that occurs twice (clocks set back, e.g. 02:30 on 27 October 2024 in `Europe/Paris`) or never (clocks set forward, e.g. 02:30 on 31 March 2024) cannot be converted reliably. Such files keep their local time and a warning is printed: nothing is converted silently.

**Time zone database.** IANA zones need a time zone database. Linux and macOS provide one; on Windows, the optional `tzdata` package is needed (installed by default by `uv sync`; skipped by `uv sync --no-group timezones`). Without it, the program runs in **degraded mode**: `--tz` and device zones are ignored with a warning and local times are kept (dates stored with an offset in the metadata are still converted to UTC). `--check` reports whether the database is available; its absence is a warning, not a failure. A syntactically invalid `--tz` is still rejected.

### Limits of zone-less metadata

- Most EXIF dates and many video dates carry no zone: they are the camera's wall-clock time, whatever the zone it was set to. Without `--tz` or a device `timezone`, file names use that wall-clock time as is.
- QuickTime dates (`CreateDate`, `MediaCreateDate`, `TrackCreateDate` in videos) are UTC according to the specification, but many cameras (GoPro among them) store local time. ExifTool returns them as they are, and they are treated as zone-less local time. If a camera really writes UTC (DJI Osmo Action cameras do), set its device `timezone` to `UTC`, or adjust its `offset`.
- A zone applies to the whole batch (`--tz`) or to a whole device: photos taken while travelling across zones need separate runs, or the camera clock set to the local zone.

### Device matching

Each file is matched against `conf/devices.json`, in file order; the first matching entry wins. Comparisons are case-insensitive and ignore extra spaces.

- `model`, when set in an entry, must match.
- `make` and `serial_number` are only compared when the file provides them: many videos (e.g. GoPro MP4 files) have a model but no make or serial number.
- A file with neither `Make` nor `Model` but an `Encoder` tag naming a camera (DJI Osmo Action videos, e.g. `DJI OsmoAction5 Pro`) uses that encoder as its model. Other encoders (e.g. `Lavf58.76.100` from re-encoding software) are ignored.
- An empty criterion is ignored.

`--device ID` forces the device for the whole batch.

### Unknown devices

When files come from a device missing in `conf/devices.json`, the tool shows its make and model, how many files it took, their capture date range, a few file names and, when present, the software or firmware that wrote them. It then asks, once per device and before touching any file, for an id (used in file names; it must not already exist), an optional label, a clock offset (signed `HH:MM:SS`), an optional IANA time zone and, when the file has one, whether to match this camera body by its serial number. The answer is saved to `conf/devices.json` (with a `devices.json.bak` copy) and reused for every matching file.

Without an interactive terminal, the run stops with the list of unknown devices: add them to `conf/devices.json`, run in a terminal, or use `--device`. Files with neither make nor model are skipped unless `--device` is given.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): checking ExifTool, inspecting a file's tags, fixing a device entry, adjusting an offset, missing dates, time zones and collisions.

## Running the tests

```sh
uv run pytest
```

## License

MediaRenamer's original code and documentation are released under the [MIT license](LICENSE).

ExifTool is a separate program by Phil Harvey, distributed under its own license (the same terms as Perl: Artistic License or GPL). It is **not** included in this repository and **not** covered by the MIT license; you install it yourself (see [Install ExifTool](#3-install-exiftool)). The optional `tzdata` and the development `pytest` packages installed by `uv sync` also keep their own licenses. See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES).
