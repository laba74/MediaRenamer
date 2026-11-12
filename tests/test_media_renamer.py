"""Unit tests for the ``media_renamer`` module."""

import io
import json
import os
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import media_renamer as mr


REPO_ROOT = Path(__file__).resolve().parent.parent
EXE_NAME = mr.exiftool_executable_name()


def make_device(**overrides: Any) -> Dict[str, Any]:
    """
    Build a valid device entry, optionally overriding some fields.

    :param overrides: Fields to replace or add.
    :return: Device entry.
    """
    device = {
        "id": "gopro9",
        "label": "GoPro9",
        "match": {"make": "GoPro", "model": "HERO9 Black", "serial_number": ""},
        "offset": "-01:00:00",
    }
    device.update(overrides)
    return device


def write_json(path: Path, data: Any) -> Path:
    """
    Write a JSON document to a file.

    :param path: Target file.
    :param data: Data to serialize.
    :return: The written path.
    """
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_fake_exe(path: Path) -> Path:
    """
    Create an empty executable file (parents included).

    :param path: Path of the file to create.
    :return: The created path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    os.chmod(str(path), 0o755)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """
    Create a project folder with the real ``*.default.json`` templates.

    :param tmp_path: Pytest temporary folder.
    :return: Project root.
    """
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in ("config.default.json", "devices.default.json"):
        shutil.copy(str(REPO_ROOT / "conf" / name), str(conf / name))
    (tmp_path / "bin").mkdir()
    return tmp_path


@pytest.fixture
def config_data() -> Dict[str, Any]:
    """
    Return the content of the default configuration template.

    :return: Parsed ``conf/config.default.json``.
    """
    return json.loads((REPO_ROOT / "conf" / "config.default.json").read_text(encoding="utf-8"))


# --- build_arg_parser ---------------------------------------------------------


def test_build_arg_parser_parses_every_option() -> None:
    """
    Check that every option of the plan is parsed.
    """
    args = mr.build_arg_parser().parse_args(
        ["--source", "in", "--dest", "out", "--apply", "--recursive",
         "--device", "gopro9", "--tz", "Europe/Paris", "--check"]
    )
    assert (args.source, args.dest, args.device, args.tz) == ("in", "out", "gopro9", "Europe/Paris")
    assert args.apply and args.recursive and args.check


def test_build_arg_parser_defaults() -> None:
    """
    Check that flags default to ``False`` and values to ``None``.
    """
    args = mr.build_arg_parser().parse_args([])
    assert not args.apply and not args.recursive and not args.check
    assert args.source is None and args.dest is None and args.device is None and args.tz is None


def test_build_arg_parser_help_lists_every_option(capsys: pytest.CaptureFixture) -> None:
    """
    Check that ``--help`` lists every option of the plan.
    """
    with pytest.raises(SystemExit) as exc:
        mr.build_arg_parser().parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for option in ("--source", "--dest", "--apply", "--recursive", "--device", "--tz", "--check", "--help"):
        assert option in out


# --- ensure_personal_files ----------------------------------------------------


def test_ensure_personal_files_creates_missing_files(project: Path) -> None:
    """
    Check that missing personal files are copied from their templates.
    """
    conf = project / "conf"
    created = mr.ensure_personal_files(conf)
    assert created == [conf / "config.json", conf / "devices.json"]
    assert (conf / "config.json").read_bytes() == (conf / "config.default.json").read_bytes()
    assert (conf / "devices.json").read_bytes() == (conf / "devices.default.json").read_bytes()


def test_ensure_personal_files_never_overwrites(project: Path) -> None:
    """
    Check that existing personal files are left untouched.
    """
    conf = project / "conf"
    (conf / "config.json").write_text("personal config", encoding="utf-8")
    (conf / "devices.json").write_text("personal devices", encoding="utf-8")
    assert mr.ensure_personal_files(conf) == []
    assert (conf / "config.json").read_text(encoding="utf-8") == "personal config"
    assert (conf / "devices.json").read_text(encoding="utf-8") == "personal devices"


def test_ensure_personal_files_creates_only_the_missing_one(project: Path) -> None:
    """
    Check that only the missing personal file is created.
    """
    conf = project / "conf"
    (conf / "config.json").write_text("personal config", encoding="utf-8")
    assert mr.ensure_personal_files(conf) == [conf / "devices.json"]
    assert (conf / "config.json").read_text(encoding="utf-8") == "personal config"


def test_ensure_personal_files_missing_template(tmp_path: Path) -> None:
    """
    Check that a missing template raises ``ConfigError``.
    """
    with pytest.raises(mr.ConfigError, match="template"):
        mr.ensure_personal_files(tmp_path)


# --- load_json_file -----------------------------------------------------------


def test_load_json_file_valid(tmp_path: Path) -> None:
    """
    Check that a valid JSON file is parsed, even with a UTF-8 BOM.
    """
    path = tmp_path / "a.json"
    path.write_bytes(b'\xef\xbb\xbf{"a": 1}')
    assert mr.load_json_file(path) == {"a": 1}


def test_load_json_file_invalid_json(tmp_path: Path) -> None:
    """
    Check that invalid JSON is reported with its position.
    """
    path = tmp_path / "a.json"
    path.write_text('{"a": 1,}', encoding="utf-8")
    with pytest.raises(mr.ConfigError, match="invalid JSON at line 1"):
        mr.load_json_file(path)


def test_load_json_file_missing(tmp_path: Path) -> None:
    """
    Check that a missing file raises ``ConfigError``.
    """
    with pytest.raises(mr.ConfigError, match="file not found"):
        mr.load_json_file(tmp_path / "missing.json")


def test_load_json_file_unreadable(tmp_path: Path) -> None:
    """
    Check that a non UTF-8 file raises ``ConfigError``.
    """
    path = tmp_path / "a.json"
    path.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(mr.ConfigError, match="cannot be read"):
        mr.load_json_file(path)


# --- find_invalid_filename_chars ----------------------------------------------


def test_find_invalid_filename_chars() -> None:
    """
    Check detection of reserved and control characters, without duplicates.
    """
    assert mr.find_invalid_filename_chars("ok_name-1") == []
    assert mr.find_invalid_filename_chars('a<b>c:d"e/f\\g|h?i*j<') == list('<>:"/\\|?*')
    assert mr.find_invalid_filename_chars("tab\there") == ["\t"]


# --- validate_filename_pattern ------------------------------------------------


def test_validate_filename_pattern_valid() -> None:
    """
    Check that the default pattern and every token are accepted.
    """
    assert mr.validate_filename_pattern("{YYYY}-{MM}-{DD}_{HH}-{MIN}-{SEC}_{DEVICE}") == []
    assert mr.validate_filename_pattern("{ORIGINAL} ({DEVICE})") == []


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("{YYYY}_{FOO}", "unknown token '{FOO}'"),
        ("{yyyy}", "unknown token '{yyyy}'"),
        ("photo", "at least one token"),
        ("{YYYY}/{MM}", "path separators"),
        ("..\\{YYYY}", "path separators"),
        ("{YYYY}:{MM}", "invalid characters"),
        ("{YYYY}*", "invalid characters"),
        ("{YYYY}_{MM", "unbalanced"),
        ("{YYYY}}", "unbalanced"),
        (".{YYYY}", "must not start with '.'"),
        ("{YYYY}.", "must not end"),
        ("{YYYY} ", "must not end"),
        ("", "non-empty string"),
        (42, "non-empty string"),
    ],
)
def test_validate_filename_pattern_invalid(pattern: Any, expected: str) -> None:
    """
    Check that invalid patterns are rejected with an explicit message.
    """
    errors = mr.validate_filename_pattern(pattern)
    assert any(expected in error for error in errors), errors


# --- validate_extensions ------------------------------------------------------


def test_validate_extensions_valid() -> None:
    """
    Check that a list of dotted extensions is accepted.
    """
    assert mr.validate_extensions([".jpg", ".MP4", ".cr3"]) == []


@pytest.mark.parametrize(
    "extensions, expected",
    [
        ([], "non-empty list"),
        (".jpg", "non-empty list"),
        (["jpg"], "invalid extension"),
        ([".j/pg"], "invalid extension"),
        ([3], "invalid extension"),
        ([".jpg", ".JPG"], "duplicate extension"),
    ],
)
def test_validate_extensions_invalid(extensions: Any, expected: str) -> None:
    """
    Check that invalid extension lists are rejected.
    """
    assert any(expected in error for error in mr.validate_extensions(extensions))


# --- validate_config / load_config --------------------------------------------


def test_validate_config_default_is_valid(config_data: Dict[str, Any]) -> None:
    """
    Check that the default configuration template is valid.
    """
    assert mr.validate_config(config_data) == []


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"extra": 1}, "unknown setting 'extra'"),
        ({"dry_run_by_default": "yes"}, "dry_run_by_default must be true or false"),
        ({"log_level": "VERBOSE"}, "log_level must be one of"),
        ({"filename_pattern": "{BAD}"}, "unknown token"),
        ({"supported_extensions": []}, "non-empty list"),
    ],
)
def test_validate_config_invalid_values(config_data: Dict[str, Any], change: Dict[str, Any], expected: str) -> None:
    """
    Check that invalid settings are reported.
    """
    config_data.update(change)
    assert any(expected in error for error in mr.validate_config(config_data))


def test_validate_config_missing_setting(config_data: Dict[str, Any]) -> None:
    """
    Check that a missing setting is reported.
    """
    del config_data["log_level"]
    assert "missing setting 'log_level'" in mr.validate_config(config_data)


def test_validate_config_not_an_object() -> None:
    """
    Check that a non-object configuration is rejected.
    """
    assert mr.validate_config([]) == ["the configuration must be a JSON object"]


def test_load_config_normalizes_extensions(tmp_path: Path, config_data: Dict[str, Any]) -> None:
    """
    Check that a valid configuration loads with lower-case extensions.
    """
    config_data["supported_extensions"] = [".JPG", ".Mp4"]
    config = mr.load_config(write_json(tmp_path / "config.json", config_data))
    assert config["supported_extensions"] == [".jpg", ".mp4"]


def test_load_config_invalid(tmp_path: Path, config_data: Dict[str, Any]) -> None:
    """
    Check that an invalid configuration raises ``ConfigError`` listing the errors.
    """
    config_data["filename_pattern"] = "{NOPE}"
    with pytest.raises(mr.ConfigError, match="unknown token"):
        mr.load_config(write_json(tmp_path / "config.json", config_data))


# --- parse_offset -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("00:00:00", 0),
        ("+01:00:00", 3600),
        ("-01:00:00", -3600),
        ("-00:00:30", -30),
        ("01:02:03", 3723),
        ("48:00:00", 172800),
    ],
)
def test_parse_offset_valid(text: str, seconds: int) -> None:
    """
    Check that signed ``HH:MM:SS`` offsets are converted to seconds.
    """
    assert mr.parse_offset(text) == seconds


@pytest.mark.parametrize("text", ["1:00:00", "01:60:00", "01:00:60", "01:00", "+-01:00:00", "abc", "", 3600, None])
def test_parse_offset_invalid(text: Any) -> None:
    """
    Check that malformed offsets raise ``ValueError``.
    """
    with pytest.raises(ValueError, match="invalid offset"):
        mr.parse_offset(text)


# --- get_available_timezones / validate_timezone ------------------------------


def test_get_available_timezones_returns_set() -> None:
    """
    Check that the known zones are returned as a set (possibly empty).
    """
    zones = mr.get_available_timezones()
    assert isinstance(zones, set)
    assert all(isinstance(zone, str) for zone in zones)


def test_get_available_timezones_without_zoneinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that no zone is returned when ``zoneinfo`` is unavailable.
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    assert mr.get_available_timezones() == set()


def test_has_timezone_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that the database is reported available only when zones are known.
    """
    monkeypatch.setattr(mr, "get_available_timezones", lambda: {"Europe/Paris"})
    assert mr.has_timezone_database() is True
    monkeypatch.setattr(mr, "get_available_timezones", lambda: set())
    assert mr.has_timezone_database() is False


