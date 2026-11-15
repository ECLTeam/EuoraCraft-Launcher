# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件发现：扫描插件目录并解析 plugin.json 元数据，统一登记到唯一运行服务。
#
# 公开接口：
#   - class PluginDiscovery — 负责插件候选项的发现、依赖解析与登记加载。
# ============================================================

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ECL.plugins.dependencies import (
    DependencyResolution,
    PluginDependencyInfo,
    parse_dependency,
    resolve_dependencies,
)
from ECL.plugins.installation_store import manifest_validation_error_code

from .base import _PluginState


class PluginDiscovery(_PluginState):
    """
    负责插件候选项的发现、依赖解析与实例化加载。
    """

    def _collect_candidates(self, base_dir: Path, is_system: bool) -> list[dict[str, Any]]:
        # 扫描目录，读取各插件的 plugin.json 并构造候选条目列表。
        candidates: list[dict[str, Any]] = []
        if not base_dir.is_dir():
            return candidates
        for plugin_dir in sorted(base_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            metadata_path = plugin_dir / "plugin.json"
            if not metadata_path.is_file():
                continue
            try:
                if metadata_path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("插件清单超过上限")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError, OSError):
                self.logger.warning("插件清单无法读取或超过上限")
                continue
            if not isinstance(metadata, dict):
                self.logger.warning("插件清单必须是对象")
                continue
            name = metadata.get("name")
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None:
                self.logger.warning("插件清单名称无效")
                continue
            candidates.append(
                {
                    "name": name,
                    "plugin_dir": plugin_dir,
                    "metadata_path": metadata_path,
                    "metadata": metadata,
                    "is_system": is_system,
                }
            )
        return candidates

    def _resolve_candidate_dependencies(self, candidates: list[dict[str, Any]]) -> DependencyResolution:
        # 根据候选插件元数据解析依赖关系与加载顺序。
        seen: set[str] = set()
        infos: list[PluginDependencyInfo] = []
        for candidate in candidates:
            name = candidate["name"]
            if name in seen:
                self.logger.warning("插件 %s 重复，跳过", name)
                continue
            seen.add(name)
            if not candidate["is_system"] and self._status.get(name) == "unsupported":
                continue
            metadata = candidate["metadata"]
            deps_meta = metadata.get("dependencies", {})
            deps: list[Any] = []
            for dep_name, dep_value in deps_meta.items():
                req = parse_dependency(dep_name, dep_value)
                if req is None:
                    self.logger.warning("插件 %s 的依赖 %s 格式无效", name, dep_name)
                    continue
                deps.append(req)
            infos.append(
                PluginDependencyInfo(
                    name=name,
                    version=metadata.get("version", "0.0.0"),
                    dependencies=deps,
                    is_system=candidate["is_system"],
                )
            )
        return resolve_dependencies(infos)

    def _load_plugins_in_order(self, candidates: list[dict[str, Any]], load_order: list[str]) -> None:
        # 按依赖拓扑顺序实例化并加载插件；被禁用的插件直接跳过。
        candidate_map = {c["name"]: c for c in candidates}
        for name in self._disabled_plugins:
            candidate = candidate_map.get(name)
            if candidate is None:
                continue
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], candidate["is_system"])
            if self._status.get(name) != "unsupported":
                self._status[name] = "disabled"
        for name in load_order:
            if name in self._disabled_plugins:
                self.logger.info("插件 %s 已被禁用，跳过加载", name)
                continue
            candidate = candidate_map.get(name)
            if candidate is None:
                continue
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], candidate["is_system"])

    def _load_plugin(self, plugin_dir: Path, metadata_path: Path, is_system: bool) -> None:
        # 登记单个插件：读取清单并交给唯一运行服务；系统插件进程内执行，用户插件子进程执行。
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.logger.warning("插件元数据解析失败: %s", metadata_path)
            return
        name = metadata.get("name")
        if not name:
            return
        reason = self._session_disable_reason(name)
        if reason:
            self._status[name] = "disabled"
            self._plugin_errors[name] = reason
            return
        try:
            assert self.worker_service is not None
            active = self._package_entries.get(name, {}).get("active")
            dependency = active.dependency_directory if active else None
            source = "system" if is_system else ("package" if active else "directory")
            self.worker_service.register(metadata, plugin_dir, source, dependency)
            self._status[name] = "unloaded"
            self.logger.info(
                "插件已登记: %s；执行形态：%s", name, ("进程内" if is_system else "子进程")
            )
        except (OSError, ValueError) as exc:
            self._status[name] = "unsupported"
            if isinstance(exc, ValidationError):
                self._plugin_errors[name] = manifest_validation_error_code(exc)
            else:
                self._plugin_errors[name] = getattr(exc, "error_code", "PLUGIN_MANIFEST_INVALID")
