# MediaRenamer — Troubleshooting

Start with a preview (no `--apply`) and `--check`: neither changes any file. Every run is also recorded in `.logs/rename.log` (set `"log_level": "DEBUG"` in `conf/config.json` for more detail).

In the commands below, `exiftool` stands for your ExifTool executable. With ExifTool in `bin/` on Windows, use its path instead, e.g. `bin\exiftool-13.62_64\exiftool.exe`.

- [Verify ExifTool](#verify-exiftool)
- [Inspect a file's tags](#inspect-a-files-tags)
- [Fix a device entry](#fix-a-device-entry)
- [Adjust a clock offset](#adjust-a-clock-offset)
- [Diagnose a missing date](#diagnose-a-missing-date)
- [Time zones](#time-zones)
- [Collisions and `_01` suffixes](#collisions-and-_01-suffixes)
- [Configuration errors](#configuration-errors)
- [Other messages](#other-messages)

## Verify ExifTool

```sh
uv run python media_renamer.py --check
```

The last line shows the ExifTool version and the executable used, e.g. `[OK] ExifTool 13.62: ...\bin\exiftool-13.62_64\exiftool.exe`. ExifTool is searched in `bin/` (and its direct subfolders, highest version first), then on the `PATH`, then in `EXIFTOOL_PATH`.

| Symptom | Fix |
| --- | --- |
| `ExifTool not found. Looked in ...` | Install ExifTool (see the README), or set `EXIFTOOL_PATH` to the executable or its folder. |
| `... Found ...exiftool(-k).exe: rename it to 'exiftool.exe'` | Rename the Windows `exiftool(-k).exe` to `exiftool.exe`. The `(-k)` variant waits for a key press. |
| The wrong version is used | `bin/` wins over `PATH`: remove old folders from `bin/`, or check which folder name sorts highest. |
| Timeout or start error on Windows | Keep `exiftool_files/` next to `exiftool.exe`; check that an antivirus does not block it. |
| `exiftool` works in a shell but not in the program | The program reads the `PATH` of the process that starts `uv`; open a new terminal after changing it, or use `EXIFTOOL_PATH`. |

Run ExifTool directly to be sure it works:

```sh
exiftool -ver
```

## Inspect a file's tags

MediaRenamer reads only these tags: `DateTimeOriginal`, `CreateDate`, `MediaCreateDate`, `TrackCreateDate` (dates, in this priority), `OffsetTimeOriginal`, `OffsetTimeDigitized` (UTC offsets), `Make`, `Model`, `SerialNumber` (device identification), and `Software`, `FirmwareVersion` (only shown to help recognize an unknown device, never used for matching). To see what ExifTool returns for one file:

```sh
exiftool -s -DateTimeOriginal -CreateDate -MediaCreateDate -TrackCreateDate -OffsetTimeOriginal -OffsetTimeDigitized -Make -Model -SerialNumber -Software -FirmwareVersion FILE
```

To list every date of the file with the group it comes from:

```sh
exiftool -a -G1 -s -time:all FILE
```

`-s` prints tag names exactly as the program uses them. Note: these commands show the full serial number; do not paste their output publicly.

## Fix a device entry

Devices are stored in `conf/devices.json` (see the README for the format). Back it up before editing it by hand, then run `uv run python media_renamer.py --check`.

| Problem | Fix |
| --- | --- |
| Wrong label in the names | Change `label`. Already renamed files keep their old names; run again to rename them (the pattern must still match). |
| A typo was entered when the device was registered | Edit the entry, or restore `conf/devices.json.bak` (the version before the last run that registered devices). |
| Files go to the wrong device | Entries are tried in file order and the first match wins: move the more specific entry (with `make` and `serial_number`) above the generic one. |
| Two bodies of the same model need different labels or offsets | Add `serial_number` to each entry's `match`. Files without a serial number (many videos) only match entries whose `model`/`make` match, so keep a generic entry last for them. |
| A device is asked again at every run | Its `Make`/`Model` in the files differ from `match`: compare them with [Inspect a file's tags](#inspect-a-files-tags). Comparisons ignore case and extra spaces, not other differences. |
| `duplicate device id` | Each `id` must be unique; rename one of them. |
| Start over for a device | Delete its entry: the program asks again at the next run. |
| Run without a terminal (script, scheduler) | Unknown devices cannot be asked: add them to `conf/devices.json` first, or use `--device ID`. |

`--device ID` forces one device for the whole batch: useful for files with no `Make`/`Model` at all, or a folder known to come from one camera.

## Adjust a clock offset

The `offset` of a device is **added** to the date read from the file, to correct a camera clock that was wrong. It is a signed `HH:MM:SS` (`+01:00:00`, `-00:02:30`, `+24:00:00` for a whole day).

1. Find a file whose real time you know (e.g. a photo of a clock, or of a phone screen showing the time).
2. Compare it with the date shown in the preview, e.g. `(DateTimeOriginal 2025-07-14 17:19:24)`.
3. `offset = real time - camera time`. If the camera shows 17:19:24 and the real time was 18:19:00, the offset is `+00:59:36`. If the camera is ahead, the offset is negative.
4. Set it in `conf/devices.json`, then preview again: the effective date (first date of each `[OK]` line) is corrected, the original date between parentheses is not.

The offset fixes the clock, it is not a time zone: for a camera left on another zone, prefer its `timezone` or `--tz` (see below) if you want UTC names; use an offset if you want local names. Metadata is never modified: changing the offset and running again is always safe.

## Diagnose a missing date

A file reported `no valid capture date (checked DateTimeOriginal, CreateDate, MediaCreateDate, TrackCreateDate)` is skipped: the program never falls back to the file system date.

1. [Inspect its tags](#inspect-a-files-tags). Typical causes:
   - none of the four tags is present (screenshots, images saved by messaging apps, edited exports that dropped EXIF);
   - the date is zero or impossible (`0000:00:00 00:00:00`), which some cameras write when their clock was never set;
   - the file is corrupt or unreadable: it is reported as `unreadable metadata (...)` with ExifTool's error.
2. If the file has no usable date, leave it, rename it by hand, or write a date into it yourself with ExifTool (MediaRenamer never does), e.g. `exiftool -DateTimeOriginal="2022:01:17 20:26:23" FILE` (ExifTool keeps a `FILE_original` copy).
3. If a date exists under another tag (see `-time:all`), it is not used on purpose: such tags are often file system or edit dates, not capture dates.

A file reported `no Make/Model metadata to identify the device (use --device)` has a date but no camera identification: use `--device ID`.

## Time zones

Without a zone, names use the camera's wall-clock time, marked `local` in the preview. A date is normalized to UTC (marked `UTC`) only when its zone is known: a UTC offset stored in the file, then `--tz`, then the device `timezone`.

| Symptom | Explanation / fix |
| --- | --- |
| Videos are a few hours off compared with photos | QuickTime dates are UTC in theory, but many cameras (GoPro included) write local time. They are treated as local. If a camera really writes UTC, set its `timezone` to `UTC`; otherwise correct it with its `offset`. |
| Some files are `UTC`, others `local` in one batch | Files with a stored UTC offset (`OffsetTime*` tags, many phones) are always converted; give `--tz` or the device `timezone` so the others are converted too. |
| `... occurs twice (clocks set back) in Europe/Paris: local time kept` | The local time exists twice on the night clocks go back; it cannot be converted reliably. The file keeps its local time; rename it by hand if needed. |
| `... does not exist (clocks set forward) ...` | Same, for the hour skipped when clocks go forward (usually a wrong camera clock). |
| `time zone '...' not found in the time zone database` | Use a valid IANA name (`Europe/Paris`, `America/New_York`, `UTC`), not an abbreviation such as `CET`. |
| `--check` warns that the time zone database is missing | On Windows, run `uv sync` to install the optional `tzdata` package. Without it, `--tz` and device zones are ignored (degraded mode); everything else works. |
| A trip across zones | A zone applies to the whole batch or device: run once per zone, each with its own `--tz`. |

## Collisions and `_01` suffixes

`_01`, `_02`… is added before the extension when the target name is already planned for another file, or already exists, **in the same target folder** (names compared case-insensitively and regardless of Unicode normalization).

| Symptom | Explanation / fix |
| --- | --- |
| Burst photos get `_01`, `_02` | Several shots in the same second: the pattern has a one-second resolution. Expected; add `{ORIGINAL}` to the pattern to keep them apart. |
| Photos from two cameras collide | Add `{DEVICE}` to the pattern (it is in the default one). |
| A second run adds `_01` to files already renamed | Not expected: a file whose name is already its target is `unchanged`. Check that the device label, offset, zone and pattern did not change between runs. |
| Files with only a case difference (`a.JPG` / `A.jpg`) get suffixes | They would overwrite each other on Windows and macOS, so they are kept apart on every system. |
| Two long names get `_01` although their originals differ | Rendered names are cut to 200 UTF-8 bytes; when `{ORIGINAL}` is long, the end that differs may be cut. Shorten the pattern or the original names. |
| `NotADirectoryError: a file stands where a folder is needed` | With `--dest`, a file in the destination has the name of a subfolder to create. Rename or move that file. |
| `PermissionError: ...` | The file is open in another program (Windows) or its folder is read-only. Only that file fails; close the program or fix the permissions and run again. |
| `target already exists, not overwritten` | A file appeared at the target after the plan was built (another program, or two runs at once). Nothing was overwritten; run again. |
| GoPro `.LRV`/`.THM` get a different suffix than their video | They never do: a main video and its auxiliary files always share one name. If they do not, they were not paired — see below. |

Files in different folders never collide with each other: each folder (or each destination subfolder with `--recursive`) is resolved on its own.

## Configuration errors

`--check` and every run validate `conf/config.json` and `conf/devices.json` first; an error stops the run before any file is touched, with a message naming the file and the problem:

- invalid JSON (missing comma or quote, trailing comma): the message gives the line and column;
- unknown token in `filename_pattern` (only `{YYYY}` `{MM}` `{DD}` `{HH}` `{MIN}` `{SEC}` `{DEVICE}` `{ORIGINAL}`), path separators or invalid characters;
- an extension without a leading dot, or an unknown `log_level`;
- an offset not in signed `HH:MM:SS` form (minutes and seconds `00`–`59`), a duplicate device `id`, a `match` without `make` or `model`, a label with invalid file name characters or a leading/trailing space or dot, an unknown field.

If a personal file is beyond repair, restore your backup or reset it from its template (see "Back up your personal files" in the README).

## Other messages

| Message | Meaning |
| --- | --- |
| `no main video with the same base name in its folder; not renamed` | A GoPro `.LRV`/`.THM`/`.WAV` without its `.MP4`/`.MOV` in the same folder. Process the folder that holds both, or rename it by hand. |
| `main video ... is skipped; not renamed` | The auxiliary file follows a video that has no valid date or device: fix the video first. |
| `another .LRV file already follows ...; not renamed` | Two auxiliary files of the same type (e.g. `GH011892.LRV` and `GL011892.LRV`) match one video: only the first one, in name order, follows it. Rename the other by hand or remove the duplicate. |
| `[WARNING] Log file disabled: cannot write ...` | `.logs/` is not writable; the run continues without a log. |
| `N unknown device(s) and no interactive terminal to register them` | See [Fix a device entry](#fix-a-device-entry). |
| `--source: folder not found: PATH` | Check `--source` (quote paths with spaces). |