def test_describe_missing_timezone_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check the explanation given for a missing database, with and without ``zoneinfo``.
    """
    assert "tzdata" in mr.describe_missing_timezone_database()
    monkeypatch.setattr(mr, "zoneinfo", None)
    assert "Python 3.9" in mr.describe_missing_timezone_database()


def test_validate_timezone_syntax_only() -> None:
    """
    Check syntactic validation when no time zone database is available.
    """
    assert mr.validate_timezone("Europe/Paris", set()) is None
    assert mr.validate_timezone("America/Argentina/Buenos_Aires", set()) is None
    assert mr.validate_timezone("UTC", set()) is None
    for bad in ("", "Europe/", "../etc", "Europe Paris", "+02:00", 5):
        assert "invalid time zone" in mr.validate_timezone(bad, set())


def test_validate_timezone_with_database() -> None:
    """
    Check that unknown zones are rejected when a database is available.
    """
    known = {"Europe/Paris", "UTC"}
    assert mr.validate_timezone("Europe/Paris", known) is None
    assert "unknown time zone" in mr.validate_timezone("Europe/Atlantis", known)


# --- validate_match -----------------------------------------------------------


def test_validate_match_valid() -> None:
    """
    Check that make/model criteria, with an optional serial, are accepted.
    """
    assert mr.validate_match({"make": "GoPro", "model": "HERO9 Black"}) == []
    assert mr.validate_match({"model": "HERO9 Black", "serial_number": ""}) == []


@pytest.mark.parametrize(
    "match, expected",
    [
        ("GoPro", "must be an object"),
        ({}, "non-empty 'make' or 'model'"),
        ({"make": " ", "model": ""}, "non-empty 'make' or 'model'"),
        ({"make": "GoPro", "lens": "x"}, "unknown match criterion 'lens'"),
        ({"make": "GoPro", "serial_number": 123}, "'serial_number' must be a string"),
    ],
)
def test_validate_match_invalid(match: Any, expected: str) -> None:
    """
    Check that invalid match criteria are rejected.
    """
    assert any(expected in error for error in mr.validate_match(match))


# --- validate_device ----------------------------------------------------------


def test_validate_device_valid() -> None:
    """
    Check that a complete device, with an optional time zone, is accepted.
    """
    assert mr.validate_device(make_device()) == []
    assert mr.validate_device(make_device(timezone="Europe/Paris"), set()) == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"offset": "-1:00"}, "invalid offset"),
        ({"id": ""}, "'id' must be a non-empty string"),
        ({"label": ""}, "'label' must be a non-empty string"),
        ({"label": "Go/Pro"}, "invalid file name characters"),
        ({"match": {}}, "non-empty 'make' or 'model'"),
        ({"timezone": "Not a zone"}, "invalid time zone"),
        ({"color": "red"}, "unknown field 'color'"),
    ],
)
def test_validate_device_invalid(overrides: Dict[str, Any], expected: str) -> None:
    """
    Check that invalid device fields are reported.
    """
    errors = mr.validate_device(make_device(**overrides), set())
    assert any(expected in error for error in errors), errors


def test_validate_device_missing_field() -> None:
    """
    Check that missing required fields are reported.
    """
    device = make_device()
    del device["offset"]
    assert "missing field 'offset'" in mr.validate_device(device)
    assert mr.validate_device("x") == ["a device must be a JSON object"]


# --- validate_devices / load_devices ------------------------------------------


def test_validate_devices_valid() -> None:
    """
    Check that an empty list and distinct devices are accepted.
    """
    assert mr.validate_devices({"devices": []}) == []
    assert mr.validate_devices({"devices": [make_device(), make_device(id="eos")]}) == []


def test_validate_devices_duplicate_id() -> None:
    """
    Check that duplicate ids, compared case-insensitively, are reported.
    """
    errors = mr.validate_devices({"devices": [make_device(), make_device(id="GoPro9")]})
    assert errors == ["device #2 ('GoPro9'): duplicate id (already used by device #1)"]


def test_validate_devices_prefixes_entry_errors() -> None:
    """
    Check that entry errors name the faulty device.
    """
    errors = mr.validate_devices({"devices": [make_device(offset="bad")]})
    assert errors[0].startswith("device #1 ('gopro9'): invalid offset")


@pytest.mark.parametrize("data", [[], {}, {"devices": {}}])
def test_validate_devices_bad_structure(data: Any) -> None:
    """
    Check that a document without a ``devices`` list is rejected.
    """
    assert mr.validate_devices(data) == ["the devices file must be a JSON object with a 'devices' list"]


def test_validate_devices_unknown_top_level_key() -> None:
    """
    Check that unexpected top-level keys are reported.
    """
    assert mr.validate_devices({"devices": [], "extra": 1}) == ["unknown top-level key 'extra'"]


def test_load_devices_valid(tmp_path: Path) -> None:
    """
    Check that a valid devices file loads its entries.
    """
    path = write_json(tmp_path / "devices.json", {"devices": [make_device()]})
    assert mr.load_devices(path) == [make_device()]


def test_load_devices_invalid(tmp_path: Path) -> None:
    """
    Check that an invalid devices file raises ``ConfigError``.
    """
    path = write_json(tmp_path / "devices.json", {"devices": [make_device(), make_device()]})
    with pytest.raises(mr.ConfigError, match="duplicate id"):
        mr.load_devices(path)


# --- find_device --------------------------------------------------------------


def test_find_device() -> None:
    """
    Check case-insensitive lookup by id.
    """
    devices = [make_device(), make_device(id="eos")]
    assert mr.find_device(devices, "EOS") == devices[1]
    assert mr.find_device(devices, "missing") is None


# --- write_devices_file -------------------------------------------------------


def test_write_devices_file_creates_file_without_backup(tmp_path: Path) -> None:
    """
    Check that a new file is written and no backup is made when none existed.
    """
    path = tmp_path / "devices.json"
    data = {"devices": [make_device()]}
    mr.write_devices_file(path, data)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert not (tmp_path / "devices.json.bak").exists()


def test_write_devices_file_backs_up_previous_content(tmp_path: Path) -> None:
    """
    Check that the previous content is saved to ``devices.json.bak``.
    """
    path = write_json(tmp_path / "devices.json", {"devices": []})
    previous = path.read_text(encoding="utf-8")
    mr.write_devices_file(path, {"devices": [make_device()]})
    assert (tmp_path / "devices.json.bak").read_text(encoding="utf-8") == previous
    assert mr.load_devices(path) == [make_device()]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["devices.json", "devices.json.bak"]


def test_write_devices_file_rejects_invalid_data(tmp_path: Path) -> None:
    """
    Check that invalid data is refused and the file is left untouched.
    """
    path = write_json(tmp_path / "devices.json", {"devices": []})
    with pytest.raises(mr.ConfigError, match="refusing"):
        mr.write_devices_file(path, {"devices": [make_device(offset="x")]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"devices": []}


def test_write_devices_file_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that a failed replace keeps the original file and removes the temporary file.
    """
    path = write_json(tmp_path / "devices.json", {"devices": []})

    def failing_replace(source: str, target: str) -> None:
        """
        Simulate an interruption during the atomic replace.

        :param source: Temporary file.
        :param target: Target file.
        :raises OSError: Always.
        """
        raise OSError("interrupted")

    monkeypatch.setattr(mr.os, "replace", failing_replace)
    with pytest.raises(OSError):
        mr.write_devices_file(path, {"devices": [make_device()]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"devices": []}
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["devices.json", "devices.json.bak"]


# --- exiftool_executable_name / is_executable_file / natural_sort_key ---------


def test_exiftool_executable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check the executable name on Windows and POSIX.
    """
    monkeypatch.setattr(mr.os, "name", "nt")
    assert mr.exiftool_executable_name() == "exiftool.exe"
    monkeypatch.setattr(mr.os, "name", "posix")
    assert mr.exiftool_executable_name() == "exiftool"


def test_is_executable_file(tmp_path: Path) -> None:
    """
    Check that executable files are detected and folders or missing paths are not.
    """
    assert mr.is_executable_file(make_fake_exe(tmp_path / EXE_NAME))
    assert not mr.is_executable_file(tmp_path)
    assert not mr.is_executable_file(tmp_path / "missing")


@pytest.mark.skipif(os.name == "nt", reason="execute permission is POSIX only")
def test_is_executable_file_requires_permission(tmp_path: Path) -> None:
    """
    Check that a non-executable file is rejected on POSIX.
    """
    path = tmp_path / "exiftool"
    path.write_text("", encoding="utf-8")
    os.chmod(str(path), 0o644)
    assert not mr.is_executable_file(path)


def test_natural_sort_key() -> None:
    """
    Check that numbers inside names are compared numerically.
    """
    names = ["exiftool-13.62_64", "exiftool-9.0", "exiftool-13.7"]
    assert sorted(names, key=mr.natural_sort_key) == ["exiftool-9.0", "exiftool-13.7", "exiftool-13.62_64"]


# --- find_exiftool_in_bin / find_keep_open_variants ---------------------------


def test_find_exiftool_in_bin_direct_wins(tmp_path: Path) -> None:
    """
    Check that ``bin/<exe>`` is preferred over versioned subfolders.
    """
    direct = make_fake_exe(tmp_path / EXE_NAME)
    make_fake_exe(tmp_path / "exiftool-13.62_64" / EXE_NAME)
    assert mr.find_exiftool_in_bin(tmp_path, EXE_NAME) == direct


def test_find_exiftool_in_bin_highest_version_subfolder(tmp_path: Path) -> None:
    """
    Check that the highest versioned subfolder wins.
    """
    make_fake_exe(tmp_path / "exiftool-9.0" / EXE_NAME)
    newest = make_fake_exe(tmp_path / "exiftool-13.62_64" / EXE_NAME)
    assert mr.find_exiftool_in_bin(tmp_path, EXE_NAME) == newest


def test_find_exiftool_in_bin_not_found(tmp_path: Path) -> None:
    """
    Check that ``None`` is returned for a missing or empty ``bin/``.
    """
    assert mr.find_exiftool_in_bin(tmp_path / "missing", EXE_NAME) is None
    (tmp_path / "empty").mkdir()
    assert mr.find_exiftool_in_bin(tmp_path, EXE_NAME) is None


def test_find_keep_open_variants(tmp_path: Path) -> None:
    """
    Check that ``exiftool(-k)`` executables are listed.
    """
    variant = make_fake_exe(tmp_path / "exiftool-13.62_64" / "exiftool(-k).exe")
    assert mr.find_keep_open_variants(tmp_path) == [variant]
    assert mr.find_keep_open_variants(tmp_path / "missing") == []


# --- find_exiftool ------------------------------------------------------------


def test_find_exiftool_prefers_bin(tmp_path: Path) -> None:
    """
    Check that ``bin/`` wins over ``PATH`` and ``EXIFTOOL_PATH``.
    """
    in_bin = make_fake_exe(tmp_path / "bin" / EXE_NAME)
    on_path = make_fake_exe(tmp_path / "path" / EXE_NAME)
    env = {"PATH": str(on_path.parent), "EXIFTOOL_PATH": str(on_path)}
    assert mr.find_exiftool(tmp_path / "bin", env) == in_bin


def test_find_exiftool_uses_path(tmp_path: Path) -> None:
    """
    Check that ``PATH`` is used when ``bin/`` has no ExifTool.
    """
    on_path = make_fake_exe(tmp_path / "path" / EXE_NAME)
    found = mr.find_exiftool(tmp_path / "bin", {"PATH": str(on_path.parent)})
    assert found.resolve() == on_path.resolve()


def test_find_exiftool_uses_env_var_file_or_folder(tmp_path: Path) -> None:
    """
    Check that ``EXIFTOOL_PATH`` may point to the executable or its folder.
    """
    exe = make_fake_exe(tmp_path / "tools" / EXE_NAME)
    assert mr.find_exiftool(tmp_path / "bin", {"PATH": "", "EXIFTOOL_PATH": str(exe)}) == exe
    assert mr.find_exiftool(tmp_path / "bin", {"PATH": "", "EXIFTOOL_PATH": str(exe.parent)}) == exe


def test_find_exiftool_missing(tmp_path: Path) -> None:
    """
    Check the explicit error when ExifTool is nowhere.
    """
    env = {"PATH": "", "EXIFTOOL_PATH": str(tmp_path / "nowhere")}
    with pytest.raises(mr.ExifToolError, match="ExifTool not found"):
        mr.find_exiftool(tmp_path / "bin", env)


def test_find_exiftool_missing_suggests_renaming_keep_open_variant(tmp_path: Path) -> None:
    """
    Check that the error suggests renaming an ``exiftool(-k)`` executable.
    """
    make_fake_exe(tmp_path / "bin" / "exiftool-13.62_64" / "exiftool(-k).exe")
    with pytest.raises(mr.ExifToolError, match="rename it to"):
        mr.find_exiftool(tmp_path / "bin", {"PATH": ""}, "exiftool.exe")


# --- get_exiftool_version -----------------------------------------------------


def fake_run(stdout: str = "", stderr: str = "", returncode: int = 0, error: Optional[BaseException] = None) -> Any:
    """
    Build a replacement for ``subprocess.run``.

    :param stdout: Standard output to return.
    :param stderr: Standard error to return.
    :param returncode: Exit code to return.
    :param error: Exception to raise instead of returning.
    :return: Function usable in place of ``subprocess.run``.
    """

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        """
        Return the configured result or raise the configured error.

        :param args: Ignored positional arguments.
        :param kwargs: Ignored keyword arguments.
        :return: Completed process.
        """
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run


def test_get_exiftool_version_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that the version printed by ExifTool is returned.
    """
    monkeypatch.setattr(mr.subprocess, "run", fake_run(stdout="13.62\r\n"))
    assert mr.get_exiftool_version(Path("exiftool")) == "13.62"


@pytest.mark.parametrize(
    "run, expected",
    [
        (fake_run(error=subprocess.TimeoutExpired("exiftool", 1)), "no answer"),
        (fake_run(error=FileNotFoundError("missing")), "cannot be started"),
        (fake_run(returncode=1, stderr="boom"), "boom"),
        (fake_run(stdout=""), "failed"),
    ],
)
def test_get_exiftool_version_errors(monkeypatch: pytest.MonkeyPatch, run: Any, expected: str) -> None:
    """
    Check that ExifTool failures raise ``ExifToolError``.
    """
    monkeypatch.setattr(mr.subprocess, "run", run)
    with pytest.raises(mr.ExifToolError, match=expected):
        mr.get_exiftool_version(Path("exiftool"), timeout=1)


# --- run_check ----------------------------------------------------------------


def run_check_captured(project: Path, **kwargs: Any) -> Any:
    """
    Run ``run_check`` and capture its output.

    :param project: Project root.
    :param kwargs: Extra arguments for ``run_check``.
    :return: Tuple ``(exit_code, stdout, stderr)``.
    """
    out, err = io.StringIO(), io.StringIO()
    code = mr.run_check(project, out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def checked_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Prepare a project with personal files and a fake working ExifTool.

    :param project: Project root.
    :param monkeypatch: Pytest monkeypatch fixture.
    :return: Project root.
    """
    mr.ensure_personal_files(project / "conf")
    make_fake_exe(project / "bin" / "exiftool-13.62_64" / EXE_NAME)
    monkeypatch.setattr(mr.subprocess, "run", fake_run(stdout="13.62\n"))
    return project


def test_run_check_success(checked_project: Path) -> None:
    """
    Check that a valid setup passes and reports the ExifTool version and location.
    """
    code, out, err = run_check_captured(checked_project, timezone="Europe/Paris", environ={"PATH": ""})
    assert code == 0, err
    assert "ExifTool 13.62" in out and "exiftool-13.62_64" in out
    assert "0 device(s)" in out
    assert err == ""


@pytest.mark.parametrize(
    "file_name, content, expected",
    [
        ("config.json", "{not json", "invalid JSON"),
        ("config.json", None, "unknown token"),
        ("devices.json", {"devices": [make_device(offset="1h")]}, "invalid offset"),
        ("devices.json", {"devices": [make_device(), make_device()]}, "duplicate id"),
    ],
)
def test_run_check_reports_config_errors(
    checked_project: Path, config_data: Dict[str, Any], file_name: str, content: Any, expected: str
) -> None:
    """
    Check that ``--check`` fails explicitly on configuration errors.
    """
    path = checked_project / "conf" / file_name
    if content is None:
        config_data["filename_pattern"] = "{YYYY}_{UNKNOWN}"
        write_json(path, config_data)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        write_json(path, content)
    code, _, err = run_check_captured(checked_project, environ={"PATH": ""})
    assert code == 1
    assert expected in err


def test_run_check_reports_bad_timezone(checked_project: Path) -> None:
    """
    Check that an invalid ``--tz`` value fails the check.
    """
    code, _, err = run_check_captured(checked_project, timezone="Mars/Olympus Mons", environ={"PATH": ""})
    assert code == 1
    assert "--tz" in err


def test_run_check_reports_timezone_database(checked_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that ``--check`` reports the time zone database when it is available.
    """
    monkeypatch.setattr(mr, "get_available_timezones", lambda: {"Europe/Paris", "UTC"})
    _, out, _ = run_check_captured(checked_project, environ={"PATH": ""})
    assert "[OK] Time zone database: 2 zone(s)" in out


def test_run_check_degraded_without_timezone_database(
    checked_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Check that a missing time zone database only yields warnings, even with ``--tz``.
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    code, out, err = run_check_captured(checked_project, timezone="Europe/Paris")
    assert "[WARNING] Time zone database" in out and "degraded mode" in out
    assert "[WARNING] --tz: time zone 'Europe/Paris' ignored" in out
    assert "--tz" not in err
    assert code == 0


def test_run_check_reports_missing_exiftool(project: Path) -> None:
    """
    Check that ``--check`` fails explicitly when ExifTool is missing.
    """
    mr.ensure_personal_files(project / "conf")
    code, out, err = run_check_captured(project, environ={"PATH": ""})
    assert code == 1
    assert "ExifTool not found" in err
    assert "[OK] Configuration" in out


# --- path_key / discover_files ------------------------------------------------


def test_path_key_normalizes() -> None:
    """
    Check that equivalent spellings of a path share the same key.
    """
    assert mr.path_key("a/b/../c.jpg") == mr.path_key(Path("a") / "c.jpg")
    if os.name == "nt":
        assert mr.path_key("C:/Dir/File.JPG") == mr.path_key("c:\\dir\\file.jpg")


def make_tree(root: Path, names: Any) -> None:
    """
    Create empty files (and their folders) under a root folder.

    :param root: Root folder.
    :param names: Relative POSIX paths of the files to create.
    """
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


@pytest.fixture
def media_tree(tmp_path: Path) -> Path:
    """
    Create a source folder with nested media and non-media files.

    :param tmp_path: Pytest temporary folder.
    :return: Source folder.
    """
    source = tmp_path / "src"
    make_tree(source, [
        "b.JPG", "a.mp4", "notes.txt", "sub/c.jpg", "sub/deep/d.MOV", "sub/deep/e.json",
        ".logs/f.jpg", "out/g.jpg", "empty/readme.md",
    ])
    return source


def test_discover_files_flat(media_tree: Path) -> None:
    """
    Check that only the source folder is scanned without ``--recursive``, case-insensitively.
    """
    assert mr.discover_files(media_tree, [".jpg", ".mp4", ".mov"]) == [Path("a.mp4"), Path("b.JPG")]


def test_discover_files_recursive_keeps_relative_paths(media_tree: Path) -> None:
    """
    Check that a recursive scan keeps relative paths and skips ``.logs`` and excluded folders.
    """
    found = mr.discover_files(media_tree, [".jpg", ".mp4", ".mov"], recursive=True,
                              excluded_dirs=[media_tree / "out"])
    assert found == [Path("a.mp4"), Path("b.JPG"), Path("sub/c.jpg"), Path("sub/deep/d.MOV")]


def test_discover_files_recursive_without_exclusion(media_tree: Path) -> None:
    """
    Check that a destination is only skipped when excluded.
    """
    found = mr.discover_files(media_tree, [".jpg"], recursive=True)
    assert Path("out/g.jpg") in found and Path(".logs/f.jpg") not in found


def test_discover_files_missing_source(tmp_path: Path) -> None:
    """
    Check that a missing source folder raises ``NotADirectoryError``.
    """
    with pytest.raises(NotADirectoryError, match="source folder not found"):
        mr.discover_files(tmp_path / "missing", [".jpg"])


# --- ExifTool wrapper ---------------------------------------------------------


def test_build_exiftool_command() -> None:
    """
    Check that the command requests JSON, UTF-8 names and only the needed tags.
    """
    command = mr.build_exiftool_command(Path("exiftool"), Path("list.args"))
    assert command[0] == "exiftool" and "-json" in command
    assert command[-2:] == ["-@", "list.args"]
    assert "-DateTimeOriginal" in command and "-SerialNumber" in command
    assert not any("GPS" in part for part in command)


def test_find_stderr_message() -> None:
    """
    Check that the ExifTool error about a file is extracted.
    """
    path = Path("/data/a - b.jpg")
    lines = ["Warning: other - /data/x.jpg", "Error: File not found - /data/a - b.jpg"]
    assert mr.find_stderr_message(lines, path) == "Error: File not found"
    assert mr.find_stderr_message(lines, Path("/data/none.jpg")) is None


def test_parse_exiftool_output_splits_results() -> None:
    """
    Check that metadata, error records and missing files are sorted out.
    """
    ok, bad, missing, silent = (Path("/d/ok.jpg"), Path("/d/bad.jpg"), Path("/d/missing.jpg"), Path("/d/s.jpg"))
    stdout = json.dumps([
        {"SourceFile": "/d/ok.jpg", "Make": "GoPro"},
        {"SourceFile": "/d/bad.jpg", "Error": "File format error"},
        {"SourceFile": "/d/other.jpg"},
        "garbage",
    ])
    stderr = "Error: File not found - /d/missing.jpg\n"
    metadata, errors = mr.parse_exiftool_output(stdout, stderr, [ok, bad, missing, silent])
    assert metadata == {ok: {"SourceFile": "/d/ok.jpg", "Make": "GoPro"}}
    assert errors == {
        bad: "File format error",
        missing: "Error: File not found",
        silent: "no metadata returned by ExifTool",
    }


@pytest.mark.parametrize("stdout, expected", [("{oops", "unreadable"), ("{}", "unexpected")])
def test_parse_exiftool_output_invalid(stdout: str, expected: str) -> None:
    """
    Check that unusable output marks every file as failed.
    """
    metadata, errors = mr.parse_exiftool_output(stdout, "", [Path("/d/a.jpg")])
    assert metadata == {} and expected in errors[Path("/d/a.jpg")]


def test_run_exiftool_batch_uses_argfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Check that file names are passed in a UTF-8 argument file, removed afterwards.
    """
    path = tmp_path / "été.jpg"
    seen = {}  # type: Dict[str, Any]

    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        """
        Record the argument file and answer like ExifTool.

        :param command: Command line.
        :param kwargs: Ignored keyword arguments.
        :return: Completed process with JSON output.
        """
        seen["argfile"] = Path(command[-1])
        seen["content"] = seen["argfile"].read_text(encoding="utf-8")
        stdout = json.dumps([{"SourceFile": path.as_posix(), "Model": "X"}]).encode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(mr.subprocess, "run", run)
    metadata, errors = mr.run_exiftool_batch(Path("exiftool"), [path], timeout=5)
    assert metadata[path]["Model"] == "X" and errors == {}
    assert seen["content"] == path.as_posix() + "\n"
    assert not seen["argfile"].exists()


def test_run_exiftool_batch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that a timeout marks every file of the batch as failed.
    """
    monkeypatch.setattr(mr.subprocess, "run", fake_run(error=subprocess.TimeoutExpired("exiftool", 5)))
    metadata, errors = mr.run_exiftool_batch(Path("exiftool"), [Path("/a.jpg"), Path("/b.jpg")], timeout=5)
    assert metadata == {} and len(errors) == 2
    assert "no answer after 5 seconds" in errors[Path("/a.jpg")]


def test_run_exiftool_batch_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that an ExifTool that cannot start raises ``ExifToolError``.
    """
    monkeypatch.setattr(mr.subprocess, "run", fake_run(error=FileNotFoundError("missing")))
    with pytest.raises(mr.ExifToolError, match="cannot be started"):
        mr.run_exiftool_batch(Path("exiftool"), [Path("/a.jpg")], timeout=5)


def test_read_metadata_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that files are split into batches and results merged.
    """
    calls = []

    def batch(executable: Path, paths: Any, timeout: float) -> Any:
        """
        Record the batch and fail on ``c.jpg``.

        :param executable: Ignored.
        :param paths: Batch of files.
        :param timeout: Batch timeout.
        :return: Tuple ``(metadata, errors)``.
        """
        calls.append((list(paths), timeout))
        return ({p: {} for p in paths if p.name != "c.jpg"}, {p: "bad" for p in paths if p.name == "c.jpg"})

    monkeypatch.setattr(mr, "run_exiftool_batch", batch)
    paths = [Path("a.jpg"), Path("b.jpg"), Path("c.jpg")]
    metadata, errors = mr.read_metadata(Path("exiftool"), paths, batch_size=2)
    assert [len(call[0]) for call in calls] == [2, 1]
    assert calls[0][1] > calls[1][1] >= mr.EXIFTOOL_TIMEOUT_SECONDS
    assert set(metadata) == {Path("a.jpg"), Path("b.jpg")} and errors == {Path("c.jpg"): "bad"}


# --- dates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2024:07:14 17:19:24", datetime(2024, 7, 14, 17, 19, 24)),
        ("2024:02:29 23:59:58.123", datetime(2024, 2, 29, 23, 59, 58)),
        ("2024:07:14 17:19", datetime(2024, 7, 14, 17, 19, 0)),
        ("2024-07-14T17:19:24", datetime(2024, 7, 14, 17, 19, 24)),
        ("2024:07:14 17:19:24Z", datetime(2024, 7, 14, 17, 19, 24, tzinfo=timezone.utc)),
        ("2024:07:14 17:19:24+02:00", datetime(2024, 7, 14, 17, 19, 24, tzinfo=timezone(timedelta(hours=2)))),
        ("2024:07:14 17:19:24-0530",
         datetime(2024, 7, 14, 17, 19, 24, tzinfo=timezone(-timedelta(hours=5, minutes=30)))),
        ("0000:00:00 00:00:00", None),
        ("2023:02:29 10:00:00", None),
        ("    :  :     :  :  ", None),
        ("", None),
        (None, None),
        (20240714, None),
    ],
)
def test_parse_exif_datetime(value: Any, expected: Optional[datetime]) -> None:
    """
    Check valid, partial, zoned and invalid ExifTool dates.
    """
    assert mr.parse_exif_datetime(value) == expected


def test_select_capture_date_priority() -> None:
    """
    Check that ``DateTimeOriginal`` wins and invalid values fall through to the next tag.
    """
    tags = {"CreateDate": "2020:01:01 00:00:00", "DateTimeOriginal": "2021:01:01 00:00:00"}
    assert mr.select_capture_date(tags) == ("DateTimeOriginal", datetime(2021, 1, 1))
    tags = {"DateTimeOriginal": "0000:00:00 00:00:00", "MediaCreateDate": "2022:05:06 07:08:09"}
    assert mr.select_capture_date(tags) == ("MediaCreateDate", datetime(2022, 5, 6, 7, 8, 9))
    assert mr.select_capture_date({"FileModifyDate": "2022:05:06 07:08:09"}) is None


def test_select_capture_date_uses_offset_tags() -> None:
    """
    Check that ``OffsetTimeOriginal`` makes the date aware, and that invalid offsets are ignored.
    """
    tags = {"DateTimeOriginal": "2024:07:14 17:19:24", "OffsetTimeOriginal": "+02:00"}
    expected = datetime(2024, 7, 14, 17, 19, 24, tzinfo=timezone(timedelta(hours=2)))
    assert mr.select_capture_date(tags) == ("DateTimeOriginal", expected)
    tags = {"CreateDate": "2024:07:14 17:19:24", "OffsetTimeDigitized": "-05:00"}
    assert mr.select_capture_date(tags)[1].utcoffset() == timedelta(hours=-5)
    tags = {"DateTimeOriginal": "2024:07:14 17:19:24", "OffsetTimeOriginal": "   :  "}
    assert mr.select_capture_date(tags)[1].tzinfo is None
    tags = {"MediaCreateDate": "2024:07:14 17:19:24", "OffsetTimeOriginal": "+02:00"}
    assert mr.select_capture_date(tags)[1].tzinfo is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Z", timedelta(0)),
        ("+02:00", timedelta(hours=2)),
        ("-0530", timedelta(hours=-5, minutes=-30)),
        (" +14:00 ", timedelta(hours=14)),
        ("+24:00", None),
        ("+02:60", None),
        ("02:00", None),
        ("", None),
        (None, None),
        (2, None),
    ],
)
def test_parse_utc_offset(value: Any, expected: Optional[timedelta]) -> None:
    """
    Check valid and invalid UTC offsets.
    """
    zone = mr.parse_utc_offset(value)
    assert (zone.utcoffset(None) if zone else None) == expected


