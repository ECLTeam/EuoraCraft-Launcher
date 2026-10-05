# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：有界解析本地模组声明与内嵌 Jar，诊断启用状态、提供 ID 和原始版本约束。
#
# 公开接口：
#   - class ModDependency — 保存加载器声明的依赖类别与原始约束。
#   - class ModDeclaration — 保存一个加载器模组及其提供的别名。
#   - class LocalModMetadata — 保存一个物理文件的声明与解析覆盖问题。
#   - class LocalModParser — 解析 Fabric、Quilt、Forge 和 NeoForge 元数据。
#   - class ModDependencyDiagnostics — 根据启用文件和实际环境生成诊断。
# ============================================================
from __future__ import annotations

import io
import json
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import JsonValue, TypeAdapter, ValidationError

from .mod_versions import ModVersionPredicate


@dataclass(frozen=True, slots=True)
class ModDependency:
    """
    保留一个加载器依赖的原始类别、约束与运行侧。
    """

    dependency_id: str
    kind: str
    constraints: tuple[str, ...]
    syntax: str
    side: str = "both"


@dataclass(frozen=True, slots=True)
class ModDeclaration:
    """
    描述一个声明模组及其随容器文件启用的提供别名。
    """

    mod_id: str
    version: str
    name: str
    loader: str
    provides: tuple[str, ...]
    dependencies: tuple[ModDependency, ...]
    embedded_path: str = ""


@dataclass(frozen=True, slots=True)
class LocalModMetadata:
    """
    保存物理文件中的全部声明和解析覆盖问题。
    """

    filename: str
    enabled: bool
    declarations: tuple[ModDeclaration, ...]
    author: str
    icon: str | None
    issues: tuple[str, ...]

    def legacy_summary(self) -> dict[str, JsonValue]:
        """
        在旧资源 IPC 边界输出兼容字段，并补充明确的模组身份。

        :return: 可序列化的清单摘要；projectId 仅作为旧加载器 ID 别名保留
        """
        first = self.declarations[0] if self.declarations else None
        dependencies = [
            dependency.dependency_id
            for declaration in self.declarations
            for dependency in declaration.dependencies
            if dependency.kind == "required"
        ]
        return {
            "name": first.name if first else self.filename.removesuffix(".disabled").removesuffix(".jar"),
            "modId": first.mod_id if first else None,
            "projectId": first.mod_id if first else None,
            "version": first.version if first else "",
            "loader": first.loader if first else "unknown",
            "author": self.author,
            "icon": self.icon,
            "dependencies": list(dict.fromkeys(dependencies)),
            "gameVersion": next(
                (
                    " || ".join(dependency.constraints)
                    for declaration in self.declarations
                    for dependency in declaration.dependencies
                    if dependency.dependency_id == "minecraft"
                ),
                None,
            ),
            "declaredModIds": [declaration.mod_id for declaration in self.declarations],
            "providedModIds": list(
                dict.fromkeys(
                    identifier
                    for declaration in self.declarations
                    for identifier in (declaration.mod_id, *declaration.provides)
                )
            ),
            "dependencyDeclarations": [
                {
                    "consumerId": declaration.mod_id,
                    "modId": dependency.dependency_id,
                    "kind": dependency.kind,
                    "constraints": list(dependency.constraints),
                    "syntax": dependency.syntax,
                    "side": dependency.side,
                    "embeddedPath": declaration.embedded_path,
                }
                for declaration in self.declarations
                for dependency in declaration.dependencies
            ],
            "metadataIssues": list(self.issues),
        }


