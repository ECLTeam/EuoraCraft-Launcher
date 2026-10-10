# 公告服务合并与后端信息卡冗余清理实施方案

状态：用户已确认开始实施；公告服务迁移、接口同步、自动检查与 Windows 桌面验收已完成。

## 目标与方案

删除后端重复的小贴士、欢迎语和信息卡展示配置，将仍有实际用途的公告获取、校验与缓存逻辑合并到现有 `ECL/services/updates.py`，删除 `ECL/host/info_card.py`。

已比较的方案：

1. 合并到 `services/updates.py`，以独立公告服务类保留职责边界，减少一个生产模块；用户已选择。
2. 改为独立 `announcements.py`，职责集中但生产模块数量不变；不采用。

前端负责本地文案、多语言、欢迎状态和轮播；后端仅提供公告列表。同步调整正式接口，不添加旧模块、旧类或旧命令的兼容包装。

## 当前实现与依赖

- `InfoCardManager.default_info_card` 包含 `tip_title`、`announcement_title`、`tips`、`welcome`、`mode`、`interval`。前端 `useGameInfoCard` 实际只使用后端公告，再通过 `createLocalizedInfoCard` 生成当前语言的信息卡，因此这些后端字段为重复数据。
- 公告获取包含共享 HTTP 客户端、超时与重试、JSON 版本校验、条目校验、启用和时间范围过滤、优先级排序、翻译保留、300 秒内存复用、磁盘缓存和离线回退。
- `ECL/application.py` 创建并保存 `info_card` 服务，`ECL/api/bridge.py` 向处理器传递该对象，`ECL/api/system.py` 的 `info_card_get` 在线程边界调用服务。
- 主仓库模块导出、服务装配测试、IPC 测试及测试替身引用旧服务；前端请求类型、命令表、查询缓存、store、展示模式 transport/fixture 引用旧信息卡接口。
- `services/updates.py` 已承载版本更新比较、Release 查询及后台启动检测，适合增加同属启动器发布信息的公告服务。公告与版本检测分别使用独立类和状态，不共用刷新计时器或缓存。

## 具体改动

### 后端公告服务

1. 在 `ECL/services/updates.py` 增加 `AnnouncementService`，将现有公告相关实现移入其中，并更新模块职责、公开接口清单与导出。
2. 服务公开方法使用 `get_announcements()`，返回结构明确的公告列表。使用类型明确的公告/翻译结构表达稳定字段，远程 JSON 的不可信类型限制在解析边界内。
3. 删除 `default_info_card`、`get_info_card()` 及仅用于组装展示数据的逻辑；公告列表读取直接承担锁保护、刷新和独立返回副本的职责。
4. 保持 `https://api.euora.cn/raw/ECLTeam/EuoraCraft-Launcher.API/main/notice.json`、`notice.json` 缓存路径、JSON 版本和有效期含义。JSON `null` 仍表示无结束时间，字符串 `"null"` 和畸形日期仍过滤。
5. 公告使用应用已有的共享 HTTP 客户端和网络配置；保留用于可控测试的加载器与时钟注入，不为不存在的旧调用方增加兼容构造或转发入口。
6. 将 `ApplicationContext.info_card` 和 API 装配字段改为 `announcements`，更新导入、初始化和相关测试替身。共享客户端仍由应用统一关闭。
7. 删除 `ECL/host/info_card.py` 以及 `ECL/host/__init__.py` 中旧类导出。

### 正式 IPC 与日志

1. 删除 `info_card_get`，替换为 `launcher_announcements_get`。
2. 请求不携带参数，成功响应的 `data` 直接为公告数组；服务不拼装 IPC 协议，由 `SystemHandlers` 调用线程边界并返回标准成功响应。
3. 更新命令注册、模型/契约覆盖及 `IpcLogPolicy`，中文操作名为“获取启动器公告”，按查询通道记录。
4. 验证旧命令不再注册，新命令可用，接口不再返回后端小贴士、欢迎语或轮播配置。

### 前端消费与展示模式

1. 将 `gameHomeApi.getInfoCard()` 改为 `getAnnouncements()`，调用新命令并返回 `InfoCardAnnouncement[]`。
2. 更新请求/响应类型、命令名称表和查询键，将远程 store 状态收窄为公告列表；保持首页加载和设置加载的现有错误行为。
3. `useGameInfoCard` 从公告列表调用 `createLocalizedInfoCard`，保留本地语言文案、公告翻译回退、欢迎状态、轮播和暂停交互。
4. 删除只用于旧远程完整卡片对象的 `EMPTY_INFO_CARD`、`normalizeInfoCard` 及相应用例；前端展示用 `InfoCardData` 继续用于卡片组件，不将它当作远程响应类型。
5. Showcase transport 改用新命令，fixture 仅提供公告列表，删除其未被页面采用的重复提示和欢迎语。
6. 同步所有实际引用、测试和说明，不保留旧接口别名。

