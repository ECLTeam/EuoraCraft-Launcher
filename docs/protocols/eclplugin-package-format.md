# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名 ZIP 归档，不是加密文件。第三方插件使用 API v2，在独立 Worker 中运行；系统插件仍由宿主维护。安装只准备文件，不自动审阅、授予权限或执行入口。依赖安装到独立库目录，不另建解释器或 venv；Worker 复用当前发行物。归档摘要用于完整性校验，不证明作者身份。Worker 不提供恶意 Python 的操作系统文件或网络沙箱。

后端用 `plugin_package_inspect` 预检，再通过 `plugin_install` 传入 `plugin_path`、`confirm_unverified_source: true` 和 `allow_network` 安装。Dev Channel 对应 `plugin.inspect` 和 `plugin.install`，路径字段为 `path`。预检返回 `dependencies_ready`；安装结果包含 `status` 和 `message`，列表提供 `pending_restart` 与 `installed_version`（Dev Channel 为 `pendingRestart`、`installedVersion`）。不再接受运行时离线包参数。未允许联网时仅使用包内 wheels、共享 wheel 缓存和已就绪依赖目录。

## 文件结构

插件代码仍按现有 `plugin.json` 的 `entry_point` 约定放在归档根目录。`package-manifest.json` 由打包命令生成，不能放进插件源目录。

```text
sample.eclplugin
  plugin.json
  main.py
  resources/...
  locks/<target>.txt      # 当前目标完整传递依赖锁
  wheels/<target>/*.whl   # 可选，当前目标离线 wheel
  package-manifest.json
```

清单为 UTF-8 JSON，`format_version` 固定为 `1`，`files` 枚举除清单自身之外的所有普通文件：

```json
{
  "files": [
    { "path": "main.py", "sha256": "<64 位小写十六进制 SHA-256>", "size": 123 },
    {
      "path": "plugin.json",
      "sha256": "<64 位小写十六进制 SHA-256>",
      "size": 456
    }
  ],
  "format_version": 1
}
```

文件路径使用相对 POSIX 格式。归档不接受符号链接、显式目录条目、路径穿越、大小写折叠后重名、加密条目或 `package-signature.json`。本版没有签名或验签能力；安装界面会警告来源未验证，并要求用户显式确认。

## Python 依赖锁

`plugin.json.pythonDependencies` 是 Python 直接需求字符串数组，与插件间的 `dependencies` 完全独立。需求可以包含版本范围和 extras，但首版不支持需求中的 URL 或环境标记；平台差异通过各目标完整锁表达。目标标签按实际宿主确定：`<os>-<arch>-cp<主次版本>`，例如 Python 3.12 的 `windows-x86_64-cp312`，Python 3.13 的 `windows-x86_64-cp313`。系统取 `windows`、`linux`、`darwin`；架构取 `x86_64`、`arm64`。缺少当前目标锁时不能安装含 Python 依赖的插件。cp312 锁不能在 cp313 宿主上冒充兼容。

锁文件是 UTF-8 文本，每个包占一行，列出**全部传递依赖**的精确版本和允许的 wheel SHA-256；可有空行或整行注释，不接受索引配置、任意 URL、版本范围、editable 或源码构建。示例：

```text
example-core==2.1.0 --hash=sha256:<64 位小写十六进制摘要>
example-data==1.3.0 --hash=sha256:<摘要一> --hash=sha256:<摘要二>
```

哈希对应最终 wheel 文件，而非解包后内容。`wheels/<target>/` 只允许 `.whl`。启动器按包内 wheel、共享缓存的顺序查找；显式允许联网后才从固定 PyPI Simple JSON 索引补齐锁内精确版本与摘要的兼容 wheel。没有现场版本求解、源码构建、pip 配置或私有索引支持。作者应使用自己的开发环境准备完整锁与 wheels；制作命令不代替作者求解依赖。

准备时检查 wheel 元数据、RECORD、Requires-Python、宿主标签、完整传递依赖及 extras 闭包。`.pth`、sitecustomize/usercustomize、原始脚本布局不支持并拒绝安装；console/gui scripts 不生成解释器包装器，安装结果明确提示仅支持库导入。原生扩展仍依赖实际宿主 ABI 和动态库兼容性。

## 安装与运行边界

同一完整锁字节、宿主 ABI 和平台共用 `plugin_deps/<key>/site-packages`；不同锁共享 `plugin_cache/wheels/<sha256>`，不向系统或宿主 site-packages 安装。就绪目录逐文件复核，禁止增量覆盖损坏目录；代码版本位于 `plugin_packages/<name>/pkg-<摘要>`，活动指针为 `active.json`。

不同 Worker 可以使用同名包的不同版本；每个 Worker 校验自己的完整锁、代码和依赖目录。不得覆盖标准库、ECL 或 API，单一环境内不同包提供同名导入仍拒绝。原生扩展须匹配当前解释器 ABI。

归档安装和更新仍显示待重启；重启后也必须通过内容审阅和必要授权才能启用。目录安装完成后同样待审阅。授权绑定宿主安装身份、代码、权限、完整锁和 ABI；更新内容不会自动继承许可，卸载重装使用新安装身份。损坏数据保留原文件并明确失败，不转换旧权限为 allow，也不回退到第三方主进程运行。

禁用、重载和撤销通过 Worker 监管处理，确认旧进程树停止后才建立新运行。已完成的设置写入不会因权限撤销自动回滚。设置和授权原文件保留，详细状态见插件管理页。

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels。

## 第三方 API v2

目录与归档共用清单和 SDK；`plugin.json` 必须显式声明 `api_version: 2`。示例：

```json
{
  "name": "example",
  "api_version": 2,
  "version": "1.0.0",
  "entry_point": "main:Plugin",
  "permissions": []
}
```

```python
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def on_load(self):
        self.define_command(
            "greet", self.greet,
            input_fields=({"name": "name", "type": "string"},),
            output_fields=({"name": "message", "type": "string"},),
        )

    def greet(self, name):
        return {"message": f"Hello, {name}"}
```

