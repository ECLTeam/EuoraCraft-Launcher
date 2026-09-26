# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名的 ZIP 归档，不是加密文件。当前实现提供作者侧制作命令和完整性预检；启动器内的安装、Python 依赖和 Worker 运行仍按[实施方案](plugin-dependency-isolation-installation-plan.md)分阶段接入。不要把归档哈希误认为作者身份认证。

## 文件结构

插件代码仍按现有 `plugin.json` 的 `entry_point` 约定放在归档根目录。`package-manifest.json` 由打包命令生成，不能放进插件源目录。

```text
sample.eclplugin
  plugin.json
  main.py
  resources/...
  locks/<target>.txt      # 后续依赖安装阶段使用
  wheels/<target>/*.whl   # 可选，后续离线安装阶段使用
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

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels，后续依赖安装阶段会补充它们的精确契约。
