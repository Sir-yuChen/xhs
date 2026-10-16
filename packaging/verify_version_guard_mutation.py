"""变异验证（mutation-verified guards）：新守卫到底咬不咬得住？

三条断言各自**真的红过一次**才算数（skill: mutation-verified-guards）：

1. build.ps1 的 `$pyVerify` 对「包内只剩 1 条」必须红；
2. build.ps1 的 `$pyVerify` 对「summary 空」必须红；
3. smoke-test.ps1 的 `$pyVersionAudit` 对同一个坏包必须红。

本脚本直接调**真实文件里的片段**（不重写一份），在临时目录造好/坏包各一份。
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "build.ps1"
SMOKE = ROOT / "packaging" / "smoke-test.ps1"
SRC = ROOT / "config" / "version.json"


def here(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"\${name}\s*=\s*@'\r?\n(.*?)\r?\n'@", text, re.S)
    assert m, f"{path.name}: 找不到 {name}"
    return m.group(1)


MERGE = here(BUILD, "pyMerge")
VERIFY = here(BUILD, "pyVerify")
AUDIT = here(SMOKE, "pyVersionAudit")


def run(code, *args):
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, encoding="utf-8")


def build_good(tmp: Path) -> Path:
    dst = tmp / "good.json"
    p = run(MERGE, str(SRC), str(dst), "1.0.3", "2026-10-15T19:16:38+08:00", "afd944f")
    assert p.returncode == 0, p.stderr
    return dst


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        good = build_good(tmp)
        src = json.loads(SRC.read_text(encoding="utf-8-sig"))
        n = len(src["versions"])

        # --- 正例：好包必须过两道守卫（否则守卫是恒红的噪音）
        v = run(VERIFY, str(SRC), str(good))
        if v.returncode != 0:
            failures.append(f"正例失守：build 守卫对好包报红 {v.stderr!r}")
        a = run(AUDIT, str(good), str(SRC))
        if a.returncode != 0:
            failures.append(f"正例失守：smoke 守卫对好包报红 {a.stderr!r}")
        if a.stdout.strip() != str(n):
            failures.append(f"smoke 守卫回传条数不对：{a.stdout.strip()!r} != {n}")
        print(f"[正例] 好包（{n} 条）build 守卫 0 / smoke 守卫 0，回传 {a.stdout.strip()} ✓")

        # --- 变异①：复刻 1.0.3 发出去的坏包（只剩 1 条 + summary 空）
        bad = tmp / "bad_1_row.json"
        bad.write_text(
            json.dumps(
                {"current": "1.0.3", "versions": [{"version": "1.0.3", "built_at": "2026-10-15T19:16:38+08:00", "commit": "afd944f", "notes": ""}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for label, code in (("build", VERIFY), ("smoke", AUDIT)):
            r = run(code, str(SRC), str(bad)) if label == "build" else run(code, str(bad), str(SRC))
            if r.returncode == 0:
                failures.append(f"变异① 逃逸：{label} 守卫对「只剩 1 条 + summary 空」的坏包放行")
            else:
                print(f"[变异①] {label} 守卫红了（{len(r.stderr.strip().splitlines())} 行）✓")

        # --- 变异②：条数对但 summary 空（只咬 summary 那一半）
        bad2 = tmp / "bad_no_summary.json"
        payload = json.loads(good.read_text(encoding="utf-8"))
        payload["versions"][0]["summary"] = ""
        bad2.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for label, code in (("build", VERIFY), ("smoke", AUDIT)):
            r = run(code, str(SRC), str(bad2)) if label == "build" else run(code, str(bad2), str(SRC))
            if r.returncode == 0:
                failures.append(f"变异② 逃逸：{label} 守卫对「summary 空」放行")
            else:
                print(f"[变异②] {label} 守卫红了 ✓")

        # --- 变异③：条数对但 built_at 空
        bad3 = tmp / "bad_no_built_at.json"
        payload3 = json.loads(good.read_text(encoding="utf-8"))
        payload3["versions"][0]["built_at"] = ""
        bad3.write_text(json.dumps(payload3, ensure_ascii=False), encoding="utf-8")
        for label, code in (("build", VERIFY), ("smoke", AUDIT)):
            r = run(code, str(SRC), str(bad3)) if label == "build" else run(code, str(bad3), str(SRC))
            if r.returncode == 0:
                failures.append(f"变异③ 逃逸：{label} 守卫对「built_at 空」放行")
            else:
                print(f"[变异③] {label} 守卫红了 ✓")

        # --- 变异④：静默回退（源码无 versions）必须停构建且不留坏文件
        nohist = tmp / "src_nohist.json"
        nohist.write_text(json.dumps({"current": "1.0.3"}), encoding="utf-8")
        out = tmp / "should_not_exist.json"
        r = run(MERGE, str(nohist), str(out), "1.0.3", "now", "abc")
        if r.returncode == 0 or out.exists():
            failures.append("变异④ 逃逸：源码无 versions 时 merge 没停（或留下了坏文件）")
        else:
            print(f"[变异④] merge 对「源码无 versions」停构建且未落文件 ✓（{r.stderr.strip().splitlines()[-1][:40]}…）")

    print()
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("全部变异被咬住，守卫生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
