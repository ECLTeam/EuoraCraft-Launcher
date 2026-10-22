# 实例扫描候选收窄与残留清理实施方案

## 1. 背景与问题

启动器在扫描 `versions/` 目录时，会把其中**所有非点号子目录**都当作候选实例，导致大量非实例目录（`logs`、`mods`、`crash-reports`、`downloads` 等）被列为「无效实例」。同时，扫描过程会向这些无关目录写入 `eclversion.json`（版本运行统计文件），污染用户目录。

### 实机复现

用户当前游戏目录 `D:\Projects\EuoraCraft-Launcher\EuoraCraft-Launcher\.minecraft\versions` 下：

| 目录 | 含 `<目录名>.json` | 含 `<目录名>.jar` | 当前表现 |
| --- | --- | --- | --- |
| `114514`、`26.3-pre-2`、`26.3-rc-2`、`26.3-snapshot-9/10` | 是 | — | 正常实例 |
| `fabric-loader-0.19.3-26.2`、`neoforge-21.1.248-1.21.1` | 是 | — | 正常实例 |
| `logs`、`mods`、`crash-reports` | 否 | 否 | 被误列为无效实例 |
| `downloads`（含 0 字节 `downloads.json`） | 是（空文件） | 否 | 被误列为无效实例 |

上述 `logs`、`mods`、`crash-reports`、`downloads` 目录内均已被写入 `eclversion.json`（内容为默认统计值）。

## 2. 根因

引入点：提交 `b094a2f`（feat: 保留实例健康诊断并区分 Java 运行时信息）。

`ECL/services/game/scan.py` 的 `ScanCoordinator._scan_game_path`（约第 338–344 行）新增补全逻辑：

```python
for directory in versions_path.iterdir():
    if (
        directory.is_dir()
        and not directory.name.startswith(".")
        and directory.resolve().parent == versions_path.resolve()
    ):
        versions.setdefault(directory.name, {})   # 任何目录都当成版本
```

失效链条：

1. Core 的 `SearchMinecraft.search_minecraft()` 只返回**含 `<目录名>.json`** 的目录（无 JSON 直接 `continue`）。
2. 上述循环把 `versions/` 下**所有非点号目录**补入，信息为空 `{}`。
3. `_normalize_scanned_version` → `InstanceInspection.inspect` 读不到 `<name>.json` → 报 `missing_json`（error）→ `canLaunch=false` → `isBroken=true`。
4. 同一循环产出的条目还会进入 `_version_stats.ensure(game_path, version_id)`，于是启动器向无关目录写入 `eclversion.json`。

原意：让「Core 漏扫、但确实是实例目录」的条目仍能显示（对应测试 `tests/test_instance_health.py::test_scan_retains_directory_omitted_by_core`，其构造的 `broken` 目录是**带 `broken.json`** 的）。但实现未做任何标志判断，收得过宽。

## 3. 目标与非目标

**目标**

- 非实例目录不再出现在实例列表中。
- 不再向非实例目录写入 `eclversion.json`。
- 保留「有实例标志但 JSON 缺失/损坏」条目的诊断能力。

**非目标**

- 不改动 `InstanceInspection` 的健康判定规则（`missing_json`/`invalid_json`/`missing_jar` 等诊断码保持不变）。
- 不改动真实实例目录内的任何文件。
- 不重构实例列表前端展示（本方案不含「默认折叠无效实例」）。
- **不自动清理历史遗留的 `eclversion.json`**：已确认由用户自行处理，本方案不新增清理逻辑。

## 4. 方案（已确认）

**按实例标志收窄候选目录**：仅当目录含 `<目录名>.jar`，或含**非空**的 `<目录名>.json` 时，才补入候选实例。

该规则：

- 排除 `logs`、`mods`、`crash-reports`（无 JSON、无 Jar）。
- 排除 `downloads`（`downloads.json` 为 0 字节）。
- 保留「JSON 损坏（非空但不可解析）」「缺 JSON 但有主 Jar」的诊断能力。

## 5. 详细改动点

### 5.1 `ECL/services/game/scan.py`

新增静态辅助方法：

```python
@staticmethod
def _has_instance_marker(version_directory: Path) -> bool:
    """判断版本目录是否带实例标志：主 Jar 存在，或版本 JSON 存在且非空。"""
    name = version_directory.name
    if (version_directory / f"{name}.jar").is_file():
        return True
    manifest_path = version_directory / f"{name}.json"
    try:
        return manifest_path.is_file() and manifest_path.stat().st_size > 0
    except OSError:
        return False
```

`_scan_game_path` 的补全循环增加该判断：

```python
for directory in versions_path.iterdir():
    if (
        directory.is_dir()
        and not directory.name.startswith(".")
        and directory.resolve().parent == versions_path.resolve()
        and self._has_instance_marker(directory)
    ):
        versions.setdefault(directory.name, {})
```

副作用随之消失：非实例目录不再进入 `normalized_versions`，`_version_stats.ensure` 不再被调用。

### 5.2 残留清理（不在本方案内）

历史遗留的 `eclversion.json` 由用户自行清理。本方案不新增清理代码，改动后启动器也不会再向非实例目录写入该文件。

修复后，`.minecraft/versions` 下非实例目录中已存在的 `eclversion.json` 需用户手动删除；这些文件仅是启动器写入的默认统计值，删除不影响实例。

## 6. 测试计划

### 6.1 新增 / 更新单元测试（`tests/`）

1. `test_scan_skips_directories_without_instance_marker`
   构造 `versions/` 下的 `logs`、`mods`、`crash-reports`（无 JSON/Jar）与一个真实实例，断言结果只包含真实实例。
2. `test_scan_skips_empty_version_json`
   构造 `downloads/downloads.json`（0 字节），断言其不被列入。
3. `test_scan_retains_corrupt_version_json`
   构造 `<name>.json` 内容为 `broken`（非空不可解析），断言其仍被列入且 `isBroken` 为真、诊断码为 `invalid_json`。
4. `test_scan_retains_jar_only_directory`
   构造仅有 `<name>.jar` 的目录，断言其被列入且 `isBroken` 为真（`missing_json`）。
5. 保留并复核 `test_scan_retains_directory_omitted_by_core`：其目录带 `broken.json`，应仍然通过。

### 6.2 质量门禁

- `ruff check ECL tests`、`ruff format --check ECL tests`
- `pytest`（含上述新增用例）
- 前端未改动，无需 `pnpm check`；但需确认无前端依赖变更。

### 6.3 实机核对

- 以当前 `.minecraft` 运行扫描，确认实例列表不再出现 `logs`/`mods`/`crash-reports`/`downloads`。

## 7. 影响范围与风险

- **影响**：`ECL/services/game/scan.py` 的实例补全逻辑；实例列表内容。
- **风险 1**：收窄后，某些「Core 漏扫且无任何标志」的目录将不再显示。这与方案目标一致，属预期取舍。
- **风险 2**：非实例目录中历史遗留的 `eclversion.json` 需用户手动删除；不清理不影响功能。