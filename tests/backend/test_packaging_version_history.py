"""打包版本历史：端到端实证（真跑 build.ps1 / smoke-test.ps1 里的 Python 片段 + PS 守卫）。

背景（任务卡 t_ad5b82f0）：1.0.3 包内 `config/version.json` 只剩 1 条、summary 空，
装机后「检查版本更新」弹窗看不到历史版本 —— 纯静默失效（构建全绿、自检全过）。

本脚本不是复述结论，而是把两处修复**真的跑一遍**：

A. 从 build.ps1 / smoke-test.ps1 **抽取真实片段**（`$pyMerge` / `$pyVerify` / PS 守卫），
   对着真实 PyInstaller 产物形状（`ensure_ascii=False, indent=2`，源码是 index 0）
   跑，断言：条数 == 源码、summary 不丢、current 的时间被刷成本次值。
B. **变异测试**：把片段换成旧的坏行为（`catch { $history = @() }` 静默回退 / 空 summary），
   断言新守卫**必须红** —— 否则这道守卫咬不住本次缺陷。
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_PS1 = ROOT / "packaging" / "build.ps1"
SMOKE_PS1 = ROOT / "packaging" / "smoke-test.ps1"
SRC_VERSION_JSON = ROOT / "config" / "version.json"


def _extract_herestring(path: Path, name: str) -> str:
    """抽出 PowerShell here-string `$name = @' ... '@` 的内容（真实片段，不是抄一份）。"""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"\${name}\s*=\s*@'\r?\n(.*?)\r?\n'@", text, re.S)
    assert m, f"{path.name} 里找不到 here-string ${name}"
    return m.group(1)


MERGE_PY = _extract_herestring(BUILD_PS1, "pyMerge")
VERIFY_PY = _extract_herestring(BUILD_PS1, "pyVerify")


def run_py(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True, encoding="utf-8"
    )


def make_source(versions):
    return {"current": versions[0]["version"], "versions": versions}


def sample_versions():
    return [
        {
            "version": "1.0.3",
            "built_at": "2026-10-15 00:00",
            "commit": "pending",
            "summary": "商品发布链路打通 + 数据包列规则",
            "notes": "要点一\n要点二",
        },
        {
            "version": "1.0.2",
            "built_at": "2026-10-12 00:52",
            "commit": "c8cea23",
            "summary": "两业务线架构分层 + 千帆模块拆包",
            "notes": "侧栏改二级菜单",
        },
        {
            "version": "1.0.1",
            "built_at": "2026-09-29 10:27",
            "commit": "45d5da9",
            "summary": "第一阶段改造收口 + 卡登录页等缺陷修复",
            "notes": "话题体系重构",
        },
        {
            "version": "1.0.0",
            "built_at": "2026-09-24 23:41",
            "commit": "e828052",
            "summary": "首个正式版本：小红书一站式智能运营平台",
            "notes": "账号矩阵",
        },
    ]


class MergeBlock(unittest.TestCase):
    """build.ps1 的 `$pyMerge`：读源码历史 → 刷当前条 → 写包内。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src.json"
        self.dst = self.dir / "dst.json"
        self.src.write_text(json.dumps(make_source(sample_versions()), ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_full_history_and_does_not_drop_summary(self):
        # 真跑 build.ps1 里的片段（version / built_at / commit 三个参数照打包时传）。
        proc = run_py(
            MERGE_PY,
            str(self.src),
            str(self.dst),
            "1.0.3",
            "2026-10-15T19:16:38+08:00",
            "afd944f",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "4")  # 回传历史条数
        out = json.loads(self.dst.read_text(encoding="utf-8"))
        self.assertEqual(out["current"], "1.0.3")
        self.assertEqual([v["version"] for v in out["versions"]], ["1.0.3", "1.0.2", "1.0.1", "1.0.0"])
        head = out["versions"][0]
        self.assertEqual(head["built_at"], "2026-10-15T19:16:38+08:00")  # 刷成本次打包时间
        self.assertEqual(head["commit"], "afd944f")
        # 关键：摘要没被覆盖成空（本次缺陷就是它变空）。
        self.assertEqual(head["summary"], "商品发布链路打通 + 数据包列规则")
        self.assertEqual(head["notes"], "要点一\n要点二")
        # 历史条目的内容一律不动。
        self.assertEqual(out["versions"][1]["summary"], "两业务线架构分层 + 千帆模块拆包")

    def test_returns_history_count_for_the_caller(self):
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "2026-10-15T19:16:38+08:00", "afd944f")
        self.assertEqual(proc.stdout.strip(), str(len(sample_versions())))

    def test_new_version_goes_to_front(self):
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.4", "2026-10-20T10:00:00+08:00", "beef123")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(self.dst.read_text(encoding="utf-8"))
        self.assertEqual([v["version"] for v in out["versions"]], ["1.0.4", "1.0.3", "1.0.2", "1.0.1", "1.0.0"])
        self.assertEqual(out["current"], "1.0.4")

    def test_source_without_history_fails_loudly(self):
        """不复现旧的静默回退：源码没有 versions ⇒ 非 0 退出（build.ps1 会 Fail）。

        ⚠️ 这条曾经是**假的**：旧写法 `d.get("versions") or []` 让 `versions` 缺键
        和空列表都变成 `[]`，随后 `if not history: sys.exit(1)` —— 看起来守卫在，
        但缺键时先 `KeyError`、空列表时才是那句。收成一句后（按字段存在性判），
        两种坏法各自报各自的错，且都**在写入前**退出（不留半个坏文件）。
        """
        self.src.write_text(json.dumps({"current": "1.0.3"}), encoding="utf-8")
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "now", "abc")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.dst.exists(), "停构建时不该留下包内 version.json")

    def test_empty_history_fails_loudly(self):
        """`versions: []` 同样必须停构建 —— 它正是「包内只剩 1 条」的入口。"""
        self.src.write_text(json.dumps({"current": "1.0.3", "versions": []}), encoding="utf-8")
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "now", "abc")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.dst.exists(), "停构建时不该留下半个包内 version.json")

    def test_history_of_non_objects_fails_loudly(self):
        self.src.write_text(json.dumps({"current": "1.0.3", "versions": ["1.0.3"]}), encoding="utf-8")
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "now", "abc")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("非对象项", proc.stderr)

    def test_versions_not_a_list_fails_loudly(self):
        self.src.write_text(json.dumps({"current": "1.0.3", "versions": {"1.0.3": {}}}), encoding="utf-8")
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "now", "abc")
        self.assertNotEqual(proc.returncode, 0)

    def test_unparsable_source_fails_loudly(self):
        self.src.write_text("{ not json", encoding="utf-8")
        proc = run_py(MERGE_PY, str(self.src), str(self.dst), "1.0.3", "now", "abc")
        self.assertNotEqual(proc.returncode, 0)

    def test_real_shipped_version_json_survives_roundtrip(self):
        """对**真实仓库** `config/version.json` 跑一遍（不是造数据）。"""
        proc = run_py(
            MERGE_PY,
            str(SRC_VERSION_JSON),
            str(self.dst),
            "1.0.3",
            "2026-10-15T19:16:38+08:00",
            "afd944f",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        want = json.loads(SRC_VERSION_JSON.read_text(encoding="utf-8-sig"))
        got = json.loads(self.dst.read_text(encoding="utf-8"))
        self.assertEqual(len(got["versions"]), len(want["versions"]))
        self.assertGreaterEqual(len(got["versions"]), 4)  # 1.0.0–1.0.3 基线
        for a, b in zip(got["versions"], want["versions"]):
            self.assertEqual(a["version"], b["version"])
            self.assertEqual(a["summary"], b["summary"])
            self.assertEqual(a["notes"], b["notes"])


class VerifyBlock(unittest.TestCase):
    """build.ps1 的 `$pyVerify`：打包后守卫（条数 + 时间 + 简介）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src.json"
        self.dst = self.dir / "dst.json"
        self.src.write_text(json.dumps(make_source(sample_versions()), ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_dst(self, versions, current="1.0.3"):
        self.dst.write_text(json.dumps({"current": current, "versions": versions}, ensure_ascii=False), encoding="utf-8")

    def _verify(self):
        return run_py(VERIFY_PY, str(self.src), str(self.dst))

    def test_good_bundle_passes(self):
        self._write_dst(sample_versions())
        proc = self._verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")

    def test_1_0_3_shipped_defect_would_now_be_caught(self):
        """变异①：复刻 1.0.3 发出去的坏包（只剩 1 条 + summary 空）⇒ 守卫必须红。"""
        self._write_dst([{"version": "1.0.3", "built_at": "2026-10-15T19:16:38+08:00", "commit": "afd944f", "notes": ""}])
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("历史条数不符", proc.stderr)
        self.assertIn("summary 为空", proc.stderr)

    def test_missing_summary_is_caught(self):
        versions = sample_versions()
        versions[0]["summary"] = ""
        self._write_dst(versions)
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("summary 为空", proc.stderr)

    def test_missing_built_at_is_caught(self):
        versions = sample_versions()
        versions[0]["built_at"] = ""
        self._write_dst(versions)
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("built_at 为空", proc.stderr)

    def test_rewritten_summary_is_caught(self):
        versions = sample_versions()
        versions[0]["summary"] = "被改写过的简介"
        self._write_dst(versions)
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("summary 与源码不一致", proc.stderr)

    def test_current_missing_from_history_is_caught(self):
        self._write_dst(sample_versions(), current="9.9.9")
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("找不到 current", proc.stderr)

    def test_source_missing_summary_is_caught(self):
        versions = sample_versions()
        versions[0]["summary"] = ""
        self.src.write_text(json.dumps(make_source(versions), ensure_ascii=False), encoding="utf-8")
        self._write_dst(versions)
        proc = self._verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("源码", proc.stderr)


class PipingEndToEnd(unittest.TestCase):
    """两段串起来（merge → verify）：真实 index-0 条目也要刷时间且不丢简介。"""

    def test_real_source_through_merge_then_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst.json"
            m = run_py(MERGE_PY, str(SRC_VERSION_JSON), str(dst), "1.0.3", "2026-10-15T19:16:38+08:00", "afd944f")
            self.assertEqual(m.returncode, 0, m.stderr)
            v = run_py(VERIFY_PY, str(SRC_VERSION_JSON), str(dst))
            self.assertEqual(v.returncode, 0, v.stderr)
            out = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(out["versions"][0]["built_at"], "2026-10-15T19:16:38+08:00")
            self.assertTrue(out["versions"][0]["summary"])


class SmokeTestGuardStatic(unittest.TestCase):
    """smoke-test.ps1 的装机判据：必须按**语义**校验，不能只看「文件在不在」。"""

    def setUp(self):
        self.text = SMOKE_PS1.read_text(encoding="utf-8")

    def test_audits_history_against_source(self):
        self.assertIn("$pyVersionAudit", self.text)
        self.assertIn("$shippedCount = & $Py $pyAuditFile", self.text)
        self.assertIn('"config\\version.json"', self.text)

    def test_audit_code_never_travels_on_the_command_line(self):
        """审计代码必须写临时文件再执行，**不许** `-c $pyVersionAudit` / 裸变量。

        2026-10-16 实测两条同源坑：① `-c <含中文的代码>` 被 PS 5.1 按控制台代码页
        编码 ⇒ `SyntaxError: '(' was never closed`；② 裸 `& $Py $code` 把多行代码
        当**文件名** ⇒ `can't open file '...import json...'`。都会让守卫恒红/恒死。
        """
        self.assertNotIn("-c $pyVersionAudit", self.text)
        self.assertIn("[System.IO.File]::WriteAllText($pyAuditFile, $pyVersionAudit", self.text)
        self.assertIn("& $Py $pyAuditFile", self.text)

    def test_asserts_summary_and_built_at_non_empty(self):
        self.assertIn('for field in ("built_at", "summary")', self.text)

    def test_uses_the_scripts_own_interpreter_variable(self):
        """必须用本脚本解析出的 `$Py`；`& $Py` 与调用点形状都要对得上。"""
        self.assertIn("$Py = $null", self.text)
        self.assertIn('$env:SPIDER_XHS_PYTHON', self.text)
        self.assertIn("& $Py $pyAuditFile", self.text)

    def test_guard_runs_before_the_install_is_exercised(self):
        """守卫必须在起服务之前跑（坏包第一时间红，而不是等服务超时）。"""
        self.assertLess(self.text.index("$pyVersionAudit"), self.text.index("Start-Process -FilePath $AppExe"))

    def test_python_missing_is_a_failure_not_a_skip(self):
        """python 缺失 ⇒ Fail（不许 Write-Host [跳过] 静默放过）。"""
        guard_start = self.text.index("版本历史必须")
        guard_end = self.text.index("# --- 3. 启动 + 健康检查")
        guard = self.text[guard_start:guard_end]
        self.assertIn("if (-not $Py) { Fail", guard)
        self.assertNotIn("[跳过]", guard)

    def test_does_not_hardcode_a_version_count(self):
        """不许写死「恰好 N 条」—— 一发版就假红。"""
        self.assertNotRegex(self.text, r"versions\)\.Count\s+-lt\s+\d")
        self.assertNotRegex(self.text, r"\$shippedCount\s+-ne\s+\d")

    def test_does_not_reparse_version_json_with_convertfromjson(self):
        """版本守卫不许**用** PS 的 ConvertFrom-Json 解析 version.json。

        注释里解释「为何不用它」是允许的（典故要留），因此判据落在
        **可执行行**上：守卫段的非注释行里不许出现 ConvertFrom-Json。
        """
        guard_start = self.text.index("版本历史必须")
        guard_end = self.text.index("# --- 3. 启动 + 健康检查")
        guard = self.text[guard_start:guard_end]
        code = "\n".join(ln for ln in guard.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("ConvertFrom-Json", code)
        self.assertIn("$pyVersionAudit", code)


class BuildScriptStatic(unittest.TestCase):
    """build.ps1：不许再有静默回退，且必须调用新守卫。"""

    def setUp(self):
        self.text = BUILD_PS1.read_text(encoding="utf-8")

    def test_no_silent_fallback_on_history(self):
        # 旧写法 `$history = @()` + `catch { $history = @() }` 必须彻底消失
        # （注释里回顾典故不算代码，只查**非注释行**）。
        # ⚠️ 判据要精确到「旧的 `$history` 变量」—— 新代码里的 `$historyCount`
        #    是合法后继，不能用裸 `$history` 子串一棍打死。
        code_lines = [ln for ln in self.text.splitlines() if not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertNotIn("$history ", code)
        self.assertNotIn("$history.", code)
        self.assertNotIn("$history =", code)
        self.assertNotIn("$history)", code)
        self.assertNotIn("$(history", code)
        self.assertNotIn("ConvertFrom-Json", code)
        # 新变量确实在（摘要/输出都用它）。
        self.assertIn("$historyCount", code)

    def test_calls_the_bundle_guard(self):
        self.assertIn("& $Python $pyVerifyFile $srcVersionJson $versionJson", self.text)
        self.assertIn("版本历史自检通过", self.text)

    def test_python_blocks_go_through_temp_files_not_dash_c(self):
        """两段 Python 必须写临时文件再执行：PS 5.1 用 `-c` 传含中文的代码会被
        控制台代码页破坏（实测 `SyntaxError: '(' was never closed`）⇒ 构建必挂。
        裸变量（漏 `-c`）更糟：多行代码被当**文件名**。两条都在这里钉死。"""
        self.assertNotIn("& $Python -c $pyMerge", self.text)
        self.assertNotIn("& $Python -c $pyVerify", self.text)
        self.assertIn('$pyMergeFile = Write-Py', self.text)
        self.assertIn('$pyVerifyFile = Write-Py', self.text)

    def test_temp_python_written_without_bom(self):
        """必须 .NET 写无 BOM：PS 5.1 的 `Set-Content -Encoding utf8` 带 BOM，
        python 读源码时会 `SyntaxError: invalid non-printable character`。"""
        self.assertIn("[System.IO.File]::WriteAllText", self.text)
        self.assertIn("New-Object System.Text.UTF8Encoding $false", self.text)

    def test_writer_does_not_read_outer_scope(self):
        """`Write-Py` 的目录必须**当参数传**：`$script:` / 外层变量在
        `[scriptblock]::Create` 或 dot-source 场景下为空（实测 Join-Path 报空值）。"""
        self.assertIn("function Write-Py", self.text)
        seg = self.text[self.text.index("function Write-Py") : self.text.index("$pyMerge = @'")]
        self.assertIn("Join-Path $Dir $Name", seg)
        self.assertNotIn("$script:", seg)

    def test_temp_dir_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/packaging/.py-tmp/", gitignore)

    def test_no_dangling_history_variable(self):
        """修复漏网的 `$($history.Count)`（变量已删 ⇒ 输出恒空 ⇒ 假报账）。"""
        self.assertNotIn("$history.Count", self.text)

    def test_python_is_configured_before_use(self):
        self.assertLess(
            self.text.index("$Python = $env:SPIDER_XHS_PYTHON"),
            self.text.index("& $Python $pyMergeFile"),
        )

    def test_dangling_history_variable_check_is_specific(self):
        """`$history` 这个旧变量必须彻底消失（新代码只许有 `$historyCount`）。"""
        code = "\n".join(ln for ln in self.text.splitlines() if not ln.lstrip().startswith("#"))
        for bad in ('$history =', '$history)', '$(history', '$history.'):
            self.assertNotIn(bad, code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