# --- offsets and time zones ---------------------------------------------------


@pytest.mark.parametrize(
    "date, offset, expected",
    [
        (datetime(2024, 1, 2, 3, 4, 5), "+00:00:00", datetime(2024, 1, 2, 3, 4, 5)),
        (datetime(2024, 1, 2, 3, 4, 5), "+01:30:15", datetime(2024, 1, 2, 4, 34, 20)),
        (datetime(2024, 1, 2, 3, 4, 5), "-01:00:00", datetime(2024, 1, 2, 2, 4, 5)),
        (datetime(2024, 1, 2, 3, 4, 5), "00:00:30", datetime(2024, 1, 2, 3, 4, 35)),
        (datetime(2024, 1, 31, 23, 30, 0), "+01:00:00", datetime(2024, 2, 1, 0, 30, 0)),
        (datetime(2024, 1, 1, 0, 30, 0), "-01:00:00", datetime(2023, 12, 31, 23, 30, 0)),
        (datetime(2023, 12, 31, 23, 59, 59), "+00:00:01", datetime(2024, 1, 1, 0, 0, 0)),
        (datetime(2024, 2, 28, 23, 0, 0), "+02:00:00", datetime(2024, 2, 29, 1, 0, 0)),
        (datetime(2024, 3, 1, 0, 30, 0), "-01:00:00", datetime(2024, 2, 29, 23, 30, 0)),
        (datetime(2023, 2, 28, 23, 0, 0), "+02:00:00", datetime(2023, 3, 1, 1, 0, 0)),
        (datetime(2024, 1, 1, 12, 0, 0), "-48:00:00", datetime(2023, 12, 30, 12, 0, 0)),
    ],
)
def test_apply_offset(date: datetime, offset: str, expected: datetime) -> None:
    """
    Check positive and negative offsets with day, month, year and leap-day rollovers.
    """
    assert mr.apply_offset(date, offset) == expected


