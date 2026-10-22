from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from ECL.plugins.grant_models import GrantLease
from ECL.plugins.runtime.file_handles import FileHandle
from ECL.plugins.runtime.file_storage import PluginFileStorage, StagedFile
from ECL.plugins.runtime.models import FileRef


def prepare_case(data_path: Path) -> tuple[PluginFileStorage, StagedFile, FileHandle]:
    storage = PluginFileStorage(data_path)
    lease = GrantLease("a" * 32, "b" * 64, "c" * 32, 1, "filesystem.replace", ())
    with storage.access(lease.installation_id, initialize=True, admit=lambda: None) as space:
        source = space.directory.file(".initial", create=True)
        source.write(0, b"original")
        source.flush()
        source.publish("target.bin")
        source.close()
        owned = []
        original = space.directory.delete_target("target.bin", owned.append)
        old = original.information()
        upload = StagedFile(
            FileRef(root_ref="d" * 32, relative_path="target.bin"),
            lease,
            space.directory,
            3,
            3,
            expected_etag=old.etag(hashlib.sha256(b"original").hexdigest()),
        )
        storage.reserve(upload)
        space.directory.file(
            upload.temporary_name, create=True, on_create=lambda stream: setattr(upload, "stream", stream)
        )
        upload.stream.write(0, b"new")
        upload.stream.flush()
        upload.received_bytes = 3
        upload.owner = "task"
        return storage, upload, original


def main():
    data_path, point = Path(sys.argv[1]).resolve(), sys.argv[2]
    data_path.mkdir(exist_ok=True)
    storage, upload, original = prepare_case(data_path)
    ready_path = data_path / "ready"

    def freeze():
        ready_path.write_text("ready", encoding="utf-8")
        sys.stdin.read()
        raise RuntimeError("测试应在此阶段终止进程")

    save = storage.replacements._save
    move, publish = FileHandle.move, FileHandle.publish

    def save_at(item):
        if point == "before_prepared" and item.record.phase == "prepared":
            freeze()
        save(item)
        if point == item.record.phase:
            freeze()

    def move_at(stream, name):
        move(stream, name)
        if point == "after_move" and name.startswith(".replace-"):
            freeze()

    def publish_at(stream, name):
        if point == "conflict":
            target_path = data_path / "plugin_data" / upload.lease.installation_id / "files" / name
            target_path.write_bytes(b"foreign")
            freeze()
        publish(stream, name)
        if point == "after_publish":
            freeze()

    storage.replacements._save = save_at
    FileHandle.move, FileHandle.publish = move_at, publish_at
    storage.replacements.commit(
        upload,
        original,
        original.information(),
        upload.stream.information(),
        hashlib.sha256(b"new").hexdigest(),
        hashlib.sha256(b"original").hexdigest(),
    )
    if point == "after_backup_deleted":
        item = storage.replacements.for_upload(upload.write_ref)
        storage.replacements._settle(item)
        freeze()
    raise AssertionError("没有到达指定崩溃点")


if __name__ == "__main__":
    main()
