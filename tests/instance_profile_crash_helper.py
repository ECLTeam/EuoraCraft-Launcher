from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event
from time import monotonic

from ECL.foundation import EventBus
from ECL.services.game.instance_profile_models import InstanceProfilePatch
from ECL.services.game.instance_profile_transactions import InstanceProfileTarget
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore
from ECL.services.operations import OperationContext
from ECL.utils.instance_reading import directory_identity
from ECL.utils.windows_file_handles import FileHandle


def main():
    case, point, has_original = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3] == "existing"
    root = case / "game"
    instance = root / "versions/one"
    instance.mkdir(parents=True)
    (instance / "one.json").write_text('{"id":"one"}', encoding="utf-8")
    profile_path = instance / ".ecl/instance.json"
    if has_original:
        profile_path.parent.mkdir()
        profile_path.write_text('{"schemaVersion":1,"alias":"Original","private":"retained"}', encoding="utf-8")
    store = InstanceProfileStore(Path(sys.argv[4]) if len(sys.argv) > 4 else case / "data", VersionStatsStore())
    version = store.snapshot(root, "one", monotonic() + 30).etag
    target = InstanceProfileTarget(root, "one", directory_identity(root), directory_identity(instance))
    item = store.transactions.reserve(target, "a" * 32)

    def freeze():
        (case / "ready").write_text("ready", encoding="utf-8")
        sys.stdin.read()
        raise AssertionError("测试必须在此阶段终止进程")

    save = store.transactions._save
    move, publish = FileHandle.move, FileHandle.publish

    def save_at(resource):
        if point == "before_prepared" and resource.record.phase == "prepared":
            freeze()
        save(resource)
        if point == resource.record.phase:
            freeze()

    def move_at(stream, name):
        move(stream, name)
        if point == "after_move" and name.startswith(".profile-old-"):
            freeze()

    def publish_at(stream, name):
        if name == "instance.json" and point == "conflict":
            profile_path.write_text('{"alias":"External"}', encoding="utf-8")
            freeze()
        publish(stream, name)
        if name == "instance.json" and point == "after_publish":
            freeze()

    store.transactions._save = save_at
    FileHandle.move, FileHandle.publish = move_at, publish_at
    store.transactions.patch(
        item,
        version,
        InstanceProfilePatch(alias="Published"),
        OperationContext("b" * 32, EventBus(), Event()),
        monotonic() + 30,
        lambda: None,
    )
    if point == "after_backup_deleted":
        store.transactions._settle(item, item.native.profile_directory)
        freeze()
    raise AssertionError(json.dumps({"point": point, "reached": False}))


if __name__ == "__main__":
    main()
