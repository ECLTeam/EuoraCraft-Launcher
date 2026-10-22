# 整合包功能补全实施方案

> 状态：待用户确认（AGENTS.md 工作流第 5 步）
> 日期：2026-09-26
> 来源：[docs/feature-comparison-and-optimization-plan.md](feature-comparison-and-optimization-plan.md) P0-3
> 用户已确认的决策：**方案 A（完整对标）**；导入/在线安装时**自动安装基础版本与加载器**；导出做 **Modrinth + CurseForge 双指纹反查**。

## 1. 背景与目标

当前整合包能力存在四个缺口（证据见第 2 节）：

1. **导入不下载文件**：`import_instance_pack` 只解压 `overrides/`，不下载 mrpack `files[]` 与 CurseForge `manifest.json` 声明的模组，导入结果为残缺实例。
2. **无在线安装**：不能在启动器内搜索并安装 Modrinth/CurseForge 整合包。
3. **基础版本需预装**：缺少基础版本直接报 `PACK_BASE_VERSION_MISSING`，不自动安装。
4. **导出不标准**：`files: []`、mods 全塞 overrides，其他启动器无法增量更新。

目标：导入"装完即玩"、在线搜索安装（Modrinth / CurseForge / FTB）、支持 MCBBS/HMCL/MultiMC 格式、导出为标准可增量更新的 mrpack。

## 2. 现状（代码证据）

| 现状 | 位置 |
| --- | --- |
| 导入仅识别 `modrinth.index.json` / `manifest.json` / `ecl-pack.json`，只复制 overrides，要求基础版本已安装 | `ECL/services/game/resources.py:1379-1429` |
| 导出仅 modrinth 格式，`files: []`，mods 全部打入 overrides | `ECL/services/game/resources.py:1324-1377`（ADR-017） |
| 在线搜索无 modpack 类型（仅 mod/resourcepack/shaderpack/datapack/world） | `ECL/services/game/resources.py:89-121`（`ResourceCatalogPolicy`） |
| 哈希反查已有 Modrinth 单哈希实现，CF 侧为空占位 | `ECL/services/game/resources.py:1192-1219`（`identify_resource_hash`） |
| 安装管线支持 vanilla/fabric/forge/neoforge/quilt，异步任务化 | `ECL/services/game/install.py:113-346`（`install_version`/`_run_install`） |
| 长任务为线程池同步 worker（OperationContext 进度/取消） | `ECL/services/game/operations.py:101-144` |
| CF 文件下载地址解析已有（含无 Key 报错、下载地址专用接口回退） | `ECL/services/game/resources.py:951-984`（`_fetch_curseforge_file`） |
| 前端导入对话框/拖放/状态机已有；在线搜索组件按 resourceType 参数化 | `frontend/src/components/instances/ModpackImportModal.vue`、`frontend/src/components/mods/OnlineModSearch.vue`、`frontend/src/features/instances/stores/modpackImportStore.ts` |
| 下载器为并发引擎，条目为 `(url, path)` 二元组，无逐条目校验 | `ECL/game/Core/Downloader.py:127-166` |
| 安全解压（路径穿越/符号链接）已有 | `ECL/services/game/workspace.py`（`safe_extract_zip`） |

## 3. 总体设计

### 3.1 模块划分

- **新文件 `ECL/services/game/modpack.py`**：`ModpackCoordinator`（GameService Mixin，与现有 13 个协调器同构）。
  - 承载：格式识别与解析、导入编排、在线安装、导出反查。
  - `import_instance_pack` / `export_instance_pack` 从 `resources.py` **迁入**本模块，公开签名与 IPC 命令名不变（`self.game.*` 聚合面不变，`api/workspace.py:213-227` 无需改动调用方式）。
  - 同时落实对比文档 4.2-7 对 resources.py（1429 行）的拆分方向。
- **`ECL/services/game/install.py` 重构**：抽出同步安装核心 `install_blocking(...)`（见 3.5）。
- `resources.py` 保留通用资源（mod/资源包/光影/数据包/原理图/存档）逻辑；`_fetch_curseforge_file`、`_download_online_file`、`_proxied_*` 等被 modpack 复用的工具函数下沉到共享位置（迁移到 modpack.py 或保留在 resources.py 由其引用，实施时按依赖方向取后者，避免循环导入）。

### 3.2 统一计划模型

