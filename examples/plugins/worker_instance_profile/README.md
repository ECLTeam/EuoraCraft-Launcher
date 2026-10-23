# 实例资料修改示例

先在启动器中分别授予实例读取和具体实例的资料修改许可。读取整个目录不会允许修改其中所有实例，也不会自动包含以后新增实例的写权限。

显式调用discover/scan取得当前运行引用，用inspect取得profile_etag，再向update提供目标、确认的旧版本、保留的mutation_key及要修改的alias、description、favorite或tags_json。false和空标签数组有实际含义，未提供的字段保持原值。加载、卸载及停止不自动修改资料。

update仅返回任务接纳回执。用status查看state、effect和cleanup_pending；提交等待失败先用原键查询mutations.get，不换键重放，不自动读取新版本强行覆盖。只可取消尚未接受提交的自身任务。

原资料名称可能短暂缺失，不承诺外部路径的一次原子覆盖。外部竞争时保留后来文件及原备份，通过启动器“文件恢复 → 实例资料”处理；插件不能调用主窗口恢复管理入口。成功提交不提供旧版本撤销。仅支持Windows x64、本地NTFS及原、新资料均至多64 KiB。


示例声明 `entry_point: main:Plugin`，不在加载阶段扫描或写入。使用主仓库现行打包接口生成归档，例如在仓库根目录执行：

```powershell
python -c "from pathlib import Path; from ECL.plugins.package_archive import build_plugin_package; build_plugin_package(Path('examples/plugins/worker_instance_profile'), Path('build/worker_instance_profile.eclplugin'))"
```

完整输入、错误与恢复契约见 [获准实例资料修改](../../../docs/protocols/eclplugin-package-format.md#获准实例资料修改)。
