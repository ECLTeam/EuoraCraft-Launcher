# EuoraCraft Launcher 功能补齐实施方案

> 对标 HMCL / Qomicex.Tauri / PCL-CE 的功能差距，本方案只覆盖报告中五项大功能。
> 所有改动点均为对既有代码库的**核实后结论**，落点以实际桩为准。方案遵循 AGENTS.md：后端以 `ruff check ECL` 与 `pytest` 为准，前端以 `pnpm build` + 实际启动验证为准，每项完成后立即提交独立 commit（feat/fix）。

## 总览

| 功能 | 现状核实结论 | 改动性质 | 复杂度 | 关键文件 | 建议排期 |
| --- | --- | --- | --- | --- | --- |
| 内存与进程优化 | 已有内存设置，但自动内存是固定值、无内存锁定、无进程优先级 | **增强现有** | 中 | launch.py / 游戏启动参数构建 / instanceSettings.ts | P0 |
| 多渠道下载源切换 | 后端已支持 official/bmclapi，前端未暴露、缺失败回退 | **前端暴露 + 增强** | 小 | base.py / install.py / 设置页 | P1 |
| 启动器自动更新 | 仅检测新版本并跳转下载，无下载替换 | **新增** | 高 | updates.py / 关于页 / maintenance | P1 |
| 可视化存档与设置编辑器 | 仅有 3 个字段 patch，options 直接改文本 | **扩展** | 中高 | worlds.py / nbt.py / 世界详情页 | P2 |
| 原理图 3D 预览 | 仅能管理 schematic 资源，无可视化 | **新增** | 高 | resources.py / 资源详情页 | P3 |

排期思路：先把改动面小、收益快的「内存优化」「下载源」落地；自动更新不改用户使用习惯、收益直接，紧随其后；可视化编辑器与 3D 预览体量较大、可独立迭代，放在后两期。

---

## P0：启动内存与进程优化（增强现有）

### 现状（已核实）
- 前端 `config/game.ts` 定义 `MEMORY_MIN=1024`、`MEMORY_MAX_RATIO=0.8`、`AUTO_MEMORY_DEFAULT=4096`；`instanceSettings.ts` 已有 `customMemory`、`memory`（默认 4096）字段。
- 后端 `launch.py#launch_instance` 接受 `memory`（MiB），经 `_normalize_positive_int` 归一化后写入 `LaunchConfig(use_ram=ram)`，最终参与 JVM 参数生成（只设上限堆，未见 `-Xms` 对齐）。
- 已有 Java 自动扫描（`JavaScanner.py`）与 32 位 Java 内存上限校验（`launch.py`，`JAVA_ARCH_MEMORY_LIMIT`：x86 下 >1536 MiB 报错）。
- 进程创建在 `services/processes.py#spawn` → `create_instance`（`new_session=True`），**当前未设置进程优先级**。
- 所谓 "自动内存" 实为固定值 4096，并非按系统内存动态建议。

### 目标
1. 按系统可用内存动态给出"推荐内存"，一键应用。
2. 支持内存锁定（`-Xms` 与 `-Xmx` 对齐），减少 GC 抖动。
3. 支持设置游戏进程优先级（保守档位），并受现有 32 位内存校验保护。

### 后端改动
- **动态建议内存**：新增系统内存读取（后端，避免依赖前端 `navigator`），提供 `memory/recommend` 接口或由后端在启动前计算建议值。利用已有 `MEMORY_MAX_RATIO=0.8` 与 `SAFE_MEMORY_MIN`，建议值 = `min(物理内存×0.8, 上限)` 并按 `MEMORY_STEP=256` 取整。
- **内存锁定**：在游戏启动参数构建处（`game` 核心的 JVM 参数生成）读取 `lockMemory` 配置；开启时把 `-Xms` 设为与 `-Xmx` 相同。`LaunchConfig` 增加对应字段透传。
- **进程优先级**：在 `processes.py#spawn` / `create_instance` 的进程创建调用处传入优先级别——Windows 用创建子进程的 priority class，POSIX 用 `os.setpriority`（经 `preexec_fn`）。
- 新增设置项：`game.lockMemory`（bool）、`game.priority`（枚举），并入现有配置持久化；向后兼容（缺省即现行为）。

### 前端改动
- `config/game.ts`：新增 save 常量；把 `AUTO_MEMORY_DEFAULT` 改为保留默认参考、让推荐值来自后端。
- `instanceSettings.ts`：新增 `lockMemory` 字段。
- 设置-游戏页：内存滑块旁加"推荐"按钮 + "启动前锁定内存（-Xms=-Xmx）"开关 + "进程优先级"下拉（正常/低于正常，避免过高档位干扰系统）。
- 实例/版本设置页同步新增锁定开关。