插件获得 HostClient，不能取得宿主 framework、账户服务或配置对象。回调留在 Worker 内，跨进程只传 JSON。当前已开放自身命令、设置、自查询、自身设置变化订阅、获准实例读取和自身任务控制；尚未接线的业务方法明确失败，依赖未实现必要能力的插件不可启用。第三方自定义前端保持关闭。

设置写入必须提供 `expected_revision`；变更 RPC 使用本运行 `mutation_key` 去重，同键不同内容拒绝。等待超时不能当作动作已经停止或回滚，也不能无条件换新键重放副作用。

命令字段 schema 当前只接受 `string`、`number`、`integer`、`bool`，支持 `required`、`max_length`、`minimum`、`maximum`、`enum`，不执行插件验证器、不支持任意 JSON Schema 引用或嵌套对象。每组最多 32 个字段，单运行最多 64 个命令、32 个订阅；请求和结果额外限制为 64 KiB。设置支持 `bool`、`string`、`number`、`select`，所有写入经定义校验并原子提交。

需要超时后核对写入的调用方应显式保留原 `mutation_key`，通过 `host.call("mutations.get", {"mutation_key": key})` 查询同一运行的 `pending`、`succeeded` 或 `failed`，结果包含 `result` 和 `error_code`。回执最多保留 1024 个，容量满时拒绝新变更；不淘汰有效回执后重放。授权 epoch 改变后不交付旧回执结果；run 结束后回执不再可用。SDK 自动生成的键不会构成自动重试承诺。

撤销首先阻止新提交；已接受的设置写入可能继续落盘。宿主在途写入、Worker 和事件资源均结束后才报告清理完成，超时只报告 pending 或失败。事件队列满及可靠事件交付失败会关闭该 Broker 并回收运行；已经提交的设置仍报告真实 revision，不伪造回滚。

完整示例见 [worker_echo](../../examples/plugins/worker_echo/README.md)。现行运行边界见 [无头运行说明](../guides/headless-runtime.md)，总体契约与后续能力见 [Worker/API v2 设计](../designs/plugin-worker-api-v2-implementation-design.md)。`ECL.plugins.Plugin` 仅供发行资源中的可信系统插件使用，不为第三方提供旧 SDK 适配。

### 权限与启用

基础能力随确认启用当前内容获得：自身命令、设置、自身设置事件、私有数据文件及固定来源的游戏版本目录不再逐项声明。`plugin.base` 是绑定当前安装、内容和运行的内部准入，不能写进权限清单或作为用户授权项。

清单接受 `instances.read`（查看游戏）、`instances.manage`（管理游戏）、`network.read`（联网），以及独立的 `java.read`、`java.use`、`java.catalog.read`、`java.install`、`accounts.read`。管理游戏包含查看、安装、资料修改、启动、资源与整合包管理，以及停止本运行创建的游戏；游戏授权统一选择根目录，覆盖其中现有及以后新增的实例。只读插件不能据此管理游戏。需要只读降级的插件可以同时声明必要查看和可选管理，管理生效时包含同一范围的查看能力。Java 与账户不继承游戏范围，详见下文。

基础插件点击启用即确认当前内容；业务插件在一个游戏目录选择区和联网站点说明中完成“同意并启用”。当前有效游戏目录可预选，但须在确认前显示。授权已保存而启动失败时可以直接重试启用；已启用插件保存访问范围不等于重新启用。授权继续绑定完整内容，不继承到更新后的不同内容。

旧细分权限声明明确拒绝，插件作者须更新清单，不做别名、自动合并或迁移。旧摘要的授权记录和插件数据原地保留，新清单使用现有的新内容摘要记录。完整方案见[权限简化方案 B](../plans/plugin-permission-simplification-plan-2026-10-30.md)。

### 获准实例读取

清单请求 `{"capability":"instances.read","required":true}` 或 `instances.manage`，不能自报本机资源 ID。用户在可信主窗口选择游戏根目录，未选择不满足必要能力。`host.call("resources.list", {"capability":"instances.read"})` 返回当前运行获准根引用；用 `instances.scan` 取得具体实例引用，再调用 `instances.get`。管理权限直接支持这些查看方法，无需额外声明读取。示例见 [instance_reader](../../examples/plugins/instance_reader/README.md)。

资源和扫描列表均返回 `{items,next_cursor}`，可提交 `cursor`、`limit`（1—100，默认 50）。扫描另接受 `force`，仅刷新本次只读快照。单个摘要返回 `instance_ref`、`version_id`、`alias`、`loader`、`game_version`、`profile:{description,favorite,tags}`、`profile_etag`、`running_count`；不返回真实路径或完整实例配置。etag 绑定完整本地资料的存在状态、物理身份、稳定元数据和原始字节摘要，不是递增 revision；其他字段变化或同名同内容重建也会使其失效。令牌本身不授予写权限。

引用不能跨运行保存重用，授权变更、资源退休和内容变化后需重新发现。`permissions.list` 中该能力的资源引用同样是运行引用。查询在读取前及交付前校验许可；符号链接、重解析点、损坏资料和过期游标明确失败。实例读取不修改游戏资料、统计或进程。其他业务接口及 OS 文件/网络沙箱不由该能力提供。

### 获准实例资料修改

清单声明 `instances.manage`，声明中的 `resource_refs` 为空。用户批准游戏根后，可修改该根中实例的允许资料字段，操作和结果投影始终绑定同一原始管理准入。`permissions.list` 只投影当前运行根引用，不返回持久资源 ID。

```python
before = host.call("instances.get", {"instance_ref": instance_ref})
receipt = host.call("instances.profile_patch", {
    "instance_ref": instance_ref,
    "expected_profile_etag": before["profile_etag"],
    "patch": {"description": "朋友服务器", "favorite": True},
}, mutation_key=saved_key)
# 仅表示接纳；通过 operations.get 查询 receipt["operation_ref"]。
```

