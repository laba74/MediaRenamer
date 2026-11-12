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

**Time zone database.** IANA zones need Python 3.9+ and a time zone database. Linux and macOS provide one; on Windows, the `tzdata` package is needed (installed by `uv sync`). Without it, `--tz` is rejected and device zones produce a warning while the local time is kept.

### Limits of zone-less metadata

- Most EXIF dates and many video dates carry no zone: they are the camera's wall-clock time, whatever the zone it was set to. Without `--tz` or a device `timezone`, file names use that wall-clock time as is.
- QuickTime dates (`CreateDate`, `MediaCreateDate`, `TrackCreateDate` in videos) are UTC according to the specification, but many cameras (GoPro among them) store local time. ExifTool returns them as they are, and they are treated as zone-less local time. If a camera really writes UTC, set its device `timezone` to `UTC`, or adjust its `offset`.
- A zone applies to the whole batch (`--tz`) or to a whole device: photos taken while travelling across zones need separate runs, or the camera clock set to the local zone.

### Device matching

Each file is matched against `conf/devices.json`, in file order; the first matching entry wins. Comparisons are case-insensitive and ignore extra spaces.

- `model`, when set in an entry, must match.
- `make` and `serial_number` are only compared when the file provides them: many videos (e.g. GoPro MP4 files) have a model but no make or serial number.
- An empty criterion is ignored.

`--device ID` forces the device for the whole batch.

### Unknown devices

When files come from a device missing in `conf/devices.json`, the tool asks, once per device and before touching any file, for a label, a clock offset (signed `HH:MM:SS`), an optional IANA time zone and, when the file has one, whether to match this camera body by its serial number. The answer is saved to `conf/devices.json` (with a `devices.json.bak` copy) and reused for every matching file.

Without an interactive terminal, the run stops with the list of unknown devices: add them to `conf/devices.json`, run in a terminal, or use `--device`. Files with neither make nor model are skipped unless `--device` is given.