class LocalModParser:
    """
    有界读取加载器元数据与声明的内嵌 Jar，不提取文件到磁盘。

    内嵌内容仅在成功解析声明时提供模组 ID；超限、损坏和未知结构保留
    覆盖问题，不把未读取的内容当作依赖已经满足。
    """

    max_entries: int = 4096
    max_metadata_bytes: int = 2 * 1024 * 1024
    max_nested_bytes: int = 32 * 1024 * 1024
    max_depth: int = 3
    max_nested_jars: int = 128
    json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

    @classmethod
    def parse(cls, file_path: Path) -> LocalModMetadata:
        """
        解析一个物理模组文件，禁用后缀由容器文件决定。

        :param file_path: 要读取的 Jar 或 .jar.disabled 文件
        :return: 不可变的本地声明与覆盖问题
        """
        declarations: list[ModDeclaration] = []
        issues: list[str] = []
        details: dict[str, str] = {}
        budget = [cls.max_nested_bytes, cls.max_nested_jars]
        try:
            with zipfile.ZipFile(file_path) as archive:
                cls._archive(archive, declarations, issues, details, budget, 0, "")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ValidationError):
            issues.append("invalid_archive")
        if not declarations and not issues:
            issues.append("unrecognized_metadata")
        return LocalModMetadata(
            file_path.name,
            not file_path.name.casefold().endswith(".disabled"),
            tuple(declarations),
            details.get("author", ""),
            details.get("icon"),
            tuple(dict.fromkeys(issues)),
        )

    @staticmethod
    def _object(value: JsonValue) -> dict[str, JsonValue]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _strings(value: JsonValue) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        return (json.dumps(value, ensure_ascii=False),)

    @classmethod
    def _read(cls, archive: zipfile.ZipFile, name: str, limit: int | None = None) -> bytes:
        """
        先核对声明大小再读取单个条目，拒绝异常压缩比与超限内容。
        """
        entry = archive.getinfo(name)
        if (
            entry.file_size > (cls.max_metadata_bytes if limit is None else limit)
            or entry.file_size > max(1, entry.compress_size) * 1000
        ):
            raise ValueError("metadata_limit")
        return archive.read(entry)

    @classmethod
    def _document(cls, archive: zipfile.ZipFile, name: str) -> dict[str, JsonValue]:
        """
        在有界读取后校验外部 JSON/TOML，业务解析只消费合法 JSON 值。
        """
        content = cls._read(archive, name).decode("utf-8-sig")
        value = tomllib.loads(content) if name.endswith(".toml") else json.loads(content)
        return cls._object(cls.json_adapter.validate_python(value))

    @classmethod
    def _metadata(
        cls,
        archive: zipfile.ZipFile,
        declarations: list[ModDeclaration],
        issues: list[str],
        details: dict[str, str],
        embedded_path: str,
    ) -> list[str]:
        """
        解析当前容器声明并返回明确列出的内嵌路径。
        """
        if len(archive.infolist()) > cls.max_entries:
            issues.append("entry_limit")
            return []
        names = set(archive.namelist())
        nested: list[str] = []
        try:
            if "fabric.mod.json" in names:
                document = cls._document(archive, "fabric.mod.json")
                declarations.extend(cls._fabric(document, "fabric", embedded_path))
                nested = (
                    [str(cls._object(item).get("file")) for item in document.get("jars", []) if isinstance(item, dict)]
                    if isinstance(document.get("jars"), list)
                    else []
                )
                authors = document.get("authors", [])
                if isinstance(authors, list):
                    details.setdefault(
                        "author",
                        ", ".join(
                            item if isinstance(item, str) else str(cls._object(item).get("name", ""))
                            for item in authors
                        ),
                    )
                icon = document.get("icon")
                if isinstance(icon, dict):
                    icon = next((item for item in icon.values() if isinstance(item, str)), None)
                if isinstance(icon, str):
                    details.setdefault("icon", icon)
            elif "quilt.mod.json" in names:
                document = cls._document(archive, "quilt.mod.json")
                declarations.extend(cls._quilt(document, embedded_path))
                issues.append("quilt_predicate_coverage")
            else:
                toml_name = (
                    "META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names else "META-INF/mods.toml"
                )
                if toml_name in names:
                    document = cls._document(archive, toml_name)
                    cls._resolve_jar_version(archive, document)
                    declarations.extend(
                        cls._forge(document, "neoforge" if "neoforge" in toml_name else "forge", embedded_path)
                    )
                    mods = document.get("mods", [])
                    first = cls._object(mods[0]) if isinstance(mods, list) and mods else {}
                    details.setdefault("author", str(first.get("authors", "")))
                    icon = first.get("logoFile") or first.get("icon")
                    if isinstance(icon, str):
                        details.setdefault("icon", icon)
            if "META-INF/jarjar/metadata.json" in names:
                jars = cls._document(archive, "META-INF/jarjar/metadata.json").get("jars", [])
                if isinstance(jars, list):
                    nested.extend(str(cls._object(item).get("path", "")) for item in jars)
        except (ValueError, KeyError, RuntimeError, ValidationError):
            issues.append("invalid_metadata")
        return nested

    @classmethod
    def _resolve_jar_version(cls, archive: zipfile.ZipFile, document: dict[str, JsonValue]) -> None:
        """
        只替换当前解析器拥有的元数据副本中的标准 Jar 版本占位符。
        """
        if "META-INF/MANIFEST.MF" not in archive.namelist():
            return
        manifest = cls._read(archive, "META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
        jar_version = next(
            (
                line.partition(":")[2].strip()
                for line in manifest.splitlines()
                if line.startswith("Implementation-Version:")
            ),
            "",
        )
        mods = document.get("mods")
        for mod in mods if isinstance(mods, list) else []:
            if isinstance(mod, dict) and mod.get("version") == "${file.jarVersion}":
                mod["version"] = jar_version

    @classmethod
    def _archive(
        cls,
        archive: zipfile.ZipFile,
        declarations: list[ModDeclaration],
        issues: list[str],
        details: dict[str, str],
        budget: list[int],
        depth: int,
        embedded_path: str,
    ) -> None:
        """
        递归解析声明的 Jar；读取量、深度和数量共享同一预算。
        """
        nested = cls._metadata(archive, declarations, issues, details, embedded_path)
        for name in dict.fromkeys(nested):
            if depth >= cls.max_depth or budget[1] <= 0:
                issues.append("nested_limit")
                break
            try:
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or ":" in name
                    or ".." in PurePosixPath(name.replace("\\", "/")).parts
                ):
                    raise ValueError("invalid_nested_path")
                content = cls._read(archive, name, min(budget[0], cls.max_nested_bytes))
                budget[0] -= len(content)
                budget[1] -= 1
                if budget[0] < 0:
                    raise ValueError("nested_limit")
                with zipfile.ZipFile(io.BytesIO(content)) as child:
                    cls._archive(
                        child, declarations, issues, {}, budget, depth + 1, f"{embedded_path}!{name}".lstrip("!")
                    )
            except (ValueError, KeyError, RuntimeError, zipfile.BadZipFile):
                issues.append("invalid_nested_archive")

    @classmethod
    def _fabric(cls, document: dict[str, JsonValue], loader: str, embedded_path: str) -> list[ModDeclaration]:
        """
        保留 Fabric 的提供别名与不同依赖类别，不将可选依赖升级为必需。
        """
        mod_id = document.get("id")
        if not isinstance(mod_id, str) or not mod_id:
            return []
        dependencies: list[ModDependency] = []
        for field, kind in (
            ("depends", "required"),
            ("recommends", "recommended"),
            ("suggests", "optional"),
            ("breaks", "incompatible"),
            ("conflicts", "discouraged"),
        ):
            for identifier, constraint in cls._object(document.get(field)).items():
                dependencies.append(ModDependency(identifier, kind, cls._strings(constraint), "fabric"))
        provides = document.get("provides", [])
        return [
            ModDeclaration(
                mod_id,
                str(document.get("version", "")),
                str(document.get("name") or mod_id),
                loader,
                tuple(item for item in provides if isinstance(item, str)) if isinstance(provides, list) else (),
                tuple(dependencies),
                embedded_path,
            )
        ]

    @classmethod
    def _quilt(cls, document: dict[str, JsonValue], embedded_path: str) -> list[ModDeclaration]:
        """
        读取 Quilt 提供者与原始版本声明；未覆盖的谓词交由未知诊断呈现。
        """
        loader = cls._object(document.get("quilt_loader"))
        metadata = cls._object(loader.get("metadata"))
        mod_id = loader.get("id")
        if not isinstance(mod_id, str):
            return []
        raw_dependencies = loader.get("depends", [])
        dependencies = (
            tuple(
                ModDependency(
                    str(item.get("id", "")),
                    "optional" if item.get("optional") is True else "required",
                    cls._strings(item.get("versions", "*")),
                    "quilt",
                )
                for item in raw_dependencies
                if isinstance(item, dict)
            )
            if isinstance(raw_dependencies, list)
            else ()
        )
        provides = loader.get("provides", [])
        identifiers = (
            tuple(
                str(item.get("id", "")) if isinstance(item, dict) else item
                for item in provides
                if isinstance(item, (dict, str))
            )
            if isinstance(provides, list)
            else ()
        )
        return [
            ModDeclaration(
                mod_id,
                str(loader.get("version", "")),
                str(metadata.get("name") or mod_id),
                "quilt",
                identifiers,
                dependencies,
                embedded_path,
            )
        ]

    @classmethod
    def _forge(cls, document: dict[str, JsonValue], loader: str, embedded_path: str) -> list[ModDeclaration]:
        """
        逐个读取 Forge/NeoForge 声明，现代依赖类别优先于旧 mandatory 字段。
        """
        mods = document.get("mods", [])
        if not isinstance(mods, list):
            return []
        result: list[ModDeclaration] = []
        dependency_by_consumer = cls._object(document.get("dependencies"))
        for item in mods:
            if not isinstance(item, dict) or not isinstance(item.get("modId"), str):
                continue
            mod_id = str(item["modId"])
            raw_dependencies = dependency_by_consumer.get(mod_id, [])
            dependencies = []
            for dependency in raw_dependencies if isinstance(raw_dependencies, list) else []:
                if not isinstance(dependency, dict) or not isinstance(dependency.get("modId"), str):
                    continue
                kind = str(
                    dependency.get("type")
                    or (
                        "required"
                        if dependency.get("mandatory") is True
                        else "optional"
                        if "mandatory" in dependency
                        else "required"
                    )
                )
                dependencies.append(
                    ModDependency(
                        str(dependency["modId"]),
                        kind,
                        cls._strings(dependency.get("versionRange", "*")),
                        "maven",
                        str(dependency.get("side", "BOTH")).lower(),
                    )
                )
            result.append(
                ModDeclaration(
                    mod_id,
                    str(item.get("version", "")),
                    str(item.get("displayName") or mod_id),
                    loader,
                    (),
                    tuple(dependencies),
                    embedded_path,
                )
            )
        return result