只允许 `alias`、`description`、`favorite`、`tags`，至少一项，拒绝 null、额外字段和类型转换。别名去空白后 1—120 字符；描述至多 1000 字符，允许空字符串；收藏为严格布尔；标签至多 20 项，每项去空白后 1—40 字符，按大小写去重并保留第一项文本。未提供字段保持原值，false 和空数组会保存。原资料的其他合法字段完整保留，不提供任意 JSON、图标、启动设置或存档写入。原、新资料均至多 64 KiB；损坏 JSON、重复字段、未知显式版本和字段类型异常会明确失败并保留原文件。

提交最多 5 秒；任务 `plugin_instance_profile_patch` 的总截止为 30 秒，包含排队。成功视图为 `{instance_ref,alias,profile:{description,favorite,tags},profile_etag}`，绑定真实提交后的完整版本，交付前再次验证原始管理准入和物理实例；失权时 result 为 null，不改写 effect。完整版本冲突返回 `PLUGIN_INSTANCE_PROFILE_CONFLICT`，调用方不得自动读新版本强制覆盖或换键重放。

平台与文件系统限制见[文件平台边界](#文件平台边界)。宿主固定原目录和文件对象，核对原资料版本后释放其独占句柄，再用**原子替换**发布新资料：要么旧资料完整保留，要么新资料完整可见，不会出现半写或名称缺失。替换成功后旧版本不可找回，宿主不保留备份、不登记恢复记录。暂存或句柄尚未实际释放时保持 cleanup_pending；重启不恢复旧 Worker、旧任务或旧补丁。

示例见 [worker_instance_profile](../../examples/plugins/worker_instance_profile/README.md)，边界及验收见 实例资料修改方案。

### 自身任务控制

`operations.get({operation_ref})`、`operations.list({cursor?,limit?})` 和 `operations.cancel({operation_ref})` 只操作当前安装、内容和运行自己的任务。控制自身已有任务不需要重新授予业务权限；结果必须具有创建时显式绑定的结果准入，并在交付时逐次复核，失权后 result 为 null。FileView必须有独立读取许可，删除的无内容确认绑定原删除许可；写许可不能代替读许可。不能传入宿主 operationId、owner、路径或其他 run；跨运行引用统一拒绝。取消需要本 run 的 mutation_key，HostClient.call 默认生成；同键重复调用不会重复副作用。

TaskView 返回 `operation_ref,kind,state,percent,stage,message,can_cancel,cancellation_requested,effect,cleanup_pending,result,error_code`。state 为 queued、running、committing、completed、failed 或 cancelled。cleanup_pending 为布尔值，非终态为 false；终态仍有实际 future、领域资源或审计未收尾时为 true。查询和监管只重试清理、观察及保存，不重放业务。取消返回 `{requested,state,effect}`，requested 只表示已请求，实际停止要继续查询。提交已接受时取消失败 `PLUGIN_COMMIT_STARTED`，撤销或 Worker 退出不把随后成功或真实错误改成取消。stage/message 为安全阶段说明，不交付原始日志、路径或异常。

列表 limit 为 1—100、默认 50；每页受 64 KiB 限制，游标最多 16 个快照、120 秒，绑定本 run 和 limit。快照固定引用集合，每页重新查询状态和过滤结果。每 run 最多 128 个引用，宿主同时跟踪至多 1024 个插件任务；容量耗尽明确拒绝，不淘汰活跃任务。关闭 run 后只有实际 future 和审计保存完成才释放记录。

不开放通用 `operations.submit` 或安装任务创建接口。具体业务入口为 instances.profile_patch、files.write_commit、files.delete 与下文 downloads.submit；未提交任务的插件列表可以为空。设置短写保持现行同步结果和 mutations.get 回执。任务控制基础见 任务归属与提交控制方案。

## 私有文件与新建事务

私有文件属于启用后可用的基础能力，不在清单中声明额外文件权限。通过 `resources.list({capability:"plugin.base"})` 取得本安装的私有数据根引用，随后调用现有文件 API。读取、新建、替换和删除共用原始基础准入；新建仍禁止覆盖，替换仍要求目标版本。范围固定为自身空间，不允许创建子目录或访问用户游戏目录。

### 文件平台边界

真实源码门禁覆盖 Windows x64 本地 NTFS、Linux x86_64 和 macOS ARM64。POSIX 文件系统须为实现允许的本地类型，并能提供可靠出生时间和原生不覆盖重命名；远程、未知或缺少必要接口时明确拒绝。目录描述符固定祖先与目标绑定，拒绝链接、硬链接及跨设备对象；普通路径检查不能替代物理身份核对。私有文件、资料修改、游戏资源和安装调用同一平台入口，实际测试范围见[跨平台验收](../plans/plugin-posix-files-and-webviews-2026-11-01.md)。

### 私有文件接口

先调用 resources.list，capability 为上述独立文件能力，取得当前 run 的 root_ref。FileRef 为 `{root_ref,relative_path}`，文件名为 1—120 个 ASCII 字符，须以字母或数字开始，后续仅允许字母、数字、点、下划线和连字符；拒绝设备名、尾点和路径组件。空名称只用于私有根 stat/list。引用不能跨 run/授权代次重用，任何接口都不返回宿主路径。

读取使用 files.stat、files.list、files.read。read 的 count 为 1—32768，结果为 Base64、eof 和完整内容与物理身份绑定的 etag；expected_etag 变化时明确失败。FileView 时间字段为 modified_ms。读取不会初始化首次空间；损坏或丢失的已有空间不能当作空空间重建。

新建按 files.write_begin `{target,total_bytes,overwrite:false}`、files.write_chunk `{write_ref,offset,data_b64}`、files.write_commit `{write_ref,sha256}` 执行。单文件至多 8 MiB，原始块至多 32 KiB；严格 Base64、连续偏移、完整 SHA-256。零长度文件省略 chunk。begin/chunk/commit 需要稳定 mutation_key 才能重试同一请求，HostClient.call 默认每次生成新键；重试须保存原键。

commit 返回 `{operation_ref,state:"queued"}`，表示已接纳；实际结果通过 operations.get/list 查询。发布始终原生不覆盖，目标突现时保留外部文件。任务结果只在提交时和读取时都有有效读许可时交付。提交等待超时后先查 mutations.get，不能将超时解释为任务未创建。

files.write_abort `{write_ref}` 不消耗变更回执，失去可选文件权限后仍能清理自己的上传。交给任务后返回 released:false，取消改用 operations.cancel；已接受提交不再取消。清理失败、实际线程未结束或审计未完成时保持 pending，不提前释放配额或允许重新启用。

同一安装更新保留数据但使旧 run 引用失效；重新安装获得新的私有空间，旧文件保留。空间总量 64 MiB，目录最多 1024 项，每 run 最多 4 个上传及 32 MiB 预留，宿主总上传预留 128 MiB；空闲上传 60 秒过期。未登记的崩溃残留计入空间，不自动恢复或删除；替换事务按下节原子替换。参见 完整协议与验收 和可安装的 [worker_snapshot 示例](../../examples/plugins/worker_snapshot/README.md)。

### 按版本删除私有文件

`files.delete({target:FileRef,expected_etag:64位小写十六进制})` 使用本运行基础准入，无独立删除开关。目标必须是当前私有根内非空名称的普通单链接文件，至多8 MiB。拒绝目录、ADS、链接、递归、force、额外字段和真实路径；目标缺失返回 PLUGIN_RESOURCE_NOT_FOUND，超大返回 PLUGIN_FILE_TOO_LARGE。根发现和删除均不初始化首次空间。

RPC最多5秒只登记名称占用并返回 `{operation_ref,state:"queued"}`；任务 plugin_file_delete 从本次提交起共30秒，包含排队。上传、下载和删除共用大小写不敏感目标占用，冲突为 PLUGIN_FILE_BUSY。每run至多4个、宿主32个未释放删除资源，与上传字节预留独立。

执行时相对原根独占打开目标，在同一句柄计算完整SHA-256并核对身份、长度、修改时间和expected_etag；同名同内容重建也拒绝。原lease、取消及截止均在最终接受前复核，先保存committing审计，再设置内核删除标记。接受前取消/失权/审计失败保留原文件。标记或关闭失败只有原句柄撤回标记并确认后才报告零效果；无法确认则保留unknown和待清理资源。

成功必须确认原对象目录项已消失，最终仅返回 `{deleted:true}`。该无内容确认绑定原基础准入，失权后result为null，effect不改。自身CloseHandle成功不代表实际删除；外部元数据句柄可能延迟移除，超时返回 PLUGIN_FILE_DELETE_PENDING、effect=unknown、cleanup_pending=true。内核删除仍可能晚到完成；后续只观察原对象身份，不重开名称补删，不触及同名新对象。实际清理完成可以清除cleanup_pending，但不把unknown改报成功。

这是直接删除，成功后不可恢复，不进入回收站。Windows 沿用原对象句柄的延迟删除；POSIX 在实际移除前持久登记并隔离原对象，只有物理身份一致才 unlink。POSIX 中断后再次访问目录时，只恢复本实现严格记录中尚存的对象且不覆盖占名文件；身份、权限或记录异常时保留全部数据并明确失败。该恢复不复活旧 Worker、任务或授权，不扫描清理其他历史文件。提交等待超时使用原mutation_key查询mutations.get；接受后不可取消。未释放资源阻止重新启用，暂存清理同样等待已知原对象实际消失才归还名额。参见 实施与验收 和 [显式清理示例](../../examples/plugins/worker_file_cleanup/README.md)。

### 按版本替换私有文件

`files.replace_begin({target:FileRef,expected_etag,total_bytes})` 使用本运行基础准入，返回与新建相同的 `{write_ref,max_chunk_bytes}`。随后使用既有write_chunk、write_commit、write_abort；上传始终绑定原始基础准入，运行或代次失效后不能复活旧上传。原、新文件均至多8 MiB，分块32 KiB；不接受overwrite、force或真实路径。begin不初始化缺失空间或目标。已有文件和新暂存同时计入配额。

commit创建plugin_file_replace任务，从提交起共30秒，包含排队。执行时从原句柄完整核对原对象与expected_etag，最终接纳前复核原lease、取消、截止及审计。核对通过后释放原对象独占句柄，用**原子替换**把暂存发布为原名：要么旧内容完整保留，要么新内容完整可见，不会出现半写文件。新建与下载继续禁止覆盖。

替换成功后旧内容不可找回，宿主不保留备份、不登记恢复记录，也不阻塞该路径的后续写入。接受前失败保留原文件；替换本身失败按真实结果报告effect，失败时原文件内容不变、暂存由任务收尾清理，`cleanup_pending` 只在暂存或句柄尚未实际释放时保持。

崩溃语义：替换前中断，目标保持旧内容（`.upload-*` 暂存残留计入空间，不自动恢复或删除）；替换后中断，目标是新内容。重启不继续旧上传、不恢复旧run控制引用，也不扫描删除未知残留。

示例见 [worker_file_replace](../../examples/plugins/worker_file_replace/README.md)。

## 只读网络请求

清单声明 `{"capability":"network.read","required":false,"resource_refs":["https:api.example.com:443"]}`。必须指定 1—16 个唯一精确站点键，使用小写 ASCII DNS/punycode、显式规范端口；不接受 URL 形式、通配符、尾点、单标签或 IP 字面量。不同协议、端口、父域和子域不互相授权。用户在现行权限弹窗审阅完整站点列表，整组允许或拒绝；允许覆盖站点全部路径与查询。

可信管理视图每个 PermissionItem 都提供 `requested_resource_refs`（绑定清单的申请范围）、`selected_resource_refs`（用户决定）和 `resource_refs`（当前实际有效范围）。Worker 的 permissions.list 仍只表示实际许可，不取得管理入口。

调用 `host.call("network.request", {"method":"GET","url":"https://api.example.com/meta"}, timeout=30)`，严格只支持 GET/HEAD，无请求体。返回 `{status_code,url,headers,body_b64}`，url 为校验后的最终 URL。输入与规范 URL 各至多4096字节；GET 完整实体至多32768字节，超限、断流或长度不符明确失败。HEAD、204、304不读实体；HTTP 4xx/5xx 返回真实 HTTP 状态。请求不是任务，也不保存 mutation 回执；重复调用可能重新发送，不承诺远端零副作用或撤回已发送字节。

请求头仅 Accept、If-None-Match、If-Modified-Since、User-Agent、Authorization、X-API-Key，认证头只允许 HTTPS。最多16项、总4096字节、值最多1024个可打印 ASCII 字符；拒绝大小写重复、控制字符和其他头。响应仅投影小写 content-type、content-length、etag、last-modified、cache-control，总4096字节。GET 不解压，宿主要求 identity，其他实体编码拒绝；无 Cookie、共享账户、环境代理、自定义 CA 或 HTTP/2。

每跳独立 DNS 和 HTTP/1 连接，只连接已验证数字公网地址，并核实真实 peer，TLS 始终保留原 DNS 名称与证书校验。任一 DNS 结果是内网、特殊用途、映射或转换地址则整跳拒绝；公网许可也不能访问这些目标。最多5次跳转，逐跳复核精确许可和原租约；禁止 HTTPS 降级，跨站永久移除认证头，Cookie 不保存。总截止最多30秒，每次阶段 I/O 最多5秒，每次底层读取最多8 KiB，每跳原始 HTTP 收包最多256 KiB。

每 run 同时2个请求，宿主8个请求、4个真实 DNS 工作者；满额立即拒绝。DNS超时或取消不证明实际解析结束，未结束时保留名额和清理 pending，阻止同安装重新启用；迟到结果只清理，不继续连接。解析工作者为有界守护线程，应用不无限 join，实际结束仍如实跟踪。连接/TLS 尚未返回真实流时按有限阶段预算等待，返回后登记并清理；关闭失败保留监管重试。

稳定失败包含 `PLUGIN_SCHEMA_INVALID`、`PLUGIN_RESOURCE_DENIED`、`PLUGIN_NETWORK_TARGET_DENIED`、`PLUGIN_NETWORK_RESOLUTION_FAILED`、`PLUGIN_NETWORK_FAILED`、`PLUGIN_REDIRECT_DENIED`、`PLUGIN_RESPONSE_TOO_LARGE`、`PLUGIN_NETWORK_ENCODING_UNSUPPORTED`、`PLUGIN_DEADLINE_EXCEEDED`、`PLUGIN_RATE_LIMITED`，授权失效沿用原授权错误码。项目文件、控制台和前端日志不记录 URL、头、响应或传输原始异常；其他上下文日志保持原行为。network.write、network.lan 仍未开放；私有空间下载见下节。参见 实施与验收 和 [worker_network_read](../../examples/plugins/worker_network_read/README.md)。

## 私有空间下载任务

`downloads.submit({url,target:FileRef,overwrite:false,expected_sha256?})` 只向已批准的精确公网站点发送 GET，将完整 HTTP 200 实体新建到当前安装私有根。URL 至多4096字节，文件至多8 MiB；文件名规则与 FileRef 相同，必须非空。overwrite 必须显式严格 false，拒绝覆盖、任意路径、子目录、请求体、自定义请求头、Range、续传或解压。expected_sha256 可省略，提供时须为64位小写十六进制，不接受 null。

必须同时拥有原始 network.read 许可与基础私有数据准入。每跳重新核验许可、DNS/IP/peer/TLS 与总截止，最多5次重定向、不降级、不重试、不继承代理/Cookie/账户。仅最终200进入文件写入；204、206、304和错误状态不发布文件，返回 PLUGIN_DOWNLOAD_FAILED。固定长度响应须完整匹配，缺少长度允许到 EOF；实体和原始收包分别限制8 MiB、每跳9 MiB，分块开销过大也明确失败。network.request 仍保持32 KiB实体、256 KiB每跳原始收包和30秒限制。

提交 RPC 至多5秒，返回 `{operation_ref,state:"queued"}`，通过 `operations.get/list/cancel` 查询与取消，不能把接纳当成文件已经保存。整个任务从首次提交开始最多1800秒，含排队与最终提交前校验；每个网络阶段至多5秒。队列不占网络/DNS名额，但预留目标和8 MiB空间，与上传共用每run4个暂存、32 MiB预留、宿主128 MiB预留和安装64 MiB空间。空间不足8 MiB时小文件也可能被拒绝；实际资源释放前不提前归还对应名额。

同一 run 的 mutation_key 和相同请求只接纳一次。RPC 等待超时用原键查询 mutations.get，不换键自动重放；任务控制回执不因业务授权变更而恢复执行许可。下载的原网络和写 lease 必须共同有效，最终发布按同一授权锁与任务锁排序；取消/撤销先于提交接受则无目标文件，发布接受先发生则按真实结果完成，不伪造回滚。

成功为 completed、effect committed；最终 result 使用发布前捕获的 FileView，仅在提交时的原始基础准入于交付时仍有效才返回，否则为 null。重新启用不会恢复旧结果准入；可以通过当前 files API 重新读取。摘要始终计算并从原生暂存复核，可选期望摘要不符为 PLUGIN_HASH_MISMATCH。未知长度接收阶段不伪造百分比；只有文件发布完成才到100%。

失败、排队取消和审计失败都尝试清理暂存与实际网络工作；系统 DNS 尚未返回、关闭/删除失败或审计未保存时保持 cleanup pending，阻止同安装重新启用。清理重试不会重做 HTTP 或发布，不删除成功文件，不扫描清除崩溃残留，不恢复旧run控制权。其余稳定错误沿用网络、文件和任务接口。参见 完整下载方案与验收 和 [worker_download](../../examples/plugins/worker_download/README.md)。

## 获准游戏目录、安装与进程控制

公开版本和加载器目录属于基础能力，由宿主固定来源提供，不接收插件指定地址。安装、启动、运行管理和停止使用 `instances.manage`，统一由可信主窗口批准游戏根；管理包含查看，不需要再声明其他游戏细项。修改授权使原始准入失效，重新批准不复活旧任务及结果。

| 方法 | 请求 | 结果及限制 |
| --- | --- | --- |
| `game.catalog` | `{type?:"release"\|"snapshot",cursor?,limit?:1..100}` | `{items:[{version_id,type,release_time}],next_cursor}`；不含旧Alpha/Beta分类或下载地址 |
| `game.loaders` | `{game_version,loader:"fabric"\|"forge"\|"neoforge"\|"quilt",cursor?,limit?:1..100}` | `{items:[{loader_version,is_latest}],next_cursor}`；版本降序，最新标记与安装默认选择一致 |
| `game.install` | `{root_ref,version_id,version_name?,loader?:"vanilla"\|"fabric"\|"forge"\|"neoforge"\|"quilt",loader_version?}` | `{operation_ref,state:"queued"}`；原始管理准入仍有效时，成功结果为 `InstanceView` |
| `game.launch` | `{instance_ref,options?:LaunchOptions}` | 自身任务回执；需要所属游戏根的管理准入，成功结果为 `GameRunView` |
| `game.running` | `{instance_ref}` | `{items:[GameRunView]}`；只返回当前Worker运行创建的进程记录，包含保留的实际退出码 |
| `game.stop` | `{game_run_ref}` | 自身任务回执；管理准入只控制本运行创建的确切进程，结果绑定同一原始管理准入 |

所有请求严格拒绝未知字段和错误类型。分页默认50，目录最多10000项；游标绑定当前运行、查询和授权代次，120秒过期，不跨运行复用。查询最多30秒，使用宿主当前有效下载源及已有目录服务，不接受URL、代理、源配置或账户信息。显式加载器版本必须存在于该游戏版本的目录；省略时由宿主选择兼容的最高版本。

`LaunchOptions` 仅有 `memory_mb?:512..65536`（默认4096）、`width?:320..16384`（默认854）、`height?:240..16384`（默认480）、`fullscreen?:bool`（默认false）。整数不接受布尔值。账户、Java、下载源和游戏语言由宿主提供；不开放 `runtime_ref`、Java路径、JVM/游戏参数、wrapper、命令、脚本或环境变量。需要当前账户时返回 `PLUGIN_ACCOUNT_REQUIRED`，提交前账户变化返回 `PLUGIN_ACCOUNT_CHANGED`。

`GameRunView` 为 `{game_run_ref,instance_ref,pid,running,exit_code}`。引用是本运行随机引用，不返回路径、命令、日志、账户、内部任务编号或内部进程token。宿主按原始进程对象与所有者校验停止目标，不按PID、名称或可复用的实例编号找进程。每运行最多保留128条进程记录；跨运行查询不交付别人的进程，外来停止引用返回 `PLUGIN_OWNER_MISMATCH`。

安装、启动、停止沿用 `mutation_key` 去重和 `operations.get/list/cancel`，接纳RPC至多5秒；安装总截止1800秒、启动180秒、停止30秒，均含排队。等待超时使用原键查询 `mutations.get`，不换键自动重放。接纳、请求取消、实际结束和清理完成相互独立。Worker任务视图的 `stage` 返回实际业务阶段，可信主窗口任务中心显示相同任务及安装权威快照；结果只显式投影并复核原始管理准入。

安装遵循[文件平台边界](#文件平台边界)。新实例名称须为安全的单个目录组件，不允许路径、ADS、设备名、尾点或空白归一化。原生目录句柄固定游戏根和目标身份；同一物理根及名称的占位由界面安装、整合包基础安装和插件共用，实际线程与句柄清理完成前不释放。已有用户实例拒绝覆盖；失败文件原地保留，只允许同一运行对同一物理身份及相同安装请求继续。插件关闭不删除文件，新运行不接管旧残留。写入开始后失败如实为 `effect:"partial"`，完整安装完成才为 `committed`。

用户与插件共用单个启动准备占位，冲突返回忙。异步账户和下载准备沿用宿主既有事件循环，进程创建是短提交点，之前再次核对原始许可、取消、目录身份和当前账户；之后撤权或关闭插件不会杀游戏。准备可能已经补全游戏文件，即使未创建进程也可能是partial。已创建游戏仍在启动器正常运行列表中；停止使用有限等待，并在未确认退出时返回 `PLUGIN_GAME_STOP_TIMEOUT`。重复停止已退出的自身进程成功且保留退出码。

常见失败包括 `PLUGIN_SCHEMA_INVALID`、`PLUGIN_GAME_VERSION_INVALID`、`PLUGIN_LOADER_VERSION_INVALID`、`PLUGIN_INSTALL_BUSY`、`PLUGIN_LAUNCH_BUSY`、`PLUGIN_HOST_NOT_READY`、`PLUGIN_FILE_EXISTS`、`PLUGIN_OWNER_MISMATCH`，其余沿用授权、资源、任务和安装错误。参见[安装示例](../../examples/plugins/worker_game_install/README.md)、[启动示例](../../examples/plugins/worker_game_launch/README.md)及[实施方案](../plans/plugin-game-install-launch-plan-2026-10-25.md)。

### Java 资源和固定来源安装

`java.read`、`java.use` 的清单范围必须为空，由可信主窗口分别选择已登记运行时；两项不自动预选。候选 `kind` 为 `java_runtime`，使用独立 `java_resource_revision` 提交授权。`java.catalog.read` 和 `java.install` 不接受资源范围，后者的用户可见含义为“安装并使用官方 Java”。清单不能提供 URL、磁盘路径或全局删除能力。

| 方法 | 参数与结果 |
| --- | --- |
| `java.inventory` | `{cursor?,limit?}`；所选运行时的最后登记摘要，不扫描或运行 Java |
| `resources.list` | `{capability:"java.read",cursor?,limit?}`；当前运行 `runtime_ref`，不返回登记 ID 或路径 |
| `java.validate` | `{runtime_ref,required_major?}`；执行固定参数的有界单目标探测，不修改登记或配置 |
| `java.catalog` | `{major_version?,runtime_kind?:"JRE"|"JDK",cursor?,limit?}`；固定官方来源包的 `package_ref` 和公开摘要 |
| `java.install_plan` | `{package_ref}`；当前运行中未过期的包生成 `plan_ref` 和计划摘要，属于有去重键的变更请求 |
| `java.install` | `{plan_ref}`；返回自身任务回执；实际安装、校验和登记后交付 `runtime_ref` |

已登记目标校验与选用要求对应 `java.read` / `java.use` 的明确选择。本运行在原始 `java.install` 许可下安装成功的目标可直接选用；这不授予其他全局 Java 的访问权。`game.launch.options.runtime_ref` 可传入此引用，宿主在执行前与进程创建前重新核验原始许可及文件身份。其他启动参数不增加裸 Java 路径、JVM 参数或命令入口。

每个运行最多保留 32 个未过期安装计划。安装目标先检查真实任务所有者，用户与不同插件的在途安装返回忙，不复用或取消其任务。重复去重键只查询同一回执。取消、超时和撤权不等于安装线程已结束；目标占位保留至实际清理。安装结果受原始许可约束，撤权保留文件且不复活旧引用。示例见 [Java 查询与安装](../../examples/plugins/worker_java_query/README.md)。

### 模组、资源包、光影与在线目录

以下 `kind` 取 `mods`、`resourcepacks` 或 `shaders`，文件后缀分别为 `.jar`、`.zip`、`.zip`。查看使用 `instances.read` 或其管理包含的查看范围；写入使用所选根的 `instances.manage`。所有副作用通过自身任务提交，并使用稳定去重键。

| 方法 | 参数与结果 |
| --- | --- |
| `<kind>.list` | `{instance_ref,cursor?,limit?}`；返回 `resource_ref`、文件名、大小和适用的启用状态，不返回路径 |
| `<kind>.install` | `{instance_ref,source:FileRef,filename}`；从本插件私有普通文件复制到实例实际资源目录，成功才提交新文件 |
| `<kind>.delete` | `{resource_ref}`；只删除原列表对应的文件版本，替换后旧引用失败 |
| `mods.set_enabled` | `{resource_ref,enabled:boolean}`；设置明确目标状态，重复禁用不反向启用 |
| `<kind>.search` | `{source?:"modrinth"|"curseforge",query?,game_version?,loader?,offset?,limit?}`；公开项目白名单与下一偏移 |
| `<kind>.versions` | `{source?,project_id,game_version?,loader?,cursor?,limit?}`；公开平台版本分页 |
| `<kind>.download_info` | `{source?,project_id,version_id}`；公开文件名、URL、站点、大小和适用的 SHA-512 摘要，不直接下载 |

目录查询属于固定来源基础能力，复用用户设置的现有模组源，不接受插件指定 API 地址或提供方密钥。搜索每页 1—50 条，偏移最多 1000；版本分页每页 1—50 条。下载文件仍使用 `downloads.submit` 及单独批准的精确网络站点，完成后再传私有 `FileRef` 安装。`download_info` 不隐式授予站点或写入权限。

隔离与共享位置使用现有实例数据归属规则，不能根据实例名称自行拼路径。源与目标均固定实际句柄、拒绝链接和重解析点；同名目标不覆盖，资源版本变化后重新读取列表。当前单个资源安装最多 64 MiB，并继续受私有源文件自身配额约束。仅模组提供启用状态操作。示例与实际命令序列见 [游戏资源与整合包](../../examples/plugins/worker_game_tools/README.md)。

### 整合包所属任务

| 方法 | 参数与结果 |
| --- | --- |
| `modpacks.prepare` | `{source:FileRef}`；复制私有归档并执行现有校验，所属任务完成后返回 `preparation_ref` |
| `modpacks.get` | `{preparation_ref,cursor?,limit?}`；名称、格式、Minecraft、加载器和有限文件预览 |
| `modpacks.install` | `{root_ref,preparation_ref,version_name,excluded_file_ids?}`；批准游戏根中的新实例安装任务，拒绝已有实例覆盖 |
| `modpacks.export_preview` | `{instance_ref}`；读取所选实例并产生含 `preview_ref` 的导出预览任务 |
| `modpacks.export_files` | `{preview_ref,cursor?,limit?}`；分页列出候选文件，不自动全选 |
| `modpacks.export` | `{preview_ref,selected_file_ids,destination:FileRef,name,version,summary?}`；只导出明确选择的文件到私有新 `.mrpack` |

准备使用基础准入，导入使用管理游戏范围，导出同时要求查看实例与私有写入。每次运行最多保留 32 个准备引用和 32 个导出预览引用；准备另受领域有效期约束，运行关闭时释放，分页上限 100，明确选择或排除文件上限 1024。归档识别、路径穿越拒绝、文件大小和格式约束沿用现有领域实现。

清单的每个下载地址单独通过 `network.read` 站点、重定向、DNS 和连接检查，不转交普通共享下载器绕过许可；当前单文件下载上限 128 MiB。共享基础版本安装仍属于该插件的实际任务，原始许可变化在阶段检查处拒绝。取消可能保留已经安装的共享基础版本，按真实 partial / committed 及清理状态报告，不宣称完整事务回滚。导出中间归档不作为最终提交，私有文件原子发布成功才接受任务提交。

导入实例保留实际 Minecraft 父版本关系及完整启动描述，导出不从自定义实例名称猜测版本。插件导入的暂存目录独占创建，不复用或清理既有同名用户暂存目录。

### 公共配置与所选账户摘要

`config.public` 只接受 `{}`，在基础准入下返回 `locale`、`theme_id`、`theme_mode`、`accent_color`、`default_memory_mb`。没有配置分区透传或通用写入口，不返回路径、启动命令、网络密钥或账户配置。

`accounts.read` 清单不能自报资源，由可信主窗口明确选择账户，不预选全部或当前账户。候选 `kind:"account"` 与独立 `account_resource_revision` 绑定；空允许范围拒绝。`accounts.list({cursor?,limit?})` 与 `resources.list({capability:"accounts.read",cursor?,limit?})` 只返回 `account_ref`、`alias`、`uuid`、`type`、`is_current`，`accounts.get({account_ref})` 查询单项。UUID 是 Minecraft 玩家 UUID，宿主真实账户标识、邮箱、令牌和提供方私有数据均不返回。

账户目录只保存随机授权身份和最小摘要指纹；账户内容或授权目录身份变化使旧引用失效，新账户不继承选择。仅当前选择状态变化可在相同引用下更新。账户或 Java 目录损坏时保持相关许可拒绝，保留原文件并明确失败，不通过空列表冒充成功。可信授权 API 对游戏、Java、账户分别复核自己的目录版本，不能将三个版本取最大值后共用。

## 产物验证

游戏目录与授权边界由 `tests/test_plugin_game_queries.py` 验证；安装、共享占位及结果读取由 `test_plugin_game_install.py` 验证；启动、进程所有权、撤权竞争及停止由 `test_plugin_game_launch.py` 验证。后两者包含公开示例经过真实源码与冻结Worker的RPC用例，完成记录见[游戏操作实施方案](../plans/plugin-game-install-launch-plan-2026-10-25.md#8-验证与交付)。

真实冻结引导、依赖和生命周期用例位于 `tests/test_plugin_worker_bootstrap.py`、`test_plugin_worker_dependencies.py`、`test_plugin_worker_lifecycle.py`、`test_plugin_operation_worker.py`、`test_plugin_network_worker.py` 和 `test_plugin_download_worker.py`；正式管理器流程位于 `test_plugin_worker_manager.py`、`test_plugin_manager_packages.py` 与 `test_packaged_plugin_directories.py`。网络策略、真实流、日志和解释器退出另由 `test_plugin_network_targets.py`、`test_plugin_network_api.py`、`test_plugin_network_logging.py`、`test_plugin_network_shutdown.py` 验证。下载的流式实体、双权限/提交竞争、配额、取消和失败恢复由 `test_plugin_download_api.py` 验证。测试报告区分源码、真实冻结 Worker 和真实桌面主窗口，不把历史版本结果作为当前代码的验证。


## 第二批独立窗口、认证与联机契约

这三项分别声明 `ui.custom.execute`、`auth.provider`、`connector.manage`，不能从基本启用推导。原有 `connector.extend` 仅允许状态补充。权限审阅和六语言能力说明同步展示，撤权使旧运行许可失效。

### 包内独立前端

清单可声明 `custom_ui: {entry_file: "index.html", title: "Example", width: 800, height: 600}`，并须同时声明 `ui.custom.execute`。入口必须是代码快照内的 UTF-8 HTML（至多 512 KiB），资源由作者预先内联；不能使用绝对路径、URL、反斜杠或父目录。每次加载复核实际字节及批准摘要。

Worker 使用 `ui.windows.open({})`，返回 `window_ref`、`ready`、`closing`、`closed`、`error_code`；`ui.windows.get/focus/close({window_ref})` 仅操作自身引用。每次运行至多两个活动窗口、十六个累计引用。关闭与撤权先失效消息准入，再确认原生销毁；禁用等待实际清理。

页面通过可信壳提供的 `ECL.call(method, params)` 异步调用自身命令和设置。窗口不提供账户、通用 IPC、任意路径或主窗口对象；主窗口注入继续遵循其单独契约。专用桥内部使用窗口凭据、运行代次、请求 ID 和变更去重，不接受页面自报安装身份。窗口请求白名单以 `RuntimeCustomUi` 为准。

当前需要维护版 `pytauri-wheel 0.8.0+ecl.2`。真实原生验收覆盖 Windows x64 / WebView2 `156.0.4314.58`、Linux x86_64 / WebKitGTK `2.52.6`、macOS ARM64 / WKWebView build `21624.5.1.11.3`；Windows/macOS 的精确引擎版本由来源清单固定，缺少必需原生控制时在执行插件语句前拒绝。普通主窗口不受插件专用版本门禁影响。构建、许可、网络与来源边界见[原生运行时](../../native/pytauri-runtime/README.md)。示例见 [worker_custom_ui](../../examples/plugins/worker_custom_ui/README.md)。

### 认证与联机

方法、参数、返回模型、预算及原始许可生命周期集中定义于[第三方登录与联机契约](../designs/plugin-auth-and-connector-extension-design.md)。认证只交付本提供方自己的持久数据；联机只操作本来源会话。撤权再授权不复活旧注册或旧引用。会话退出请求不代表实际资源已停止，最终状态以所属任务及清理结果为准。


## 第三批 Worker 平台引导契约

正式入口统一为 `--headless --role plugin --endpoint <本地端点> --host-pid <PID> --run-id <32位运行标识>`。`--endpoint` 同时用于 Windows 命名管道和 POSIX Unix socket，不保留旧参数别名。握手字段 `is_supervised` 表示系统监管条件已建立；宿主仍独立核对内核对端身份、发行入口及监管归属，不信任插件自报 PID 或该布尔值作为授权。

Windows 保留先分配 Job 再恢复进程的路径。POSIX 的 `plugin-guardian` 是不导入插件的内部监管角色，专属会话和控制管道与插件 RPC 分离。运行目录独占创建为 0700，端点固定绑定运行标识；Linux 查询内核 PID/UID，macOS 查询原生 peer PID 和 UID。宿主 EOF 后守护角色仍清理普通后代；停止超时继续持有原始组首进程身份，不提前回收或误报停止。

该边界管理正常插件及其普通进程树，不是恶意 Python 的 OS 沙箱。主动脱离会话/进程组的进程及其他处理器按所属能力另行验收。Windows x64、Linux x86_64、macOS ARM64 的基础源码、原生监管、文件事务与原生自定义窗口门禁已通过真实 runner；处理器、引擎、用例数及原始报告见[跨平台验收](../plans/plugin-posix-files-and-webviews-2026-11-01.md)。不以基础 Worker 可启动推导全部 API 均可用。冻结发行物不由源码用例代替验收，当前不执行 Nuitka 构建。

实现与原始测试范围见[平台适配记录](../plans/plugin-posix-worker-adaptation-2026-11-01.md)。
