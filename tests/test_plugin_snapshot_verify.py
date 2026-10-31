from __future__ import annotations

from pathlib import Path

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugin_api.snapshot import CodeSnapshot


def _make_code_tree(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "resources").mkdir()
    (root / "resources" / "data.json").write_text('{"value": 1}', encoding="utf-8")
    return root


def test_unchanged_tree_reuses_previous_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    snapshot = CodeSnapshot.capture(code_path)

    captures: list[Path] = []
    original_capture = CodeSnapshot.capture.__func__

    def record_capture(cls: type[CodeSnapshot], root_path: Path) -> CodeSnapshot:
        captures.append(Path(root_path))
        return original_capture(cls, root_path)

    monkeypatch.setattr(CodeSnapshot, "capture", classmethod(record_capture))

    snapshot.verify(snapshot.digest())

    assert captures == []


def test_changed_content_with_same_size_is_rejected(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    snapshot = CodeSnapshot.capture(code_path)
    snapshot.verify(snapshot.digest())

    (code_path / "main.py").write_text("print('HELLO')\n", encoding="utf-8")

    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest())


def test_added_file_is_rejected(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    snapshot = CodeSnapshot.capture(code_path)
    snapshot.verify(snapshot.digest())

    (code_path / "extra.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest())


def test_removed_file_is_rejected(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    snapshot = CodeSnapshot.capture(code_path)
    snapshot.verify(snapshot.digest())

    (code_path / "resources" / "data.json").unlink()

    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest())


def test_touched_file_with_same_content_still_passes(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    snapshot = CodeSnapshot.capture(code_path)
    snapshot.verify(snapshot.digest())

    target = code_path / "main.py"
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

    snapshot.verify(snapshot.digest())
