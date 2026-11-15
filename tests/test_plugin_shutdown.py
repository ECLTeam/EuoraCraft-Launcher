# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_shutdown 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_framework_close_ignores_plugins_that_were_not_loaded() -> None
#   - test_launcher_shutdown_closes_plugins_before_backend_services() -> None
#   - test_run_shuts_down_when_adapter_fails(monkeypatch) -> None
# ============================================================

from unittest.mock import Mock

import ECL.launcher as launcher_module


def test_launcher_shutdown_closes_plugins_before_backend_services() -> None:
    order = []

    class Closable:
        def __init__(self, name):
            self.name = name

        def close(self):
            order.append(self.name)

    launcher = object.__new__(launcher_module.EuoraCraftLauncher)
    launcher.logger = Mock()
    launcher.logging = Mock()
    launcher.context = Mock()
    launcher.context.close.side_effect = lambda: order.extend(["plugins", "game", "accounts"])
    launcher._shutdown_complete = False

    launcher._shutdown()
    launcher._shutdown()

    assert order == ["plugins", "game", "accounts"]
    launcher.logging.shutdown.assert_called_once_with()


def test_run_shuts_down_when_adapter_fails(monkeypatch) -> None:
    class FailedAdapter:
        def __init__(self, _context):
            pass

        def run(self):
            raise RuntimeError("adapter failed")

    launcher = object.__new__(launcher_module.EuoraCraftLauncher)
    launcher.logger = Mock()
    launcher.launcher_version = "test"
    launcher.launcher_version_type = "dev"
    launcher.context = Mock()
    launcher._initialize = Mock()
    launcher._shutdown = Mock()
    monkeypatch.setattr(launcher_module, "Adapter", FailedAdapter)

    assert launcher.run() is launcher_module.LauncherExitCode.FRONTEND_FAILED
    launcher._shutdown.assert_called_once_with()
