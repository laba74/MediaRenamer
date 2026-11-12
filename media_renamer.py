"""MediaRenamer entry point: renames photos and videos from their metadata."""

import argparse
import errno
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    TextIO,
    Tuple,
    Union,
)

try:
    import zoneinfo
except ImportError:  # pragma: no cover - Python 3.8 has no zoneinfo module
    zoneinfo = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent
CONF_DIR_NAME = "conf"
BIN_DIR_NAME = "bin"
CONFIG_FILE_NAME = "config.json"
DEVICES_FILE_NAME = "devices.json"
PERSONAL_FILES = (
    ("config.default.json", CONFIG_FILE_NAME),
    ("devices.default.json", DEVICES_FILE_NAME),
)

KNOWN_TOKENS = ("YYYY", "MM", "DD", "HH", "MIN", "SEC", "DEVICE", "ORIGINAL")
TOKEN_RE = re.compile(r"\{([^{}]*)\}")
INVALID_FILENAME_CHARS = '<>:"/\\|?*'
CONFIG_KEYS = ("filename_pattern", "supported_extensions", "dry_run_by_default", "log_level")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]+$")

DEVICE_REQUIRED_KEYS = ("id", "label", "match", "offset")
DEVICE_OPTIONAL_KEYS = ("timezone",)
MATCH_KEYS = ("make", "model", "serial_number")
OFFSET_RE = re.compile(r"^([+-])?(\d{2,}):([0-5]\d):([0-5]\d)$")
TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")

EXIFTOOL_ENV_VAR = "EXIFTOOL_PATH"
EXIFTOOL_KEEP_OPEN_GLOB = "exiftool(-k)*"
EXIFTOOL_TIMEOUT_SECONDS = 30
EXIFTOOL_SECONDS_PER_FILE = 5
EXIFTOOL_BATCH_SIZE = 100

LOGS_DIR_NAME = ".logs"
LOG_FILE_NAME = "rename.log"
LOGGER_NAME = "media_renamer"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# Shorter secrets are not redacted from the log: they could match dates or counters.
MIN_REDACTED_LENGTH = 5
AUXILIARY_EXTENSIONS = (".lrv", ".thm")
# GoPro "RAW audio" files (GH<n>.WAV) follow their main video too, but only
# when their name holds a GoPro file id: other .wav files are regular media.
GOPRO_AUDIO_EXTENSIONS = (".wav",)
# GoPro file id at the end of a base name (GOPR0123, GP010123, GH010123,
# GX010123, GL010123), possibly after a prefix added by another tool.
GOPRO_FILE_ID_RE = re.compile(r"(?<![a-z0-9])(gopr\d{4}|g[phxl]\d{6})$")

# Capture date tags, by decreasing priority. The first one holding a valid date
# wins. File system dates (FileModifyDate...) are never used.
DATE_TAGS = ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate")
# EXIF 2.31 offset tags giving the UTC offset of a zone-less date tag.
DATE_OFFSET_TAGS = {"DateTimeOriginal": "OffsetTimeOriginal", "CreateDate": "OffsetTimeDigitized"}
IDENTITY_TAGS = ("Make", "Model", "SerialNumber")
# Shown to help the user recognize an unknown device, never used for matching
# (a firmware update would otherwise look like a new device). First non-empty wins.
SOFTWARE_TAGS = ("Software", "FirmwareVersion")
# Only these tags are requested from ExifTool, so GPS and other personal data
# never enter the program.
METADATA_TAGS = DATE_TAGS + tuple(DATE_OFFSET_TAGS.values()) + IDENTITY_TAGS + SOFTWARE_TAGS + ("Error",)
# Number of example file names shown for an unknown device.
UNKNOWN_DEVICE_EXAMPLES = 3
EXIF_DATE_RE = re.compile(
    r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$"
)
UTC_OFFSET_RE = re.compile(r"^(Z|([+-])(\d{2}):?(\d{2}))$")

LOCAL_TIME_VALID = "valid"
LOCAL_TIME_AMBIGUOUS = "ambiguous"
LOCAL_TIME_NONEXISTENT = "nonexistent"
ZONE_SOURCE_METADATA = "metadata"
ZONE_SOURCE_CLI = "--tz"
ZONE_SOURCE_DEVICE = "device"

STATUS_READY = "ready"
STATUS_AUXILIARY = "auxiliary"
STATUS_SKIPPED = "skipped"
STATUS_UNKNOWN_DEVICE = "unknown_device"
DEFAULT_OFFSET = "+00:00:00"

ACTION_RENAME = "rename"
ACTION_COPY = "copy"
ACTION_UNCHANGED = "unchanged"
ACTION_SKIP = "skip"
# Extensions of the main videos that GoPro auxiliary files (.LRV, .THM, .WAV) follow.
MAIN_VIDEO_EXTENSIONS = (".mp4", ".mov")
# GoPro HERO6+ names the low-resolution video GL<n>.LRV next to GH<n>.MP4 / GX<n>.MP4.
GOPRO_LRV_PREFIX = "gl"
GOPRO_VIDEO_PREFIXES = ("gh", "gx")
WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{}".format(index) for index in range(1, 10)]
    + ["LPT{}".format(index) for index in range(1, 10)]
)
COLLISION_SUFFIX_FORMAT = "_{:02d}"
# Maximum size of a rendered name, in UTF-8 bytes, before the collision suffix
# and the extension: file systems limit a name to 255 bytes (ext4, APFS) or
# 255 UTF-16 units (NTFS, exFAT), which is never more restrictive than bytes.
MAX_STEM_BYTES = 200

RESULT_DONE = "done"
RESULT_FAILED = "failed"
RESULT_UNCHANGED = "unchanged"
RESULT_SKIPPED = "skipped"


class ConfigError(Exception):
    """Raised when a configuration file is missing, unreadable or invalid."""


class ExifToolError(Exception):
    """Raised when ExifTool cannot be found or does not run correctly."""


class UnknownDeviceError(Exception):
    """Raised when unknown devices cannot be registered (non-interactive run)."""


class DeviceIdentity(NamedTuple):
    """Camera identification read from a file's metadata (empty string when absent)."""

    make: str
    model: str
    serial_number: str


class EffectiveDate(NamedTuple):
    """Date used in file names, after clock offset and time zone policy."""

    value: datetime
    zone: str
    zone_source: str
    warnings: Tuple[str, ...]


class FileAnalysis(NamedTuple):
    """Result of analyzing one discovered file."""

    relative: Path
    status: str
    message: str
    date_tag: str
    date: Optional[datetime]
    identity: DeviceIdentity
    device: Optional[Dict[str, Any]]
    effective: Optional[EffectiveDate] = None
    software: str = ""


class PlanEntry(NamedTuple):
    """
    One immutable step of the rename plan.

    ``source`` is relative to the source folder and ``target`` relative to the
    target folder (the source folder in place, the destination with ``--dest``).
    """

    source: Path
    target: Optional[Path]
    action: str
    message: str
    warnings: Tuple[str, ...]


class OperationResult(NamedTuple):
    """Outcome of executing one plan entry."""

    entry: PlanEntry
    status: str
    message: str


class RunSummary(NamedTuple):
    """
    Counts and reasons of a run, in preview or after execution.

    ``counts`` is keyed by ``renamed``, ``copied``, ``unchanged``, ``skipped``,
    ``errors`` and ``warnings``; in preview, ``renamed`` and ``copied`` count
    the planned operations. ``skip_reasons`` and ``error_reasons`` pair each
    distinct reason with its number of files, most frequent first.
    """

    applied: bool
    counts: Dict[str, int]
    skip_reasons: Tuple[Tuple[str, int], ...]
    error_reasons: Tuple[Tuple[str, int], ...]


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the command line parser with every option of the plan.

    :return: Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="media_renamer.py",
        description="Rename photos and videos from their metadata (preview by default).",
    )
    parser.add_argument("--source", metavar="PATH", help="Source folder (required unless --check).")
    parser.add_argument(
        "--dest",
        metavar="PATH",
        help="Copy renamed files to this folder instead of renaming in place.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the renames or copies (preview only by default).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, even when dry_run_by_default is false in conf/config.json.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Also scan subfolders (the folder tree is never flattened).",
    )
    parser.add_argument("--device", metavar="ID", help="Force the device id for the whole batch.")
    parser.add_argument(
        "--tz",
        metavar="TIMEZONE",
        help="Reference IANA time zone of the batch (e.g. Europe/Paris).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the configuration and show the ExifTool version and location.",
    )
    return parser


def ensure_personal_files(conf_dir: Path) -> List[Path]:
    """
    Create the personal configuration files from their templates when missing.

    Existing personal files are never overwritten.

    :param conf_dir: Configuration folder holding the ``*.default.json`` templates.
    :return: Paths of the personal files created during this call.
    :raises ConfigError: If a template needed to create a missing file is absent.
    """
    created = []
    for template_name, personal_name in PERSONAL_FILES:
        personal_path = conf_dir / personal_name
        if personal_path.exists():
            continue
        template_path = conf_dir / template_name
        if not template_path.is_file():
            raise ConfigError(
                "{}: missing, and its template {} was not found".format(personal_path, template_path)
            )
        content = template_path.read_bytes()
        try:
            with open(str(personal_path), "xb") as handle:
                handle.write(content)
        except FileExistsError:
            continue
        created.append(personal_path)
    return created


def load_json_file(path: Path) -> Any:
    """
    Read and parse a JSON file encoded in UTF-8 (a BOM is tolerated).

    :param path: Path of the JSON file.
    :return: Parsed JSON value.
    :raises ConfigError: If the file cannot be read or is not valid JSON.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError("{}: file not found".format(path))
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("{}: cannot be read ({})".format(path, error))
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(
            "{}: invalid JSON at line {}, column {}: {}".format(
                path, error.lineno, error.colno, error.msg
            )
        )


def find_invalid_filename_chars(text: str) -> List[str]:
    """
    List the characters of a text that are not allowed in file names.

    Forbidden characters are the Windows reserved ones and control characters.

    :param text: Text to inspect.
    :return: Distinct offending characters, in order of first appearance.
    """
    found = []
    for char in text:
        if (char in INVALID_FILENAME_CHARS or ord(char) < 32) and char not in found:
            found.append(char)
    return found


def validate_filename_pattern(pattern: Any) -> List[str]:
    """
    Validate the ``filename_pattern`` setting.

    The pattern may only use known tokens, must contain at least one token and
    must not contain invalid file name characters, path separators or a leading
    dot, nor end with a dot or a space.

    :param pattern: Value of ``filename_pattern`` read from the configuration.
    :return: Error messages; empty when the pattern is valid.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return ["filename_pattern must be a non-empty string"]
    errors = []
    tokens = TOKEN_RE.findall(pattern)
    for token in tokens:
        if token not in KNOWN_TOKENS:
            errors.append(
                "filename_pattern: unknown token '{{{}}}' (allowed: {})".format(
                    token, ", ".join("{" + name + "}" for name in KNOWN_TOKENS)
                )
            )
    if not tokens:
        errors.append("filename_pattern must contain at least one token")
    literal = TOKEN_RE.sub("", pattern)
    if "{" in literal or "}" in literal:
        errors.append("filename_pattern: unbalanced '{' or '}'")
    if "/" in literal or "\\" in literal:
        errors.append("filename_pattern must not contain path separators ('/' or '\\')")
    invalid = [char for char in find_invalid_filename_chars(literal) if char not in "/\\"]
    if invalid:
        errors.append(
            "filename_pattern contains invalid characters: {}".format(
                " ".join(repr(char) for char in invalid)
            )
        )
    if pattern.startswith("."):
        errors.append("filename_pattern must not start with '.'")
    if pattern.endswith((".", " ")):
        errors.append("filename_pattern must not end with '.' or a space")
    return errors


def validate_extensions(extensions: Any) -> List[str]:
    """
    Validate the ``supported_extensions`` setting.

    :param extensions: Value of ``supported_extensions`` read from the configuration.
    :return: Error messages; empty when the list is valid.
    """
    if not isinstance(extensions, list) or not extensions:
        return ["supported_extensions must be a non-empty list"]
    errors = []
    seen = set()  # type: Set[str]
    for extension in extensions:
        if not isinstance(extension, str) or not EXTENSION_RE.match(extension):
            errors.append(
                "supported_extensions: invalid extension {!r} (expected e.g. '.jpg')".format(
                    extension
                )
            )
            continue
        key = extension.lower()
        if key in seen:
            errors.append("supported_extensions: duplicate extension '{}'".format(extension))
        seen.add(key)
    return errors


