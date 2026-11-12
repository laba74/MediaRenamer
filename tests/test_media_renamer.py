"""Unit tests for the ``media_renamer`` module."""

import io
import json
import os
import shutil
import subprocess
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


def test_run_check_reports_missing_exiftool(project: Path) -> None:
    """
    Check that ``--check`` fails explicitly when ExifTool is missing.
    """
    mr.ensure_personal_files(project / "conf")
    code, out, err = run_check_captured(project, environ={"PATH": ""})
    assert code == 1
    assert "ExifTool not found" in err
    assert "[OK] Configuration" in out


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


def test_main_rename_not_implemented(checked_project: Path, capsys: pytest.CaptureFixture) -> None:
    """
    Check that a rename run validates the configuration then reports it is not implemented yet.
    """
    assert mr.main(["--source", "in"], project_root=checked_project) == 1
    assert "not implemented yet" in capsys.readouterr().err


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
