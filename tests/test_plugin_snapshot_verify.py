from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.api.snapshot import CodeSnapshot


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


def test_git_metadata_is_excluded_from_content_identity(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    git_dir = code_path / ".git"
    (git_dir / "objects" / "a1").mkdir(parents=True)
    (git_dir / "objects" / "a1" / "blob").write_text("object", encoding="utf-8")

    snapshot = CodeSnapshot.capture(code_path)
    assert all(not item.name.startswith(".git") for item in snapshot.files)

    # .git 内的任何变化都不改变内容身份：摘要与逐文件复核均不受影响。
    (git_dir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    assert CodeSnapshot.capture(code_path).digest() == snapshot.digest()
    snapshot.verify(snapshot.digest())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 创建符号链接需要特权，不作为排除逻辑的前提条件")
def test_git_symlink_is_ignored_rather_than_rejected(tmp_path: Path) -> None:
    code_path = _make_code_tree(tmp_path / "code")
    (code_path / ".git").symlink_to(tmp_path / "elsewhere", target_is_directory=False)

    snapshot = CodeSnapshot.capture(code_path)
    snapshot.verify(snapshot.digest())