def validate_config(data: Any) -> List[str]:
    """
    Validate the content of ``conf/config.json``.

    :param data: Parsed JSON content.
    :return: Error messages; empty when the configuration is valid.
    """
    if not isinstance(data, dict):
        return ["the configuration must be a JSON object"]
    errors = []
    for key in sorted(set(data) - set(CONFIG_KEYS)):
        errors.append("unknown setting '{}'".format(key))
    for key in CONFIG_KEYS:
        if key not in data:
            errors.append("missing setting '{}'".format(key))
    if "filename_pattern" in data:
        errors.extend(validate_filename_pattern(data["filename_pattern"]))
    if "supported_extensions" in data:
        errors.extend(validate_extensions(data["supported_extensions"]))
    if "dry_run_by_default" in data and not isinstance(data["dry_run_by_default"], bool):
        errors.append("dry_run_by_default must be true or false")
    if "log_level" in data and data["log_level"] not in LOG_LEVELS:
        errors.append(
            "log_level must be one of {} (got {!r})".format(", ".join(LOG_LEVELS), data["log_level"])
        )
    return errors


def load_config(path: Path) -> Dict[str, Any]:
    """
    Load and validate ``conf/config.json``.

    :param path: Path of the configuration file.
    :return: Validated configuration, with extensions normalized to lower case.
    :raises ConfigError: If the file is unreadable, not valid JSON or invalid.
    """
    data = load_json_file(path)
    errors = validate_config(data)
    if errors:
        raise ConfigError("{}:\n  - {}".format(path, "\n  - ".join(errors)))
    config = dict(data)
    config["supported_extensions"] = [ext.lower() for ext in data["supported_extensions"]]
    return config


def parse_offset(text: str) -> int:
    """
    Parse a signed ``HH:MM:SS`` offset into seconds.

    The sign is optional (``+`` by default); hours may exceed 23 to express
    offsets of several days.

    :param text: Offset such as ``"-01:00:00"`` or ``"+00:00:30"``.
    :return: Offset in seconds, negative for a negative offset.
    :raises ValueError: If the text is not a valid signed ``HH:MM:SS`` offset.
    """
    match = OFFSET_RE.match(text) if isinstance(text, str) else None
    if not match:
        raise ValueError("invalid offset {!r} (expected signed HH:MM:SS, e.g. '-01:00:00')".format(text))
    sign, hours, minutes, seconds = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return -total if sign == "-" else total


def get_available_timezones() -> Set[str]:
    """
    Return the IANA time zones known to the local time zone database.

    :return: Known zone names; empty when no database is available
        (e.g. Windows without the ``tzdata`` package, or Python 3.8).
    """
    if zoneinfo is None:
        return set()
    try:
        return set(zoneinfo.available_timezones())
    except Exception:  # pragma: no cover - defensive, depends on the platform
        return set()


def has_timezone_database() -> bool:
    """
    Tell whether a time zone database is available.

    Without one (Windows without the ``tzdata`` package, or Python 3.8), the
    program runs in degraded mode: zones cannot be applied and local times are kept.

    :return: ``True`` when at least one IANA zone is known.
    """
    return bool(get_available_timezones())


def describe_missing_timezone_database() -> str:
    """
    Explain why no time zone database is available and how to get one.

    :return: Human-readable explanation.
    """
    if zoneinfo is None:
        return "no time zone database available: Python 3.9+ is required"
    return "no time zone database found (on Windows, install the 'tzdata' package, e.g. with 'uv sync')"


def validate_timezone(name: Any, available: Optional[Set[str]] = None) -> Optional[str]:
    """
    Validate an IANA time zone name.

    The syntax is always checked; membership is checked only when a time zone
    database is available.

    :param name: Time zone name, e.g. ``"Europe/Paris"``.
    :param available: Known zone names; looked up with ``get_available_timezones`` when ``None``.
    :return: An error message, or ``None`` when the name is valid.
    """
    if not isinstance(name, str) or not TIMEZONE_RE.match(name) or ".." in name:
        return "invalid time zone {!r} (expected an IANA name such as 'Europe/Paris')".format(name)
    known = get_available_timezones() if available is None else available
    if known and name not in known:
        return "unknown time zone {!r} (expected an IANA name such as 'Europe/Paris')".format(name)
    return None


def validate_match(match: Any) -> List[str]:
    """
    Validate the ``match`` criteria of a device.

    :param match: Value of the device ``match`` field.
    :return: Error messages; empty when the criteria are valid.
    """
    if not isinstance(match, dict):
        return ["'match' must be an object with 'make', 'model' and optional 'serial_number'"]
    errors = []
    for key in sorted(set(match) - set(MATCH_KEYS)):
        errors.append("unknown match criterion '{}'".format(key))
    for key in MATCH_KEYS:
        if key in match and not isinstance(match[key], str):
            errors.append("match criterion '{}' must be a string".format(key))
    if not any(isinstance(match.get(key), str) and match[key].strip() for key in ("make", "model")):
        errors.append("'match' needs a non-empty 'make' or 'model'")
    return errors


def validate_device(device: Any, available_timezones: Optional[Set[str]] = None) -> List[str]:
    """
    Validate one entry of ``conf/devices.json``.

    :param device: Parsed device entry.
    :param available_timezones: Known IANA zone names, forwarded to ``validate_timezone``.
    :return: Error messages; empty when the entry is valid.
    """
    if not isinstance(device, dict):
        return ["a device must be a JSON object"]
    errors = []
    allowed = set(DEVICE_REQUIRED_KEYS) | set(DEVICE_OPTIONAL_KEYS)
    for key in sorted(set(device) - allowed):
        errors.append("unknown field '{}'".format(key))
    for key in DEVICE_REQUIRED_KEYS:
        if key not in device:
            errors.append("missing field '{}'".format(key))
    if "id" in device and (not isinstance(device["id"], str) or not device["id"].strip()):
        errors.append("'id' must be a non-empty string")
    if "label" in device:
        label = device["label"]
        if not isinstance(label, str) or not label.strip():
            errors.append("'label' must be a non-empty string")
        else:
            invalid = find_invalid_filename_chars(label)
            if invalid:
                errors.append(
                    "'label' contains invalid file name characters: {}".format(
                        " ".join(repr(char) for char in invalid)
                    )
                )
    if "match" in device:
        errors.extend(validate_match(device["match"]))
    if "offset" in device:
        try:
            parse_offset(device["offset"])
        except ValueError as error:
            errors.append(str(error))
    if "timezone" in device:
        error = validate_timezone(device["timezone"], available_timezones)
        if error:
            errors.append(error)
    return errors


def validate_devices(data: Any, available_timezones: Optional[Set[str]] = None) -> List[str]:
    """
    Validate the content of ``conf/devices.json``, including duplicate ids.

    Ids are compared case-insensitively.

    :param data: Parsed JSON content.
    :param available_timezones: Known IANA zone names, forwarded to ``validate_timezone``.
    :return: Error messages; empty when the content is valid.
    """
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return ["the devices file must be a JSON object with a 'devices' list"]
    errors = []
    for key in sorted(set(data) - {"devices"}):
        errors.append("unknown top-level key '{}'".format(key))
    seen = {}  # type: Dict[str, int]
    for index, device in enumerate(data["devices"]):
        name = "device #{}".format(index + 1)
        if isinstance(device, dict) and isinstance(device.get("id"), str):
            name = "device #{} ('{}')".format(index + 1, device["id"])
            key = device["id"].strip().lower()
            if key and key in seen:
                errors.append("{}: duplicate id (already used by device #{})".format(name, seen[key]))
            elif key:
                seen[key] = index + 1
        for error in validate_device(device, available_timezones):
            errors.append("{}: {}".format(name, error))
    return errors


def load_devices(path: Path) -> List[Dict[str, Any]]:
    """
    Load and validate ``conf/devices.json``.

    :param path: Path of the devices file.
    :return: Validated list of device entries.
    :raises ConfigError: If the file is unreadable, not valid JSON or invalid.
    """
    data = load_json_file(path)
    errors = validate_devices(data)
    if errors:
        raise ConfigError("{}:\n  - {}".format(path, "\n  - ".join(errors)))
    return list(data["devices"])


def find_device(devices: List[Dict[str, Any]], device_id: str) -> Optional[Dict[str, Any]]:
    """
    Find a device by id, case-insensitively.

    :param devices: Validated device entries.
    :param device_id: Id to look for.
    :return: The matching device entry, or ``None`` when absent.
    """
    wanted = device_id.strip().lower()
    for device in devices:
        if device["id"].strip().lower() == wanted:
            return device
    return None


def write_devices_file(path: Path, data: Dict[str, Any]) -> None:
    """
    Write ``conf/devices.json`` safely.

    The current file is first copied to ``devices.json.bak``; the new content
    is written to a temporary file in the same folder, then atomically moved
    over the target so an interruption never leaves a truncated file.

    :param path: Path of the devices file.
    :param data: Content to write (``{"devices": [...]}``).
    :raises ConfigError: If ``data`` is not a valid devices document.
    """
    errors = validate_devices(data)
    if errors:
        raise ConfigError("refusing to write invalid devices:\n  - {}".format("\n  - ".join(errors)))
    if path.exists():
        shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        if os.path.exists(temp_name):
            os.remove(temp_name)
        raise


def exiftool_executable_name() -> str:
    """
    Return the ExifTool executable file name for the current platform.

    :return: ``exiftool.exe`` on Windows, ``exiftool`` elsewhere.
    """
    return "exiftool.exe" if os.name == "nt" else "exiftool"


def is_executable_file(path: Path) -> bool:
    """
    Tell whether a path is a regular file that can be executed.

    :param path: Path to test.
    :return: ``True`` for an executable file (any file on Windows).
    """
    return path.is_file() and (os.name == "nt" or os.access(str(path), os.X_OK))


def natural_sort_key(text: str) -> List[Any]:
    """
    Build a sort key comparing embedded numbers numerically.

    For example ``exiftool-9.0`` sorts before ``exiftool-13.62``.

    :param text: Text to build a key for.
    :return: Sort key made of lower-cased text chunks and integers.
    """
    return [(0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()) for chunk in re.split(r"(\d+)", text)]


def find_exiftool_in_bin(bin_dir: Path, executable_name: str) -> Optional[Path]:
    """
    Look for ExifTool in ``bin/`` and its direct subfolders.

    ``bin/<name>`` wins; otherwise subfolders are tried from the highest
    version-like name down (e.g. ``bin/exiftool-13.62_64/``).

    :param bin_dir: The ``bin/`` folder of the project.
    :param executable_name: ExifTool executable file name.
    :return: Path of the executable, or ``None`` when not found.
    """
    if not bin_dir.is_dir():
        return None
    direct = bin_dir / executable_name
    if is_executable_file(direct):
        return direct
    subdirs = sorted(
        (entry for entry in bin_dir.iterdir() if entry.is_dir()),
        key=lambda entry: natural_sort_key(entry.name),
        reverse=True,
    )
    for subdir in subdirs:
        candidate = subdir / executable_name
        if is_executable_file(candidate):
            return candidate
    return None


def find_keep_open_variants(bin_dir: Path) -> List[Path]:
    """
    List ``exiftool(-k)`` executables in ``bin/`` and its direct subfolders.

    This Windows variant waits for a key press and must be renamed to
    ``exiftool.exe`` before use.

    :param bin_dir: The ``bin/`` folder of the project.
    :return: Paths of the variants found, sorted.
    """
    if not bin_dir.is_dir():
        return []
    found = list(bin_dir.glob(EXIFTOOL_KEEP_OPEN_GLOB)) + list(bin_dir.glob("*/" + EXIFTOOL_KEEP_OPEN_GLOB))
    return sorted(found)


