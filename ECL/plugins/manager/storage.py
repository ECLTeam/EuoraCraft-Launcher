# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件存储：插件数据目录与键值持久化。
#
# 公开接口：
#   - class PluginStorage — 负责插件配置与禁用状态文件的读写及持久化。
# ============================================================

import json

from ECL.utils import atomic_write_text

from .base import _PluginState


class PluginStorage(_PluginState):
    """
    负责插件配置与禁用状态文件的读写及持久化。
    """

    def _load_plugin_state(self) -> None:
        # 从 plugins/state.json 读取已禁用的插件列表。
        if self._plugin_state_path is None or not self._plugin_state_path.is_file():
            self._disabled_plugins = set()
            return
        try:
            state = json.loads(self._plugin_state_path.read_text(encoding="utf-8"))
            disabled = state.get("disabled", [])
            self._disabled_plugins = set(disabled) if isinstance(disabled, list) else set()
        except (json.JSONDecodeError, OSError):
            self.logger.warning("插件状态文件解析失败: %s", self._plugin_state_path)
            self._disabled_plugins = set()

    def _save_plugin_state(self) -> None:
        # 将已禁用的插件列表写入 plugins/state.json。
        if self._plugin_state_path is None:
            return
        state = {"disabled": sorted(self._disabled_plugins)}
        atomic_write_text(self._plugin_state_path, json.dumps(state, ensure_ascii=False, indent=2))

    def _prune_plugin_state(
        self,
        available_plugins: set[str],
        non_disableable_plugins: set[str] | None = None,
    ) -> None:
        # 清理已不存在的插件所留下的禁用记录。
        removed_plugins = (self._disabled_plugins - available_plugins) | (
            self._disabled_plugins & (non_disableable_plugins or set())
        )
        if not removed_plugins:
            return
        self._disabled_plugins.difference_update(removed_plugins)
        self._save_plugin_state()
        self.logger.info("已清理无效的插件禁用记录: %s", sorted(removed_plugins))
