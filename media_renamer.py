"""MediaRenamer entry point: renames photos and videos from their metadata."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, TextIO

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


class ConfigError(Exception):
    """Raised when a configuration file is missing, unreadable or invalid."""


class ExifToolError(Exception):
    """Raised when ExifTool cannot be found or does not run correctly."""


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
    if timezone is not None:
        tz_error = validate_timezone(timezone)
        if tz_error:
            print("[ERROR] --tz: {}".format(tz_error), file=err)
            failed = True
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
) -> int:
    """
    Run the MediaRenamer command line interface.

    :param argv: Command line arguments, defaults to ``sys.argv[1:]`` when ``None``.
    :param project_root: Project folder containing ``conf/`` and ``bin/``; this file's folder when ``None``.
    :param environ: Environment variables; ``os.environ`` when ``None``.
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
        load_config(conf_dir / CONFIG_FILE_NAME)
        devices = load_devices(conf_dir / DEVICES_FILE_NAME)
    except ConfigError as error:
        print("[ERROR] {}".format(error), file=sys.stderr)
        return 1
    if args.tz is not None and validate_timezone(args.tz):
        print("[ERROR] --tz: {}".format(validate_timezone(args.tz)), file=sys.stderr)
        return 1
    if args.device is not None and find_device(devices, args.device) is None:
        print("[ERROR] --device: unknown device id {!r}".format(args.device), file=sys.stderr)
        return 1
    print("[ERROR] Renaming is not implemented yet (see docs/BACKLOG.md, Epic 2+).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