```python
@dataclass(frozen=True, slots=True)
class PackFileEntry:
    target_relative: str          # 相对实例目录，统一 "/" 分隔
    url: str | None               # 直链；CF 文件经 download-url 解析后填入
    sha1: str | None
    sha512: str | None
    size: int | None
    env_client: str               # required / optional / unsupported
    project_id: str | None        # CF projectID / Modrinth project_id
    file_id: str | None

@dataclass(frozen=True, slots=True)
class ModpackPlan:
    format_name: str              # mrpack / curseforge / mcbbs / hmcl / multimc / ecl-legacy
    pack_name: str
    summary: str
    minecraft_version: str
    loader_type: str              # vanilla / fabric / forge / neoforge / quilt
    loader_version: str | None    # None 表示解析最新（复用现有 catalog/安装回退行为）
    files: tuple[PackFileEntry, ...]
    overrides_dir: Path | None    # 解压后 overrides 根；无 overrides 为 None
    needs_reinstall_loader: bool  # MultiMC：不复用其 patch，改为重装 loader
    warnings: tuple[str, ...]
```

所有解析器输出 `ModpackPlan`，导入编排只面向该模型，格式差异收敛在解析器内。

### 3.3 格式识别与解析器

识别顺序（对解压后的临时目录判断，zip 与目录统一处理）：

| 顺序 | 特征 | 格式 | 关键解析规则 |
| --- | --- | --- | --- |
| 1 | `modrinth.index.json` | Modrinth mrpack | `dependencies`：`minecraft` / `fabric-loader` / `forge` / `neoforge` / `quilt-loader`；`files[].{path,hashes.sha1,hashes.sha512,env.client,downloads[0],fileSize}` |
| 2 | `manifest.json` 且 `manifestType == "minecraftModpack"` | CurseForge | `minecraft.version` + `modLoaders[].id`（`forge-x` / `fabric-x` / `neoforge-x` / `quilt-x`，按首个 `-` 拆分）；`files[].{projectID,fileID,required}`，required=false 映射为 optional |
| 3 | `mcbbs.packmeta` | MCBBS | `name/version/author/description`；`addons` + `launchInfo` 判定加载器；`files[]`：CurseFile（`projectID/fileID/fileName/url`）与本地条目；字段以 HMCL `McbbsModpackManifest.java` 为准（GPL-3.0 可参考） |
| 4 | `modpack.json` | HMCL | 识别特征与 PCL-CE `ModModpack.cs:114` 一致；字段以 HMCL `modpack/Modpack.java` 为准 |
| 5 | 目录内 `mmc-pack.json`（或 zip 单层子目录内） | MultiMC | `components[]`：`net.minecraft`→MC 版本；`net.minecraftforge` / `net.fabricmc.fabric-loader` / `net.neoforged` / `org.quiltmc.quilt-loader`→加载器；`org.lwjgl3` 忽略。**决策：不移植 HMCL json-patch 合并引擎**，改为按解析出的版本+加载器走现有安装管线重装，`.minecraft/` 内容作为 overrides；差异记录进 `warnings` |
| 6 | `ecl-pack.json` | ECL 旧包 | 保留现有兼容分支（整体内容即实例） |
| — | 其他 | 报 `INVALID_PACK_ARCHIVE` | |

`env.client == "unsupported"` 的条目跳过并计入结果；`optional` 默认下载（请求体预留 `exclude_files` 字段，勾选 UI 列入后续迭代）。

### 3.4 导入编排（长任务 worker，进度为总任务百分比）

1. `safe_extract_zip` 解压到 `versions/` 同级临时目录（沿用现有模式，5%）。
2. 识别格式 → `ModpackPlan`（10%）。
3. **基础版本保障**：`versions/<mc>/<mc>.json` 缺失时调用 `install_blocking(vanilla)`；加载器非 vanilla 时继续 `install_blocking(loader)`（10-35%，`subtask="base_install"`；已存在则跳过）。加载器版本缺失时：fabric/quilt 取最新（`loader_versions`），forge/neoforge 由包声明提供，未提供时报 `PACK_LOADER_VERSION_REQUIRED`。
4. 生成下载清单：CF 条目先经 `_fetch_curseforge_file` 解析直链与哈希（无 Key 报 `CURSEFORGE_KEY_REQUIRED`）→ 交 `Downloader` 并发下载（35-80%，文件计数进度）。
5. 统一校验：SHA1/SHA512（Downloader 条目无校验字段，校验在下载后执行）；失败文件单独重试一次，仍失败抛 `PACK_FILE_HASH_MISMATCH` 并列出文件（80-85%）。
6. 复制 overrides 到 staging 版本目录，写 `{version_id}.json`（`inheritsFrom` 基础版本）（85-95%）。
7. staging 原子 `replace` 落位，输出结果摘要（格式、已装文件数、跳过数、warnings）（100%）。

取消：每阶段边界与下载循环内 `check_cancelled()`。失败回滚：全程在 staging 目录操作，落位前失败自动清理（沿用现有 `.{name}.ecl-import` 模式）。旧 `ecl-pack.json` 分支行为保持不变。