def test_apply_offset_keeps_time_zone_and_rejects_bad_values() -> None:
    """
    Check that an aware date stays aware, and that invalid or overflowing offsets fail.
    """
    zone = timezone(timedelta(hours=2))
    assert mr.apply_offset(datetime(2024, 1, 1, 12, tzinfo=zone), "-01:00:00") == datetime(
        2024, 1, 1, 11, tzinfo=zone)
    with pytest.raises(ValueError, match="invalid offset"):
        mr.apply_offset(datetime(2024, 1, 1), "1h")
    with pytest.raises(ValueError, match="out of range"):
        mr.apply_offset(datetime(9999, 12, 31, 23), "+02:00:00")


@pytest.fixture
def paris() -> Any:
    """
    Load the Europe/Paris zone, or skip the test when no time zone database is available.

    :return: The ``ZoneInfo`` object.
    """
    try:
        return mr.load_zone("Europe/Paris")
    except ValueError as error:
        pytest.skip(str(error))


def test_load_zone(paris: Any) -> None:
    """
    Check that a known zone loads and that unknown zones are reported.
    """
    assert str(paris) == "Europe/Paris"
    with pytest.raises(ValueError, match="not found"):
        mr.load_zone("Europe/Atlantis")


def test_check_cli_timezone(paris: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check syntax errors, unknown zones and a missing time zone database for ``--tz``.
    """
    assert mr.check_cli_timezone("Europe/Paris") == (None, None)
    error, warning = mr.check_cli_timezone("not a zone")
    assert "invalid time zone" in error and warning is None
    error, warning = mr.check_cli_timezone("Europe/Atlantis")
    assert "unknown time zone" in error and warning is None


def test_check_cli_timezone_load_error_with_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that a zone that fails to load from an available database is an error.
    """
    def failing_load_zone(name: str) -> Any:
        """
        Simulate a zone listed in the database but impossible to load.

        :param name: Zone name.
        :raises ValueError: Always.
        """
        raise ValueError("time zone {!r} not found".format(name))

    monkeypatch.setattr(mr, "get_available_timezones", lambda: {"Europe/Paris"})
    monkeypatch.setattr(mr, "load_zone", failing_load_zone)
    error, warning = mr.check_cli_timezone("Europe/Paris")
    assert "not found" in error and warning is None


def test_check_cli_timezone_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that, without a time zone database, a valid ``--tz`` only yields a warning.
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    error, warning = mr.check_cli_timezone("Europe/Paris")
    assert error is None
    assert "ignored, local times kept" in warning and "Python 3.9" in warning
    error, warning = mr.check_cli_timezone("not a zone")
    assert "invalid time zone" in error and warning is None


def test_load_zone_without_zoneinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check the error raised when ``zoneinfo`` is unavailable (Python 3.8).
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    with pytest.raises(ValueError, match="Python 3.9"):
        mr.load_zone("Europe/Paris")


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2024, 7, 14, 12, 0, 0), mr.LOCAL_TIME_VALID),
        (datetime(2024, 10, 27, 2, 30, 0), mr.LOCAL_TIME_AMBIGUOUS),
        (datetime(2024, 3, 31, 2, 30, 0), mr.LOCAL_TIME_NONEXISTENT),
        (datetime(2024, 3, 31, 3, 0, 0), mr.LOCAL_TIME_VALID),
        (datetime(2024, 10, 27, 3, 0, 0), mr.LOCAL_TIME_VALID),
    ],
)
def test_classify_local_time(paris: Any, local: datetime, expected: str) -> None:
    """
    Check normal, ambiguous (clocks set back) and non-existent (clocks set forward) local times.
    """
    assert mr.classify_local_time(local, paris) == expected


def test_choose_zone() -> None:
    """
    Check that ``--tz`` wins over the device zone, and that no zone is the default.
    """
    device = make_device(timezone="Europe/Paris")
    assert mr.choose_zone(device, "Asia/Tokyo") == ("Asia/Tokyo", mr.ZONE_SOURCE_CLI)
    assert mr.choose_zone(device, None) == ("Europe/Paris", mr.ZONE_SOURCE_DEVICE)
    assert mr.choose_zone(make_device(), None) == ("", "")


def test_compute_effective_date_without_zone() -> None:
    """
    Check that a zone-less date only gets the clock offset and stays naive.
    """
    effective = mr.compute_effective_date(datetime(2024, 1, 2, 3, 4, 5), make_device())
    assert effective == mr.EffectiveDate(datetime(2024, 1, 2, 2, 4, 5), "", "", ())


def test_compute_effective_date_with_device_zone(paris: Any) -> None:
    """
    Check normalization to UTC with the device zone, offset applied first (winter and summer).
    """
    device = make_device(timezone="Europe/Paris")
    winter = mr.compute_effective_date(datetime(2024, 1, 2, 3, 4, 5), device)
    assert winter.value == datetime(2024, 1, 2, 1, 4, 5, tzinfo=timezone.utc)
    assert (winter.zone, winter.zone_source, winter.warnings) == ("Europe/Paris", mr.ZONE_SOURCE_DEVICE, ())
    summer = mr.compute_effective_date(datetime(2024, 7, 1, 0, 30, 0), device)
    assert summer.value == datetime(2024, 6, 30, 21, 30, 0, tzinfo=timezone.utc)


def test_compute_effective_date_with_cli_zone(paris: Any) -> None:
    """
    Check that ``--tz`` overrides the device zone.
    """
    device = make_device(offset="+00:00:00", timezone="Asia/Tokyo")
    effective = mr.compute_effective_date(datetime(2024, 1, 2, 12, 0, 0), device, "Europe/Paris")
    assert effective.value == datetime(2024, 1, 2, 11, 0, 0, tzinfo=timezone.utc)
    assert (effective.zone, effective.zone_source) == ("Europe/Paris", mr.ZONE_SOURCE_CLI)


