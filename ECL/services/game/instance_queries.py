# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过受控读取生成实例摘要，不执行桌面扫描、统计写入或外部插件回调。
#
# 公开接口：
#   - class InstanceSummary — 不含路径的只读实例摘要。
#   - class InstanceQueryCoordinator — 既有 GameService 的无写入查询入口。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ECL.game import InstanceMetadata
from ECL.plugin_api.contracts import JsonValue
from ECL.utils.instance_reading import InstanceReadBoundary, InstanceReadError

from .base import _GameState
from .instance_profiles import InstanceProfileStore


@dataclass(frozen=True, slots=True)
class InstanceSummary:
    """
    保存显式投影的公开字段，不携带路径、凭据和原始元数据。
    """

    version_id: str
    alias: str
    loader: str | None
    game_version: str | None
    description: str
    favorite: bool
    tags: tuple[str, ...]
    profile_etag: str
    running_count: int

    def to_protocol(self, instance_ref: str) -> dict[str, JsonValue]:
        """
        将摘要和宿主提供的运行引用转换为公开 DTO。

        :param instance_ref: 当前运行有效的引用
        :return: 不含服务对象和路径的 JSON 对象
        """
        return {
            "instance_ref": instance_ref,
            "version_id": self.version_id,
            "alias": self.alias,
            "loader": self.loader,
            "game_version": self.game_version,
            "profile": {"description": self.description, "favorite": self.favorite, "tags": list(self.tags)},
            "profile_etag": self.profile_etag,
            "running_count": self.running_count,
        }


class InstanceQueryCoordinator(_GameState):
    """
    复用游戏领域规则，只读取指定资源的本地元数据和运行状态快照。
    """

    @staticmethod
    def _query_name(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or value.endswith((".", " "))
            or value in {".", ".."}
            or any(character in value for character in "/\\:\0")
        ):
            raise InstanceReadError("实例标识无效", "PLUGIN_INSTANCE_METADATA_INVALID")
        return value

    def query_instance_names(self, root_path: Path, deadline: float) -> tuple[str, ...]:
        """
        有界枚举根内直接实例，不创建统计、配置或目录监视器。

        :param root_path: 宿主批准的规范目录
        :param deadline: 本次查询截止时间
        :return: 有实例声明或 Jar 标记的目录名称
        :raises InstanceReadError: 目录、身份或条目数量无效时抛出
        """
        reader = InstanceReadBoundary(root_path, deadline)
        versions_path = root_path / "versions"
        if not versions_path.exists() and not versions_path.is_symlink():
            reader.check()
            return ()
        reader.directory(Path("versions"))
        names = []
        try:
            for candidate in versions_path.iterdir():
                reader.check()
                if candidate.name.startswith(".") or not candidate.is_dir():
                    continue
                name = self._query_name(candidate.name)
                reader.directory(Path("versions") / name)
                if (candidate / f"{name}.json").exists() or (candidate / f"{name}.jar").is_file():
                    names.append(name)
                if len(names) > 4096:
                    raise InstanceReadError("实例数量超过查询上限", "PLUGIN_MESSAGE_TOO_LARGE")
            reader.directory(Path("versions"))
            return tuple(sorted(names))
        except OSError as exc:
            raise InstanceReadError("无法枚举实例目录") from exc

    def query_instance_summary(
        self, root_path: Path, version_id: str, deadline: float, *, reader: InstanceReadBoundary | None = None
    ) -> InstanceSummary:
        """
        只读取指定实例及其必要父声明，不扫描其他实例资料或执行退出收尾。

        :param root_path: 宿主批准的规范目录
        :param version_id: 明确选定的实例目录名
        :param deadline: 本次查询截止时间
        :param reader: 同一次扫描共用的读取预算
        :return: 公开字段摘要
        :raises InstanceReadError: 文件损坏、继承越界或读取失败时抛出
        """
        name = self._query_name(version_id)
        reader = reader or InstanceReadBoundary(root_path, deadline)
        identity = reader.directory(Path("versions") / name)
        documents: list[dict[str, JsonValue]] = []
        visited: set[str] = set()
        current = name
        while current:
            current = self._query_name(current)
            if current in visited or len(visited) >= 64:
                raise InstanceReadError("实例继承链无效", "PLUGIN_INSTANCE_METADATA_INVALID")
            visited.add(current)
            document = reader.read_json(Path("versions") / current / f"{current}.json", 8 * 1024 * 1024, optional=True)
            if document is None:
                if documents:
                    raise InstanceReadError("实例父声明不存在", "PLUGIN_INSTANCE_METADATA_INVALID")
                break
            if not isinstance(document.get("id"), str):
                raise InstanceReadError("实例声明缺少标识", "PLUGIN_INSTANCE_METADATA_INVALID")
            documents.append(document)
            parent = document.get("inheritsFrom")
            if parent is not None and not isinstance(parent, str):
                raise InstanceReadError("实例父声明无效", "PLUGIN_INSTANCE_METADATA_INVALID")
            current = parent or ""
        metadata = InstanceMetadata.from_documents(tuple(documents))
        snapshot = self._instance_profiles.snapshot(root_path, name, deadline)
        try:
            fields = InstanceProfileStore.parse_local_fields(snapshot.document)
        except ValueError as exc:
            raise InstanceReadError("实例资料格式无效", "PLUGIN_INSTANCE_METADATA_INVALID") from exc
        profile = {
            "alias": fields.get("alias", name),
            "description": fields.get("description", ""),
            "favorite": fields.get("favorite", False),
            "tags": fields.get("tags", []),
        }
        etag = snapshot.etag
        with self._lock:
            running_count = sum(
                1
                for run in self._running_games.values()
                if not run.pending
                and not run.exited
                and run.exit_code is None
                and run.game_path == root_path
                and run.version_id == name
            )
        if reader.directory(Path("versions") / name) != identity:
            raise InstanceReadError("实例目录已经被替换")
        return InstanceSummary(
            name,
            str(profile["alias"]),
            next(iter(dict(metadata.components)), "Vanilla" if documents else None),
            metadata.mod_environment()["minecraft"],
            str(profile["description"]),
            profile["favorite"] is True,
            tuple(profile["tags"]),
            etag,
            running_count,
        )