### 3.5 install.py 重构（自动安装的基础设施）

- 从 `_run_install` 抽出**同步核心** `install_blocking(task_id, version_id, save_name, loader, loader_version, game_path, source, java_path, progress, cancel_event)`：构建下载清单 → `Downloader.run()` → 失败补全（`_retry_install_downloads` 逻辑一并迁移）。
- 异步桥接：worker 线程内 `asyncio.run(downloader.run())`（工作线程无事件循环，安全；**约束**：`install_blocking` 仅供无事件循环的线程调用，docstring 注明）。
- `_run_install` 改为 `to_thread.run_sync(install_blocking, ...)` + 现有事件映射，**对外行为与事件负载不变**（`game:install_progress` 契约不动）。
- 进度上报改为注入的 `progress` 回调，任务化路径包装为现有 `_emit_install_progress`。

### 3.6 在线安装（Modrinth + CurseForge + FTB）

- `search_online_resources` 新增 `resource_type="modpack"`：Modrinth `project_type:modpack`；CurseForge `classId 4471`、`web path "modpacks"`。modpack 类型不做 mcmod 译名查询。
- `fetch_project_info` / `fetch_project_versions` 对 modpack 透传（现有双源实现通用，无需改动核心逻辑，仅放宽类型映射）。
- 新 IPC 命令 `game_modpack_online_install`：`{source, project_id, file_id, game_path, version_name}` → 用 `_download_online_file` 下载包文件到临时目录 → 走 3.4 导入编排（同一 operationId 内完成），返回 `{operationId, versionId}`。
- **FTB（最后实施）**：`https://api.feed-the-beast.com/modpacks/v1/modpack/search?term=`、`/modpack/all`、`/modpack/{id}`、`/modpack/{id}/{versionId}`。该 API **无官方文档**（社区逆向维护，端点历史上多次迁移），实现为独立 provider：搜索/详情/文件映射到统一卡片模型；任何 4xx/5xx 时返回明确错误并在前端隐藏 FTB 源入口（降级开关），不做为默认源。

### 3.7 导出反查（Modrinth + CurseForge 双指纹）

1. 收集 `mods/`（隔离感知）下的启用 jar，计算 sha1 + sha512。
2. **Modrinth 反查**：`POST /v2/version_files`（`hashes` 批量，algorithm `sha1`，每批 ≤100），命中取 `project_id/version_id/url/filename`。
3. **CurseForge 指纹**：实现 `curseforge_fingerprint(data: bytes) -> int`——MurmurHash2 32 位（seed=1，公共域算法，参考 HMCL `CurseForgeRemoteAddonRepository.java:201-227` 的归一化：过滤 `0x09/0x0A/0x0D/0x20`）→ `POST https://api.curseforge.com/v1/fingerprints/432`，响应 `data[].{id,fileId,latestFilesIndexes[]}`，按当前 `gameVersion + loader` 匹配 `latestFilesIndexes` 得到 fileId，再走 `_fetch_curseforge_file` 拿直链与哈希。
4. 命中的 mod 从 overrides 剔除，写入 `modrinth.index.json` 的 `files[]`（`path`、`hashes`、`env: {client:"required", server:"required"}`、`downloads`、`fileSize`）；未命中保留 overrides。Modrinth/CF 同时命中时**优先 Modrinth**。
5. CF Key 缺失或接口失败：跳过 CF 反查并写入 warnings，不阻塞导出。
6. 导出进度更新（反查阶段约 20%，打包 80%）；`pack_format` 仍仅允许 `modrinth`（ADR-017 不推翻）。

### 3.8 API 层

- `ECL/api/models.py`：新增 `ModpackOnlineInstallRequest`（source/project_id/file_id/game_path/version_name）；`InstancePackImportRequest` 预留 `exclude_files: list[str] | None`。
- `ECL/api/workspace.py`：新增 `game_modpack_online_install` handler（错误边界、契约同现有）。
- `ECL/api/registry.py`：`command_names` 增加 `game_modpack_online_install`。
- 既有 `game_instance_import` / `game_instance_export` 请求模型与响应契约不变。

### 3.9 前端

- `frontend/src/types`：`DownloadResourceType` 增加 `'modpack'`。
- `OnlineModSearch.vue`：modpack 变体——搜索/详情/版本列表复用现有 API；操作按钮改为「安装整合包」，点击后打开 `ModpackImportModal`（新增 online 预填模式：跳过文件选择，展示已选整合包与版本，保留安装目录/实例名）。
- `modpackImportStore.ts`：扩展 `online` 模式字段（source/projectId/fileId/packTitle），`importPack` 分叉调用 `game_modpack_online_install`；导入中改用 `game_operation_get`/`game:operation_progress` 展示分阶段进度（含基础安装/下载/校验子状态）。
- `Download.vue`：新增「整合包」Tab（`resourceType="modpack"`，固定 `fixedSource=undefined` 保持双源切换）。
- i18n：六个语言文件补齐 modpack 相关键。
- 拖放：`.mrpack/.zip` 拖入维持现有全局导入入口，无变化。