def find_exiftool(
    bin_dir: Path,
    environ: Optional[Mapping[str, str]] = None,
    executable_name: Optional[str] = None,
) -> Path:
    """
    Locate ExifTool: ``bin/`` first, then ``PATH``, then ``EXIFTOOL_PATH``.

    ``EXIFTOOL_PATH`` may point to the executable or to its folder.

    :param bin_dir: The ``bin/`` folder of the project.
    :param environ: Environment variables; ``os.environ`` when ``None``.
    :param executable_name: Executable name; platform default when ``None``.
    :return: Path of the ExifTool executable.
    :raises ExifToolError: If ExifTool cannot be found anywhere.
    """
    env = os.environ if environ is None else environ
    name = executable_name or exiftool_executable_name()
    found = find_exiftool_in_bin(bin_dir, name)
    if found:
        return found
    on_path = shutil.which(name, path=env.get("PATH", ""))
    if on_path:
        return Path(on_path)
    env_value = env.get(EXIFTOOL_ENV_VAR, "").strip()
    if env_value:
        env_path = Path(env_value)
        candidate = env_path / name if env_path.is_dir() else env_path
        if is_executable_file(candidate):
            return candidate
    message = (
        "ExifTool not found. Looked in {} (and its subfolders), in PATH and in {}. "
        "Install ExifTool from https://exiftool.org/ and place '{}' in bin/, "
        "or set {} to its location.".format(bin_dir, EXIFTOOL_ENV_VAR, name, EXIFTOOL_ENV_VAR)
    )
    variants = find_keep_open_variants(bin_dir)
    if variants:
        message += " Found {}: rename it to '{}' (keep it next to its exiftool_files/ folder).".format(
            variants[0], name
        )
    raise ExifToolError(message)


