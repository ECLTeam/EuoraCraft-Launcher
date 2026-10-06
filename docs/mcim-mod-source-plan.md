# MCIM 模组源接入实施方案

状态：已完成实施并通过本地验收。结果见第 7 节。

## 1. 目标与决策依据

在「设置 → 下载设置」中，于现有「下载源」下方新增独立的「模组源」设置项，可选「官方」与「MCIM」。
模组源决定 Mod / 资源包 / 数据包 / 整合包相关请求（搜索、详情、版本、依赖、文件下载、图标头像）走官方平台域名还是 MCIM 镜像域名。

用户已确认的决策：

1. **双向回退**：选「官方」时官方失败回退 MCIM；选「MCIM」时 MCIM 失败回退官方。
2. **覆盖范围**：能覆盖的全部覆盖（搜索、详情、版本、依赖、文件下载、图标头像、整合包元数据与文件）。
3. **CurseForge 免 Key**：模组源为 MCIM 时，未配置 CurseForge API Key 也能正常搜索与下载 CurseForge 资源。
4. **独立设置**：游戏本体的「下载源」与新增的「模组源」互不影响；现有「下载源」文案改为「游戏下载源」。

5. **Fabric API 归属模组源**：Fabric API 版本查询与下载地址解析跟随模组源，不跟随游戏下载源。该逻辑原位于 `catalog.py`，合并远端重构后归属 `scan.py`。
6. **UA 已登记**：启动器名与 User-Agent `EuoraCraft-Launcher/{version}` 已在 MCIM 仓库完成登记。

## 2. 调研结论

### 2.1 MCIM 接口事实（已实测验证）

Base URL：`https://mod.mcimirror.top`

| 官方域名 | MCIM 替换 | 说明 |
| --- | --- | --- |
| `api.modrinth.com/v2/...` | `mod.mcimirror.top/modrinth/v2/...` | 路径与响应结构一致 |
| `cdn.modrinth.com/...` | `mod.mcimirror.top/...` | 302 重定向到文件镜像 |
| `api.curseforge.com/v1/...` | `mod.mcimirror.top/curseforge/v1/...` | **无需 API Key** |
| `edge.forgecdn.net/files/<a>/<b>/<f>` | `mod.mcimirror.top/files/<a>/<b>/<f>` | 302 重定向 |
| `mediafilez.forgecdn.net` | **禁止替换** | 官方文档明确警告 |

实测结果：

- `mod.mcimirror.top/modrinth/v2/project/sodium` → 200
- `mod.mcimirror.top/modrinth/v2/search?query=sodium` → 200
- `mod.mcimirror.top/curseforge/v1/mods/238222` → 200（无 Key）
- `mod.mcimirror.top/curseforge/v1/mods/search?gameId=432` → 200（无 Key）
- `mod.mcimirror.top/files/8937/628/<file>.jar` → 200，2273731 字节
- `mod.mcimirror.top/data/<pid>/versions/<vid>/<file>.jar` → 200，968419 字节

**关键发现**：MCIM 的 API 响应中**仍然返回官方 CDN 地址**（Modrinth 的 `files[].url` 指向 `cdn.modrinth.com`，CurseForge 的 `downloadUrl` 指向 `edge.forgecdn.net`）。
因此仅替换 API 域名不足以保证文件下载走镜像，必须对文件 URL 单独做域名重写。

### 2.2 官方文档的接入要求

- 接入前到 MCIM 仓库登记启动器名与 User-Agent；UA 格式为 `名称/版本号`，不得使用空 UA 或 HTTP 库默认 UA。
- 禁止在镜像上做二次封装；合理设置并发与重试。
- 缓存响应带 `sync_at` / `checked_at`；未命中返回 404 并进入补抓队列，稍后重试可命中。
- 文件下载稳定性建议：**官方源下载失败时再尝试镜像**，而不是默认只走镜像。

### 2.3 现状（受影响代码）

现有 `download.mirror_source`（`official` / `bmclapi`）**只作用于游戏本体**：
`GameService._normalize_source` → `_api_config` → `BmclApiUrl` / `ApiUrlConfig` → `PreferredApiClient` 主备回退。
它完全不参与 Mod 侧逻辑。