### 3.10 错误码

新增：`PACK_FILE_HASH_MISMATCH`、`PACK_LOADER_VERSION_REQUIRED`、`PACK_ONLINE_FILE_INVALID`。保留：`INVALID_PACK_ARCHIVE`、`PACK_BASE_VERSION_MISSING`（自动安装失败时兜底提示）、`CURSEFORGE_KEY_REQUIRED`。

## 4. 实施步骤（每步独立提交，遵循 Conventional Commits + 测试通过后提交）

| 步骤 | 内容 | 提交类型 |
| --- | --- | --- |
| 1 | install.py 抽 `install_blocking`，`_run_install` 改包装，行为不变 | refactor |
| 2 | `modpack.py`：`ModpackPlan` + mrpack/CurseForge/ECL 旧包三个解析器与格式识别 | feat |
| 3 | 导入编排：文件下载、env 过滤、哈希校验、进度/取消、失败回滚 | feat |
| 4 | 基础版本自动安装接入导入编排 | feat |
| 5 | MurmurHash2 指纹 + 导出反查（Modrinth 批量 + CF fingerprints） | feat |
| 6 | 在线搜索（modpack 类型）与 `game_modpack_online_install` 命令（Modrinth+CF） | feat |
| 7 | MCBBS / HMCL / MultiMC 解析器 | feat |
| 8 | FTB 在线源（非官方 API，失败降级隐藏入口） | feat |
| 9 | 前端：整合包 Tab、安装流、i18n | feat |
| 10 | 文档：junsi-dev-docs API 规范更新 + 新 ADR | docs |

## 5. 测试计划

- **后端新增 `tests/test_modpack.py`**（样例包在测试内用 `zipfile` 动态构造，不提交二进制）：
  - 五种格式 + ECL 旧包的识别与解析断言（含 `fabric-loader-x`/`forge-x` 前缀拆分、MCBBS CurseFile、MultiMC components）。
  - env 过滤：`unsupported` 跳过、`optional` 默认下载。
  - 哈希不匹配 → `PACK_FILE_HASH_MISMATCH` 且 staging 目录被清理；中断（取消）→ 无残留。
  - 自动安装联动：mock `install_blocking`，断言调用参数与先后顺序（vanilla → loader → 文件下载）。
  - 导出反查：mock Modrinth `version_files` 与 CF `fingerprints`，断言 `files[]` 内容与 overrides 剔除；CF Key 缺失降级。
  - `curseforge_fingerprint` 测试向量（实施时用真实 CF 文件计算一次固定为常量）。
  - 识别优先级（同时含 `modrinth.index.json` 与 `manifest.json` 时按顺序判定）。
- **回归**：现有 `tests/test_frontend_api_config.py`、实例导入导出相关用例全部保持通过。
- **命令**：`ruff check ECL tests`、`ruff format --check ECL tests`、`pytest`；前端 `pnpm check` 与 `pnpm build`。
- **实机验证**（`pnpm build` 后启动启动器）：① 在线安装一个真实 Modrinth mrpack（含 fabric 加载器）并启动进游戏；② 拖入一个 CurseForge zip 导入（需配置 Key）；③ 导出含在线来源 mod 的实例，用 mrpack 校验（`modrinth.index.json` 的 `files[]` 可被官方格式接受）；④ MultiMC 目录导入。

## 6. 风险与开放问题

| 风险 | 对策 |
| --- | --- |
| FTB API 无官方文档，端点可能变动 | 独立 provider + 失败降级隐藏入口；作为第 8 步最后实施 |
| CF 下载地址依赖 `CURSEFORGE_API_KEY` | 沿用现有 Key 机制与报错指引；无 Key 时 Modrinth/本地导入不受影响 |
| 大整合包（100+ 文件）下载压力 | 复用 `Downloader` 现有并发/限速控制，无新增并发路径 |
| MultiMC 特殊 patch 包与重装结果不一致 | `warnings` 明确提示；不追求 100% patch 等价 |
| MCBBS `fileApi` 远端多数失效 | 直链/`url` 字段优先，失败给可操作错误提示 |
| mrpack `optional` 文件默认下载 | 预留 `exclude_files` 请求字段，勾选 UI 列入后续迭代 |
