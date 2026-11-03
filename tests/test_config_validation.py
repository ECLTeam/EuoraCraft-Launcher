from __future__ import annotations

from pathlib import Path

import pytest

from ECL.utils.config import ConfigStore
from ECL.utils.errors import ConfigValidationError


@pytest.mark.parametrize("section", sorted(ConfigStore.allowed_sections))
def test_section_save_rejects_non_object_without_changing_state(tmp_path: Path, section: str) -> None:
    store = ConfigStore(tmp_path / "data")
    previous = store.get_config()
    previous_file = store.config_path.read_bytes()
    notifications: list[str] = []
    store.events.subscribe("config:updated", lambda section, _snapshot: notifications.append(section))

    with pytest.raises(ConfigValidationError, match="配置分区必须为对象"):
        store.save_config(section, 42)

    assert store.get_config() == previous
    assert store.config_path.read_bytes() == previous_file
    assert notifications == []