### 测试
- 后端：断言启动命令含对齐的 `-Xms`；建议内存计算（边界：内存过小、非 64 位）。补充 `tests/` 用例。
- 前端：设置字段 round-trip、推荐按钮刷新。
- 验证：`ruff check ECL` → 相关 `pytest` → `pnpm build` → 实际启动确认命令正确。

### 风险与回滚
- 优先级档位过高可能拖慢系统 → 仅提供保守档位。
- 内存锁定在少内存机器上可能启动失败 → 建议值受上限约束，且仅对 64 位生效。
- 新增配置可手动删除回退到现行为。

---

## P1：多渠道下载源切换（前端暴露 + 增强）

### 现状（已核实）
- 后端 `base.py`：`_normalize_source` 已接受 `official` / `bmclapi` 并校验；`_api_config(source)` 返回 `BmclApiUrl()` 或 `ApiUrlConfig()`；`_context(path, source)` 把选定源传入游戏核心；`install.py`、`catalog.py` 也按 source 生成版本/库清单 URL。
- `launch.py#launch_instance.source`（默认 "official"）已打通下载源到启动补全文件。
- **缺口**：前端没有暴露源选择 UI；失败时仅 `get_with_retries` 重试，**没有换源回退**。

### 目标
1. 前端提供"下载源"全局设置（官方 / BMCLAPI），并把选择持久化到后端，作用于版本安装、库下载与启动补全。
2. 单资源在首选源失败时自动回退到备选源。

### 后端改动
- 全局默认源：新增配置项 `launcher.downloadSource`（默认保留现状），在 `_context` / install 入口读取并作为缺省 `source`。
- 失败回退：在下发下载清单前，把"换源"纳入重试策略——首选源失败（状态码/网络错误）时基于 `BmclApiUrl` / `ApiUrlConfig` 的同路径差异重建 URL 重试；**避免无限循环**，限换源一次。保留 `utils/network.py` 的既有重试语义。
- 新增/调整接口：`api` 层暴露读取与写入源配置。

### 前端改动
- 设置页（游戏/下载区）新增"下载源"下拉：官方源 / BMCLAPI。
- 写入后端配置并在安装/启动时将 `source` 一并传入（现有参数通道已就绪，补上取值即可）。

### 测试
- 后端：源选择持久化；选择 bmclapi 后生成的清单/库 URL 指向镜像；首选失败时换源重试且只尝试一次。
- 验证：`ruff`、相关 `pytest`、`pnpm build`。

### 风险
- BMCLAPI 覆盖度与速率受限 → 保留"回退官方源"路径。
- 回退逻辑需幂等，避免同一次启动里源震荡。

---

## P1：启动器自动更新（新增）

### 现状（已核实）
- `services/updates.py`：`UpdateChecker#check` 按通道（alpha 禁用 / beta / release）查询 GitHub Releases，返回 `UpdateCheckResult`（含 `latest_url` 与 `latest_notes`）；`parse_version` / `compare_versions` 已可比较 SemVer（含预发布）。
- **无任何"下载新包并替换可执行文件"的实现**；现有更新链路止步于"提示有新版本 → 跳转下载页"。

### 目标
1. 检测到新版本后，用户可一键"下载并更新"，完成后重启启动器即完成升级。
2. 下载、校验、替换过程可见进度、可取消、可安全回滚。

### 后端改动
- 扩展 `services/updates.py`（或新增封装的 `UpdateApplier`）：
  - 从当前最新 release 的 `assets` 中按平台匹配安装包（Windows 主 exe / zip）。
  - 下载到临时目录，校验（可选 SHA-256 / 签名）与完整性后再进入替换。
  - **自替换策略**：运行中的 exe 无法直接覆盖 → 采用"先写临时新包 + 落一个 pending 标记，退出/下次启动时由 `apply_pending` 完成替换并重启"的延迟替换方案；或拉起一个独立短命进程执行替换后重启，二选一并在计划定稿时定版。
  - 复用现有 `game:install_progress` 事件机制上报下载进度/取消。
- 通道约束：仅 beta/rc/release 开放自更新；alpha 与 dev（源码运行）保持现状不提示新按钮，避免污染调试态。
- 保留旧版本备份以支持失败回滚。

### 前端改动
- 设置-关于页的"检查更新"全屏弹窗：在"有新版本"态新增"下载并重启"操作；展示下载进度（复用现有进度组件）。
- 更新完成/失败的结果提示与入口。

