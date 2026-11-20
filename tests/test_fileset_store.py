# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对用户媒体文件集存储与引用检查的自动化测试。
#
# 公开接口：
#   - test_import_copies_file_and_reports_new_item(tmp_path) -> None
#   - test_import_deduplicates_same_content(tmp_path) -> None
#   - test_import_rejects_corrupted_image(tmp_path) -> None
#   - test_import_rejects_unsupported_extension(tmp_path) -> None
#   - test_import_rejects_oversized_image(tmp_path) -> None
#   - test_import_reports_partial_failure(tmp_path) -> None
#   - test_thumbnail_generated_for_image(tmp_path) -> None
#   - test_video_has_no_thumbnail(tmp_path) -> None
#   - test_rename_updates_display_name(tmp_path) -> None
#   - test_delete_removes_file_and_thumbnail(tmp_path) -> None
#   - test_corrupted_metadata_is_backed_up_and_rebuilt(tmp_path) -> None
#   - test_delete_blocks_background_reference(tmp_path) -> None
# ============================================================

from types import SimpleNamespace

from PIL import Image

from ECL.api.fileset import FileSetHandlers
from ECL.fileset.store import FileSetError, FileSetStore


def _write_image(path, size: tuple[int, int] = (64, 48)) -> None:
    Image.new("RGB", size, (20, 120, 200)).save(path)


def _make_store(tmp_path) -> FileSetStore:
    return FileSetStore(tmp_path / "data")


def test_import_copies_file_and_reports_new_item(tmp_path) -> None:
    store = _make_store(tmp_path)
    source = tmp_path / "picture.png"
    _write_image(source)

    item, duplicated = store.import_file(source)

    assert duplicated is False
    assert item.kind == "image"
    assert (item.width, item.height) == (64, 48)
    path, kind = store.resolve(item.id)
    assert path.is_file()
    assert kind == "image"
    assert path.parent == store.files_path


def test_import_deduplicates_same_content(tmp_path) -> None:
    store = _make_store(tmp_path)
    first = tmp_path / "a.png"
    copy = tmp_path / "b.png"
    _write_image(first)
    copy.write_bytes(first.read_bytes())

    item, duplicated_first = store.import_file(first)
    same_item, duplicated_second = store.import_file(copy)

    assert duplicated_first is False
    assert duplicated_second is True
    assert same_item.id == item.id
    assert len(store.list_items()) == 1
    assert len(list(store.files_path.iterdir())) == 1


def test_import_rejects_corrupted_image(tmp_path) -> None:
    store = _make_store(tmp_path)
    fake = tmp_path / "fake.png"
    fake.write_text("not an image", encoding="utf-8")

    try:
        store.import_file(fake)
    except FileSetError as exc:
        assert exc.code == "FILESET_IMAGE_INVALID"
    else:  # pragma: no cover - 失败路径必须抛错
        raise AssertionError("伪装图片应被拒绝")


def test_import_rejects_unsupported_extension(tmp_path) -> None:
    store = _make_store(tmp_path)
    text = tmp_path / "note.txt"
    text.write_text("hello", encoding="utf-8")

    try:
        store.import_file(text)
    except FileSetError as exc:
        assert exc.code == "FILESET_FILE_UNSUPPORTED"
    else:  # pragma: no cover - 失败路径必须抛错
        raise AssertionError("不支持的扩展名应被拒绝")


def test_import_rejects_oversized_image(tmp_path) -> None:
    store = _make_store(tmp_path)
    store.max_image_bytes = 10
    source = tmp_path / "big.png"
    _write_image(source)

    try:
        store.import_file(source)
    except FileSetError as exc:
        assert exc.code == "FILESET_FILE_TOO_LARGE"
    else:  # pragma: no cover - 失败路径必须抛错
        raise AssertionError("超过大小上限应被拒绝")


def test_import_reports_partial_failure(tmp_path) -> None:
    store = _make_store(tmp_path)
    good = tmp_path / "good.png"
    bad = tmp_path / "bad.png"
    _write_image(good)
    bad.write_text("broken", encoding="utf-8")

    result = store.import_files([good, bad])

    assert len(result["imported"]) == 1
    assert result["duplicated"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0]["path"].endswith("bad.png")


def test_thumbnail_generated_for_image(tmp_path) -> None:
    store = _make_store(tmp_path)
    source = tmp_path / "picture.png"
    _write_image(source, size=(1024, 768))
    item, _ = store.import_file(source)

    thumbnail = store.thumbnail_path(item.id)

    assert thumbnail is not None
    assert thumbnail.is_file()
    assert thumbnail.parent == store.thumbnail_root
    with Image.open(thumbnail) as image:
        assert max(image.size) <= store.thumbnail_size


def test_video_has_no_thumbnail(tmp_path) -> None:
    store = _make_store(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")
    item, _ = store.import_file(video)

    assert item.kind == "video"
    assert store.thumbnail_path(item.id) is None


def test_rename_updates_display_name(tmp_path) -> None:
    store = _make_store(tmp_path)
    source = tmp_path / "picture.png"
    _write_image(source)
    item, _ = store.import_file(source)

    updated = store.rename_item(item.id, "  背景图  ")

    assert updated.name == "背景图"
    assert updated.id == item.id
    assert updated.sha256 == item.sha256
    assert store.list_items()[0].name == "背景图"


def test_delete_removes_file_and_thumbnail(tmp_path) -> None:
    store = _make_store(tmp_path)
    source = tmp_path / "picture.png"
    _write_image(source)
    item, _ = store.import_file(source)
    path, _kind = store.resolve(item.id)
    thumbnail = store.thumbnail_path(item.id)

    deleted = store.delete_items([item.id])

    assert deleted == [item.id]
    assert store.list_items() == []
    assert not path.exists()
    assert thumbnail is not None and not thumbnail.exists()


def test_corrupted_metadata_is_backed_up_and_rebuilt(tmp_path) -> None:
    store = _make_store(tmp_path)
    store.metadata_path.write_text("{ not json", encoding="utf-8")

    rebuilt = FileSetStore(tmp_path / "data")

    assert rebuilt.list_items() == []
    backups = list(rebuilt.metadata_path.parent.glob("filesets.corrupt-*.json"))
    assert len(backups) == 1


def test_delete_blocks_background_reference(tmp_path) -> None:
    store = _make_store(tmp_path)
    source = tmp_path / "background.png"
    _write_image(source)
    item, _ = store.import_file(source)
    background_path = str(store.resolve(item.id)[0])

    handler = object.__new__(FileSetHandlers)
    handler.fileset = store
    handler.config = SimpleNamespace(get_config=lambda section: {"background": {"image": {"path": background_path}}})

    assert handler._items_in_use([item.id]) == {item.id}

    handler.config = SimpleNamespace(get_config=lambda section: {"background": {"image": {"path": ""}}})
    assert handler._items_in_use([item.id]) == set()