## 实施步骤

1. 检查当前工作区与原始本地 main，创建本次 `codex/` 分支，保护无关改动。
2. 迁移公告服务及领域类型，调整应用装配，删除旧生产模块和导出。
3. 替换正式 IPC 命令及日志声明，迁移测试替身和断言。
4. 同步前端类型、API、查询缓存、store、卡片 composable 和展示模式；删除失去调用的旧远程卡片处理。
5. 将 `tests/test_info_card.py` 中的公告行为测试合并到 `tests/test_updates.py`，删除只验证后端重复文案的断言和旧测试模块，补充新接口与服务装配覆盖。
6. 执行自动检查及实际 Windows 启动器验收，确保公告服务迁移未影响版本更新检测。
7. 按中文 Conventional Commits 分别提交前端及主仓库，主仓库记录前端指针；先同步原始前端本地 main，再同步主仓库本地 main。远端推送遵循本次修改的明确推送指令。

## 测试与验收标准

- 后端执行 `ruff check ECL tests`、`ruff format --check ECL tests`、更新/公告服务测试、API 注册及模型覆盖测试、应用装配测试和主仓库完整 pytest；执行 CI 要求的 Core 检查与集成测试。
- 保留日期过滤、禁用条目、去重、优先级、翻译保留、JSON 空结束时间、错误结束时间隔离、新域名请求、缓存持久化、网络失败回退和刷新间隔覆盖。
- 验证新 API 成功响应只含公告列表；旧命令未注册；无效请求按项目既有协议返回失败。
- 前端执行 `pnpm check`，通过后执行 `pnpm build`；确认本地小贴士与欢迎语在无公告时仍可用，语言切换不依赖后端文案。
- 在实际 Windows Tauri/WebView2 启动器中验证：新 IPC 获取线上公告、首页标题和详情正文显示、小贴士切换/轮播正常、语言切换有效、无公告时保留正常空状态。
- 验证 Showcase 的新公告接口与卡片展示，以及版本更新检测的既有用例和桌面状态。
- 全仓确认旧模块、旧类、旧服务字段、旧命令及废弃前端归一化函数没有实际引用；历史方案文档保留其记录用途。

## 影响范围与约束

这次改动跨主仓库与前端子模块；两者必须同步更新，旧前端使用已移除命令时无法获取公告。公告服务和版本更新服务仅合并所在模块，保持各自独立的执行时机、网络查询与状态。

保留现有公告缓存文件格式，无需数据迁移。实例启动、账户和联机不属于本次功能修改范围。

实施完成后在本文补充实际提交、自动检查、桌面验收和本地 main 同步结果。

## 实施结果

- 公告实现已合并为 `services/updates.py` 中的 `AnnouncementService`，使用类型明确的公告与翻译结构；已删除 `host/info_card.py`、旧类及模块导出。
- 后端重复文案、轮播配置和不再需要的独立 HTTP 兜底包装已清理。应用统一注入并关闭共享 HTTP 客户端，公告与版本检测各自维护状态。
- 正式 IPC 改为 `launcher_announcements_get`，成功响应直接提供公告数组；注册、中文查询日志、前端命令类型、查询键、store 与 Showcase 同步更新，无旧命令兼容入口。
- 前端已删除旧远程完整卡片的默认对象和归一化函数；保留本地文案、语言切换、欢迎状态和轮播，Showcase fixture 仅保留公告列表。
- 公告测试已并入 `tests/test_updates.py`，删除旧测试模块；保留有效期、空结束时间、错误条目隔离、翻译、排序、内存复用与缓存回退覆盖，新增正式接口返回结构、非法请求拒绝和旧命令退休验证。
- 主仓库 Ruff 与格式检查通过；完整 pytest 为 1320 项通过、7 项跳过。Core Ruff 与独立测试通过，12 项测试通过。
- 前端 `pnpm check` 通过，154 个测试文件通过，809 项测试通过、1 项 todo；`pnpm build` 通过，构建器仍提示部分分块超过默认建议大小。
- 实际 Windows Tauri/WebView2 使用新 IPC 获取线上公告数组，首页标题与详情正文正常显示，旧 IPC 已被拒绝；小贴士切换与轮播正常，版本检测返回 `update_available`。
- 通过启动器设置页面切换英文，信息卡显示英文“Did You Know”及本地提示正文。
- 使用受控空公告源重新启动实际启动器，新 IPC 和首页 store 均为空数组，欢迎语与本地小贴士可用，公告页显示“No announcements”。受控数据只用于隔离测试目录。
- 本次按前端、主仓库顺序分别提交并同步原始本地 main，不执行远端推送。验收资料归档于本次任务的 `announcement-service-consolidation-20261011/playwright` 目录。
