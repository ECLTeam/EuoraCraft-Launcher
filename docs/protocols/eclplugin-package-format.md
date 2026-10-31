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

清单只接受 `instances.read`（查看游戏）、`instances.manage`（管理游戏）和 `network.read`（联网）。管理游戏包含查看、安装、资料修改、启动和停止本运行创建的游戏；游戏授权统一选择根目录，覆盖其中现有及以后新增的实例。只读插件不能据此管理游戏。需要只读降级的插件可以同时声明必要查看和可选管理，管理生效时包含同一范围的查看能力。

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

仅支持 Windows x64、本地 NTFS。宿主固定原目录和文件对象，保留原对象备份后不覆盖发布，原资料名称可能短暂缺失，不承诺一次原子覆盖。实际占用和备份清理完成前保持 cleanup_pending；重启只恢复或清理登记对象，不恢复旧 Worker、旧任务或旧补丁。外部文件占名时保留双方数据，从“文件恢复 → 实例资料”选择原位置恢复、受限后缀另存为 `recovered-profile-{suffix}.json`，或经二次确认丢弃原备份。成功提交不提供旧版本撤销。

仅可信主 WebView 可调用 `plugin_instance_profile_recoveries_list({cursor?,limit?})` 和 `plugin_instance_profile_recovery_resolve({recovery_ref,expected_revision,expected_etag,action,saved_suffix?})`；Worker、其他窗口及 Dev Channel 均被拒绝。action 为 restore/save_as/discard，save_as 必须提供 1—64 位 ASCII 字母、数字、下划线或连字符后缀，其余动作不接受后缀。查询 DTO 为 `{items:[{recovery_ref,name,revision,etag,state,actions}],next_cursor}`，不含主机路径或原始资料；处理返回应用用户任务回执，关闭弹窗不取消已接纳任务。孤立安装和已移出配置的原目录仍可恢复，但实际物理身份必须一致。

示例见 [worker_instance_profile](../../examples/plugins/worker_instance_profile/README.md)，边界及验收见 实例资料修改方案。

### 自身任务控制

`operations.get({operation_ref})`、`operations.list({cursor?,limit?})` 和 `operations.cancel({operation_ref})` 只操作当前安装、内容和运行自己的任务。控制自身已有任务不需要重新授予业务权限；结果必须具有创建时显式绑定的结果准入，并在交付时逐次复核，失权后 result 为 null。FileView必须有独立读取许可，删除的无内容确认绑定原删除许可；写许可不能代替读许可。不能传入宿主 operationId、owner、路径或其他 run；跨运行引用统一拒绝。取消需要本 run 的 mutation_key，HostClient.call 默认生成；同键重复调用不会重复副作用。

TaskView 返回 `operation_ref,kind,state,percent,stage,message,can_cancel,cancellation_requested,effect,cleanup_pending,result,error_code`。state 为 queued、running、committing、completed、failed 或 cancelled。cleanup_pending 为布尔值，非终态为 false；终态仍有实际 future、领域资源或审计未收尾时为 true。查询和监管只重试清理、观察及保存，不重放业务。取消返回 `{requested,state,effect}`，requested 只表示已请求，实际停止要继续查询。提交已接受时取消失败 `PLUGIN_COMMIT_STARTED`，撤销或 Worker 退出不把随后成功或真实错误改成取消。stage/message 为安全阶段说明，不交付原始日志、路径或异常。

列表 limit 为 1—100、默认 50；每页受 64 KiB 限制，游标最多 16 个快照、120 秒，绑定本 run 和 limit。快照固定引用集合，每页重新查询状态和过滤结果。每 run 最多 128 个引用，宿主同时跟踪至多 1024 个插件任务；容量耗尽明确拒绝，不淘汰活跃任务。关闭 run 后只有实际 future 和审计保存完成才释放记录。

不开放通用 `operations.submit` 或安装任务创建接口。具体业务入口为 instances.profile_patch、files.write_commit、files.delete 与下文 downloads.submit；未提交任务的插件列表可以为空。设置短写保持现行同步结果和 mutations.get 回执。任务控制基础见 任务归属与提交控制方案。

## 私有文件与新建事务

私有文件属于启用后可用的基础能力，不在清单中声明额外文件权限。通过 `resources.list({capability:"plugin.base"})` 取得本安装的私有数据根引用，随后调用现有文件 API。读取、新建、替换和删除共用原始基础准入；新建仍禁止覆盖，替换仍要求目标版本。范围固定为自身空间，不允许创建子目录或访问用户游戏目录。实际文件事务仅支持 Windows x64、本地 NTFS。

先调用 resources.list，capability 为上述独立文件能力，取得当前 run 的 root_ref。FileRef 为 `{root_ref,relative_path}`，文件名为 1—120 个 ASCII 字符，须以字母或数字开始，后续仅允许字母、数字、点、下划线和连字符；拒绝设备名、尾点和路径组件。空名称只用于私有根 stat/list。引用不能跨 run/授权代次重用，任何接口都不返回宿主路径。

