"""MediaRenamer entry point: renames photos and videos from their metadata."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
AUXILIARY_EXTENSIONS = (".lrv", ".thm")

# Capture date tags, by decreasing priority. The first one holding a valid date
# wins. File system dates (FileModifyDate...) are never used.
DATE_TAGS = ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate")
# EXIF 2.31 offset tags giving the UTC offset of a zone-less date tag.
DATE_OFFSET_TAGS = {"DateTimeOriginal": "OffsetTimeOriginal", "CreateDate": "OffsetTimeDigitized"}
IDENTITY_TAGS = ("Make", "Model", "SerialNumber")
# Only these tags are requested from ExifTool, so GPS and other personal data
# never enter the program.
METADATA_TAGS = DATE_TAGS + tuple(DATE_OFFSET_TAGS.values()) + IDENTITY_TAGS + ("Error",)
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the renames or copies (preview only by default).",
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

    GoPro auxiliary files (``.LRV``, ``.THM``) are only flagged: they take the
    date and device of their main video later. Files without readable metadata,
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
    if relative.suffix.lower() in AUXILIARY_EXTENSIONS:
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
    return FileAnalysis(relative, STATUS_UNKNOWN_DEVICE, "", date_tag, date, identity, None)


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
) -> Dict[str, Any]:
    """
    Ask the user to describe an unknown device.

    The serial number is only used as a criterion when the user asks for it,
    so files of the same model without a serial number still match.

    :param identity: Identification of the unknown device.
    :param devices: Existing device entries, used to build a unique id.
    :param input_func: Function reading one line, such as ``input``.
    :param out: Stream for messages.
    :return: New, valid device entry.
    :raises EOFError: If the input stream is closed.
    """
    print("\nUnknown device: {}".format(describe_identity(identity)), file=out)
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
            device = prompt_new_device(unknown[0], known, input_func, out)
        except EOFError:
            raise UnknownDeviceError(format_unknown_devices_error(unknown))
        known.append(device)
        write_devices_file(devices_path, {"devices": known})
        print("  Saved device '{}' to {}".format(device["id"], devices_path), file=out)
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
    if args.tz is not None:
        tz_error, tz_warning = check_cli_timezone(args.tz)
        if tz_error:
            print("[ERROR] --tz: {}".format(tz_error), file=sys.stderr)
            return 1
        if tz_warning:
            print("[WARNING] --tz: {}".format(tz_warning), file=sys.stderr)
    elif any(device.get("timezone") for device in devices) and not has_timezone_database():
        print("[WARNING] Device time zones ignored, local times kept: {}".format(
            describe_missing_timezone_database()), file=sys.stderr)
    forced_device = None
    if args.device is not None:
        forced_device = find_device(devices, args.device)
        if forced_device is None:
            print("[ERROR] --device: unknown device id {!r}".format(args.device), file=sys.stderr)
            return 1
    source = Path(args.source)
    if not source.is_dir():
        print("[ERROR] --source: folder not found: {}".format(source), file=sys.stderr)
        return 1
    excluded = [root / LOGS_DIR_NAME] + ([Path(args.dest)] if args.dest else [])
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    try:
        executable = find_exiftool(root / BIN_DIR_NAME, environ)
        analyses = analyze_source(
            source, config["supported_extensions"], devices, conf_dir / DEVICES_FILE_NAME, executable,
            args.recursive, forced_device, excluded, is_interactive, input_func, cli_tz=args.tz,
        )
    except (ConfigError, ExifToolError, UnknownDeviceError) as error:
        print("[ERROR] {}".format(error), file=sys.stderr)
        return 1
    print_analysis(analyses, sys.stdout)
    if args.apply:
        print("[ERROR] Renaming and copying are not implemented yet (see docs/BACKLOG.md, Epic 4+).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