### 测试
- 后端：asset 匹配（分平台）、校验失败拒绝替换、pending 状态落盘与 `apply_pending` 幂等、失败后回滚保留旧版。
- 验证：`ruff`、`pytest`、`pnpm build` + 实际启动；发布链路在 CI 产物齐全时联调。

### 风险
- exe 占用/权限导致替换失败 → 备份 + 延迟替换保证可恢复。
- 更新包网络中断 → 断点/重试 + 取消。
- 自替换是发布链路的高风险改动，需在 beta 通道灰度验证后再放开 release。

---

## P2：可视化存档与设置编辑器（扩展）

### 现状（已核实）
- `utils/nbt.py` 已具备 NBT 读写能力。
- `services/game/worlds.py`：`patch_world` 目前只支持修改**难度 / 作弊 / 锁定**三个字段；世界详情已能展示基本信息和图标等。
- 资源层面 `toggle_resource` 已能直接改写 `options.txt`（如全屏开关写法见 `launch.py#_apply_fullscreen_option`），但无结构化编辑器。

### 目标
1. 可视编辑 `level.dat` 的常用字段：游戏模式、难度、天气、出生点、种子等，开写前自动备份，失败可回滚。
2. 可视编辑 `options.txt`（结构化为键值表单），覆盖常用游戏设置。

### 后端改动
- 扩展 world API：
  - `world/readSettings`：读取并结构化 `level.dat` 字段。
  - `world/patchSettings`：支持多字段写入；写前基于既有备份机制自动备份，校验 NBT 类型后再落盘。
  - `settings/readOptions` / `settings/patchOptions`：对 `options.txt` 结构化读写（键值/多语言数组类型安全）。
- 蓝图复用 `nbt.py` 与 `toggle_resource` 的既有写入路径，避免重复实现。

### 前端改动
- 世界详情页新增"编辑"面板：游戏模式/难度/天气/出生点等可视化控件。
- 实例资源/世界 tab 增加 `options.txt` 结构化编辑入口。

### 测试
- 后端：NBT 读-改-写往返；非法类型/越界输入被拒；写前备份与回滚断言。补充 `tests/` 用例。
- 验证：`ruff`、`pytest`、`pnpm build` + 实际启动验证改动生效且不损坏存档。

### 风险
- 版本间字段差异 → 对未知字段保守只读不写。
- 写入损坏 → 始终保留自动备份，写入失败提示并支持从备份还原。

---

## P3：原理图 3D 预览（新增）

### 现状（已核实）
- `services/game/resources.py` 已能扫描、分类、拷贝与管理 schematic（`ResourceCoordinator` 统一资源模型）；前端 `InstanceResourcesTab.vue` 展示资源列表。
- **无任何 3D 可视化**：schematic 只做管理，不能查看内容。

### 目标
1. 实例资源列表中查看 `.litematic` / `.schem` 的 3D 预览：缩放、旋转、Y 层切片、材质高亮。

### 后端改动
- 新增内容解析（可放 `services/game/` 新模块或扩展 `resources.py`）：解析 `.litematic`（复合 NBT）/ `.schem`（SNBT）为体素模型——尺寸、块列表、材质→调色板映射。复用 `utils/nbt.py`。
- 提供 `schematic/preview`：返回体素点集 + 调色板。大文件需限制体量或做降低采样/分块，避免单次 IPC 载荷过大。

### 前端改动
- 新增 Three.js 渲染组件（前端新增依赖），支持旋转/缩放、Y 层切片、材质高亮；接入资源详情视图。

### 测试
- 后端：解析用例（litematic/schem 样例）、降采样边界、大文件失败降级。补充 `tests/` 用例。
- 前端：渲染组件挂载与空态（无可渲染数据时显示引导）。
- 验证：`ruff`、`pytest`、`pnpm build` + 实际启动加载真实原理图。

### 风险
- 大模型内存/载荷 → 降采样与上限保护。
- 新增 Three.js 依赖影响打包体积 → 按需动态引入。

---

## 交付基线（每项功能提交前必须满足）

- 后端：`ruff check ECL` 无新增告警；相关 `pytest` 通过；为新增/修复行为补充或更新 `tests/` 用例。
- 前端：先 `cd frontend && pnpm build`，再实际启动启动器验证功能可用，不得只做静态检查。
- 提交：按功能分块提交独立 commit（feat/fix），不夹带无关文件；检查 `git status` 避免带入子模块指针或构建产物。
- 计划内的 alpha 通道与 dev 源码运行保持调试态不变。

## 排期路线图

- **M1（P0）** 内存与进程优化
- **M2（P1）** 下载源前端暴露 + 自动更新（各自独立 commit）
- **M3（P2）** 可视化存档与设置编辑器
- **M4（P3）** 原理图 3D 预览（可与非阻断的中型功能并行）