class ModDependencyDiagnostics:
    """
    分离文件启用状态与模组提供关系，保留无法判断的约束。

    内嵌声明继承外层文件的启用状态；运行环境只接受实际已知版本。
    """

    @classmethod
    def evaluate(
        cls, files: tuple[LocalModMetadata, ...], environment: dict[str, str | None] | None = None
    ) -> dict[str, list[dict[str, JsonValue]]]:
        """
        按物理文件生成可呈现的依赖诊断，不自动修改任何模组。

        :param files: 同一实例数据目录内的模组元数据
        :param environment: 已确认的 Minecraft、加载器和 Java 版本
        :return: 文件名到诊断列表的映射
        """
        providers: dict[str, list[tuple[LocalModMetadata, ModDeclaration]]] = {}
        for file in files:
            for declaration in file.declarations:
                for identifier in dict.fromkeys((declaration.mod_id, *declaration.provides)):
                    providers.setdefault(identifier, []).append((file, declaration))
        results: dict[str, list[dict[str, JsonValue]]] = {}
        for file in files:
            issues: list[dict[str, JsonValue]] = [
                {"code": "metadata_unknown", "detail": issue, "severity": "warning"} for issue in file.issues
            ]
            if file.enabled:
                for declaration in file.declarations:
                    for identifier in dict.fromkeys((declaration.mod_id, *declaration.provides)):
                        enabled = [provider for provider in providers.get(identifier, []) if provider[0].enabled]
                        if len(enabled) > 1:
                            issues.append({"code": "duplicate_provider", "modId": identifier, "severity": "error"})
                    for dependency in declaration.dependencies:
                        if dependency.side == "server":
                            continue
                        issues.extend(cls._dependency(dependency, providers, environment or {}))
            results[file.filename] = issues
        return results

    @staticmethod
    def _dependency(
        dependency: ModDependency,
        providers: dict[str, list[tuple[LocalModMetadata, ModDeclaration]]],
        environment: dict[str, str | None],
    ) -> list[dict[str, JsonValue]]:
        """
        仅由启用文件和已知环境匹配约束，缺失、冲突及未知结果分别保留。
        """
        candidates = providers.get(dependency.dependency_id, [])
        enabled = [declaration.version for file, declaration in candidates if file.enabled]
        builtin = dependency.dependency_id in {"minecraft", "java", "fabricloader", "forge", "neoforge", "quilt_loader"}
        if builtin:
            enabled = [environment.get(dependency.dependency_id)]
        base: dict[str, JsonValue] = {
            "modId": dependency.dependency_id,
            "constraints": list(dependency.constraints),
            "severity": "error" if dependency.kind in {"required", "incompatible"} else "warning",
        }
        if dependency.kind not in {
            "required",
            "recommended",
            "optional",
            "incompatible",
            "discouraged",
        } or dependency.side not in {"both", "client", "server"}:
            return [{**base, "code": "constraint_unknown"}]
        if not enabled:
            if dependency.kind != "required":
                return []
            return [
                {
                    **base,
                    "code": "disabled_provider" if candidates else "missing_required",
                    "providers": [file.filename for file, _declaration in candidates],
                }
            ]
        matches = [
            ModVersionPredicate.matches(version, dependency.constraints, dependency.syntax) for version in enabled
        ]
        if dependency.kind in {"incompatible", "discouraged"}:
            return (
                [{**base, "code": "conflicting_provider"}]
                if True in matches
                else [{**base, "code": "constraint_unknown"}]
                if None in matches
                else []
            )
        if True in matches:
            return []
        return [{**base, "code": "constraint_unknown" if None in matches else "version_mismatch"}]
