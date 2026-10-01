from __future__ import annotations

import pytest

from ECL.services.game.base import GameServiceError
from ECL.services.game.resources import ResourceCoordinator


def test_batch_delete_reports_missing_resources_and_keeps_other_failures(tmp_path, monkeypatch) -> None:
    service = object.__new__(ResourceCoordinator)
    monkeypatch.setattr(ResourceCoordinator, "_resource_root", lambda *args: tmp_path)
    (tmp_path / "first.zip").write_bytes(b"test")
    result = service.delete_resources(tmp_path, "test", "resourcepack", ["first.zip", "missing.zip"])
    assert result["deleted"] == ["first.zip"]
    assert result["failed"][0]["resourceId"] == "missing.zip"
    assert not (tmp_path / "first.zip").exists()


@pytest.mark.parametrize("invalid_id", ["../outside.zip", ".", " "])
def test_batch_delete_validates_whole_batch_before_deleting(tmp_path, monkeypatch, invalid_id) -> None:
    service = object.__new__(ResourceCoordinator)
    monkeypatch.setattr(ResourceCoordinator, "_resource_root", lambda *args: tmp_path)
    existing = tmp_path / "first.zip"
    existing.write_bytes(b"keep")
    with pytest.raises(GameServiceError):
        service.delete_resources(tmp_path, "test", "resourcepack", ["first.zip", invalid_id])
    assert existing.read_bytes() == b"keep"