def test_compute_effective_date_with_metadata_offset() -> None:
    """
    Check that an offset stored in the metadata wins over ``--tz`` and the device zone.
    """
    date = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    effective = mr.compute_effective_date(date, make_device(timezone="Europe/Paris"), "America/New_York")
    assert effective.value == datetime(2024, 1, 2, 2, 0, 0, tzinfo=timezone.utc)
    assert (effective.zone, effective.zone_source, effective.warnings) == ("+0900", mr.ZONE_SOURCE_METADATA, ())


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2024, 10, 27, 3, 30, 0), "occurs twice"),
        (datetime(2024, 3, 31, 3, 30, 0), "does not exist"),
    ],
)
def test_compute_effective_date_dst_warnings(paris: Any, local: datetime, expected: str) -> None:
    """
    Check that ambiguous and non-existent times (after the offset) are kept local with a warning.
    """
    effective = mr.compute_effective_date(local, make_device(), "Europe/Paris")
    assert effective.value == local - timedelta(hours=1)
    assert effective.value.tzinfo is None and effective.zone == ""
    assert len(effective.warnings) == 1
    assert expected in effective.warnings[0] and "Europe/Paris" in effective.warnings[0]


def test_compute_effective_date_unavailable_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that a zone missing from the database keeps the local time with a warning.
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    effective = mr.compute_effective_date(datetime(2024, 1, 2, 3, 4, 5), make_device(timezone="Europe/Paris"))
    assert effective.value == datetime(2024, 1, 2, 2, 4, 5)
    assert "local time kept" in effective.warnings[0]


def test_format_effective_date() -> None:
    """
    Check the display of UTC and local effective dates.
    """
    utc = mr.EffectiveDate(datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "Europe/Paris", "--tz", ())
    local = mr.EffectiveDate(datetime(2024, 1, 2, 3, 4, 5), "", "", ())
    assert mr.format_effective_date(utc) == "2024-01-02 03:04:05 UTC"
    assert mr.format_effective_date(local) == "2024-01-02 03:04:05 local"


# --- device identification ----------------------------------------------------


def test_clean_and_normalize_text() -> None:
    """
    Check value cleaning and case-insensitive normalization.
    """
    assert mr.clean_text(None) == "" and mr.clean_text(12345) == "12345" and mr.clean_text(" a ") == "a"
    assert mr.normalize_text("  HERO9   Black ") == "hero9 black"


def test_extract_identity() -> None:
    """
    Check that make, model and serial number are read, with empty defaults.
    """
    identity = mr.extract_identity({"Make": "GoPro ", "Model": "HERO9 Black", "SerialNumber": 123})
    assert identity == mr.DeviceIdentity("GoPro", "HERO9 Black", "123")
    assert mr.extract_identity({}) == mr.DeviceIdentity("", "", "")


@pytest.mark.parametrize(
    "match, identity, expected",
    [
        ({"make": "gopro", "model": "hero9 BLACK"}, ("GoPro", "HERO9 Black", "S1"), True),
        ({"make": "GoPro", "model": "HERO9 Black", "serial_number": ""}, ("GoPro", "HERO9 Black", "S1"), True),
        ({"make": "GoPro", "model": "HERO9 Black", "serial_number": "s1"}, ("GoPro", "HERO9 Black", "S1"), True),
        ({"make": "GoPro", "model": "HERO9 Black", "serial_number": "S2"}, ("GoPro", "HERO9 Black", "S1"), False),
        ({"make": "GoPro", "model": "HERO9 Black", "serial_number": "S2"}, ("", "HERO9 Black", ""), True),
        ({"make": "GoPro", "model": "HERO9 Black"}, ("Canon", "HERO9 Black", ""), False),
        ({"make": "GoPro", "model": "HERO9 Black"}, ("GoPro", "", ""), False),
        ({"make": "GoPro", "model": "HERO9 Black"}, ("GoPro", "HERO10 Black", ""), False),
        ({"make": "GoPro"}, ("GoPro", "Anything", ""), True),
        ({"make": "GoPro"}, ("", "Anything", ""), False),
    ],
)
def test_device_matches(match: Dict[str, str], identity: Any, expected: bool) -> None:
    """
    Check case-insensitivity, optional serial number and missing file values.
    """
    assert mr.device_matches(make_device(match=match), mr.DeviceIdentity(*identity)) is expected


def test_match_device_first_match_wins() -> None:
    """
    Check that the first matching entry wins and ``None`` is returned without match.
    """
    devices = [
        make_device(id="any", match={"model": "HERO9 Black"}),
        make_device(id="body", match={"model": "HERO9 Black", "serial_number": "S1"}),
    ]
    assert mr.match_device(devices, mr.DeviceIdentity("GoPro", "HERO9 Black", "S1"))["id"] == "any"
    assert mr.match_device(devices, mr.DeviceIdentity("GoPro", "HERO8", "")) is None


DATED_TAGS = {"DateTimeOriginal": "2024:01:02 03:04:05", "Make": "GoPro", "Model": "HERO9 Black"}


def test_analyze_file_statuses() -> None:
    """
    Check every outcome of the analysis of one file.
    """
    devices = [make_device()]
    forced = make_device(id="forced", label="Forced", match={"model": "Other"})
    aux = mr.analyze_file(Path("GL01.LRV"), None, "boom", devices)
    assert aux.status == mr.STATUS_AUXILIARY
    failed = mr.analyze_file(Path("a.jpg"), None, "File format error", devices)
    assert failed.status == mr.STATUS_SKIPPED and "File format error" in failed.message
    undated = mr.analyze_file(Path("a.jpg"), {"Make": "GoPro"}, None, devices)
    assert undated.status == mr.STATUS_SKIPPED and "no valid capture date" in undated.message
    ready = mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, devices)
    assert (ready.status, ready.device["id"], ready.date_tag) == (mr.STATUS_READY, "gopro9", "DateTimeOriginal")
    assert ready.date == datetime(2024, 1, 2, 3, 4, 5)
    assert mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [], forced).device["id"] == "forced"
    anonymous = mr.analyze_file(Path("a.mp4"), {"CreateDate": "2024:01:02 03:04:05"}, None, devices)
    assert anonymous.status == mr.STATUS_SKIPPED and "--device" in anonymous.message
    unknown = mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [])
    assert unknown.status == mr.STATUS_UNKNOWN_DEVICE and unknown.identity.model == "HERO9 Black"


def make_unknown(name: str, make: str, model: str, serial: str = "") -> mr.FileAnalysis:
    """
    Build the analysis of a file with an unknown device.

    :param name: Relative file name.
    :param make: Camera make.
    :param model: Camera model.
    :param serial: Camera serial number.
    :return: File analysis.
    """
    return mr.FileAnalysis(Path(name), mr.STATUS_UNKNOWN_DEVICE, "", "DateTimeOriginal",
                           datetime(2024, 1, 2), mr.DeviceIdentity(make, model, serial), None)


def test_collect_unknown_identities() -> None:
    """
    Check that unknown identifications are deduplicated in order of appearance.
    """
    analyses = [make_unknown("a", "X", "1"), make_unknown("b", "Y", "2"), make_unknown("c", "X", "1")]
    analyses.append(mr.analyze_file(Path("d.thm"), None, None, []))
    assert mr.collect_unknown_identities(analyses) == [mr.DeviceIdentity("X", "1", ""), mr.DeviceIdentity("Y", "2", "")]


def test_mask_serial_and_describe_identity() -> None:
    """
    Check that serial numbers are masked in descriptions.
    """
    assert mr.mask_serial("") == "" and mr.mask_serial("123") == "***"
    assert mr.mask_serial("C3441326974391") == "***4391"
    text = mr.describe_identity(mr.DeviceIdentity("GoPro", "HERO9 Black", "C3441326974391"))
    assert text == "Make 'GoPro', Model 'HERO9 Black', serial ***4391"
    assert mr.describe_identity(mr.DeviceIdentity("", "", "")) == "no identification"


def test_make_device_id() -> None:
    """
    Check that ids are slugs, unique among existing devices.
    """
    devices = [make_device(id="sony-tf1"), make_device(id="Sony-TF1-2")]
    assert mr.make_device_id("GoPro 9", []) == "gopro-9"
    assert mr.make_device_id("Sony TF1", devices) == "sony-tf1-3"
    assert mr.make_device_id("***", []) == "device"


def test_default_label() -> None:
    """
    Check that the suggested label comes from the model, then the make.
    """
    assert mr.default_label(mr.DeviceIdentity("GoPro", "HERO9: Black", "")) == "HERO9 Black"
    assert mr.default_label(mr.DeviceIdentity("NIKON", "", "")) == "NIKON"
    assert mr.default_label(mr.DeviceIdentity("", "", "")) == "Device"


def test_answer_validators() -> None:
    """
    Check the validation of each interactive answer.
    """
    assert mr.validate_label_answer("GoPro9") is None
    assert "empty" in mr.validate_label_answer("")
    assert "invalid" in mr.validate_label_answer("a/b")
    assert mr.validate_offset_answer("-01:00:00") is None
    assert "invalid offset" in mr.validate_offset_answer("1h")
    assert mr.validate_timezone_answer("") is None
    assert mr.validate_timezone_answer("not a zone") is not None
    assert mr.validate_yes_no_answer("Yes") is None and mr.validate_yes_no_answer("maybe") is not None


def scripted(answers: Any) -> Any:
    """
    Build an input function returning scripted answers.

    :param answers: Answers to return, in order.
    :return: Function usable in place of ``input``.
    """
    iterator = iter(answers)

    def read(prompt: str) -> str:
        """
        Return the next scripted answer, or raise ``EOFError`` when exhausted.

        :param prompt: Ignored question.
        :return: Next answer.
        :raises EOFError: When no answer is left.
        """
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return read


def test_ask_retries_until_valid_and_uses_default() -> None:
    """
    Check that invalid answers are re-asked and Enter selects the default.
    """
    out = io.StringIO()
    assert mr.ask(scripted(["bad", "+00:00:10"]), out, "? ", mr.validate_offset_answer) == "+00:00:10"
    assert "invalid offset" in out.getvalue()
    assert mr.ask(scripted(["  "]), out, "? ", mr.validate_offset_answer, "+00:00:00") == "+00:00:00"


def test_prompt_new_device_with_serial_and_timezone() -> None:
    """
    Check that the answers build a valid entry, with the serial number on request.
    """
    identity = mr.DeviceIdentity("GoPro", "HERO9 Black", "C3441326974391")
    out = io.StringIO()
    device = mr.prompt_new_device(identity, [], scripted(["GoPro 9", "-01:00:00", "Europe/Paris", "y"]), out)
    assert device == {
        "id": "gopro-9", "label": "GoPro 9", "offset": "-01:00:00", "timezone": "Europe/Paris",
        "match": {"make": "GoPro", "model": "HERO9 Black", "serial_number": "C3441326974391"},
    }
    assert "C3441326974391" not in out.getvalue()
    assert mr.validate_device(device, available_timezones=set()) == []


def test_prompt_new_device_defaults() -> None:
    """
    Check the default label and offset, and that the serial number is not used by default.
    """
    identity = mr.DeviceIdentity("", "HERO9 Black", "S1")
    device = mr.prompt_new_device(identity, [], scripted(["", "", "", ""]), io.StringIO())
    assert device == {"id": "hero9-black", "label": "HERO9 Black", "offset": "+00:00:00",
                      "match": {"model": "HERO9 Black"}}


def test_format_unknown_devices_error() -> None:
    """
    Check that the message lists devices (masked serial) and the possible fixes.
    """
    text = mr.format_unknown_devices_error([mr.DeviceIdentity("GoPro", "HERO9", "C3441326974391")])
    assert "1 unknown device(s)" in text and "***4391" in text and "--device" in text
    assert "C3441326974391" not in text


# --- resolve_unknown_devices --------------------------------------------------


def test_resolve_unknown_devices_nothing_to_do(tmp_path: Path) -> None:
    """
    Check that nothing is asked nor written when every device is known.
    """
    analyses = [mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [make_device()])]
    result, devices = mr.resolve_unknown_devices(analyses, [make_device()], tmp_path / "devices.json",
                                                 interactive=False, input_func=scripted([]))
    assert result == analyses and len(devices) == 1
    assert not (tmp_path / "devices.json").exists()


def test_resolve_unknown_devices_non_interactive(tmp_path: Path) -> None:
    """
    Check that a non-interactive run with unknown devices raises a clear error.
    """
    with pytest.raises(mr.UnknownDeviceError, match="no interactive terminal"):
        mr.resolve_unknown_devices([make_unknown("a.jpg", "GoPro", "HERO9")], [], tmp_path / "devices.json",
                                   interactive=False)


def test_resolve_unknown_devices_closed_input(tmp_path: Path) -> None:
    """
    Check that a closed input stream is reported like a non-interactive run.
    """
    with pytest.raises(mr.UnknownDeviceError):
        mr.resolve_unknown_devices([make_unknown("a.jpg", "GoPro", "HERO9")], [], tmp_path / "devices.json",
                                   interactive=True, input_func=scripted([]), out=io.StringIO())


