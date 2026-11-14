from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.runtime.models import SettingDefinition
from ECL.plugins.runtime.selected_files import read_selected_file


def view(path: Path) -> dict:
    return {
        "revision": 1,
        "schema": [{"key": "index", "type": "readonly_file"}],
        "values": {"index": str(path)},
        "readonly_files": {"index": str(path)},
    }


def test_readonly_file_cannot_have_plugin_supplied_default(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SettingDefinition(key="index", type="readonly_file", default=str(tmp_path))


def test_selected_file_reads_exact_chunks_and_detects_changed_revision(tmp_path: Path) -> None:
    target = tmp_path / "index.json"
    content = b"x" * 9000
    target.write_bytes(content)
    selected = view(target)
    first = read_selected_file(selected, {"key": "index"})
    assert base64.b64decode(first["content"]) == content[:8192]
    second = read_selected_file(
        selected, {"key": "index", "offset": first["next_offset"], "revision": first["revision"]}
    )
    assert base64.b64decode(second["content"]) == content[8192:]
    assert second["next_offset"] is None
    target.write_bytes(b"changed")
    with pytest.raises(ProtocolError, match="文件已变化"):
        read_selected_file(selected, {"key": "index", "revision": first["revision"]})
    assert target.read_bytes() == b"changed"


def test_selected_file_rejects_undeclared_key_directory_and_extra_path(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError):
        read_selected_file(view(tmp_path), {"key": "secret"})
    with pytest.raises(ProtocolError):
        read_selected_file(view(tmp_path), {"key": "index"})
    with pytest.raises(ValidationError):
        read_selected_file(view(tmp_path), {"key": "index", "path": "private"})


def test_schema_redefinition_cannot_authorize_file_read(tmp_path: Path) -> None:
    from ECL.plugins.settings import PluginSettingsService

    service = PluginSettingsService(tmp_path / "data")
    installation, digest, run = "a" * 32, "b" * 64, "c" * 32
    target = tmp_path / "private.json"
    target.write_text("secret", encoding="utf-8")

    def define(kind: str):
        return service.define(
            installation,
            digest,
            run,
            [SettingDefinition(key="index", type=kind, default="")],
            lambda *_args: None,
            lambda register: register(),
        )

    define("string")
    service.update(installation, digest, {"index": str(target)}, 0, lambda: None, run_id=run)
    schema = define("readonly_file")
    with pytest.raises(ProtocolError, match="未由用户选择"):
        read_selected_file(service.view(installation, digest, run), {"key": "index"})
    service.publish(installation, run)
    service.update(installation, digest, {"index": str(target)}, 1, lambda: None, schema_revision=schema)
    assert (
        base64.b64decode(read_selected_file(service.view(installation, digest, run), {"key": "index"})["content"])
        == b"secret"
    )
