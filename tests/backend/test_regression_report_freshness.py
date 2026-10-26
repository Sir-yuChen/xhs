"""测试判据产物校验（docs/04_测试文档/09 §3.2 落地）。

## 为什么需要这条

2026-10-26 实测两次：pytest 跑到一半被系统 / 残留进程挤死（PG `too many clients`
→ worker 崩），随后：
* **xml 根本没生成**；
* 退出码变成 **127**（`command not found` 的经典码，与「测试失败」毫无关系）；
* stdout 里只剩一堆点与 `F`。

人（和 AI）的第一反应都是「代码大面积坏了」，实际一行代码都没错 ——
当年就因此白查了两轮、误报「17 个失败」。

## 判据

`run_regression.py` 是**唯一的全量回归入口**，它保证三件事：
1. 开跑前删旧 xml（杜绝「读到上一轮的报告」）；
2. 跑完断言 xml 存在且 mtime >= 开跑时间；
3. 缺产物时打印明确结论并以 **exit 2** 退出（不是 127，也不会被当成测试失败）。

本文件对应的守卫是 :func:`test_xml_freshness_helper_is_load_bearing`。
"""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET

import pytest

REG_XML = pytest.importorskip("pathlib").Path(__file__).resolve().parents[2] / ".hermes" / "reports" / "reg.xml"


def parse_four_numbers(xml_path) -> dict[str, int] | None:
    """读 xml 的四个数（tests / failures / errors / skipped）。

    :returns: 成功返回 dict；文件不存在或不完整返回 ``None``
        （**不抛异常** —— 调用方要能区分「没产物」与「有产物但有失败」）。
    """
    try:
        root = ET.parse(str(xml_path)).getroot()
    except (FileNotFoundError, ET.ParseError):
        return None
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return None
    return {
        "tests": int(suite.get("tests") or 0),
        "failures": int(suite.get("failures") or 0),
        "errors": int(suite.get("errors") or 0),
        "skipped": int(suite.get("skipped") or 0),
    }


def test_parse_four_numbers_returns_none_for_missing_file(tmp_path):
    """① 文件不存在 ⇒ `None`，**不抛异常**（这是「没产物」而非「失败」）。"""
    assert parse_four_numbers(tmp_path / "nope.xml") is None


def test_parse_four_numbers_returns_none_for_broken_file(tmp_path):
    """② 文件残缺（跑到一半被杀）⇒ `None`。"""
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite tests='3' failures='0'", encoding="utf-8")
    assert parse_four_numbers(bad) is None


def test_parse_four_numbers_reads_a_real_report(tmp_path):
    """③ 正常报告 ⇒ 四个数读出来。"""
    good = tmp_path / "good.xml"
    good.write_text(
        '<testsuite tests="10" failures="2" errors="1" skipped="3"></testsuite>',
        encoding="utf-8",
    )
    assert parse_four_numbers(good) == {
        "tests": 10, "failures": 2, "errors": 1, "skipped": 3,
    }


def test_xml_freshness_helper_is_load_bearing():
    """④ 全量回归脚本必须**先删旧 xml、再校验产物**。

    ⚠️ 这条守的是「判定链」本身：脚本若忘了删旧 xml，读到的可能是上一轮的报告
    （本轮实测：读到 17:18 的旧报告，误报 17 个失败）。
    """
    script = REG_XML.parents[2] / ".hermes" / "scripts" / "run_regression.py"
    assert script.exists(), f"缺全量回归入口 {script}"
    src = script.read_text(encoding="utf-8")
    #: 必须删除旧产物（unlink 或 rm）
    assert "unlink" in src or "rm " in src, "脚本没删旧 xml ⇒ 可能读到上一轮报告"
    #: 必须校验产物新鲜度
    assert "mtime" in src or "stat()" in src, "脚本没校验 xml 是否本轮产物"
    #: 缺产物必须是明确的码，不能沿用 127（会被误读成「测试失败」）
    assert "exit(2)" in src or "SystemExit(2)" in src, (
        "缺产物时要 exit(2)；用 127 会被误读成测试失败（本轮实测踩过）"
    )