def test_resolve_unknown_devices_asks_once_per_device(tmp_path: Path) -> None:
    """
    Check that one answer covers every matching file and is persisted.

    The video has no make nor serial number, the photos have both: a single
    question must cover all of them.
    """
    path = write_json(tmp_path / "devices.json", {"devices": [make_device(id="other", match={"model": "X"})]})
    analyses = [
        make_unknown("v.mp4", "", "HERO9 Black"),
        make_unknown("p1.jpg", "GoPro", "HERO9 Black", "S1"),
        make_unknown("p2.jpg", "GoPro", "HERO9 Black", "S1"),
        make_unknown("n.jpg", "NIKON", "E4500"),
    ]
    prompts = []

    def read(prompt: str) -> str:
        """
        Record the question and accept the defaults.

        :param prompt: Question shown.
        :return: Empty answer.
        """
        prompts.append(prompt)
        return ""

    out = io.StringIO()
    result, devices = mr.resolve_unknown_devices(analyses, mr.load_devices(path), path, True, read, out)
    assert all(analysis.status == mr.STATUS_READY for analysis in result)
    assert [analysis.device["id"] for analysis in result] == ["hero9-black"] * 3 + ["e4500"]
    assert sum("Label" in prompt for prompt in prompts) == 2
    assert [device["id"] for device in mr.load_devices(path)] == ["other", "hero9-black", "e4500"]
    assert (tmp_path / "devices.json.bak").is_file()
    assert devices == mr.load_devices(path)


# --- analyze_source / print_analysis ------------------------------------------


def test_analyze_source_reads_without_modifying(
    media_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Check the whole analysis pipeline with faked metadata, and that sources are untouched.
    """
    seen = []

    def fake_read_metadata(executable: Path, paths: Any) -> Any:
        """
        Return dated GoPro tags for every file.

        :param executable: Ignored.
        :param paths: Files to describe.
        :return: Tuple ``(metadata, errors)``.
        """
        seen.extend(paths)
        return {path: dict(DATED_TAGS) for path in paths}, {}

    monkeypatch.setattr(mr, "read_metadata", fake_read_metadata)
    before = snapshot(media_tree)
    analyses = mr.analyze_source(media_tree, [".jpg", ".mp4"], [make_device()], tmp_path / "devices.json",
                                 Path("exiftool"), recursive=True, excluded_dirs=[media_tree / "out"])
    assert [analysis.relative for analysis in analyses] == [Path("a.mp4"), Path("b.JPG"), Path("sub/c.jpg")]
    assert all(path.is_absolute() for path in seen)
    assert all(analysis.status == mr.STATUS_READY for analysis in analyses)
    assert snapshot(media_tree) == before


def test_analyze_source_empty_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Check that an empty folder does not call ExifTool.
    """
    monkeypatch.setattr(mr, "read_metadata", None)
    assert mr.analyze_source(tmp_path, [".jpg"], [], tmp_path / "devices.json", Path("exiftool")) == []


def test_print_analysis_hides_serial_numbers() -> None:
    """
    Check the printed lines and summary, without serial numbers.
    """
    tags = dict(DATED_TAGS, SerialNumber="C3441326974391")
    analyses = [
        mr.analyze_file(Path("sub/a.jpg"), tags, None, [make_device()]),
        mr.analyze_file(Path("b.thm"), None, None, []),
        mr.analyze_file(Path("c.jpg"), None, "File format error", []),
    ]
    out = io.StringIO()
    mr.print_analysis(mr.add_effective_dates(analyses), out)
    text = out.getvalue()
    assert ("[OK]   sub/a.jpg  2024-01-02 02:04:05 local  device 'GoPro9'  "
            "(DateTimeOriginal 2024-01-02 03:04:05, offset -01:00:00)") in text
    assert "[AUX]  b.thm" in text and "[SKIP] c.jpg  unreadable metadata (File format error)" in text
    assert "3 file(s): 1 ready, 1 auxiliary, 1 skipped, 0 warning(s)." in text
    assert "C3441326974391" not in text


def test_print_analysis_without_effective_date_and_with_warnings(paris: Any) -> None:
    """
    Check the fallback display without effective date, and the warning lines and count.
    """
    plain = mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [make_device()])
    out = io.StringIO()
    mr.print_analysis([plain], out)
    assert "[OK]   a.jpg  2024-01-02 03:04:05  device 'GoPro9'" in out.getvalue()
    tags = dict(DATED_TAGS, DateTimeOriginal="2024:10:27 03:30:00")
    ambiguous = mr.analyze_file(Path("b.jpg"), tags, None, [make_device()])
    out = io.StringIO()
    mr.print_analysis(mr.add_effective_dates([ambiguous], "Europe/Paris"), out)
    text = out.getvalue()
    assert "[OK]   b.jpg  2024-10-27 02:30:00 local" in text
    assert "       warning: 2024-10-27 02:30:00 occurs twice (clocks set back) in Europe/Paris" in text
    assert "1 warning(s)." in text


def test_describe_date_source(paris: Any) -> None:
    """
    Check the description of the date tag, the offset (hidden when zero) and the zone.
    """
    zero = make_device(offset="+00:00:00")
    analysis = mr.add_effective_dates([mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [zero])])[0]
    assert mr.describe_date_source(analysis) == "DateTimeOriginal 2024-01-02 03:04:05"
    analysis = mr.add_effective_dates([mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [make_device()])],
                                      "Europe/Paris")[0]
    assert mr.describe_date_source(analysis) == (
        "DateTimeOriginal 2024-01-02 03:04:05, offset -01:00:00, zone Europe/Paris (--tz)")


def test_add_effective_dates() -> None:
    """
    Check that only ready files get an effective date, and that an out-of-range result is skipped.
    """
    ready = mr.analyze_file(Path("a.jpg"), DATED_TAGS, None, [make_device()])
    aux = mr.analyze_file(Path("a.thm"), None, None, [])
    late = mr.analyze_file(Path("z.jpg"), dict(DATED_TAGS, DateTimeOriginal="9999:12:31 23:30:00"), None,
                           [make_device(offset="+01:00:00")])
    result = mr.add_effective_dates([ready, aux, late])
    assert result[0].effective.value == datetime(2024, 1, 2, 2, 4, 5)
    assert result[1] == aux
    assert result[2].status == mr.STATUS_SKIPPED and "out of range" in result[2].message
    assert ready.effective is None


# --- rename plan --------------------------------------------------------------


PATTERN = "{YYYY}-{MM}-{DD}_{HH}-{MIN}-{SEC}_{DEVICE}"
BASE = "2024-01-02_03-04-05_GoPro9"
WHEN = datetime(2024, 1, 2, 3, 4, 5)


def ready(name: str, when: datetime = WHEN, label: str = "GoPro9", warnings: Any = ()) -> mr.FileAnalysis:
    """
    Build the analysis of a ready file with an effective date.

    :param name: Relative path of the file.
    :param when: Effective date.
    :param label: Device label.
    :param warnings: Warnings of the effective date.
    :return: File analysis.
    """
    return mr.FileAnalysis(Path(name), mr.STATUS_READY, "", "DateTimeOriginal", when,
                           mr.DeviceIdentity("GoPro", "HERO9 Black", ""), make_device(label=label),
                           mr.EffectiveDate(when, "", "", tuple(warnings)))


def auxiliary(name: str) -> mr.FileAnalysis:
    """
    Build the analysis of a GoPro auxiliary file.

    :param name: Relative path of the file.
    :return: File analysis.
    """
    return mr.analyze_file(Path(name), None, None, [])


def skipped(name: str) -> mr.FileAnalysis:
    """
    Build the analysis of a skipped file.

    :param name: Relative path of the file.
    :return: File analysis.
    """
    return mr.analyze_file(Path(name), {"Model": "HERO9 Black"}, None, [make_device()])


def targets(entries: Any) -> Dict[str, Optional[str]]:
    """
    Map each planned source to its target, as POSIX strings.

    :param entries: Plan entries.
    :return: Mapping of source to target (``None`` when skipped).
    """
    return {entry.source.as_posix(): entry.target.as_posix() if entry.target else None for entry in entries}


def test_sanitize_stem() -> None:
    """
    Check the deterministic replacement of invalid characters, dots, spaces and reserved names.
    """
    assert mr.sanitize_stem("a<b>c:d\"e/f\\g|h?i*j\tk") == "a_b_c_d_e_f_g_h_i_j_k"
    assert mr.sanitize_stem(" ..name.. ") == "name"
    assert mr.sanitize_stem(" . ") == "_"
    assert mr.sanitize_stem("con") == "_con" and mr.sanitize_stem("COM1.x") == "_COM1.x"
    assert mr.sanitize_stem("CONSOLE") == "CONSOLE"
    assert mr.sanitize_stem("été 2024") == "été 2024"


def test_render_stem() -> None:
    """
    Check every token, zero padding and the sanitizing of token values.
    """
    date = datetime(987, 6, 5, 4, 3, 2)
    pattern = "{YYYY}{MM}{DD}-{HH}{MIN}{SEC}_{DEVICE}_{ORIGINAL}"
    assert mr.render_stem(pattern, date, "Cam", "GOPR0001") == "09870605-040302_Cam_GOPR0001"
    assert mr.render_stem("{ORIGINAL}_{DEVICE}", date, "Cam", "a:b") == "a_b_Cam"
    with pytest.raises(ValueError, match="unknown token"):
        mr.render_stem("{YEAR}", date, "Cam", "x")


def test_build_target_stem() -> None:
    """
    Check that the effective date is used, with a fallback on the capture date.
    """
    analysis = ready("a.jpg")._replace(effective=mr.EffectiveDate(datetime(2025, 5, 6, 7, 8, 9), "", "", ()))
    assert mr.build_target_stem(analysis, PATTERN) == "2025-05-06_07-08-09_GoPro9"
    assert mr.build_target_stem(ready("a.jpg")._replace(effective=None), PATTERN) == BASE
    with pytest.raises(ValueError, match="no date or device"):
        mr.build_target_stem(ready("a.jpg")._replace(device=None), PATTERN)


def test_folder_key() -> None:
    """
    Check that folder keys ignore case and the file name.
    """
    assert mr.folder_key(Path("Sub/A.jpg")) == mr.folder_key(Path("sub/b.JPG")) == "sub"
    assert mr.folder_key(Path("a.jpg")) == "."


def test_main_video_stems() -> None:
    """
    Check the same-name candidate and the GoPro GL to GH/GX candidates.
    """
    assert mr.main_video_stems("GOPR0001") == ["gopr0001"]
    assert mr.main_video_stems("GL010001") == ["gl010001", "gh010001", "gx010001"]


def test_group_auxiliaries() -> None:
    """
    Check pairing in the same folder, GoPro GL names, orphans and duplicate extensions.
    """
    analyses = [
        ready("GH010001.MP4"), auxiliary("GL010001.LRV"), auxiliary("GH010001.THM"),
        auxiliary("GX010001.THM"), ready("GX010001.MP4"), auxiliary("sub/GH010001.THM"),
        skipped("GOPR0002.MP4"), auxiliary("GOPR0002.THM"), ready("GOPR0003.JPG"), auxiliary("GOPR0003.THM"),
        auxiliary("GL010009.LRV"), auxiliary("GL010009.THM"),
    ]
    groups, orphans = mr.group_auxiliaries(analyses)
    assert {main.as_posix(): [aux.relative.name for aux in auxes] for main, auxes in groups.items()} == {
        "GH010001.MP4": ["GL010001.LRV", "GH010001.THM"], "GX010001.MP4": ["GX010001.THM"]}
    assert "no main video" in orphans[Path("sub/GH010001.THM")]
    assert "is skipped" in orphans[Path("GOPR0002.THM")]
    assert "no main video" in orphans[Path("GOPR0003.THM")]
    assert "no main video" in orphans[Path("GL010009.LRV")]
    assert len(orphans) == 5


def test_group_auxiliaries_duplicate_extension() -> None:
    """
    Check that a main video takes only one auxiliary file per extension.
    """
    analyses = [ready("GH010001.MP4"), auxiliary("GH010001.LRV"), auxiliary("GL010001.LRV")]
    groups, orphans = mr.group_auxiliaries(analyses)
    assert [aux.relative.name for aux in groups[Path("GH010001.MP4")]] == ["GH010001.LRV"]
    assert "already follows GH010001.MP4" in orphans[Path("GL010001.LRV")]