读取使用 files.stat、files.list、files.read。read 的 count 为 1—32768，结果为 Base64、eof 和完整内容与物理身份绑定的 etag；expected_etag 变化时明确失败。FileView 时间字段为 modified_ms。读取不会初始化首次空间；损坏或丢失的已有空间不能当作空空间重建。

新建按 files.write_begin `{target,total_bytes,overwrite:false}`、files.write_chunk `{write_ref,offset,data_b64}`、files.write_commit `{write_ref,sha256}` 执行。单文件至多 8 MiB，原始块至多 32 KiB；严格 Base64、连续偏移、完整 SHA-256。零长度文件省略 chunk。begin/chunk/commit 需要稳定 mutation_key 才能重试同一请求，HostClient.call 默认每次生成新键；重试须保存原键。

commit 返回 `{operation_ref,state:"queued"}`，表示已接纳；实际结果通过 operations.get/list 查询。发布始终原生不覆盖，目标突现时保留外部文件。任务结果只在提交时和读取时都有有效读许可时交付。提交等待超时后先查 mutations.get，不能将超时解释为任务未创建。

files.write_abort `{write_ref}` 不消耗变更回执，失去可选文件权限后仍能清理自己的上传。交给任务后返回 released:false，取消改用 operations.cancel；已接受提交不再取消。清理失败、实际线程未结束或审计未完成时保持 pending，不提前释放配额或允许重新启用。

同一安装更新保留数据但使旧 run 引用失效；重新安装获得新的私有空间，旧文件保留。空间总量 64 MiB，目录最多 1024 项，每 run 最多 4 个上传及 32 MiB 预留，宿主总上传预留 128 MiB；空闲上传 60 秒过期。未登记的崩溃残留计入空间，不自动恢复或删除；替换登记事务按下节恢复。参见 完整协议与验收 和可安装的 [worker_snapshot 示例](../../examples/plugins/worker_snapshot/README.md)。

### 按版本删除私有文件

`files.delete({target:FileRef,expected_etag:64位小写十六进制})` 使用本运行基础准入，无独立删除开关。目标必须是当前私有根内非空名称的普通单链接文件，至多8 MiB。拒绝目录、ADS、链接、递归、force、额外字段和真实路径；目标缺失返回 PLUGIN_RESOURCE_NOT_FOUND，超大返回 PLUGIN_FILE_TOO_LARGE。根发现和删除均不初始化首次空间。

RPC最多5秒只登记名称占用并返回 `{operation_ref,state:"queued"}`；任务 plugin_file_delete 从本次提交起共30秒，包含排队。上传、下载和删除共用大小写不敏感目标占用，冲突为 PLUGIN_FILE_BUSY。每run至多4个、宿主32个未释放删除资源，与上传字节预留独立。

执行时相对原根独占打开目标，在同一句柄计算完整SHA-256并核对身份、长度、修改时间和expected_etag；同名同内容重建也拒绝。原lease、取消及截止均在最终接受前复核，先保存committing审计，再设置内核删除标记。接受前取消/失权/审计失败保留原文件。标记或关闭失败只有原句柄撤回标记并确认后才报告零效果；无法确认则保留unknown和待清理资源。

成功必须确认原对象目录项已消失，最终仅返回 `{deleted:true}`。该无内容确认绑定原基础准入，失权后result为null，effect不改。自身CloseHandle成功不代表实际删除；外部元数据句柄可能延迟移除，超时返回 PLUGIN_FILE_DELETE_PENDING、effect=unknown、cleanup_pending=true。内核删除仍可能晚到完成；后续只观察原对象身份，不重开名称补删，不触及同名新对象。实际清理完成可以清除cleanup_pending，但不把unknown改报成功。

这是直接删除，成功后不可恢复，不进入回收站，不提供跨重启恢复。提交等待超时使用原mutation_key查询mutations.get；接受后不可取消。未释放资源阻止重新启用，暂存清理同样等待已知原对象实际消失才归还名额。参见 实施与验收 和 [显式清理示例](../../examples/plugins/worker_file_cleanup/README.md)。

### 按版本替换私有文件

`files.replace_begin({target:FileRef,expected_etag,total_bytes})` 使用本运行基础准入，返回与新建相同的 `{write_ref,max_chunk_bytes}`。随后使用既有write_chunk、write_commit、write_abort；上传始终绑定原始基础准入，运行或代次失效后不能复活旧上传。原、新文件均至多8 MiB，分块32 KiB；不接受overwrite、force或真实路径。begin不初始化缺失空间或目标。已有文件和新暂存同时计入配额。

commit创建plugin_file_replace任务，从提交起共30秒，包含排队。执行时从原句柄完整核对原对象与expected_etag，最终接纳前复核原lease、取消、截止及审计。先持久保存事务凭据，再把原对象不覆盖移到内部备份，最后不覆盖发布新暂存。**原名称可能短暂不存在，不承诺一次原子覆盖。** 新建与下载继续禁止覆盖。