def get_exiftool_version(executable: Path, timeout: float = EXIFTOOL_TIMEOUT_SECONDS) -> str:
    """
    Run ``exiftool -ver`` and return the reported version.

    :param executable: Path of the ExifTool executable.
    :param timeout: Maximum run time in seconds.
    :return: Version string, e.g. ``"13.62"``.
    :raises ExifToolError: If ExifTool cannot be started, times out, fails or prints nothing.
    """
    try:
        result = subprocess.run(
            [str(executable), "-ver"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ExifToolError("{}: no answer after {} seconds".format(executable, timeout))
    except OSError as error:
        raise ExifToolError("{}: cannot be started ({})".format(executable, error))
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        details = result.stderr.strip() or "exit code {}".format(result.returncode)
        raise ExifToolError("{}: 'exiftool -ver' failed ({})".format(executable, details))
    return version


def path_key(path: Union[str, Path]) -> str:
    """
    Build a comparison key for a path, following the platform's case rules.

    :param path: Path to normalize.
    :return: Normalized path (lower-cased with backslashes on Windows).
    """
    return os.path.normcase(os.path.normpath(str(path)))


def discover_files(
    source: Path,
    extensions: Iterable[str],
    recursive: bool = False,
    excluded_dirs: Iterable[Path] = (),
) -> List[Path]:
    """
    List the media files of a source folder.

    Extensions are compared case-insensitively. Folders named ``.logs`` and the
    ``excluded_dirs`` (e.g. a destination inside the source) are never scanned.
    Symbolic links to folders are not followed.

    :param source: Source folder.
    :param extensions: Accepted extensions, such as ``".jpg"``.
    :param recursive: Also scan every subfolder when ``True``.
    :param excluded_dirs: Folders to skip, with their content.
    :return: Paths relative to ``source``, sorted case-insensitively.
    :raises NotADirectoryError: If ``source`` is not an existing folder.
    """
    if not source.is_dir():
        raise NotADirectoryError("source folder not found: {}".format(source))
    root = source.resolve()
    wanted = {extension.lower() for extension in extensions}
    excluded = {path_key(Path(folder).resolve()) for folder in excluded_dirs}
    found = []
    for current, dirnames, filenames in os.walk(str(root)):
        current_path = Path(current)
        if recursive:
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.casefold() != LOGS_DIR_NAME and path_key(current_path / name) not in excluded
            )
        else:
            dirnames[:] = []
        for name in filenames:
            if os.path.splitext(name)[1].lower() in wanted and (current_path / name).is_file():
                found.append((current_path / name).relative_to(root))
    return sorted(found, key=lambda path: (path.as_posix().casefold(), path.as_posix()))


def build_exiftool_command(executable: Path, argfile: Path) -> List[str]:
    """
    Build the ExifTool command reading the file list from an argument file.

    Output is JSON, restricted to ``METADATA_TAGS``; file names are exchanged in UTF-8.

    :param executable: Path of the ExifTool executable.
    :param argfile: UTF-8 file listing one absolute file path per line.
    :return: Command line, ready for ``subprocess.run``.
    """
    command = [str(executable), "-json", "-q", "-charset", "filename=utf8", "-api", "LargeFileSupport=1"]
    command.extend("-" + tag for tag in METADATA_TAGS)
    command.extend(["-@", str(argfile)])
    return command


def find_stderr_message(stderr_lines: Sequence[str], path: Path) -> Optional[str]:
    """
    Find the ExifTool error message about a file.

    ExifTool reports problems as ``"Error: <message> - <file>"``.

    :param stderr_lines: Lines printed by ExifTool on its error stream.
    :param path: File the message must refer to.
    :return: The message without the file name, or ``None`` when absent.
    """
    suffix = os.path.normcase(" - " + path.as_posix())
    for line in stderr_lines:
        if os.path.normcase(line).endswith(suffix):
            return line[: len(line) - len(suffix)].strip()
    return None


def parse_exiftool_output(
    stdout: str, stderr: str, paths: Sequence[Path]
) -> Tuple[Dict[Path, Dict[str, Any]], Dict[Path, str]]:
    """
    Split ExifTool JSON output into per-file metadata and per-file errors.

    Every requested path ends up in exactly one of the two results.

    :param stdout: JSON printed by ExifTool.
    :param stderr: Error stream printed by ExifTool.
    :param paths: Files that were passed to ExifTool.
    :return: Tuple ``(metadata, errors)``, both keyed by the given paths.
    """
    expected = {path_key(path): path for path in paths}
    metadata = {}  # type: Dict[Path, Dict[str, Any]]
    errors = {}  # type: Dict[Path, str]
    records = []  # type: List[Any]
    if stdout.strip():
        try:
            records = json.loads(stdout)
        except json.JSONDecodeError:
            return {}, {path: "unreadable ExifTool output" for path in paths}
        if not isinstance(records, list):
            return {}, {path: "unexpected ExifTool output" for path in paths}
    for record in records:
        if not isinstance(record, dict):
            continue
        path = expected.get(path_key(record.get("SourceFile", "")))
        if path is None:
            continue
        if record.get("Error"):
            errors[path] = str(record["Error"])
        else:
            metadata[path] = record
    stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for path in paths:
        if path not in metadata and path not in errors:
            errors[path] = find_stderr_message(stderr_lines, path) or "no metadata returned by ExifTool"
    return metadata, errors


def run_exiftool_batch(
    executable: Path, paths: Sequence[Path], timeout: float
) -> Tuple[Dict[Path, Dict[str, Any]], Dict[Path, str]]:
    """
    Read the metadata of a batch of files with a single ExifTool call.

    A timeout marks every file of the batch as failed instead of stopping the run.

    :param executable: Path of the ExifTool executable.
    :param paths: Absolute paths of the files to read.
    :param timeout: Maximum run time in seconds.
    :return: Tuple ``(metadata, errors)``, both keyed by the given paths.
    :raises ExifToolError: If ExifTool cannot be started.
    """
    descriptor, argfile = tempfile.mkstemp(prefix="media_renamer_", suffix=".args")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for path in paths:
                handle.write(path.as_posix() + "\n")
        try:
            result = subprocess.run(
                build_exiftool_command(executable, Path(argfile)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            message = "ExifTool gave no answer after {} seconds".format(timeout)
            return {}, {path: message for path in paths}
        except OSError as error:
            raise ExifToolError("{}: cannot be started ({})".format(executable, error))
    finally:
        os.remove(argfile)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    return parse_exiftool_output(stdout, stderr, paths)


def read_metadata(
    executable: Path, paths: Sequence[Path], batch_size: int = EXIFTOOL_BATCH_SIZE
) -> Tuple[Dict[Path, Dict[str, Any]], Dict[Path, str]]:
    """
    Read the metadata of many files, calling ExifTool once per batch.

    The timeout of each call grows with the batch size.

    :param executable: Path of the ExifTool executable.
    :param paths: Absolute paths of the files to read.
    :param batch_size: Maximum number of files per ExifTool call.
    :return: Tuple ``(metadata, errors)``, both keyed by the given paths.
    :raises ExifToolError: If ExifTool cannot be started.
    """
    metadata = {}  # type: Dict[Path, Dict[str, Any]]
    errors = {}  # type: Dict[Path, str]
    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        timeout = EXIFTOOL_TIMEOUT_SECONDS + EXIFTOOL_SECONDS_PER_FILE * len(batch)
        batch_metadata, batch_errors = run_exiftool_batch(executable, batch, timeout)
        metadata.update(batch_metadata)
        errors.update(batch_errors)
    return metadata, errors


def parse_exif_datetime(value: Any) -> Optional[datetime]:
    """
    Parse an ExifTool date such as ``"2024:07:14 17:19:24"``.

    Sub-seconds are dropped and missing seconds count as 0. A trailing UTC
    offset (``"Z"``, ``"+02:00"``) yields an aware datetime, otherwise the
    result is naive. Zero or impossible dates are rejected.

    :param value: Tag value returned by ExifTool.
    :return: The parsed date, or ``None`` when the value is not a valid date.
    """
    if not isinstance(value, str):
        return None
    match = EXIF_DATE_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    tzinfo = parse_utc_offset(zone) if zone else None
    if zone and tzinfo is None:
        return None
    try:
        return datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=tzinfo
        )
    except ValueError:
        return None


def parse_utc_offset(value: Any) -> Optional[timezone]:
    """
    Parse a UTC offset such as ``"+02:00"``, ``"-0530"`` or ``"Z"``.

    :param value: Offset text, e.g. the value of the ``OffsetTimeOriginal`` tag.
    :return: The matching fixed-offset time zone, or ``None`` when the value is not a valid offset.
    """
    if not isinstance(value, str):
        return None
    match = UTC_OFFSET_RE.match(value.strip())
    if not match:
        return None
    if match.group(1) == "Z":
        return timezone.utc
    sign, hours, minutes = match.group(2), int(match.group(3)), int(match.group(4))
    if hours > 23 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if sign == "-" else delta)


def select_capture_date(metadata: Mapping[str, Any]) -> Optional[Tuple[str, datetime]]:
    """
    Pick the capture date of a file, following the ``DATE_TAGS`` priority.

    A zone-less date receives the UTC offset of its companion tag
    (``DATE_OFFSET_TAGS``, e.g. ``OffsetTimeOriginal``) when that tag is valid.

    :param metadata: Tags returned by ExifTool for the file.
    :return: Tuple ``(tag_name, date)``, or ``None`` when no tag holds a valid date.
    """
    for tag in DATE_TAGS:
        date = parse_exif_datetime(metadata.get(tag))
        if date is None:
            continue
        if date.tzinfo is None and tag in DATE_OFFSET_TAGS:
            tzinfo = parse_utc_offset(metadata.get(DATE_OFFSET_TAGS[tag]))
            if tzinfo is not None:
                date = date.replace(tzinfo=tzinfo)
        return tag, date
    return None


def apply_offset(date: datetime, offset: str) -> datetime:
    """
    Correct a camera clock with a signed ``HH:MM:SS`` offset.

    Only the computed date changes; the file's metadata is never written.

    :param date: Date read from the metadata (naive or aware).
    :param offset: Signed offset such as ``"-01:00:00"``.
    :return: The corrected date, with the same time zone information as ``date``.
    :raises ValueError: If the offset is invalid or moves the date out of the supported range.
    """
    seconds = parse_offset(offset)
    try:
        return date + timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError("offset {} moves the date {} out of range".format(offset, date.isoformat(sep=" ")))


def load_zone(name: str) -> Any:
    """
    Load an IANA time zone from the time zone database.

    :param name: Zone name, e.g. ``"Europe/Paris"``.
    :return: The ``zoneinfo.ZoneInfo`` object.
    :raises ValueError: If no time zone database is available or the zone is unknown.
    """
    if zoneinfo is None:
        raise ValueError("time zone {!r} cannot be used: Python 3.9+ is required".format(name))
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(
            "time zone {!r} not found in the time zone database "
            "(on Windows, install the 'tzdata' package)".format(name)
        )


def check_cli_timezone(name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Validate the ``--tz`` value: IANA syntax, then loading from the time zone database.

    A syntax error, or a zone missing from an available database, is an error.
    When no database is available at all, a syntactically valid zone only yields
    a warning (degraded mode): the zone is ignored and local times are kept.

    :param name: Value of ``--tz``.
    :return: Tuple ``(error, warning)``; both ``None`` when the zone can be used.
    """
    error = validate_timezone(name)
    if error is not None:
        return error, None
    try:
        load_zone(name)
    except ValueError as load_error:
        if has_timezone_database():
            return str(load_error), None
        return None, "time zone {!r} ignored, local times kept: {}".format(
            name, describe_missing_timezone_database())
    return None, None


def classify_local_time(local: datetime, zone: Any) -> str:
    """
    Tell whether a wall-clock time exists exactly once in a time zone.

    Around daylight saving time changes, a local time may occur twice
    (clocks set back) or never (clocks set forward).

    :param local: Naive local date and time.
    :param zone: Time zone object (``tzinfo``).
    :return: ``LOCAL_TIME_VALID``, ``LOCAL_TIME_AMBIGUOUS`` or ``LOCAL_TIME_NONEXISTENT``.
    """
    first = local.replace(tzinfo=zone, fold=0)
    second = local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return LOCAL_TIME_VALID
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    return LOCAL_TIME_AMBIGUOUS if round_trip == local else LOCAL_TIME_NONEXISTENT


def choose_zone(device: Mapping[str, Any], cli_tz: Optional[str]) -> Tuple[str, str]:
    """
    Choose the reference time zone of a zone-less date.

    ``--tz`` (explicit batch policy) wins over the device's ``timezone``.

    :param device: Device entry of the file.
    :param cli_tz: Value of ``--tz``, if any.
    :return: Tuple ``(zone_name, zone_source)``; both empty when no zone is known.
    """
    if cli_tz:
        return cli_tz, ZONE_SOURCE_CLI
    if device.get("timezone"):
        return device["timezone"], ZONE_SOURCE_DEVICE
    return "", ""


def compute_effective_date(
    date: datetime, device: Mapping[str, Any], cli_tz: Optional[str] = None
) -> EffectiveDate:
    """
    Compute the date used in file names.

    The device clock offset is applied first. The result is then normalized
    to UTC only when its zone is known: offset stored in the metadata, then
    ``--tz``, then the device time zone. Otherwise, the naive local time is
    kept. Ambiguous or non-existent local times (daylight saving time changes)
    and unavailable zones are not converted: the local time is kept and a
    warning is returned.

    :param date: Capture date read from the metadata (naive or aware).
    :param device: Device entry of the file (``offset`` and optional ``timezone``).
    :param cli_tz: Value of ``--tz``, if any.
    :return: Effective date: an aware UTC date, or a naive local date.
    :raises ValueError: If the offset moves the date out of the supported range.
    """
    corrected = apply_offset(date, device.get("offset", DEFAULT_OFFSET))
    if corrected.tzinfo is not None:
        return EffectiveDate(corrected.astimezone(timezone.utc), corrected.strftime("%z"),
                             ZONE_SOURCE_METADATA, ())
    zone_name, zone_source = choose_zone(device, cli_tz)
    if not zone_name:
        return EffectiveDate(corrected, "", "", ())
    try:
        zone = load_zone(zone_name)
    except ValueError as error:
        return EffectiveDate(corrected, "", "", ("{}; local time kept".format(error),))
    kind = classify_local_time(corrected, zone)
    if kind != LOCAL_TIME_VALID:
        reason = ("occurs twice (clocks set back)" if kind == LOCAL_TIME_AMBIGUOUS
                  else "does not exist (clocks set forward)")
        warning = "{} {} in {}: local time kept, not converted to UTC".format(
            corrected.isoformat(sep=" "), reason, zone_name)
        return EffectiveDate(corrected, "", "", (warning,))
    return EffectiveDate(corrected.replace(tzinfo=zone).astimezone(timezone.utc), zone_name, zone_source, ())


def format_effective_date(effective: EffectiveDate) -> str:
    """
    Format an effective date for display.

    :param effective: Effective date.
    :return: Text such as ``"2024-01-02 03:04:05 UTC"`` or ``"2024-01-02 03:04:05 local"``.
    """
    suffix = "UTC" if effective.value.tzinfo is not None else "local"
    return "{} {}".format(effective.value.replace(tzinfo=None).isoformat(sep=" "), suffix)


def clean_text(value: Any) -> str:
    """
    Convert a metadata value to a trimmed string.

    :param value: Tag value (ExifTool may return numbers).
    :return: Trimmed text; empty for ``None``.
    """
    return "" if value is None else str(value).strip()


def normalize_text(value: str) -> str:
    """
    Normalize a text for case-insensitive comparison.

    :param value: Text to normalize.
    :return: Case-folded text with collapsed white space.
    """
    return " ".join(value.split()).casefold()


def extract_identity(metadata: Mapping[str, Any]) -> DeviceIdentity:
    """
    Read the camera identification of a file.

    :param metadata: Tags returned by ExifTool for the file.
    :return: Make, model and serial number (empty when absent).
    """
    return DeviceIdentity(
        clean_text(metadata.get("Make")),
        clean_text(metadata.get("Model")),
        clean_text(metadata.get("SerialNumber")),
    )


def extract_software(metadata: Mapping[str, Any]) -> str:
    """
    Read the software or firmware that wrote a file, to help recognize its device.

    :param metadata: Tags returned by ExifTool for the file.
    :return: First non-empty value of ``SOFTWARE_TAGS``; empty when none.
    """
    for tag in SOFTWARE_TAGS:
        value = clean_text(metadata.get(tag))
        if value:
            return value
    return ""


def device_matches(device: Mapping[str, Any], identity: DeviceIdentity) -> bool:
    """
    Tell whether a device entry matches a file's identification.

    Comparison is case-insensitive. Empty criteria of the entry are ignored.
    A ``model`` criterion must always match; ``make`` and ``serial_number``
    are only compared when the file provides them (many video containers,
    such as GoPro MP4 files, have a model but no make or serial number).

    :param device: Validated device entry.
    :param identity: Identification read from the file.
    :return: ``True`` when every applicable criterion matches and at least one was compared.
    """
    criteria = device["match"]
    actual_values = {"make": identity.make, "model": identity.model, "serial_number": identity.serial_number}
    compared = False
    for key in MATCH_KEYS:
        expected = normalize_text(criteria.get(key, ""))
        if not expected:
            continue
        actual = normalize_text(actual_values[key])
        if not actual:
            if key == "model":
                return False
            continue
        if actual != expected:
            return False
        compared = True
    return compared


def match_device(devices: Sequence[Dict[str, Any]], identity: DeviceIdentity) -> Optional[Dict[str, Any]]:
    """
    Find the first device entry matching a file's identification.

    :param devices: Validated device entries, in file order.
    :param identity: Identification read from the file.
    :return: The first matching entry, or ``None``.
    """
    for device in devices:
        if device_matches(device, identity):
            return device
    return None


def analyze_file(
    relative: Path,
    metadata: Optional[Mapping[str, Any]],
    error: Optional[str],
    devices: Sequence[Dict[str, Any]],
    forced_device: Optional[Dict[str, Any]] = None,
) -> FileAnalysis:
    """
    Decide the capture date and device of one file.

    GoPro auxiliary files (``.LRV``, ``.THM``, and ``.WAV`` named with a GoPro
    file id) are only flagged: they take the date and device of their main
    video later. Files without readable metadata,
    without a valid capture date or without any make/model are skipped; the
    system date is never used.

    :param relative: File path relative to the source folder.
    :param metadata: Tags returned by ExifTool, or ``None`` when reading failed.
    :param error: ExifTool error message for this file, if any.
    :param devices: Validated device entries.
    :param forced_device: Device imposed with ``--device``, if any.
    :return: Analysis of the file.
    """
    empty_identity = DeviceIdentity("", "", "")
    if is_auxiliary_file(relative):
        return FileAnalysis(relative, STATUS_AUXILIARY, "follows its main video", "", None, empty_identity, None)
    if metadata is None:
        message = "unreadable metadata ({})".format(error or "unknown error")
        return FileAnalysis(relative, STATUS_SKIPPED, message, "", None, empty_identity, None)
    identity = extract_identity(metadata)
    selected = select_capture_date(metadata)
    if selected is None:
        message = "no valid capture date (checked {})".format(", ".join(DATE_TAGS))
        return FileAnalysis(relative, STATUS_SKIPPED, message, "", None, identity, None)
    date_tag, date = selected
    device = forced_device or match_device(devices, identity)
    if device is not None:
        return FileAnalysis(relative, STATUS_READY, "", date_tag, date, identity, device)
    if not identity.make and not identity.model:
        message = "no Make/Model metadata to identify the device (use --device)"
        return FileAnalysis(relative, STATUS_SKIPPED, message, date_tag, date, identity, None)
    return FileAnalysis(relative, STATUS_UNKNOWN_DEVICE, "", date_tag, date, identity, None,
                        software=extract_software(metadata))


def collect_unknown_identities(analyses: Iterable[FileAnalysis]) -> List[DeviceIdentity]:
    """
    List the distinct identifications of files with an unknown device.

    :param analyses: File analyses.
    :return: Identifications, in order of first appearance.
    """
    found = []  # type: List[DeviceIdentity]
    for analysis in analyses:
        if analysis.status == STATUS_UNKNOWN_DEVICE and analysis.identity not in found:
            found.append(analysis.identity)
    return found


def mask_serial(serial: str) -> str:
    """
    Hide most of a serial number.

    :param serial: Serial number.
    :return: Only the last 4 characters, prefixed with ``***``; empty when no serial.
    """
    if not serial:
        return ""
    return "***" + serial[-4:] if len(serial) > 4 else "***"


def describe_identity(identity: DeviceIdentity) -> str:
    """
    Describe an identification for the user, with a masked serial number.

    :param identity: Identification to describe.
    :return: Text such as ``Make 'GoPro', Model 'HERO9 Black', serial ***4391``.
    """
    parts = []
    if identity.make:
        parts.append("Make '{}'".format(identity.make))
    if identity.model:
        parts.append("Model '{}'".format(identity.model))
    if identity.serial_number:
        parts.append("serial {}".format(mask_serial(identity.serial_number)))
    return ", ".join(parts) or "no identification"


def describe_unknown_files(identity: DeviceIdentity, analyses: Iterable[FileAnalysis]) -> List[str]:
    """
    Describe the files of an unknown device, to help the user recognize it.

    :param identity: Identification of the unknown device.
    :param analyses: File analyses of the batch.
    :return: Lines giving the number of files, their capture date range, a few
        example names and, when present, the software or firmware that wrote them.
    """
    files = [analysis for analysis in analyses
             if analysis.status == STATUS_UNKNOWN_DEVICE and analysis.identity == identity]
    if not files:
        return []
    dates = sorted(analysis.date.date() for analysis in files if analysis.date is not None)
    names = [analysis.relative.as_posix() for analysis in files[:UNKNOWN_DEVICE_EXAMPLES]]
    if len(files) > UNKNOWN_DEVICE_EXAMPLES:
        names.append("...")
    line = "{} file(s)".format(len(files))
    if dates:
        span = dates[0].isoformat() if dates[0] == dates[-1] else "{} to {}".format(dates[0], dates[-1])
        line += ", taken {}".format(span)
    lines = ["{}, e.g. {}".format(line, ", ".join(names))]
    software = []  # type: List[str]
    for analysis in files:
        if analysis.software and analysis.software not in software:
            software.append(analysis.software)
    if software:
        lines.append("Software: {}".format(", ".join(software)))
    return lines


def describe_device(device: Mapping[str, Any], show_serial: bool = True) -> str:
    """
    Describe a device entry for the user: its id, completed by readable details.

    The id stays the reference used in ``conf/devices.json`` and by ``--device``;
    the label and the ``match`` criteria (make, model) tell which camera it is.

    :param device: Device entry.
    :param show_serial: Whether to show the masked serial number; disable it for text that reaches the log.
    :return: Text such as ``'hero9-black' (label 'HERO9 Black'; Make 'GoPro', Model 'HERO9 Black')``.
    """
    match = device.get("match") or {}
    identity = DeviceIdentity(
        clean_text(match.get("make")),
        clean_text(match.get("model")),
        clean_text(match.get("serial_number")) if show_serial else "",
    )
    details = ["label '{}'".format(clean_text(device.get("label")))]
    if any(identity):
        details.append(describe_identity(identity))
    return "'{}' ({})".format(clean_text(device.get("id")), "; ".join(details))


def format_unknown_forced_device_error(device_id: str, devices: Sequence[Mapping[str, Any]]) -> str:
    """
    Build the error shown when ``--device`` names an unknown id.

    Serial numbers are left out because the message is also logged.

    :param device_id: Id given to ``--device``.
    :param devices: Known device entries.
    :return: Multi-line message listing the known ids with their label, make and model.
    """
    lines = ["--device: unknown device id {!r}".format(device_id)]
    if devices:
        lines.append("Known devices:")
        lines.extend("  - {}".format(describe_device(device, show_serial=False)) for device in devices)
    else:
        lines.append("No device is registered in conf/devices.json.")
    return "\n".join(lines)


def make_device_id(label: str, devices: Iterable[Mapping[str, Any]]) -> str:
    """
    Build a unique device id from a label.

    :param label: Device label chosen by the user.
    :param devices: Existing device entries.
    :return: Lower-case id made of letters, digits and dashes, suffixed with ``-2``, ``-3``... if taken.
    """
    base = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "device"
    taken = {device["id"].strip().lower() for device in devices}
    candidate = base
    counter = 2
    while candidate in taken:
        candidate = "{}-{}".format(base, counter)
        counter += 1
    return candidate


def default_label(identity: DeviceIdentity) -> str:
    """
    Suggest a device label from an identification.

    :param identity: Identification of the device.
    :return: Model (or make) without invalid file name characters; ``"Device"`` as last resort.
    """
    text = identity.model or identity.make
    invalid = set(find_invalid_filename_chars(text))
    return "".join(char for char in text if char not in invalid).strip() or "Device"


def validate_label_answer(answer: str) -> Optional[str]:
    """
    Validate a device label typed by the user.

    :param answer: Typed label.
    :return: An error message, or ``None`` when the label is valid.
    """
    if not answer:
        return "the label must not be empty"
    invalid = find_invalid_filename_chars(answer)
    if invalid:
        return "the label contains invalid file name characters: {}".format(
            " ".join(repr(char) for char in invalid)
        )
    return None


def validate_offset_answer(answer: str) -> Optional[str]:
    """
    Validate a clock offset typed by the user.

    :param answer: Typed offset.
    :return: An error message, or ``None`` when the offset is valid.
    """
    try:
        parse_offset(answer)
    except ValueError as error:
        return str(error)
    return None


def validate_timezone_answer(answer: str) -> Optional[str]:
    """
    Validate an optional IANA time zone typed by the user.

    :param answer: Typed zone; empty means no zone.
    :return: An error message, or ``None`` when the answer is valid.
    """
    return validate_timezone(answer) if answer else None


def validate_yes_no_answer(answer: str) -> Optional[str]:
    """
    Validate a yes/no answer.

    :param answer: Typed answer.
    :return: An error message, or ``None`` when the answer is ``y``, ``yes``, ``n`` or ``no``.
    """
    return None if answer.lower() in ("y", "yes", "n", "no") else "please answer y or n"


def ask(
    input_func: Callable[[str], str],
    out: TextIO,
    prompt: str,
    validate: Callable[[str], Optional[str]],
    default: str = "",
) -> str:
    """
    Ask a question until the answer is valid.

    :param input_func: Function reading one line, such as ``input``.
    :param out: Stream for validation messages.
    :param prompt: Question shown to the user.
    :param validate: Returns an error message for an invalid answer, ``None`` otherwise.
    :param default: Answer used when the user just presses Enter.
    :return: The valid, trimmed answer.
    :raises EOFError: If the input stream is closed.
    """
    while True:
        answer = input_func(prompt).strip() or default
        error = validate(answer)
        if error is None:
            return answer
        print("  {}".format(error), file=out)


def prompt_new_device(
    identity: DeviceIdentity,
    devices: Sequence[Mapping[str, Any]],
    input_func: Callable[[str], str],
    out: TextIO,
    context: Sequence[str] = (),
) -> Dict[str, Any]:
    """
    Ask the user to describe an unknown device.

    The serial number is only used as a criterion when the user asks for it,
    so files of the same model without a serial number still match.

    :param identity: Identification of the unknown device.
    :param devices: Existing device entries, used to build a unique id.
    :param input_func: Function reading one line, such as ``input``.
    :param out: Stream for messages.
    :param context: Lines describing the device's files, shown before the questions.
    :return: New, valid device entry.
    :raises EOFError: If the input stream is closed.
    """
    print("\nUnknown device: {}".format(describe_identity(identity)), file=out)
    for line in context:
        print("  {}".format(line), file=out)
    suggestion = default_label(identity)
    label = ask(input_func, out, "  Label used in file names [{}]: ".format(suggestion),
                validate_label_answer, suggestion)
    offset = ask(input_func, out, "  Clock offset to apply, signed HH:MM:SS [{}]: ".format(DEFAULT_OFFSET),
                 validate_offset_answer, DEFAULT_OFFSET)
    zone = ask(input_func, out, "  IANA time zone, e.g. Europe/Paris (empty for none) []: ",
               validate_timezone_answer)
    use_serial = False
    if identity.serial_number:
        answer = ask(input_func, out, "  Match only this camera body by its serial number? [y/N]: ",
                     validate_yes_no_answer, "n")
        use_serial = answer.lower() in ("y", "yes")
    match = {}  # type: Dict[str, str]
    if identity.make:
        match["make"] = identity.make
    if identity.model:
        match["model"] = identity.model
    if use_serial:
        match["serial_number"] = identity.serial_number
    device = {"id": make_device_id(label, devices), "label": label, "match": match, "offset": offset}
    if zone:
        device["timezone"] = zone
    return device


def format_unknown_devices_error(identities: Sequence[DeviceIdentity]) -> str:
    """
    Build the error shown when unknown devices cannot be registered interactively.

    :param identities: Unknown identifications.
    :return: Multi-line message listing the devices and the ways to fix the run.
    """
    lines = ["{} unknown device(s) and no interactive terminal to register them:".format(len(identities))]
    lines.extend("  - {}".format(describe_identity(identity)) for identity in identities)
    lines.append("Run the command in a terminal to answer the questions, add the devices to "
                 "conf/devices.json, or force a device with --device ID.")
    return "\n".join(lines)


def resolve_unknown_devices(
    analyses: Sequence[FileAnalysis],
    devices: Sequence[Dict[str, Any]],
    devices_path: Path,
    interactive: bool,
    input_func: Callable[[str], str] = input,
    out: Optional[TextIO] = None,
) -> Tuple[List[FileAnalysis], List[Dict[str, Any]]]:
    """
    Register every unknown device before any file is modified.

    Each answer is saved to ``devices.json`` at once and reused for all the
    matching files of the batch, so a device is asked for only once.

    :param analyses: File analyses, possibly with unknown devices.
    :param devices: Validated device entries.
    :param devices_path: Path of ``conf/devices.json``.
    :param interactive: Whether questions can be asked to the user.
    :param input_func: Function reading one line, such as ``input``.
    :param out: Stream for messages; ``sys.stdout`` when ``None``.
    :return: Tuple ``(analyses, devices)`` with every device resolved and the updated device list.
    :raises UnknownDeviceError: If devices are unknown and no answer can be obtained.
    :raises ConfigError: If the devices file cannot be written.
    """
    out = sys.stdout if out is None else out
    current = list(analyses)
    known = list(devices)
    unknown = collect_unknown_identities(current)
    if unknown and not interactive:
        raise UnknownDeviceError(format_unknown_devices_error(unknown))
    while unknown:
        try:
            device = prompt_new_device(unknown[0], known, input_func, out,
                                       describe_unknown_files(unknown[0], current))
        except EOFError:
            raise UnknownDeviceError(format_unknown_devices_error(unknown))
        known.append(device)
        write_devices_file(devices_path, {"devices": known})
        print("  Saved device {} to {}".format(describe_device(device), devices_path), file=out)
        current = [
            analysis._replace(status=STATUS_READY, device=device)
            if analysis.status == STATUS_UNKNOWN_DEVICE and device_matches(device, analysis.identity)
            else analysis
            for analysis in current
        ]
        unknown = collect_unknown_identities(current)
    return current, known


def add_effective_dates(analyses: Sequence[FileAnalysis], cli_tz: Optional[str] = None) -> List[FileAnalysis]:
    """
    Compute the effective date of every ready file.

    A file whose offset moves the date out of range is skipped.

    :param analyses: File analyses, with every device resolved.
    :param cli_tz: Value of ``--tz``, if any.
    :return: Analyses with their ``effective`` field set for ready files.
    """
    result = []
    for analysis in analyses:
        if analysis.status == STATUS_READY and analysis.date is not None and analysis.device is not None:
            try:
                effective = compute_effective_date(analysis.date, analysis.device, cli_tz)
                analysis = analysis._replace(effective=effective)
            except ValueError as error:
                analysis = analysis._replace(status=STATUS_SKIPPED, message=str(error))
        result.append(analysis)
    return result


def analyze_source(
    source: Path,
    extensions: Iterable[str],
    devices: Sequence[Dict[str, Any]],
    devices_path: Path,
    executable: Path,
    recursive: bool = False,
    forced_device: Optional[Dict[str, Any]] = None,
    excluded_dirs: Iterable[Path] = (),
    interactive: bool = False,
    input_func: Callable[[str], str] = input,
    out: Optional[TextIO] = None,
    cli_tz: Optional[str] = None,
) -> List[FileAnalysis]:
    """
    Discover the files of a source folder, read their metadata, identify their devices
    and compute their effective dates.

    Source files are only read. Unknown devices are resolved before returning.

    :param source: Source folder.
    :param extensions: Accepted extensions.
    :param devices: Validated device entries.
    :param devices_path: Path of ``conf/devices.json``, updated with new devices.
    :param executable: Path of the ExifTool executable.
    :param recursive: Also scan every subfolder when ``True``.
    :param forced_device: Device imposed with ``--device``, if any.
    :param excluded_dirs: Folders to skip, with their content.
    :param interactive: Whether unknown devices can be asked to the user.
    :param input_func: Function reading one line, such as ``input``.
    :param out: Stream for messages; ``sys.stdout`` when ``None``.
    :param cli_tz: Value of ``--tz``, if any.
    :return: One analysis per discovered file, in discovery order.
    :raises NotADirectoryError: If ``source`` is not an existing folder.
    :raises ExifToolError: If ExifTool cannot be started.
    :raises UnknownDeviceError: If devices are unknown and no answer can be obtained.
    :raises ConfigError: If the devices file cannot be written.
    """
    relatives = discover_files(source, extensions, recursive, excluded_dirs)
    root = source.resolve()
    absolute = [root / relative for relative in relatives]
    metadata, errors = read_metadata(executable, absolute) if absolute else ({}, {})
    analyses = [
        analyze_file(relative, metadata.get(path), errors.get(path), devices, forced_device)
        for relative, path in zip(relatives, absolute)
    ]
    analyses, _ = resolve_unknown_devices(analyses, devices, devices_path, interactive, input_func, out)
    return add_effective_dates(analyses, cli_tz)


def describe_date_source(analysis: FileAnalysis) -> str:
    """
    Describe where the effective date of a ready file comes from.

    :param analysis: Analysis of a ready file.
    :return: Text such as ``"DateTimeOriginal 2024-01-02 03:04:05, offset -01:00:00, zone Europe/Paris (--tz)"``.
    """
    parts = ["{} {}".format(analysis.date_tag, analysis.date.isoformat(sep=" ") if analysis.date else "?")]
    offset = (analysis.device or {}).get("offset", DEFAULT_OFFSET)
    if parse_offset(offset) != 0:
        parts.append("offset {}".format(offset))
    effective = analysis.effective
    if effective is not None and effective.zone:
        parts.append("zone {} ({})".format(effective.zone, effective.zone_source))
    return ", ".join(parts)


def print_analysis(analyses: Sequence[FileAnalysis], out: TextIO) -> None:
    """
    Print the analysis of every file, its warnings and a summary (serial numbers are never shown).

    :param analyses: File analyses.
    :param out: Output stream.
    """
    warnings = 0
    for analysis in analyses:
        name = analysis.relative.as_posix()
        if analysis.status == STATUS_READY:
            device = analysis.device or {}
            if analysis.effective is not None:
                shown = format_effective_date(analysis.effective)
            else:
                shown = analysis.date.isoformat(sep=" ") if analysis.date else "?"
            print("[OK]   {}  {}  device '{}'  ({})".format(
                name, shown, device.get("label", "?"), describe_date_source(analysis)), file=out)
            for warning in analysis.effective.warnings if analysis.effective else ():
                print("       warning: {}".format(warning), file=out)
                warnings += 1
        elif analysis.status == STATUS_AUXILIARY:
            print("[AUX]  {}  {}".format(name, analysis.message), file=out)
        else:
            print("[SKIP] {}  {}".format(name, analysis.message or analysis.status), file=out)
    counts = {}  # type: Dict[str, int]
    for analysis in analyses:
        counts[analysis.status] = counts.get(analysis.status, 0) + 1
    print("{} file(s): {} ready, {} auxiliary, {} skipped, {} warning(s).".format(
        len(analyses), counts.get(STATUS_READY, 0), counts.get(STATUS_AUXILIARY, 0),
        len(analyses) - counts.get(STATUS_READY, 0) - counts.get(STATUS_AUXILIARY, 0), warnings), file=out)


def truncate_utf8(text: str, max_bytes: int) -> str:
    """
    Cut a text so that its UTF-8 encoding fits in a number of bytes.

    Only whole characters are kept, so the result is always valid text.

    :param text: Text to cut.
    :param max_bytes: Maximum size of the UTF-8 encoding, in bytes.
    :return: The text itself when it fits, otherwise its longest fitting prefix.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def name_key(name: str) -> str:
    """
    Build the key used to compare file names, as case-insensitive file systems do.

    Names are case-folded and normalized to Unicode NFC, so ``"Été.jpg"`` in
    composed form and ``"été.JPG"`` in decomposed form (as macOS may list it)
    are the same name.

    :param name: File name.
    :return: Comparison key.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def sanitize_stem(stem: str) -> str:
    """
    Turn a rendered name (without extension) into a valid file name, deterministically.

    The name is normalized to Unicode NFC; invalid file name characters and
    control characters become ``_``; leading and trailing dots and spaces are
    removed; the name is cut to ``MAX_STEM_BYTES`` UTF-8 bytes (whole characters
    only); an empty result becomes ``_``; a Windows reserved device name
    (``CON``, ``NUL``, ``COM1``...) is prefixed with ``_``.

    :param stem: Rendered name, without extension.
    :return: Sanitized name.
    """
    cleaned = unicodedata.normalize("NFC", stem)
    cleaned = "".join("_" if char in INVALID_FILENAME_CHARS or ord(char) < 32 else char for char in cleaned)
    cleaned = truncate_utf8(cleaned.strip(" ."), MAX_STEM_BYTES).strip(" .") or "_"
    if cleaned.split(".")[0].rstrip(" ").upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def render_stem(pattern: str, date: datetime, label: str, original: str) -> str:
    """
    Render the file name pattern, without extension.

    :param pattern: Validated ``filename_pattern``, e.g. ``"{YYYY}-{MM}-{DD}_{DEVICE}"``.
    :param date: Effective date (its time zone information is ignored).
    :param label: Device label, used for ``{DEVICE}``.
    :param original: Original file name without extension, used for ``{ORIGINAL}``.
    :return: Sanitized name, without extension.
    :raises ValueError: If the pattern contains an unknown token.
    """
    values = {
        "YYYY": "{:04d}".format(date.year),
        "MM": "{:02d}".format(date.month),
        "DD": "{:02d}".format(date.day),
        "HH": "{:02d}".format(date.hour),
        "MIN": "{:02d}".format(date.minute),
        "SEC": "{:02d}".format(date.second),
        "DEVICE": label,
        "ORIGINAL": original,
    }

    def replace(match: "re.Match[str]") -> str:
        """
        Return the value of one pattern token.

        :param match: Token match.
        :return: Token value.
        :raises ValueError: If the token is unknown.
        """
        token = match.group(1)
        if token not in values:
            raise ValueError("unknown token '{{{}}}' in filename_pattern".format(token))
        return values[token]

    return sanitize_stem(TOKEN_RE.sub(replace, pattern))


def build_target_stem(analysis: FileAnalysis, pattern: str) -> str:
    """
    Build the target name (without extension and collision suffix) of a ready file.

    :param analysis: Analysis of a ready file, with its device.
    :param pattern: Validated ``filename_pattern``.
    :return: Sanitized name, without extension.
    :raises ValueError: If the file has no date or device, or the pattern has an unknown token.
    """
    date = analysis.effective.value if analysis.effective is not None else analysis.date
    if date is None or analysis.device is None:
        raise ValueError("{}: no date or device to build a name".format(analysis.relative.as_posix()))
    return render_stem(pattern, date, analysis.device["label"], analysis.relative.stem)


def folder_key(relative: Path) -> str:
    """
    Build a case-insensitive key for the folder of a relative path.

    :param relative: File path relative to the source folder.
    :return: Case-folded POSIX path of the parent folder.
    """
    return relative.parent.as_posix().casefold()


def gopro_file_id(stem: str) -> Optional[str]:
    """
    Extract the GoPro file id ending a base name.

    The id may follow a prefix added by another tool, separated by a
    non-alphanumeric character (e.g. ``2025-07-14_19-19-24_GH011892``).

    :param stem: File name without extension.
    :return: Case-folded id such as ``gh011892``, or ``None`` when absent.
    """
    match = GOPRO_FILE_ID_RE.search(stem.casefold())
    return match.group(1) if match else None


def is_auxiliary_file(relative: Path) -> bool:
    """
    Tell whether a file is a GoPro auxiliary file following a main video.

    ``.LRV`` and ``.THM`` files always are; ``.WAV`` files only when their name
    ends with a GoPro file id (GoPro "RAW audio").

    :param relative: File path relative to the source folder.
    :return: ``True`` for an auxiliary file.
    """
    suffix = relative.suffix.lower()
    if suffix in AUXILIARY_EXTENSIONS:
        return True
    return suffix in GOPRO_AUDIO_EXTENSIONS and gopro_file_id(relative.stem) is not None


def main_video_stems(auxiliary_stem: str) -> List[str]:
    """
    List the base names a GoPro auxiliary file may share with its main video.

    The same base name comes first; a HERO6+ low-resolution video ``GL<n>``
    also follows ``GH<n>`` then ``GX<n>``.

    :param auxiliary_stem: Name of the auxiliary file, without extension.
    :return: Case-folded candidate base names, by priority.
    """
    stem = auxiliary_stem.casefold()
    candidates = [stem]
    if stem.startswith(GOPRO_LRV_PREFIX):
        candidates.extend(prefix + stem[len(GOPRO_LRV_PREFIX):] for prefix in GOPRO_VIDEO_PREFIXES)
    return candidates


def group_auxiliaries(
    analyses: Sequence[FileAnalysis],
) -> Tuple[Dict[Path, List[FileAnalysis]], Dict[Path, str]]:
    """
    Link GoPro auxiliary files (``.LRV``, ``.THM``, ``.WAV``) to their main video.

    The main video must be in the same folder, have a ``MAIN_VIDEO_EXTENSIONS``
    extension, share the base name (see ``main_video_stems``) and be ready to
    rename. When no base name matches, the GoPro file ids ending both names
    are compared the same way (see ``gopro_file_id``), so files whose names
    were prefixed differently by another tool are still linked. A main video
    takes at most one auxiliary file per extension.

    :param analyses: File analyses, in plan order.
    :return: Tuple ``(auxiliaries, orphans)``: auxiliary analyses keyed by the
        relative path of their main video, and the warning of each auxiliary
        file left alone, keyed by its relative path.
    """
    mains = {}  # type: Dict[Tuple[str, str], FileAnalysis]
    mains_by_id = {}  # type: Dict[Tuple[str, str], FileAnalysis]
    for analysis in analyses:
        if analysis.status != STATUS_AUXILIARY and analysis.relative.suffix.lower() in MAIN_VIDEO_EXTENSIONS:
            folder = folder_key(analysis.relative)
            mains.setdefault((folder, analysis.relative.stem.casefold()), analysis)
            main_id = gopro_file_id(analysis.relative.stem)
            if main_id is not None:
                mains_by_id.setdefault((folder, main_id), analysis)
    auxiliaries = {}  # type: Dict[Path, List[FileAnalysis]]
    orphans = {}  # type: Dict[Path, str]
    for analysis in analyses:
        if analysis.status != STATUS_AUXILIARY:
            continue
        relative = analysis.relative
        folder = folder_key(relative)
        lookups = [(mains, stem) for stem in main_video_stems(relative.stem)]
        auxiliary_id = gopro_file_id(relative.stem)
        if auxiliary_id is not None:
            lookups.extend((mains_by_id, file_id) for file_id in main_video_stems(auxiliary_id))
        main = None
        for index, key in lookups:
            main = index.get((folder, key))
            if main is not None:
                break
        if main is None:
            orphans[relative] = "no main video with the same base name in its folder; not renamed"
            continue
        if main.status != STATUS_READY:
            orphans[relative] = "main video {} is skipped; not renamed".format(main.relative.name)
            continue
        group = auxiliaries.setdefault(main.relative, [])
        used = {main.relative.suffix.casefold()} | {member.relative.suffix.casefold() for member in group}
        if relative.suffix.casefold() in used:
            orphans[relative] = "another {} file already follows {}; not renamed".format(
                relative.suffix, main.relative.name)
            continue
        group.append(analysis)
    return auxiliaries, orphans


def list_existing_names(folder: Path) -> Set[str]:
    """
    List the names present in a folder (read only), as comparison keys.

    :param folder: Folder to list.
    :return: Entry names converted by ``name_key``; empty when the folder does not exist.
    """
    try:
        return {name_key(name) for name in os.listdir(str(folder))}
    except OSError:
        return set()


def resolve_collision(
    base_stem: str,
    extensions: Sequence[str],
    taken: Set[str],
    existing: Set[str],
    own_names: Set[str],
) -> str:
    """
    Choose a free name for a group of files sharing one base name.

    ``base_stem`` is tried first, then ``base_stem_01``, ``base_stem_02``...
    until every ``stem + extension`` of the group is free. Names are compared
    with ``name_key`` (case and Unicode normalization ignored), so the result is
    valid on case-insensitive and normalization-insensitive file systems.

    :param base_stem: Rendered name, without extension.
    :param extensions: Extensions of the group members, pairwise distinct (case-insensitively).
    :param taken: Keys (``name_key``) of the names already planned in the target folder.
    :param existing: Keys of the names already present in the target folder.
    :param own_names: Keys of the current names of the group members, which they may keep.
    :return: Chosen name, without extension.
    """
    counter = 0
    while True:
        stem = base_stem if counter == 0 else base_stem + COLLISION_SUFFIX_FORMAT.format(counter)
        names = [name_key(stem + extension) for extension in extensions]
        if all(name not in taken and (name not in existing or name in own_names) for name in names):
            return stem
        counter += 1


def build_plan(
    analyses: Sequence[FileAnalysis],
    pattern: str,
    source_root: Path,
    dest_root: Optional[Path] = None,
) -> Tuple[PlanEntry, ...]:
    """
    Build the rename plan, shared by preview and apply.

    Files are processed in a fixed order, so the same input always yields the
    same plan. Each file keeps its relative folder, under the source folder (in
    place) or under ``dest_root`` (copy). Collisions are resolved per target
    folder against the planned names and the files already present there;
    existing files are never targeted, except a file keeping its own name in
    place. GoPro auxiliary files take the final name of their main video with
    their own extension. Files are only listed, never modified.

    :param analyses: File analyses, with every device resolved and effective dates computed.
    :param pattern: Validated ``filename_pattern``.
    :param source_root: Source folder.
    :param dest_root: Destination folder of ``--dest``; ``None`` to rename in place.
    :return: One entry per analysis, sorted by relative source path (case-insensitively).
    """
    target_root = source_root if dest_root is None else dest_root
    ordered = sorted(analyses, key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix()))
    auxiliaries, orphans = group_auxiliaries(ordered)
    taken = {}  # type: Dict[str, Set[str]]
    existing = {}  # type: Dict[str, Set[str]]
    entries = {}  # type: Dict[str, PlanEntry]
    for analysis in ordered:
        relative = analysis.relative
        if analysis.status == STATUS_AUXILIARY:
            if relative in orphans:
                warning = orphans[relative]
                entries[relative.as_posix()] = PlanEntry(relative, None, ACTION_SKIP, warning, (warning,))
            continue
        if analysis.status != STATUS_READY:
            entries[relative.as_posix()] = PlanEntry(relative, None, ACTION_SKIP, analysis.message or analysis.status, ())
            continue
        try:
            base_stem = build_target_stem(analysis, pattern)
        except ValueError as error:
            entries[relative.as_posix()] = PlanEntry(relative, None, ACTION_SKIP, str(error), ())
            continue
        group = [analysis] + auxiliaries.get(relative, [])
        key = folder_key(relative)
        if key not in existing:
            existing[key] = list_existing_names(target_root / relative.parent)
        own_names = set() if dest_root is not None else {name_key(member.relative.name) for member in group}
        folder_taken = taken.setdefault(key, set())
        stem = resolve_collision(base_stem, [member.relative.suffix for member in group], folder_taken,
                                 existing[key], own_names)
        warnings = analysis.effective.warnings if analysis.effective is not None else ()
        for member in group:
            target = relative.parent / (stem + member.relative.suffix)
            folder_taken.add(name_key(target.name))
            if dest_root is not None:
                action = ACTION_COPY
            else:
                # Compare strings: Windows paths compare equal regardless of case.
                same = target.as_posix() == member.relative.as_posix()
                action = ACTION_UNCHANGED if same else ACTION_RENAME
            message = "" if member is analysis else "follows {}".format(relative.name)
            entries[member.relative.as_posix()] = PlanEntry(member.relative, target, action, message, warnings)
    return tuple(entries[analysis.relative.as_posix()] for analysis in ordered)


def print_plan(entries: Sequence[PlanEntry], out: TextIO) -> None:
    """
    Print the rename plan, one line per file, with its warnings.

    :param entries: Plan entries.
    :param out: Output stream.
    """
    labels = {ACTION_RENAME: "[RENAME]", ACTION_COPY: "[COPY]  ", ACTION_UNCHANGED: "[SAME]  ",
              ACTION_SKIP: "[SKIP]  "}
    print("Rename plan:", file=out)
    for entry in entries:
        line = "{} {}".format(labels.get(entry.action, entry.action), entry.source.as_posix())
        if entry.target is not None:
            line += " -> {}".format(entry.target.as_posix())
        if entry.message:
            line += "  ({})".format(entry.message)
        print(line, file=out)
        for warning in entry.warnings:
            if warning != entry.message:
                print("         warning: {}".format(warning), file=out)


def should_apply(apply_flag: bool, dry_run_flag: bool, dry_run_by_default: bool) -> bool:
    """
    Decide whether the plan is executed or only previewed.

    ``--dry-run`` always previews, ``--apply`` always executes; without either
    flag, ``dry_run_by_default`` from ``conf/config.json`` decides.

    :param apply_flag: Value of ``--apply``.
    :param dry_run_flag: Value of ``--dry-run``.
    :param dry_run_by_default: Value of ``dry_run_by_default``.
    :return: ``True`` when files must be renamed or copied.
    """
    if dry_run_flag:
        return False
    if apply_flag:
        return True
    return not dry_run_by_default


def is_same_file(first: Path, second: Path) -> bool:
    """
    Tell whether two paths designate the same file (e.g. a case-only difference).

    :param first: First path.
    :param second: Second path.
    :return: ``True`` when both paths exist and are the same file.
    """
    try:
        return os.path.samefile(str(first), str(second))
    except OSError:
        return False


def safe_rename(source: Path, target: Path, use_hard_link: Optional[bool] = None) -> None:
    """
    Rename a file without ever overwriting an existing one.

    A target that is the source itself (case-only rename on a case-insensitive
    file system) is allowed. On Windows, ``os.rename`` refuses existing targets.
    Elsewhere, a hard link is created first (it fails if the target exists),
    then the old name is removed; file systems without hard links (e.g. FAT or
    exFAT memory cards) fall back to a checked ``os.rename``.

    :param source: Existing file.
    :param target: New path, in an existing folder.
    :param use_hard_link: Use the hard link strategy; ``True`` everywhere but Windows when ``None``.
    :raises FileExistsError: If the target already exists.
    :raises OSError: If the file cannot be renamed.
    """
    link = os.name != "nt" if use_hard_link is None else use_hard_link
    if os.path.lexists(str(target)):
        if not is_same_file(source, target):
            raise FileExistsError(errno.EEXIST, "target already exists", str(target))
        os.rename(str(source), str(target))
        return
    if not link or os.path.islink(str(source)):
        os.rename(str(source), str(target))
        return
    try:
        os.link(str(source), str(target))
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(str(target)):
            raise FileExistsError(errno.EEXIST, "target already exists", str(target))
        os.rename(str(source), str(target))
        return
    os.unlink(str(source))


def safe_copy(source: Path, target: Path) -> None:
    """
    Copy a file without ever overwriting an existing one.

    Missing parent folders of the target are created; the target is created
    exclusively, its content then times and permissions are copied. A partial
    copy is removed on failure.

    :param source: Existing file.
    :param target: Path of the copy.
    :raises FileExistsError: If the target already exists.
    :raises NotADirectoryError: If a file stands where a parent folder of the target is needed.
    :raises OSError: If the file cannot be copied.
    """
    with open(str(source), "rb") as reader:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # A file stands where a folder is needed: not a "target exists" case.
            raise NotADirectoryError(errno.ENOTDIR, "a file stands where a folder is needed", str(target.parent))
        with open(str(target), "xb") as writer:
            try:
                shutil.copyfileobj(reader, writer)
            except BaseException:
                writer.close()
                os.remove(str(target))
                raise
    shutil.copystat(str(source), str(target))


def execute_plan(
    entries: Sequence[PlanEntry], source_root: Path, dest_root: Optional[Path] = None
) -> List[OperationResult]:
    """
    Execute the rename plan built by ``build_plan``.

    Renames happen in place when ``dest_root`` is ``None``; otherwise files are
    copied under ``dest_root`` and the sources are left intact. Only folders
    receiving a file are created. An existing file is never overwritten: the
    entry fails with a clear message and the other entries go on.

    :param entries: Plan entries.
    :param source_root: Source folder.
    :param dest_root: Destination folder of ``--dest``; ``None`` to rename in place.
    :return: One result per entry, in plan order.
    """
    target_root = source_root if dest_root is None else dest_root
    results = []  # type: List[OperationResult]
    for entry in entries:
        if entry.action == ACTION_UNCHANGED:
            results.append(OperationResult(entry, RESULT_UNCHANGED, "already named"))
            continue
        if entry.action not in (ACTION_RENAME, ACTION_COPY) or entry.target is None:
            results.append(OperationResult(entry, RESULT_SKIPPED, entry.message))
            continue
        source = source_root / entry.source
        target = target_root / entry.target
        try:
            if entry.action == ACTION_COPY:
                safe_copy(source, target)
            else:
                safe_rename(source, target)
        except FileExistsError:
            message = "target already exists, not overwritten"
            results.append(OperationResult(entry, RESULT_FAILED, message))
        except OSError as error:
            results.append(OperationResult(entry, RESULT_FAILED, "{}: {}".format(
                type(error).__name__, error.strerror or error)))
        else:
            results.append(OperationResult(entry, RESULT_DONE, ""))
    return results


def count_results(results: Iterable[OperationResult]) -> Dict[str, int]:
    """
    Count the results of an executed plan.

    :param results: Operation results.
    :return: Counts keyed by ``renamed``, ``copied``, ``unchanged``, ``skipped`` and ``errors``.
    """
    counts = {"renamed": 0, "copied": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    for result in results:
        if result.status == RESULT_DONE:
            counts["copied" if result.entry.action == ACTION_COPY else "renamed"] += 1
        elif result.status == RESULT_FAILED:
            counts["errors"] += 1
        elif result.status == RESULT_UNCHANGED:
            counts["unchanged"] += 1
        else:
            counts["skipped"] += 1
    return counts


def print_results(results: Sequence[OperationResult], out: TextIO) -> None:
    """
    Print the performed operations, their errors and a summary.

    :param results: Operation results.
    :param out: Output stream.
    """
    labels = {ACTION_RENAME: "[RENAMED]", ACTION_COPY: "[COPIED] "}
    print("Results:", file=out)
    for result in results:
        if result.status not in (RESULT_DONE, RESULT_FAILED):
            continue
        label = labels.get(result.entry.action, "[DONE]   ") if result.status == RESULT_DONE else "[ERROR]  "
        line = "{} {} -> {}".format(label, result.entry.source.as_posix(),
                                    result.entry.target.as_posix() if result.entry.target else "?")
        if result.message:
            line += "  ({})".format(result.message)
        print(line, file=out)


def count_reasons(reasons: Iterable[str]) -> Tuple[Tuple[str, int], ...]:
    """
    Count identical reasons.

    :param reasons: One reason per file; empty reasons are counted as ``"no reason given"``.
    :return: Pairs ``(reason, count)``, most frequent first, then in alphabetical order.
    """
    counts = {}  # type: Dict[str, int]
    for reason in reasons:
        key = reason or "no reason given"
        counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def summarize_run(entries: Sequence[PlanEntry], results: Optional[Sequence[OperationResult]] = None) -> RunSummary:
    """
    Build the run summary from the plan and, after execution, its results.

    Without results (preview), renames and copies are the planned ones and
    there is no error. Warnings are those attached to the plan entries.

    :param entries: Plan entries.
    :param results: Operation results; ``None`` in preview.
    :return: Run summary.
    """
    warnings = sum(len(entry.warnings) for entry in entries)
    if results is None:
        counts = {"renamed": 0, "copied": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        names = {ACTION_RENAME: "renamed", ACTION_COPY: "copied", ACTION_UNCHANGED: "unchanged"}
        for entry in entries:
            counts[names.get(entry.action, "skipped")] += 1
        skipped = [entry.message for entry in entries if entry.action not in names]
        counts["warnings"] = warnings
        return RunSummary(False, counts, count_reasons(skipped), ())
    counts = count_results(results)
    counts["warnings"] = warnings
    skipped = [result.message for result in results if result.status == RESULT_SKIPPED]
    failed = [result.message for result in results if result.status == RESULT_FAILED]
    return RunSummary(True, counts, count_reasons(skipped), count_reasons(failed))


def format_summary_lines(summary: RunSummary) -> List[str]:
    """
    Format a run summary for the console and the log.

    :param summary: Run summary.
    :return: A counts line, then one line per skip or error reason.
    """
    counts = summary.counts
    if summary.applied:
        line = "Summary: {renamed} renamed, {copied} copied, {unchanged} unchanged, {skipped} skipped, " \
               "{errors} error(s), {warnings} warning(s).".format(**counts)
    else:
        line = "Summary (preview): {renamed} to rename, {copied} to copy, {unchanged} unchanged, " \
               "{skipped} skipped, {warnings} warning(s).".format(**counts)
    lines = [line]
    lines.extend("  skipped ({}): {}".format(count, reason) for reason, count in summary.skip_reasons)
    lines.extend("  error ({}): {}".format(count, reason) for reason, count in summary.error_reasons)
    return lines


def print_summary(summary: RunSummary, out: TextIO) -> None:
    """
    Print a run summary.

    :param summary: Run summary.
    :param out: Output stream.
    """
    for line in format_summary_lines(summary):
        print(line, file=out)


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """
    Mask every occurrence of personal values (such as serial numbers) in a text.

    Values shorter than ``MIN_REDACTED_LENGTH`` are left alone: they could match
    ordinary text such as dates. Matching ignores case.

    :param text: Text to clean.
    :param secrets: Values to hide.
    :return: Text where each value is replaced by its masked form (see ``mask_serial``).
    """
    for secret in sorted({value for value in secrets if len(value) >= MIN_REDACTED_LENGTH}, key=len, reverse=True):
        text = re.sub(re.escape(secret), mask_serial(secret), text, flags=re.IGNORECASE)
    return text


class PrivacyFilter(logging.Filter):
    """Logging filter masking personal values (such as serial numbers) in every record."""

    def __init__(self) -> None:
        """
        Create a filter without any value to hide.
        """
        super().__init__()
        self.secrets = set()  # type: Set[str]

    def add_secrets(self, values: Iterable[str]) -> None:
        """
        Register more values to hide.

        :param values: Values to hide; empty values are ignored.
        """
        self.secrets.update(value for value in values if value)

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Replace the message of a record by its redacted form.

        :param record: Log record, modified in place.
        :return: Always ``True``: records are cleaned, never dropped.
        """
        record.msg = redact_text(record.getMessage(), self.secrets)
        record.args = None
        return True


def collect_secrets(analyses: Iterable[FileAnalysis], devices: Iterable[Mapping[str, Any]]) -> List[str]:
    """
    List the personal values that must never appear in the log.

    :param analyses: File analyses (their serial numbers are collected).
    :param devices: Device entries (their ``match.serial_number`` values are collected).
    :return: Non-empty serial numbers.
    """
    found = [analysis.identity.serial_number for analysis in analyses]
    found.extend(clean_text((device.get("match") or {}).get("serial_number")) for device in devices)
    return [value for value in found if value]


def setup_logging(log_dir: Path, level: str) -> Tuple[logging.Logger, Optional[str]]:
    """
    Configure the run logger, appending to ``<log_dir>/rename.log``.

    Handlers from a previous run are closed first. A privacy filter masks the
    values registered with ``add_secrets`` on it. When the log file cannot be
    opened, the run goes on without a log file.

    :param log_dir: Folder of the log file, created if needed.
    :param level: Validated ``log_level`` of ``conf/config.json``.
    :return: Tuple ``(logger, error)``; ``error`` describes why the log file could not be opened, else ``None``.
    """
    logger = logging.getLogger(LOGGER_NAME)
    close_logging(logger)
    logger.setLevel(level)
    logger.propagate = False
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_dir / LOG_FILE_NAME), encoding="utf-8")  # type: logging.Handler
        error = None
    except OSError as exc:
        handler = logging.NullHandler()
        error = "cannot write {}: {}".format(log_dir / LOG_FILE_NAME, exc.strerror or exc)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(PrivacyFilter())
    logger.addHandler(handler)
    return logger, error