Mod 侧域名目前硬编码，共约 15 处：

| 文件 | 处数 | 内容 |
| --- | --- | --- |
| `ECL/services/game/resources.py` | 11 | Modrinth 搜索/详情/版本/版本文件批量、CurseForge 搜索/详情/文件/下载地址/指纹、FTB |
| `ECL/services/game/modpack.py` | 2 | Modrinth `version_files` 批量反查、CurseForge `fingerprints/432` |
| `ECL/services/game/catalog.py` | 1 | Fabric API 版本查询 |
| `ECL/services/game/install.py` | 1 | Fabric API 版本与下载地址解析 |

统一出口已存在，便于集中改造：

- `_proxied_get` / `_proxied_post` / `_proxied_stream`（`resources.py`）已统一附带下载代理。
- `curseforge_available()` / `_curseforge_headers()` 统一决定 CurseForge 是否可用。

影响面：

- 前端 `frontend/src/views/settings/LauncherTab.vue`（下拉项与事件）、`frontend/src/types/config.ts`、`frontend/src/features/settings/stores/settingsStore.ts`、6 个语言包。
- 后端配置 `ECL/utils/config.py`、`ECL/utils/download_settings.py`、`ECL/api/settings.py`（经 `patch_download`）。

## 3. 方案设计

### 3.1 配置模型

在 `download` 配置分区新增 `mod_source`：

```json
"download": {
  "mirror_source": "official",
  "mod_source": "official"
}
```

- 取值：`official` | `mcim`
- 默认：`official`（保持现有用户行为不变）
- 与 `mirror_source`（游戏本体源）完全独立。

### 3.2 新增模组源策略模块

新建 `ECL/services/game/mod_sources.py`，作为模组域名的唯一裁决点：

```python
class ModSourcePolicy:
    sources = ("official", "mcim")
    # 可替换的官方域名 -> 官方/MCIM 基址
    # mediafilez.forgecdn.net 不在表内，天然不被替换
```

公开接口：

- `normalize_mod_source(value) -> str` — 校验 `official`/`mcim`，非法值回退 `official`。
- `alternate_mod_source(source) -> str` — 返回另一源，用于双向回退。
- `api_base_url(source, platform) -> str` — 返回 modrinth / curseforge 的 API 基址。
- `rewrite_file_url(url, source) -> str` — 对文件 CDN 地址做域名重写；`mediafilez` 与其他未知域名原样返回。

域名映射：

| 平台 | 官方 | MCIM |
| --- | --- | --- |
| Modrinth API | `https://api.modrinth.com/v2` | `https://mod.mcimirror.top/modrinth/v2` |
| Modrinth CDN | `https://cdn.modrinth.com` | `https://mod.mcimirror.top` |
| CurseForge API | `https://api.curseforge.com/v1` | `https://mod.mcimirror.top/curseforge/v1` |
| CurseForge CDN | `https://edge.forgecdn.net` | `https://mod.mcimirror.top` |

文件 URL 重写规则：
- `cdn.modrinth.com/<path>` → `mod.mcimirror.top/<path>`
- `edge.forgecdn.net/files/<a>/<b>/<f>` → `mod.mcimirror.top/files/<a>/<b>/<f>`
- 其他（含 `mediafilez.forgecdn.net`、`www.curseforge.com`、`modrinth.com`）保持原样。

### 3.3 双向回退执行器

仿照现有 `PreferredApiClient`，新建 `ModSourceRequestPolicy`（同模块），对「平台 + 操作」做统一的两段式执行：

- 先按首选源构造 URL 请求；
- 命中可回退异常（`httpx.HTTPError`、`json.JSONDecodeError`、`UnicodeError`、`KeyError`、`TypeError`、404 未命中）时，换另一源重试一次；
- 保留异常链，日志记录「X 从 源A 获取失败，尝试 源B」。

回退只发生在网络/远端响应失败，不重复本地安装步骤。

### 3.4 CurseForge 免 Key 规则

`curseforge_available()` 与 `_curseforge_headers()` 变为源感知：

- 模组源 = `mcim` → 始终可用，不发送 `x-api-key`。
- 模组源 = `official` → 维持现有行为（必须有 Key）。

