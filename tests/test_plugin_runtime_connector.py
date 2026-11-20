from __future__ import annotations

import subprocess
import sys

from ECL.api.registry import IpcCommandRegistry
from ECL.utils.config import default_config


def test_host_import_does_not_load_connector_dependency() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import ECL.application; print('easytier_pyo3' in sys.modules)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "False"
    assert "connector" not in default_config


def test_host_registers_no_connector_ipc_commands() -> None:
    assert not any(name.startswith("connector_") for name in IpcCommandRegistry.command_names)