def get_privacy_filter(logger: logging.Logger) -> Optional[PrivacyFilter]:
    """
    Find the privacy filter installed by ``setup_logging``.

    :param logger: Run logger.
    :return: The first privacy filter of the logger's handlers, or ``None``.
    """
    for handler in logger.handlers:
        for item in handler.filters:
            if isinstance(item, PrivacyFilter):
                return item
    return None


def close_logging(logger: logging.Logger) -> None:
    """
    Close and remove every handler of a logger.

    :param logger: Logger to clean up.
    """
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def log_plan(logger: logging.Logger, entries: Sequence[PlanEntry]) -> None:
    """
    Log the plan: planned operations at DEBUG, skips at INFO, warnings at WARNING.

    :param logger: Run logger.
    :param entries: Plan entries.
    """
    for entry in entries:
        source = entry.source.as_posix()
        if entry.target is None:
            logger.info("skip %s (%s)", source, entry.message or "no reason given")
        else:
            logger.debug("plan %s %s -> %s", entry.action, source, entry.target.as_posix())
        for warning in entry.warnings:
            if warning != entry.message or entry.target is not None:
                logger.warning("%s: %s", source, warning)


def log_results(logger: logging.Logger, results: Sequence[OperationResult]) -> None:
    """
    Log the performed operations at INFO and the failed ones at ERROR.

    :param logger: Run logger.
    :param results: Operation results.
    """
    names = {ACTION_RENAME: "renamed", ACTION_COPY: "copied"}
    for result in results:
        entry = result.entry
        target = entry.target.as_posix() if entry.target is not None else "?"
        if result.status == RESULT_DONE:
            logger.info("%s %s -> %s", names.get(entry.action, entry.action), entry.source.as_posix(), target)
        elif result.status == RESULT_FAILED:
            logger.error("failed %s %s -> %s (%s)", entry.action, entry.source.as_posix(), target, result.message)


