# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名的 ZIP 归档，不是加密文件。当前实现提供作者侧制作命令、完整性预检、依赖锁检查和未激活安装准备；插件管理器的 Worker 运行与正式安装入口仍按[实施方案](plugin-dependency-isolation-installation-plan.md)接入。不要把归档哈希误认为作者身份认证。

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
    {"path": "main.py", "sha256": "<64 位小写十六进制 SHA-256>", "size": 123},
    {"path": "plugin.json", "sha256": "<64 位小写十六进制 SHA-256>", "size": 456}
  ],
  "format_version": 1
}
```

文件路径使用相对 POSIX 格式。归档不接受符号链接、显式目录条目、路径穿越、大小写折叠后重名、加密条目或 `package-signature.json`。本版没有签名或验签能力；安装界面接入后将统一显示“来源未验证”。

## Python 依赖锁

`plugin.json` 中的 `pythonDependencies` 是 Python 直接依赖字符串数组，与表示插件间依赖的 `dependencies` 完全独立。若数组非空，当前目标的 `locks/<target>.txt` 必须存在，并锁定每一个直接依赖。目标标签固定为 `<os>-<arch>-cp312`，如 `windows-x86_64-cp312`、`windows-arm64-cp312`、`linux-x86_64-cp312`、`linux-arm64-cp312`、`darwin-x86_64-cp312`、`darwin-arm64-cp312`。归档可只支持部分目标；缺少当前目标锁时不能安装含 Python 依赖的插件。

锁文件是 UTF-8 文本，每个包占一行，列出**全部传递依赖**的精确版本和允许的 wheel SHA-256；可有空行或整行注释，不接受索引配置、任意 URL、版本范围、editable 或源码构建。示例：

```text
example-core==2.1.0 --hash=sha256:<64 位小写十六进制摘要>
example-data==1.3.0 --hash=sha256:<摘要一> --hash=sha256:<摘要二>
```

哈希必须对应最终 wheel 文件，而非解包后内容。`wheels/<target>/` 只允许 `.whl` 文件，缺少的 wheel 在用户允许联网时由环境池按锁定哈希补齐；不允许联网时只用包内 wheel 与共享缓存。当前制作命令不会替作者解析依赖或生成锁，作者需先准备完整锁和目标 wheel。即使依赖准备成功，正式插件激活仍须等待 Worker/安装事务接入。

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels。
