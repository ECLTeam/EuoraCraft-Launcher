# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 crash 堆栈分析器的自动化测试。
#
# 公开接口：
#   - test_stack_summary_selects_primary_frame_and_packages() -> None
#   - test_lambda_and_init_frames_are_normalized() -> None
#   - test_ignored_prefixes_are_skipped() -> None
# ============================================================

from __future__ import annotations

from ECL.services.game.crash.stacks import CrashStackAnalyzer


def test_stack_summary_selects_primary_frame_and_packages() -> None:
    text = "\n".join(
        [
            "java.lang.RuntimeException: boom",
            "\tat java.base/java.lang.Thread.run(Thread.java:833)",
            "\tat dev.example.crasher.Entry.hit(Entry.java:1)",
            "Caused by: java.lang.IllegalStateException: inner",
            "\tat org.spongepowered.mixin.Foo.bar(Foo.java:2)",
            "\tat net.examplemod.core.Deep.dive(Deep.java:3)",
        ]
    )

    summary = CrashStackAnalyzer().analyze(text)

    assert summary.primary is not None
    assert summary.primary.class_name == "dev.example.crasher.Entry"
    assert summary.primary.method == "hit"
    assert summary.packages == ("dev.example.crasher", "net.examplemod.core")
    assert len(summary.frames) == 2


def test_lambda_and_init_frames_are_normalized() -> None:
    text = "\n".join(
        [
            "\tat com.example.Foo.lambda$bar$0(Foo.java:10)",
            "\tat com.example.Baz.<init>(Baz.java:5)",
        ]
    )

    summary = CrashStackAnalyzer().analyze(text)

    assert summary.frames[0].class_name == "com.example.Foo"
    assert summary.frames[0].method == "lambda$bar$0"
    assert summary.frames[0].package == "com.example.Foo"
    assert summary.frames[1].class_name == "com.example.Baz"
    assert summary.frames[1].method == "<init>"


def test_ignored_prefixes_are_skipped() -> None:
    text = "\n".join(
        [
            "\tat java.base/java.lang.Thread.run(Thread.java:833)",
            "\tat net.minecraftforge.fml.Crash.report(Crash.java:1)",
        ]
    )

    summary = CrashStackAnalyzer().analyze(text)

    assert summary.frames == ()
    assert summary.packages == ()
    assert summary.primary is None