注意 MCIM 的 CurseForge 端点不读 Key，但仍会返回官方 `downloadUrl`，下载阶段由 `rewrite_file_url` 改写到镜像。

### 3.5 改造点清单

| 文件 | 改动 |
| --- | --- |
| `ECL/services/game/mod_sources.py` | 新建：域名映射、URL 重写、双向回退执行器 |
| `ECL/services/game/resources.py` | 11 处硬编码域名改为经策略构造；`_proxied_*` 增加源感知；CF 免 Key；文件 URL 重写 |
| `ECL/services/game/modpack.py` | 2 处（Modrinth 批量反查、CF 指纹）改为经策略构造；CF 免 Key |
| `ECL/services/game/catalog.py` | Fabric API 版本查询改为经策略构造 |
| `ECL/services/game/install.py` | Fabric API 版本与下载地址解析改为经策略构造 |
| `ECL/services/game/base.py` | 提供 `mod_source` 读取入口与缓存键（若需要按源缓存） |
| `ECL/utils/config.py` | 默认配置新增 `"mod_source": "official"` |
| `ECL/utils/download_settings.py` | `DownloadSettingsPatch` 新增 `mod_source` 校验 |
| `frontend/src/types/config.ts` | `DownloadConfig` 增加 `mod_source: 'official' \| 'mcim'` |
| `frontend/src/features/settings/stores/settingsStore.ts` | 默认值增加 `mod_source: 'official'` |
| `frontend/src/views/settings/LauncherTab.vue` | 新增「模组源」下拉；「下载源」改名「游戏下载源」 |
| `frontend/src/i18n/locales/*.json` | 6 个语言包新增/调整文案 |

### 3.6 文案

- 「下载源」→「游戏下载源」（zh-CN），其余语言同步为对应语义。
- 新增「模组源」，选项「官方」与「MCIM」。
- 「模组源」描述需说明：MCIM 为国内镜像，选择后 CurseForge 无需 API Key。

### 3.7 MCIM 接入合规

- 请求统一使用 UA `EuoraCraft-Launcher/<版本号>`，复用现有 `launcher_version`。
- 保持现有请求并发与重试上限，不对镜像加压。
- 不做二次封装（不代理转发镜像内容，仅客户端直连）。

## 4. 实施步骤

1. 后端：新建 `mod_sources.py`，实现策略与回退执行器。
2. 后端：改造 `resources.py`、`modpack.py`、`catalog.py`、`install.py` 的域名构造与文件 URL 重写。
3. 后端：改造 CurseForge 免 Key 判定。
4. 后端：`config.py` 与 `download_settings.py` 支持 `mod_source`。
5. 补充/更新 `tests/` 用例（见第 5 节）。
6. 前端：类型、store、设置界面与 6 个语言包。
7. 执行测试并提交。

## 5. 测试计划

后端（`tests/`）：

- 新增 `tests/test_mod_sources.py`：
  - 域名映射正确（Modrinth / CurseForge API 与 CDN）。
  - `mediafilez.forgecdn.net` **不被重写**。
  - 未知域名原样返回。
  - 双向回退：首选源失败时切换到另一源；两源均失败时保留异常链。
  - 非法 `mod_source` 回退为 `official`。
- 更新相关测试：
  - `tests/test_online_resource_search.py`：MCIM 源下 CurseForge 无需 Key。
  - `tests/test_download_settings.py`：`mod_source` 校验与 null 拒绝。
  - `tests/test_config_defaults.py`：默认值含 `mod_source`。
  - `tests/test_modpack.py` / 资源相关用例：域名来源可注入。

前端：

- `LauncherTab.test.ts`：模组源下拉渲染与切换调用 `patchDownload`。
- `settingsStore.test.ts`：默认值与更新。
- `i18n` 一致性测试通过。

验收命令（AGENTS.md 第 2 节）：

```powershell
# 后端
.\.venv\Scripts\python.exe -m ruff check ECL tests
.\.venv\Scripts\python.exe -m ruff format --check ECL tests
$env:PYTHONUTF8="1"; .\.venv\Scripts\python.exe -m pytest -q
# 前端
cd frontend; pnpm check; pnpm build
```