def test_list_existing_names(tmp_path: Path) -> None:
    """
    Check that names are case-folded and that a missing folder is empty.
    """
    (tmp_path / "A.JPG").write_bytes(b"")
    (tmp_path / "Sub").mkdir()
    assert mr.list_existing_names(tmp_path) == {"a.jpg", "sub"}
    assert mr.list_existing_names(tmp_path / "missing") == set()


def test_resolve_collision() -> None:
    """
    Check the suffixes, case-insensitive comparisons, own names and multi-extension groups.
    """
    assert mr.resolve_collision("x", [".jpg"], set(), set(), set()) == "x"
    assert mr.resolve_collision("x", [".JPG"], {"x.jpg"}, set(), set()) == "x_01"
    assert mr.resolve_collision("x", [".jpg"], {"x.jpg"}, {"x_01.jpg"}, set()) == "x_02"
    assert mr.resolve_collision("x", [".jpg"], set(), {"x.jpg"}, {"x.jpg"}) == "x"
    assert mr.resolve_collision("x", [".mp4", ".thm"], set(), {"x.thm"}, set()) == "x_01"
    taken = {"x.jpg"} | {"x_{:02d}.jpg".format(index) for index in range(1, 100)}
    assert mr.resolve_collision("x", [".jpg"], taken, set(), set()) == "x_100"


def test_build_plan_is_deterministic(tmp_path: Path) -> None:
    """
    Check that the same input, in any order, always yields the same plan.
    """
    analyses = [ready("b.jpg"), ready("A.jpg"), ready("sub/c.mp4"), auxiliary("sub/c.thm"), ready("a2.jpg"),
                skipped("z.mp4")]
    first = mr.build_plan(analyses, PATTERN, tmp_path)
    assert mr.build_plan(list(reversed(analyses)), PATTERN, tmp_path) == first
    assert mr.build_plan(analyses, PATTERN, tmp_path) == first
    assert [entry.source.as_posix() for entry in first] == ["A.jpg", "a2.jpg", "b.jpg", "sub/c.mp4",
                                                             "sub/c.thm", "z.mp4"]
    assert all(isinstance(entry, mr.PlanEntry) for entry in first)
    with pytest.raises(AttributeError):
        first[0].target = None  # type: ignore[misc]


def test_build_plan_collisions_inside_batch(tmp_path: Path) -> None:
    """
    Check suffixes for identical names and for names differing only by case.
    """
    analyses = [ready("a.jpg"), ready("b.jpg"), ready("c.JPG"), ready("d.png"),
                ready("e.jpg", label="gopro9")]
    assert targets(mr.build_plan(analyses, PATTERN, tmp_path)) == {
        "a.jpg": BASE + ".jpg", "b.jpg": BASE + "_01.jpg", "c.JPG": BASE + "_02.JPG", "d.png": BASE + ".png",
        "e.jpg": "2024-01-02_03-04-05_gopro9_03.jpg"}


def test_build_plan_collisions_with_existing_files(tmp_path: Path) -> None:
    """
    Check that existing files are never targeted, whatever their case, in place and in copy mode.
    """
    (tmp_path / (BASE.upper() + ".JPG")).write_bytes(b"")
    (tmp_path / (BASE + "_01.jpg")).write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / (BASE + ".jpg")).write_bytes(b"")
    plan = mr.build_plan([ready("a.jpg")], PATTERN, tmp_path)
    assert plan[0].target == Path(BASE + "_02.jpg") and plan[0].action == mr.ACTION_RENAME
    plan = mr.build_plan([ready("a.jpg")], PATTERN, tmp_path, dest)
    assert plan[0].target == Path(BASE + "_01.jpg") and plan[0].action == mr.ACTION_COPY


def test_build_plan_keeps_own_name(tmp_path: Path) -> None:
    """
    Check that an already renamed file keeps its name in place, and may change only its case.
    """
    (tmp_path / (BASE + ".jpg")).write_bytes(b"")
    (tmp_path / "2024-01-02_03-04-05_GOPRO9.png").write_bytes(b"")
    plan = mr.build_plan([ready(BASE + ".jpg"), ready("2024-01-02_03-04-05_GOPRO9.png")], PATTERN, tmp_path)
    assert [(entry.target.name, entry.action) for entry in plan] == [
        (BASE + ".jpg", mr.ACTION_UNCHANGED), (BASE + ".png", mr.ACTION_RENAME)]
    copy = mr.build_plan([ready(BASE + ".jpg")], PATTERN, tmp_path, tmp_path / "out")
    assert copy[0].action == mr.ACTION_COPY and copy[0].target == Path(BASE + ".jpg")


def test_build_plan_auxiliary_files_follow_main_video(tmp_path: Path) -> None:
    """
    Check that ``.LRV``/``.THM`` take the final name of their main video, collision suffix included.
    """
    (tmp_path / (BASE + ".THM")).write_bytes(b"")
    analyses = [ready("GH010001.MP4", warnings=["dst"]), auxiliary("GL010001.LRV"), auxiliary("GH010001.THM"),
                auxiliary("GOPR0009.THM")]
    plan = mr.build_plan(analyses, PATTERN, tmp_path)
    assert targets(plan) == {"GH010001.MP4": BASE + "_01.MP4", "GL010001.LRV": BASE + "_01.LRV",
                             "GH010001.THM": BASE + "_01.THM", "GOPR0009.THM": None}
    by_source = {entry.source.name: entry for entry in plan}
    assert by_source["GL010001.LRV"].message == "follows GH010001.MP4"
    assert by_source["GL010001.LRV"].warnings == ("dst",)
    orphan = by_source["GOPR0009.THM"]
    assert orphan.action == mr.ACTION_SKIP and "no main video" in orphan.warnings[0]


def test_build_plan_subfolders_are_independent(tmp_path: Path) -> None:
    """
    Check that the same base name in two subfolders gives no cross-folder pairing or suffix.
    """
    analyses = [ready("a/GH010001.MP4"), auxiliary("a/GH010001.THM"), ready("b/GH010001.MP4"),
                auxiliary("c/GH010001.THM")]
    plan = mr.build_plan(analyses, PATTERN, tmp_path)
    assert targets(plan) == {"a/GH010001.MP4": "a/" + BASE + ".MP4", "a/GH010001.THM": "a/" + BASE + ".THM",
                             "b/GH010001.MP4": "b/" + BASE + ".MP4", "c/GH010001.THM": None}


def test_build_plan_skips(tmp_path: Path) -> None:
    """
    Check the entries of skipped files and of files whose name cannot be built.
    """
    analyses = [skipped("a.mp4"), ready("b.jpg")._replace(device=None)]
    plan = mr.build_plan(analyses, PATTERN, tmp_path)
    assert [(entry.action, entry.target) for entry in plan] == [(mr.ACTION_SKIP, None)] * 2
    assert "no valid capture date" in plan[0].message and "no date or device" in plan[1].message


def test_build_plan_touches_no_file(media_tree: Path) -> None:
    """
    Check that building a plan, in place or with a destination, creates or changes nothing.
    """
    before = snapshot(media_tree)
    folders = sorted(path for path in media_tree.rglob("*") if path.is_dir())
    analyses = [ready("a.mp4"), ready("b.JPG"), ready("sub/c.jpg")]
    mr.build_plan(analyses, PATTERN, media_tree)
    mr.build_plan(analyses, PATTERN, media_tree, media_tree / "new" / "dest")
    assert snapshot(media_tree) == before
    assert sorted(path for path in media_tree.rglob("*") if path.is_dir()) == folders


def test_print_plan() -> None:
    """
    Check the printed actions, targets, messages and warnings.
    """
    entries = [
        mr.PlanEntry(Path("sub/a.jpg"), Path("sub/x.jpg"), mr.ACTION_RENAME, "", ("dst gap",)),
        mr.PlanEntry(Path("a.thm"), Path("x.thm"), mr.ACTION_COPY, "follows a.mp4", ()),
        mr.PlanEntry(Path("x.jpg"), Path("x.jpg"), mr.ACTION_UNCHANGED, "", ()),
        mr.PlanEntry(Path("b.thm"), None, mr.ACTION_SKIP, "orphan", ("orphan",)),
    ]
    out = io.StringIO()
    mr.print_plan(entries, out)
    assert out.getvalue().splitlines() == [
        "Rename plan:",
        "[RENAME] sub/a.jpg -> sub/x.jpg",
        "         warning: dst gap",
        "[COPY]   a.thm -> x.thm  (follows a.mp4)",
        "[SAME]   x.jpg -> x.jpg",
        "[SKIP]   b.thm  (orphan)",
    ]


# --- real ExifTool integration ------------------------------------------------


def snapshot(folder: Path) -> Dict[str, Any]:
    """
    Record the content and modification time of every file of a folder.

    :param folder: Folder to record.
    :return: Mapping of relative path to ``(bytes, mtime_ns)``.
    """
    return {
        path.relative_to(folder).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(folder.rglob("*")) if path.is_file()
    }


@pytest.fixture(scope="session")
def real_exiftool() -> Path:
    """
    Locate a working ExifTool (``bin/``, ``PATH`` or ``EXIFTOOL_PATH``), or skip the test.

    :return: Path of the ExifTool executable.
    """
    try:
        executable = mr.find_exiftool(REPO_ROOT / "bin")
        mr.get_exiftool_version(executable)
    except mr.ExifToolError as error:
        pytest.skip("real ExifTool not available: {}".format(error))
    return executable


MINIMAL_JPEG = b"\xff\xd8\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9"
MINIMAL_TIFF = b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def write_tags(executable: Path, path: Path, tags: Dict[str, str]) -> None:
    """
    Write tags into a sample file with ExifTool.

    :param executable: Path of the ExifTool executable.
    :param path: Sample file.
    :param tags: Tags to write.
    """
    command = [str(executable), "-q", "-overwrite_original"]
    command.extend("-{}={}".format(name, value) for name, value in tags.items())
    subprocess.run(command + [str(path)], check=True, capture_output=True)


def make_mp4(path: Path, created: datetime) -> None:
    """
    Write a minimal MP4 file whose movie header holds a creation date.

    :param path: File to create.
    :param created: Creation date stored in the ``mvhd`` atom.
    """
    seconds = int((created - datetime(1904, 1, 1)).total_seconds())
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = struct.pack(">I4I", 0, seconds, seconds, 1000, 0) + struct.pack(">IH", 0x10000, 0x100)
    mvhd += b"\x00" * 10 + matrix + b"\x00" * 24 + struct.pack(">I", 1)
    mvhd_box = struct.pack(">I", 8 + len(mvhd)) + b"mvhd" + mvhd
    moov = struct.pack(">I", 8 + len(mvhd_box)) + b"moov" + mvhd_box
    ftyp = struct.pack(">I", 20) + b"ftypisom" + struct.pack(">I", 0x200) + b"isom"
    path.write_bytes(ftyp + moov)


@pytest.fixture
def real_samples(tmp_path: Path, real_exiftool: Path) -> Path:
    """
    Create real JPG, MP4 and DNG samples, plus an undated photo, in nested folders.

    :param tmp_path: Pytest temporary folder.
    :param real_exiftool: Path of a working ExifTool.
    :return: Source folder.
    """
    source = tmp_path / "src"
    (source / "été").mkdir(parents=True)
    jpg = source / "été" / "GOPR0001.JPG"
    jpg.write_bytes(MINIMAL_JPEG)
    write_tags(real_exiftool, jpg, {"Make": "GoPro", "Model": "HERO9 Black", "SerialNumber": "C3441326974391",
                                    "DateTimeOriginal": "2024:02:29 23:59:58"})
    make_mp4(source / "GH010001.MP4", datetime(2024, 3, 1, 10, 0, 0))
    write_tags(real_exiftool, source / "GH010001.MP4", {"Model": "HERO9 Black"})
    dng = source / "raw" / "IMG_0001.DNG"
    dng.parent.mkdir()
    dng.write_bytes(MINIMAL_TIFF)
    write_tags(real_exiftool, dng, {"Make": "Canon", "Model": "EOS 6D", "DateTimeOriginal": "2023:12:31 08:00:00"})
    (source / "undated.jpg").write_bytes(MINIMAL_JPEG)
    (source / "corrupt.jpg").write_bytes(b"not a picture")
    return source


