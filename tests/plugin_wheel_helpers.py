from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

from ECL.plugins.package_archive import build_plugin_package
from ECL.plugins.package_preparation import current_plugin_target


def make_wheel(
    folder_path: Path,
    name: str = "ecl_test_dependency",
    version: str = "1.0.0",
    *,
    requirements: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    members: dict[str, bytes] | None = None,
    tag: str = "py3-none-any",
    metadata_name: str | None = None,
    requires_python: str = ">=3.11",
) -> Path:
    folder_path.mkdir(parents=True, exist_ok=True)
    wheel_path = folder_path / f"{name}-{version}-{tag}.whl"
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {metadata_name or name}\nVersion: {version}\nRequires-Python: {requires_python}\n"
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
    metadata += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    files = {
        f"{name}.py": b"VALUE = 'installed'\n",
        f"{dist_info}/METADATA": metadata.encode(),
        f"{dist_info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: ecl-test\nRoot-Is-Purelib: true\nTag: {tag}\n".encode(),
        **(members or {}),
    }
    record_name = f"{dist_info}/RECORD"
    records = []
    for filename, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        records.append(f"{filename},sha256={digest},{len(content)}\n")
    records.append(f"{record_name},,\n")
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
        archive.writestr(record_name, "".join(records))
    return wheel_path


def wheel_lock(*wheel_paths: Path) -> bytes:
    lines = []
    for wheel_path in wheel_paths:
        name, version = wheel_path.name.split("-")[:2]
        digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        lines.append(f"{name}=={version} --hash=sha256:{digest}\n")
    return "".join(lines).encode()


def make_package(
    folder_path: Path,
    name: str = "demo",
    version: str = "1.0.0",
    *,
    code: str | None = None,
    wheel_paths: tuple[Path, ...] = (),
    dependencies: tuple[str, ...] = (),
    plugin_dependencies: dict[str, str] | None = None,
    permissions: tuple[dict[str, str], ...] = (),
) -> Path:
    source_path = folder_path / f"{name}-{version}-source"
    source_path.mkdir(parents=True)
    (source_path / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "title": "归档示例",
                "entry_point": "main:Plugin",
                "pythonDependencies": list(dependencies),
                "dependencies": plugin_dependencies or {},
                "permissions": list(permissions),
            }
        ),
        encoding="utf-8",
    )
    (source_path / "main.py").write_text(
        code
        or "from ecl_plugin_sdk import Plugin as BasePlugin\nimport os\nclass Plugin(BasePlugin):\n    @BasePlugin.on_command('pid')\n    def pid(self):\n        return os.getpid()\n",
        encoding="utf-8",
    )
    if wheel_paths:
        lock_path = source_path / "locks" / f"{current_plugin_target()}.txt"
        lock_path.parent.mkdir()
        lock_path.write_bytes(wheel_lock(*wheel_paths))
        wheelhouse_path = source_path / "wheels" / current_plugin_target()
        wheelhouse_path.mkdir(parents=True)
        for wheel_path in wheel_paths:
            (wheelhouse_path / wheel_path.name).write_bytes(wheel_path.read_bytes())
    archive_path = folder_path / f"{name}-{version}.eclplugin"
    build_plugin_package(source_path, archive_path)
    return archive_path
