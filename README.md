# MediaRenamer
MediaRenamer is a configurable command-line tool that renames photos and videos using metadata, device-specific labels, and time offsets.

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

Most of these tags carry no time zone. QuickTime dates are often UTC in theory, but many cameras (GoPro among them) store local time. Device offsets and time zone handling are covered in a later step.

### Device matching

Each file is matched against `conf/devices.json`, in file order; the first matching entry wins. Comparisons are case-insensitive and ignore extra spaces.

- `model`, when set in an entry, must match.
- `make` and `serial_number` are only compared when the file provides them: many videos (e.g. GoPro MP4 files) have a model but no make or serial number.
- An empty criterion is ignored.

`--device ID` forces the device for the whole batch.

### Unknown devices

When files come from a device missing in `conf/devices.json`, the tool asks, once per device and before touching any file, for a label, a clock offset (signed `HH:MM:SS`), an optional IANA time zone and, when the file has one, whether to match this camera body by its serial number. The answer is saved to `conf/devices.json` (with a `devices.json.bak` copy) and reused for every matching file.

Without an interactive terminal, the run stops with the list of unknown devices: add them to `conf/devices.json`, run in a terminal, or use `--device`. Files with neither make nor model are skipped unless `--device` is given.