def test_read_metadata_real_samples(real_samples: Path, real_exiftool: Path) -> None:
    """
    Check metadata extraction from real JPG, MP4 and DNG files (non-ASCII folder included).
    """
    paths = [real_samples / "été" / "GOPR0001.JPG", real_samples / "GH010001.MP4",
             real_samples / "raw" / "IMG_0001.DNG", real_samples / "missing.jpg"]
    metadata, errors = mr.read_metadata(real_exiftool, paths)
    assert mr.select_capture_date(metadata[paths[0]]) == ("DateTimeOriginal", datetime(2024, 2, 29, 23, 59, 58))
    assert mr.extract_identity(metadata[paths[0]]) == mr.DeviceIdentity("GoPro", "HERO9 Black", "C3441326974391")
    assert mr.select_capture_date(metadata[paths[1]]) == ("CreateDate", datetime(2024, 3, 1, 10, 0, 0))
    assert mr.extract_identity(metadata[paths[2]]).model == "EOS 6D"
    assert "not found" in errors[paths[3]]


def test_analyze_source_real_samples(real_samples: Path, real_exiftool: Path, tmp_path: Path) -> None:
    """
    Check the full analysis with ExifTool: one question per device, persisted, no source change.
    """
    devices_path = write_json(tmp_path / "devices.json", {"devices": []})
    before = snapshot(real_samples)
    # The MP4 (no serial number) is met first, so no serial question is asked.
    answers = ["GoPro9", "-01:00:00", "", "EOS6D", "", ""]
    analyses = mr.analyze_source(real_samples, [".jpg", ".mp4", ".dng"], [], devices_path, real_exiftool,
                                 recursive=True, interactive=True, input_func=scripted(answers),
                                 out=io.StringIO())
    by_name = {analysis.relative.as_posix(): analysis for analysis in analyses}
    assert by_name["GH010001.MP4"].device["label"] == "GoPro9"
    assert by_name["été/GOPR0001.JPG"].device["label"] == "GoPro9"
    assert by_name["raw/IMG_0001.DNG"].device["label"] == "EOS6D"
    assert by_name["undated.jpg"].status == mr.STATUS_SKIPPED
    assert by_name["corrupt.jpg"].status == mr.STATUS_SKIPPED
    assert [device["label"] for device in mr.load_devices(devices_path)] == ["GoPro9", "EOS6D"]
    again = mr.analyze_source(real_samples, [".jpg", ".mp4", ".dng"], mr.load_devices(devices_path),
                              devices_path, real_exiftool, recursive=True, interactive=False)
    assert [analysis.status for analysis in again] == [analysis.status for analysis in analyses]
    assert snapshot(real_samples) == before


def test_effective_dates_leave_metadata_unchanged(real_samples: Path, real_exiftool: Path, tmp_path: Path) -> None:
    """
    Check that offsets and time zones change the effective date only, never the files or their metadata.
    """
    paths = sorted(path for path in real_samples.rglob("*") if path.is_file())
    metadata_before, _ = mr.read_metadata(real_exiftool, paths)
    before = snapshot(real_samples)
    devices = [make_device(match={"model": "HERO9 Black"}, offset="+01:30:00", timezone="Europe/Paris"),
               make_device(id="eos", label="EOS6D", match={"model": "EOS 6D"}, offset="-00:00:30")]
    devices_path = write_json(tmp_path / "devices.json", {"devices": devices})
    analyses = mr.analyze_source(real_samples, [".jpg", ".mp4", ".dng"], devices, devices_path, real_exiftool,
                                 recursive=True, cli_tz="UTC")
    by_name = {analysis.relative.as_posix(): analysis for analysis in analyses}
    assert by_name["été/GOPR0001.JPG"].effective.value == datetime(2024, 3, 1, 1, 29, 58, tzinfo=timezone.utc)
    assert by_name["raw/IMG_0001.DNG"].effective.value == datetime(2023, 12, 31, 7, 59, 30, tzinfo=timezone.utc)
    metadata_after, _ = mr.read_metadata(real_exiftool, paths)
    assert metadata_after == metadata_before
    assert snapshot(real_samples) == before


# --- main ---------------------------------------------------------------------


def test_main_requires_source_without_check(project: Path) -> None:
    """
    Check that ``--source`` is required outside ``--check``.
    """
    with pytest.raises(SystemExit) as exc:
        mr.main([], project_root=project)
    assert exc.value.code == 2


def test_main_check_creates_personal_files(
    checked_project: Path, capsys: pytest.CaptureFixture
) -> None:
    """
    Check that a first run creates the personal files and ``--check`` succeeds.
    """
    conf = checked_project / "conf"
    (conf / "config.json").unlink()
    (conf / "devices.json").unlink()
    assert mr.main(["--check"], project_root=checked_project, environ={"PATH": ""}) == 0
    out = capsys.readouterr().out
    assert "Created" in out and "Back it up" in out
    assert (conf / "config.json").is_file() and (conf / "devices.json").is_file()


def test_main_check_missing_template(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that a missing template makes ``main`` fail.
    """
    assert mr.main(["--check"], project_root=tmp_path) == 1
    assert "template" in capsys.readouterr().err


def test_main_rename_missing_source(checked_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that a missing source folder is reported.
    """
    assert mr.main(["--source", str(checked_project / "nope")], project_root=checked_project) == 1
    assert "folder not found" in capsys.readouterr().err


@pytest.fixture
def analyzed_project(checked_project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Prepare a project with a ``src`` folder of two photos and faked metadata.

    :param checked_project: Project with a fake ExifTool.
    :param monkeypatch: Pytest monkeypatch fixture.
    :return: Project root.
    """
    source = checked_project / "src"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"a")
    (source / "b.jpg").write_bytes(b"b")
    tags = {"DateTimeOriginal": "2024:01:02 03:04:05", "Make": "GoPro", "Model": "HERO9 Black"}

    def fake_read_metadata(executable: Path, paths: Any) -> Any:
        """
        Return the same tags for every file.

        :param executable: Ignored.
        :param paths: Files to describe.
        :return: Tuple ``(metadata, errors)``.
        """
        return {path: dict(tags) for path in paths}, {}

    monkeypatch.setattr(mr, "read_metadata", fake_read_metadata)
    return checked_project


def test_main_analysis_prompts_once_and_saves(analyzed_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that an interactive run registers the unknown device once and prints the analysis.
    """
    answers = iter(["GoPro9", "-01:00:00", ""])
    code = mr.main(["--source", str(analyzed_project / "src")], project_root=analyzed_project,
                   interactive=True, input_func=lambda prompt: next(answers))
    assert code == 0
    out = capsys.readouterr().out
    assert "[OK]   a.jpg" in out and "device 'GoPro9'" in out and "2 ready" in out
    assert "[RENAME] a.jpg -> 2024-01-02_02-04-05_GoPro9.jpg" in out
    assert "[RENAME] b.jpg -> 2024-01-02_02-04-05_GoPro9_01.jpg" in out
    devices = mr.load_devices(analyzed_project / "conf" / "devices.json")
    assert [device["id"] for device in devices] == ["gopro9"]


def test_main_analysis_prints_copy_plan(analyzed_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that ``--dest`` plans copies without creating the destination or touching the sources.
    """
    write_json(analyzed_project / "conf" / "devices.json", {"devices": [make_device()]})
    dest = analyzed_project / "dest"
    before = snapshot(analyzed_project / "src")
    argv = ["--source", str(analyzed_project / "src"), "--dest", str(dest)]
    assert mr.main(argv, project_root=analyzed_project, interactive=False) == 0
    out = capsys.readouterr().out
    assert "[COPY]   a.jpg -> 2024-01-02_02-04-05_GoPro9.jpg" in out
    assert "[COPY]   b.jpg -> 2024-01-02_02-04-05_GoPro9_01.jpg" in out
    assert not dest.exists()
    assert snapshot(analyzed_project / "src") == before


def test_main_analysis_non_interactive_unknown_device(
    analyzed_project: Path, capsys: pytest.CaptureFixture
) -> None:
    """
    Check that a non-interactive run with an unknown device fails clearly.
    """
    code = mr.main(["--source", str(analyzed_project / "src")], project_root=analyzed_project, interactive=False)
    assert code == 1
    assert "unknown device" in capsys.readouterr().err


def test_main_analysis_forced_device_and_apply(analyzed_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that ``--device`` avoids prompts and that ``--apply`` is reported as not implemented yet.
    """
    write_json(analyzed_project / "conf" / "devices.json", {"devices": [make_device(match={"model": "Other"})]})
    argv = ["--source", str(analyzed_project / "src"), "--device", "GOPRO9"]
    assert mr.main(argv, project_root=analyzed_project, interactive=False) == 0
    assert "2 ready" in capsys.readouterr().out
    assert mr.main(argv + ["--apply"], project_root=analyzed_project, interactive=False) == 1
    assert "not implemented yet" in capsys.readouterr().err


def test_main_analysis_applies_offset_and_tz(
    analyzed_project: Path, capsys: pytest.CaptureFixture, paris: Any
) -> None:
    """
    Check that ``--tz`` normalizes the effective dates to UTC after the device offset.
    """
    write_json(analyzed_project / "conf" / "devices.json", {"devices": [make_device()]})
    argv = ["--source", str(analyzed_project / "src"), "--tz", "Europe/Paris"]
    assert mr.main(argv, project_root=analyzed_project, interactive=False) == 0
    out = capsys.readouterr().out
    assert "[OK]   a.jpg  2024-01-02 01:04:05 UTC  device 'GoPro9'" in out
    assert "zone Europe/Paris (--tz)" in out


def test_main_analysis_tz_without_database(
    analyzed_project: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Check that, without a time zone database, ``--tz`` is ignored with a warning (degraded mode).
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    write_json(analyzed_project / "conf" / "devices.json", {"devices": [make_device()]})
    argv = ["--source", str(analyzed_project / "src"), "--tz", "Europe/Paris"]
    assert mr.main(argv, project_root=analyzed_project, interactive=False) == 0
    captured = capsys.readouterr()
    assert "[WARNING] --tz: time zone 'Europe/Paris' ignored" in captured.err
    assert "local" in captured.out and "2 ready" in captured.out


def test_main_analysis_device_tz_without_database(
    analyzed_project: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Check that device time zones are announced as ignored once when no database is available.
    """
    monkeypatch.setattr(mr, "zoneinfo", None)
    write_json(analyzed_project / "conf" / "devices.json",
               {"devices": [make_device(timezone="Europe/Paris")]})
    argv = ["--source", str(analyzed_project / "src")]
    assert mr.main(argv, project_root=analyzed_project, interactive=False) == 0
    captured = capsys.readouterr()
    assert captured.err.count("[WARNING] Device time zones ignored") == 1
    assert "2 ready" in captured.out


def test_main_analysis_missing_exiftool(analyzed_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that a missing ExifTool stops the analysis with a clear message.
    """
    shutil.rmtree(str(analyzed_project / "bin"))
    code = mr.main(["--source", str(analyzed_project / "src")], project_root=analyzed_project,
                   environ={"PATH": ""}, interactive=False)
    assert code == 1
    assert "ExifTool not found" in capsys.readouterr().err


def test_main_rename_invalid_config(checked_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that a rename run stops on an invalid configuration.
    """
    (checked_project / "conf" / "devices.json").write_text("[", encoding="utf-8")
    assert mr.main(["--source", "in"], project_root=checked_project) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_main_rename_unknown_device(checked_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that an unknown ``--device`` id is rejected.
    """
    assert mr.main(["--source", "in", "--device", "nope"], project_root=checked_project) == 1
    assert "unknown device id" in capsys.readouterr().err


def test_main_rename_invalid_tz(checked_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that an invalid ``--tz`` value is rejected.
    """
    assert mr.main(["--source", "in", "--tz", "not a zone"], project_root=checked_project) == 1
    assert "--tz" in capsys.readouterr().err


# --- repository layout --------------------------------------------------------


def test_default_templates_are_valid() -> None:
    """
    Check that the versioned templates pass validation.
    """
    assert mr.validate_config(mr.load_json_file(REPO_ROOT / "conf" / "config.default.json")) == []
    assert mr.load_json_file(REPO_ROOT / "conf" / "devices.default.json") == {"devices": []}


def test_personal_files_are_git_ignored() -> None:
    """
    Check that personal configuration files are listed in ``.gitignore``.
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/conf/config.json" in ignored
    assert "/conf/devices.json" in ignored
