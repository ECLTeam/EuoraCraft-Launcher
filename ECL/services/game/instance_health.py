# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：只读检查实例继承链、主 Jar 与组件声明，不修改损坏的用户文件。
#
# 公开接口：
#   - class InstanceDiagnostic — 描述实例健康问题及启动阻断程度。
#   - class InstanceInspection — 保存继承链、组件与健康检查结果。
# ============================================================
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue, TypeAdapter


@dataclass(frozen=True, slots=True)
class InstanceDiagnostic:
    """
    保存稳定诊断码与相关版本名，不携带个人目录路径。
    """

    code: str
    version_id: str
    severity: str


@dataclass(frozen=True, slots=True)
class InstanceInspection:
    """
    检查启动所需的本地声明，并保留全部实际组件。

    缺失可从已声明客户端下载地址恢复的 Jar 为警告；缺失或损坏 JSON、
    继承循环及无恢复来源的主 Jar 阻止启动。检查本身不下载或写入文件。
    """

    documents: tuple[dict[str, JsonValue], ...]
    diagnostics: tuple[InstanceDiagnostic, ...]
    components: tuple[tuple[str, str], ...]

    json_adapter: ClassVar[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
    component_prefixes: ClassVar[tuple[tuple[str, str], ...]] = (
        ("net.minecraftforge:forge:", "Forge"),
        ("net.neoforged:neoforge:", "NeoForge"),
        ("net.neoforged:forge:", "NeoForge"),
        ("net.fabricmc:fabric-loader:", "Fabric"),
        ("org.quiltmc:quilt-loader:", "Quilt"),
        ("optifine:OptiFine:", "OptiFine"),
    )

    @classmethod
    def inspect(cls, minecraft_root_path: Path, version_id: str) -> InstanceInspection:
        """
        在版本根目录边界内遍历继承链，最多读取 64 个有界 JSON。

        :param minecraft_root_path: 已解析的 Minecraft 根目录
        :param version_id: 实例目录名
        :return: 只读检查结果；单个坏文件不会使目录清单丢失
        """
        documents: list[dict[str, JsonValue]] = []
        diagnostics: list[InstanceDiagnostic] = []
        names: list[str] = []
        current = version_id
        while current:
            if current in names or len(names) >= 64:
                diagnostics.append(InstanceDiagnostic("inheritance_cycle", current, "error"))
                break
            if current in {".", ".."} or any(character in current for character in "/\\:"):
                diagnostics.append(InstanceDiagnostic("invalid_inheritance", current, "error"))
                break
            names.append(current)
            json_path = minecraft_root_path / "versions" / current / f"{current}.json"
            document = cls._read_document(json_path)
            if document is None:
                code = "missing_json" if not json_path.is_file() else "invalid_json"
                if current != version_id and code == "missing_json":
                    code = "missing_parent"
                diagnostics.append(InstanceDiagnostic(code, current, "error"))
                break
            documents.append(document)
            parent = document.get("inheritsFrom")
            if parent is not None and (not isinstance(parent, str) or not parent.strip()):
                diagnostics.append(InstanceDiagnostic("invalid_inheritance", current, "error"))
                break
            current = parent or ""
        if documents and not any(issue.severity == "error" for issue in diagnostics):
            cls._inspect_jar(minecraft_root_path, names, documents, diagnostics)
            if not any(isinstance(document.get("mainClass"), str) for document in documents):
                diagnostics.append(InstanceDiagnostic("metadata_unknown", version_id, "warning"))
        return cls(tuple(documents), tuple(diagnostics), cls._components(documents))

    @classmethod
    def _read_document(cls, json_path: Path) -> dict[str, JsonValue] | None:
        """
        限制元数据大小并在不可信 JSON 边界校验对象结构。
        """
        try:
            if json_path.stat().st_size > 8 * 1024 * 1024:
                return None
            document = cls.json_adapter.validate_python(json.loads(json_path.read_text(encoding="utf-8-sig")))
            return document if isinstance(document, dict) and isinstance(document.get("id"), str) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _inspect_jar(
        root: Path, names: list[str], documents: list[dict[str, JsonValue]], diagnostics: list[InstanceDiagnostic]
    ) -> None:
        """
        接受继承父版本的主 Jar 和安装器复制在子目录的父版本 Jar。

        客户端下载地址只说明现有启动流程可以补齐文件，不代表文件已下载。
        """
        candidates = [root / "versions" / name / f"{name}.jar" for name in names]
        if len(names) > 1:
            candidates.append(root / "versions" / names[0] / f"{names[1]}.jar")
        if any(path.is_file() for path in candidates):
            return
        can_download = False
        for document in documents:
            downloads = document.get("downloads")
            client = downloads.get("client") if isinstance(downloads, dict) else None
            url = client.get("url") if isinstance(client, dict) else None
            can_download |= isinstance(url, str) and url.startswith(("https://", "http://"))
        diagnostics.append(InstanceDiagnostic("missing_jar", names[0], "warning" if can_download else "error"))

    @classmethod
    def _components(cls, documents: list[dict[str, JsonValue]]) -> tuple[tuple[str, str], ...]:
        components: dict[str, str] = {}
        for document in reversed(documents):
            libraries = document.get("libraries", [])
            if not isinstance(libraries, list):
                continue
            for library in libraries:
                name = library.get("name") if isinstance(library, dict) else None
                if not isinstance(name, str):
                    continue
                for prefix, component in cls.component_prefixes:
                    if name.casefold().startswith(prefix.casefold()):
                        components[component] = name[len(prefix) :].split(":")[0]
        return tuple(components.items())

    def to_health(self) -> dict[str, JsonValue]:
        """
        输出稳定健康协议，启动阻断由诊断严重程度派生。

        :return: 健康状态、是否允许启动与具体诊断
        """
        can_launch = not any(issue.severity == "error" for issue in self.diagnostics)
        return {
            "status": "blocked" if not can_launch else "warning" if self.diagnostics else "healthy",
            "canLaunch": can_launch,
            "diagnostics": [
                {"code": issue.code, "versionId": issue.version_id, "severity": issue.severity}
                for issue in self.diagnostics
            ],
        }

    def mod_environment(self, fallback_minecraft_version: str | None = None) -> dict[str, str | None]:
        """
        提供实际声明的游戏与加载器版本，Java 未探测时保留未知。

        :param fallback_minecraft_version: Core 已识别的实际原版版本
        :return: 模组诊断使用的运行环境；不使用 Java 最低要求冒充实际版本
        """
        environment: dict[str, str | None] = {"minecraft": fallback_minecraft_version, "java": None}
        if not environment["minecraft"] and self.documents:
            identifier = self.documents[-1].get("id")
            environment["minecraft"] = (
                identifier
                if isinstance(identifier, str)
                and re.fullmatch(r"\d+(?:\.\d+)+(?:[-_].+)?|\d{2}w\d{2}[a-z]", identifier)
                else None
            )
        for name, version in self.components:
            if name == "Fabric":
                environment["fabricloader"] = version
            elif name == "Quilt":
                environment["quilt_loader"] = version
            elif name == "Forge":
                environment["forge"] = version.split("-", 1)[-1]
            elif name == "NeoForge":
                environment["neoforge"] = version
        return environment