接受前失败保留原文件；接受后按真实结果报告effect。失败若原名称仍空缺，只按原对象不覆盖恢复；后来文件占用名称时保留外部文件及原备份，effect保持unknown，cleanup_pending保持true。旧备份、暂存、记录和实际句柄全部结束后才释放名称及预留；外部元数据句柄延迟删除时，新文件可以正常读取，清理仍保留pending。成功替换不提供历史撤销。

重启仅处理有严格凭据的登记事务，核对原根、卷和文件ID及完整SHA-256；不继续旧上传，不恢复旧run控制引用，不扫描删除未知残留。NTFS复用名称可能继承原创建时间，替换恢复只识别本次新对象的明确创建时间过渡，不放宽公开FileRef版本规则。无法核实的记录或对象保留，并阻止相关安装重新启用、覆盖登记或卸载。

可信主窗口“文件恢复”可查看孤立安装记录，选择仅在缺失时恢复原位置、按明确新名称另存原文件，或二次确认永久丢弃原备份。`plugin_file_recoveries_list({cursor?,limit?})` 默认50、最多100项；返回当前宿主opaque recovery_ref、revision、etag、状态及可执行动作，不返回宿主路径和内部名称。状态为pending、conflict、cleanup或invalid；后两者无破坏性操作。

`plugin_file_recovery_resolve({recovery_ref,expected_revision,expected_etag,action,saved_name?})` 仅原生注册主WebView可调用，返回应用任务回执。action为restore/save_as/discard，只有save_as可且必须提供saved_name。执行前重新核对记录及实际文件，同一审阅选择返回同一任务；失败后刷新取得新版本才可再次尝试。关闭弹窗不停止已接纳任务，成功提示以实际任务终态为准。Worker和Dev Channel均不能调用恢复管理入口。

示例见 [worker_file_replace](../../examples/plugins/worker_file_replace/README.md)，设计及验收见 替换实施方案。

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

安装只支持Windows x64本地NTFS。新实例名称须为安全的单个目录组件，不允许路径、ADS、设备名、尾点或空白归一化。原生目录句柄固定游戏根和目标身份；同一物理根及名称的占位由界面安装、整合包基础安装和插件共用，实际线程与句柄清理完成前不释放。已有用户实例拒绝覆盖；失败文件原地保留，只允许同一运行对同一物理身份及相同安装请求继续。插件关闭不删除文件，新运行不接管旧残留。写入开始后失败如实为 `effect:"partial"`，完整安装完成才为 `committed`。

用户与插件共用单个启动准备占位，冲突返回忙。异步账户和下载准备沿用宿主既有事件循环，进程创建是短提交点，之前再次核对原始许可、取消、目录身份和当前账户；之后撤权或关闭插件不会杀游戏。准备可能已经补全游戏文件，即使未创建进程也可能是partial。已创建游戏仍在启动器正常运行列表中；停止使用有限等待，并在未确认退出时返回 `PLUGIN_GAME_STOP_TIMEOUT`。重复停止已退出的自身进程成功且保留退出码。

常见失败包括 `PLUGIN_SCHEMA_INVALID`、`PLUGIN_GAME_VERSION_INVALID`、`PLUGIN_LOADER_VERSION_INVALID`、`PLUGIN_INSTALL_BUSY`、`PLUGIN_LAUNCH_BUSY`、`PLUGIN_HOST_NOT_READY`、`PLUGIN_FILE_EXISTS`、`PLUGIN_OWNER_MISMATCH`，其余沿用授权、资源、任务和安装错误。参见[安装示例](../../examples/plugins/worker_game_install/README.md)、[启动示例](../../examples/plugins/worker_game_launch/README.md)及[实施方案](../plans/plugin-game-install-launch-plan-2026-10-25.md)。

## 产物验证

游戏目录与授权边界由 `tests/test_plugin_game_queries.py` 验证；安装、共享占位及结果读取由 `test_plugin_game_install.py` 验证；启动、进程所有权、撤权竞争及停止由 `test_plugin_game_launch.py` 验证。后两者包含公开示例经过真实源码与冻结Worker的RPC用例，完成记录见[游戏操作实施方案](../plans/plugin-game-install-launch-plan-2026-10-25.md#8-验证与交付)。

真实冻结引导、依赖和生命周期用例位于 `tests/test_plugin_worker_bootstrap.py`、`test_plugin_worker_dependencies.py`、`test_plugin_worker_lifecycle.py`、`test_plugin_operation_worker.py`、`test_plugin_network_worker.py` 和 `test_plugin_download_worker.py`；正式管理器流程位于 `test_plugin_worker_manager.py`、`test_plugin_manager_packages.py` 与 `test_packaged_plugin_directories.py`。网络策略、真实流、日志和解释器退出另由 `test_plugin_network_targets.py`、`test_plugin_network_api.py`、`test_plugin_network_logging.py`、`test_plugin_network_shutdown.py` 验证。下载的流式实体、双权限/提交竞争、配额、取消和失败恢复由 `test_plugin_download_api.py` 验证。测试报告区分源码、真实冻结 Worker 和真实桌面主窗口，不把历史版本结果作为当前代码的验证。