本机注意：`pytest` 需 `PYTHONUTF8=1`（否则 `test_instance_shortcuts` 因 GBK 解码失败）；`pnpm test` 全量并发下 `cssBuild.test.ts` 与 `InstanceServersTab.test.ts` 存在性能型超时，单独运行可通过。

前端 UI 改动后按规范实际启动启动器验证，届时使用 Vite 开发服务器 `http://localhost:5173` 调试。

## 6. 影响范围与风险

- 默认值 `official` 保证现有用户行为完全不变。
- MCIM 为第三方公益镜像，存在缓存延迟与临时不可用；双向回退可降低影响。
- CurseForge 免 Key 仅作用于 MCIM 源，官方源 Key 门禁不变。
- 文件 URL 重写仅精确匹配已知镜像域名，避免误伤 `mediafilez` 等不可替换域名。
- 建议后续在文档站补充模组源说明与 Xray 政策提示。

## 7. 实施结果

已完成全部改动并通过本地验收。

后端：

- 新建 `ECL/services/game/mod_sources.py`：`ModSourcePolicy`（域名映射与文件地址重写）、`ModSourceRequestPolicy`（双向回退）、`ModSourceAware`（共享混入）。
- 改造 `resources.py`（搜索/详情/版本/文件地址/图标/身份反查/更新检查）、`modpack.py`（Modrinth 批量反查与 CurseForge 指纹）、`scan.py` 与 `install.py`（Fabric API，改为实例方法以读取模组源）。
- `curseforge_available()` 与 `_curseforge_headers()` 源感知：MCIM 下无需 Key 且不发送 `x-api-key`。
- `config.py` 默认新增 `download.mod_source`；`DownloadSettingsPatch` 校验 `official`/`mcim`。
- `application.py` 注入 `mod_source_provider`。

前端：

- `types/config.ts` 与 `settingsStore.ts` 增加 `mod_source`。
- `LauncherTab.vue`：「下载源」改名「游戏下载源」，新增「模组源」下拉（官方 / MCIM）。
- 6 个语言包新增 `gameDownloadSource`、`modSource`、`modSourceDesc`、`modSourceOfficial`、`modSourceMcim`。

验收结果：

- `ruff check ECL tests`：通过；`ruff format --check`：224 files already formatted。
- `pytest -q`：1276 passed, 7 skipped。
- `pnpm format:check` / `pnpm lint`（0 error）/ `pnpm typecheck`：通过。
- `pnpm test`：760 passed；仅 `cssBuild.test.ts` 与 `InstanceServersTab.test.ts` 两例为本机全量并发下的既有性能型超时，单独运行均通过。
- `pnpm build`：成功。
- 真实 MCIM 接口验证：模组源为 `mcim` 时，Modrinth 与 CurseForge 搜索各返回 20 条命中，且 CurseForge 未配置 API Key 仍可用；文件地址正确重写为 `mod.mcimirror.top`。

已知限制：

- MCIM 返回缓存数据（实测同一项目版本号落后于官方），这是镜像的预期行为，也是默认「官方优先」的原因。
- `mediafilez.forgecdn.net` 按 MCIM 要求不重写，映射表未收录该主机。
- FTB 社区源不属于 Modrinth/CurseForge，不受模组源影响。

## 8. 合并远端重构

推送前发现远端 `main` 有 9 个后端分层重构提交，与本次改动大量重叠，已合并处理：

- `catalog.py` 被远端删除并并入 `scan.py`，Fabric API 的模组源改造同步迁移至 `scan.py`。
- `ECL/common/`、`ECL/events/` 等下沉为 `ECL/foundation/`，`mod_sources.py` 的版本导入改为 `ECL.foundation.version`。
- `instance_health`、`mod_metadata`、`resource_files` 等下沉至 Core 子模块，`resources.py` 导入按新结构收敛。
- `ECL/game` 子模块指针随远端更新至 `349af88`。
- 合并后重新通过 `ruff check` / `ruff format --check` / `pytest -q`（1276 passed），以及远端新增的 Core 子模块门禁（`ruff check` 与 8 passed）。
- 推送顺序：先推 `frontend`（`beec15d`），核对全部子模块指针均已在远端分支后，再推主仓库（`20ad2f7`）。
- 已用全新浅克隆加 `git submodule update --init --recursive --depth=1` 验证远端可实际检出全部子模块。