def log_summary(logger: logging.Logger, summary: RunSummary) -> None:
    """
    Log a run summary at INFO.

    :param logger: Run logger.
    :param summary: Run summary.
    """
    for line in format_summary_lines(summary):
        logger.info("%s", line.strip())


def report_error(logger: logging.Logger, message: str) -> None:
    """
    Print an error on the standard error stream and log it.

    :param logger: Run logger.
    :param message: Error message.
    """
    print("[ERROR] {}".format(message), file=sys.stderr)
    logger.error("%s", message)


def report_warning(logger: logging.Logger, message: str) -> None:
    """
    Print a warning on the standard error stream and log it.

    :param logger: Run logger.
    :param message: Warning message.
    """
    print("[WARNING] {}".format(message), file=sys.stderr)
    logger.warning("%s", message)


def run_check(
    project_root: Path,
    timezone: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """
    Validate the configuration and report the ExifTool version and location.

    Every check runs, so all problems are reported at once.

    :param project_root: Project folder containing ``conf/`` and ``bin/``.
    :param timezone: Optional ``--tz`` value to validate.
    :param environ: Environment variables; ``os.environ`` when ``None``.
    :param out: Stream for success messages; ``sys.stdout`` when ``None``.
    :param err: Stream for error messages; ``sys.stderr`` when ``None``.
    :return: Exit code: 0 when every check passed, 1 otherwise.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    conf_dir = project_root / CONF_DIR_NAME
    failed = False
    try:
        config = load_config(conf_dir / CONFIG_FILE_NAME)
        print("[OK] Configuration: {}".format(conf_dir / CONFIG_FILE_NAME), file=out)
        print("     filename_pattern: {}".format(config["filename_pattern"]), file=out)
    except ConfigError as error:
        print("[ERROR] Configuration: {}".format(error), file=err)
        failed = True
    try:
        devices = load_devices(conf_dir / DEVICES_FILE_NAME)
        print("[OK] Devices: {} ({} device(s))".format(conf_dir / DEVICES_FILE_NAME, len(devices)), file=out)
    except ConfigError as error:
        print("[ERROR] Devices: {}".format(error), file=err)
        failed = True
    if has_timezone_database():
        print("[OK] Time zone database: {} zone(s)".format(len(get_available_timezones())), file=out)
    else:
        print("[WARNING] Time zone database: {}; degraded mode, local times are kept".format(
            describe_missing_timezone_database()), file=out)
    if timezone is not None:
        tz_error, tz_warning = check_cli_timezone(timezone)
        if tz_error:
            print("[ERROR] --tz: {}".format(tz_error), file=err)
            failed = True
        elif tz_warning:
            print("[WARNING] --tz: {}".format(tz_warning), file=out)
        else:
            print("[OK] Time zone: {}".format(timezone), file=out)
    try:
        executable = find_exiftool(project_root / BIN_DIR_NAME, environ)
        version = get_exiftool_version(executable)
        print("[OK] ExifTool {}: {}".format(version, executable), file=out)
    except ExifToolError as error:
        print("[ERROR] ExifTool: {}".format(error), file=err)
        failed = True
    return 1 if failed else 0


def load_devices_quietly(path: Path) -> List[Dict[str, Any]]:
    """
    Load the device entries, ignoring any error.

    Used to learn the serial numbers registered during the run, so they can be hidden from the log.

    :param path: Path of ``conf/devices.json``.
    :return: Device entries, or an empty list when the file cannot be loaded.
    """
    try:
        return load_devices(path)
    except ConfigError:
        return []


def run_batch(
    args: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    devices: List[Dict[str, Any]],
    logger: logging.Logger,
    environ: Optional[Mapping[str, str]] = None,
    interactive: bool = False,
    input_func: Callable[[str], str] = input,
) -> int:
    """
    Analyze the source folder, print the plan and, when requested, execute it.

    Every step is logged; the run ends with a summary on the console and in the log.

    :param args: Parsed command line arguments.
    :param root: Project folder containing ``conf/`` and ``bin/``.
    :param config: Validated ``conf/config.json``.
    :param devices: Validated device entries.
    :param logger: Run logger set up by ``setup_logging``.
    :param environ: Environment variables; ``os.environ`` when ``None``.
    :param interactive: Whether unknown devices can be asked to the user.
    :param input_func: Function reading one line, such as ``input``.
    :return: Process exit code (0 on success).
    """
    devices_path = root / CONF_DIR_NAME / DEVICES_FILE_NAME
    privacy = get_privacy_filter(logger) or PrivacyFilter()
    privacy.add_secrets(collect_secrets((), devices))
    if args.tz is not None:
        tz_error, tz_warning = check_cli_timezone(args.tz)
        if tz_error:
            report_error(logger, "--tz: {}".format(tz_error))
            return 1
        if tz_warning:
            report_warning(logger, "--tz: {}".format(tz_warning))
    elif any(device.get("timezone") for device in devices) and not has_timezone_database():
        report_warning(logger, "Device time zones ignored, local times kept: {}".format(
            describe_missing_timezone_database()))
    forced_device = None
    if args.device is not None:
        forced_device = find_device(devices, args.device)
        if forced_device is None:
            report_error(logger, format_unknown_forced_device_error(args.device, devices))
            return 1
    source = Path(args.source)
    if not source.is_dir():
        report_error(logger, "--source: folder not found: {}".format(source))
        return 1
    dest_root = Path(args.dest) if args.dest else None
    applied = should_apply(args.apply, args.dry_run, config["dry_run_by_default"])
    logger.info("run started: %s, source %s%s%s", "apply" if applied else "preview", source,
                ", destination {}".format(dest_root) if dest_root else "", ", recursive" if args.recursive else "")
    excluded = [root / LOGS_DIR_NAME] + ([dest_root] if dest_root else [])
    try:
        executable = find_exiftool(root / BIN_DIR_NAME, environ)
        analyses = analyze_source(
            source, config["supported_extensions"], devices, devices_path, executable,
            args.recursive, forced_device, excluded, interactive, input_func, cli_tz=args.tz,
        )
    except (ConfigError, ExifToolError, UnknownDeviceError) as error:
        report_error(logger, str(error))
        return 1
    # Devices registered during the run may hold serial numbers too.
    privacy.add_secrets(collect_secrets(analyses, load_devices_quietly(devices_path)))
    print_analysis(analyses, sys.stdout)
    # The same plan is printed and executed, so preview and apply always agree.
    plan = build_plan(analyses, config["filename_pattern"], source, dest_root)
    print_plan(plan, sys.stdout)
    log_plan(logger, plan)
    results = None  # type: Optional[List[OperationResult]]
    if applied:
        results = execute_plan(plan, source, dest_root)
        print_results(results, sys.stdout)
        log_results(logger, results)
    summary = summarize_run(plan, results)
    print_summary(summary, sys.stdout)
    log_summary(logger, summary)
    if not applied:
        print("Preview only: no file was changed. Use --apply to perform these operations.")
    return 1 if summary.counts["errors"] else 0


def main(
    argv: Optional[List[str]] = None,
    project_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    interactive: Optional[bool] = None,
    input_func: Callable[[str], str] = input,
) -> int:
    """
    Run the MediaRenamer command line interface.

    :param argv: Command line arguments, defaults to ``sys.argv[1:]`` when ``None``.
    :param project_root: Project folder containing ``conf/`` and ``bin/``; this file's folder when ``None``.
    :param environ: Environment variables; ``os.environ`` when ``None``.
    :param interactive: Whether unknown devices can be asked; detected from the terminal when ``None``.
    :param input_func: Function reading one line, such as ``input``.
    :return: Process exit code (0 on success).
    """
    parser = build_arg_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.check and not args.source:
        parser.error("--source is required unless --check is given")
    root = PROJECT_ROOT if project_root is None else project_root
    conf_dir = root / CONF_DIR_NAME
    try:
        for created in ensure_personal_files(conf_dir):
            print("Created {} from its template. Back it up: it is not versioned.".format(created))
    except ConfigError as error:
        print("[ERROR] {}".format(error), file=sys.stderr)
        return 1
    if args.check:
        return run_check(root, args.tz, environ)
    try:
        config = load_config(conf_dir / CONFIG_FILE_NAME)
        devices = load_devices(conf_dir / DEVICES_FILE_NAME)
    except ConfigError as error:
        print("[ERROR] {}".format(error), file=sys.stderr)
        return 1
    logger, log_error = setup_logging(root / LOGS_DIR_NAME, config["log_level"])
    if log_error:
        print("[WARNING] Log file disabled: {}".format(log_error), file=sys.stderr)
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    try:
        return run_batch(args, root, config, devices, logger, environ, is_interactive, input_func)
    finally:
        close_logging(logger)


if __name__ == "__main__":
    sys.exit(main())
