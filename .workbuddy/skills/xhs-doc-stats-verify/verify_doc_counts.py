"""校验「文档声明的**规模数字**」与「代码/运行时的**实测值**」是否一致。

用法：
    py -3.12 .workbuddy/skills/xhs-doc-stats-verify/verify_doc_counts.py

与 `verify_doc_stats.py` 的分工（**两个脚本查的是两类不同的错，都要跑**）：

| 脚本 | 查什么 | 反例 |
|---|---|---|
| `verify_doc_stats.py` | **表内自洽**：明细 ↔ 合计 | P1 行声明 18 条、各列之和 19 |
| `verify_doc_counts.py`（本脚本） | **文档 ↔ 实测**：声明的规模数字 ↔ 代码事实 | 文档写 147 个端点，实际 155 |

⚠️ **为什么必须单独立一个脚本**：`verify_doc_stats.py` 只做「表内求和比对」，两侧都由同一批
声明值累加而来 —— 所以「文档集体停留在 v1.7 期的旧数字」这类漂移它**结构上就看不见**
（2026-09-21 实测：`docs/README.md` 的事实基线表落后 **28 个端点 / 4 张表 / 204 条测试**，
而 `OVERALL: PASS` 一直是绿的）。

设计要点：

* **测量源是代码，不是文档** —— 端点走 `backend.app.main:app.openapi()`（**离线**，~1.5s，
  不需要 uvicorn 在跑；已实测与线上 `/openapi.json` 一致：135 paths / 155 operations）。
* **不做启发式反推**。曾经想「自动重算前后端接线覆盖率」，但用正则抓 `await http.` 的路径串
  只能解析出 103 条，而人工审计口径是 120 条（差在**动态拼接路径**、**SSE `fetch`**、
  **非 `await` 形态**）—— 用一个偏低的粗算去「更新」文档，等于拿新错数换旧错数。
  所以接线覆盖率**不纳入本脚本**，改由文档显式标注「某日快照、勿当实时值引用」。
* **历史数字不改**：审计轨迹里的旧值（如台账「全量回归 269 passed」那行）是**当时的记录**，
  改了就是篡改历史。本脚本只校「**现状 / 基线**」语义的声明，白名单在 `DECLARATIONS`。

维护方式：新增一处「现状」声明 → 往 `DECLARATIONS` 加一行即可。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

AGENTS = ROOT / "AGENTS.md"
README = ROOT / "docs" / "README.md"
API_DOC = ROOT / "docs" / "02_设计文档" / "05_功能设计_后端API与服务层.md"
MODEL_DOC = ROOT / "docs" / "02_设计文档" / "03_数据模型设计.md"
OVERVIEW_DOC = ROOT / "docs" / "02_设计文档" / "00_设计总览.md"
CODEMAP_DOC = ROOT / "docs" / "03_开发文档" / "02_代码地图与阅读路径.md"
TESTSPEC_DOC = ROOT / "docs" / "04_测试文档" / "01_测试体系与流程规范.md"

API_SRC = ROOT / "frontend" / "src" / "lib" / "api.ts"
MODELS_DIR = ROOT / "backend" / "app" / "models"
PAGES_DIR = ROOT / "frontend" / "src" / "pages"
ROUTER = ROOT / "frontend" / "src" / "app" / "router.tsx"
MAIN_PY = ROOT / "backend" / "app" / "main.py"

TECH_DOC = ROOT / "docs" / "02_设计文档" / "01_技术栈与选型.md"
ARCH_DOC = ROOT / "docs" / "02_设计文档" / "02_系统架构设计.md"
CODEGRAPH_DOC = ROOT / "docs" / "04_测试文档" / "02_代码图谱_codegraph.md"


# --------------------------------------------------------------------------- #
# 实测（唯一真相源 = 代码）
# --------------------------------------------------------------------------- #
def measure() -> dict[str, int]:
    m: dict[str, int] = {}

    from backend.app.main import app  # noqa: E402  (放函数内：import 有副作用时长可控)

    paths = app.openapi()["paths"]
    m["operations"] = sum(len(v) for v in paths.values())
    m["paths"] = len(paths)

    m["routers"] = len(
        re.findall(r"include_router", MAIN_PY.read_text(encoding="utf-8")))

    m["request_functions"] = len(
        re.findall(r"await http\.", API_SRC.read_text(encoding="utf-8")))

    table_names = set()
    for f in sorted(MODELS_DIR.glob("*.py")):
        table_names.update(
            re.findall(r'__tablename__\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8")))
    m["tables"] = len(table_names)

    pages = sorted(PAGES_DIR.rglob("*.tsx"))
    m["pages"] = len(pages)

    router_src = ROUTER.read_text(encoding="utf-8")
    # 「页面文件数」≠「页面路由数」：前者是磁盘上的 .tsx，后者是 router.tsx 真正挂上去的。
    # 两者相差的正是死代码页（当前 23 − 22 = 1，即 `drafts-page.tsx`）。分开测，免得混为一谈。
    m["dead_pages"] = sum(1 for p in pages if p.stem not in router_src)
    m["page_routes"] = len(re.findall(r'from "\.\./pages/', router_src))

    return m


# --------------------------------------------------------------------------- #
# 声明清单（只放「现状 / 基线」语义；**历史记录不在此列**）
#     (文件, 说明, 正则 —— 必须恰好 1 个捕获组 = 数字, 实测值 key)
# --------------------------------------------------------------------------- #
DECLARATIONS: list[tuple[Path, str, str, str]] = [
    # —— AGENTS.md §5（数字口径的唯一权威）——
    (AGENTS, "§5 端点 operations",
     r"\| 后端端点 \| \*\*(\d+)\*\* operations", "operations"),
    (AGENTS, "§5 端点 paths",
     r"\| 后端端点 \| \*\*\d+\*\* operations / \*\*(\d+)\*\* paths", "paths"),
    (AGENTS, "§5 数据表",
     r"\| 数据表 \| \*\*(\d+)\*\* 张", "tables"),
    (AGENTS, "§5 前端请求函数",
     r"\| 前端请求函数 \| \*\*(\d+)\*\*", "request_functions"),
    (AGENTS, "§5 前端页面",
     r"\| 前端页面 \| \*\*(\d+)\*\* 个", "pages"),

    # —— docs/README.md §五 事实基线 + 目录树注释 ——
    (README, "§五 API 规模（端点）",
     r"\| API 规模 \| \*\*(\d+) 个端点\*\*", "operations"),
    (README, "§五 API 规模（path 条数）",
     r"path 条数 \D{0,2}(\d+)", "paths"),
    (README, "§五 前端请求函数",
     r"\| 前端请求函数 \| \*\*(\d+) 个\*\*", "request_functions"),
    (README, "§五 数据表",
     r"\| 数据表 \| \*\*(\d+) 张\*\*", "tables"),
    (README, "§五 前端页面",
     r"\| 前端页面 \| \*\*(\d+)\*\* 个页面文件", "pages"),
    (README, "§五 死代码/孤儿页面",
     r"其中 \*\*(\d+)\*\* 个是死代码", "dead_pages"),
    (README, "目录树 · 数据模型设计注释",
     r"03_数据模型设计\.md\s+(\d+) 张表逐字段", "tables"),

    # —— 其余文档的「现状」语义声明 ——
    (API_DOC, "规模行 · operations",
     r"\*\*(\d+) 个端点\*\*（`create_app\(\)`", "operations"),
    (API_DOC, "规模行 · path 条数",
     r"path 条数 \D{0,2}(\d+)", "paths"),
    (MODEL_DOC, "头部「覆盖」口径 · 模型表数",
     r"模型表 \*\*(\d+)\*\* 张", "tables"),
    (OVERVIEW_DOC, "设计总览 · 数据模型现状",
     r"数据模型设计 \| (\d+) 张模型表现状", "tables"),
    (CODEMAP_DOC, "代码地图 · models 目录注释",
     r"models/\s+(\d+) 张表", "tables"),
    (CODEMAP_DOC, "代码地图 · 入口表表数",
     r"\*\*(\d+) 张表是理解一切业务", "tables"),
    (TESTSPEC_DOC, "测试体系 · P14 覆盖表数",
     r"数据模型与迁移 \| (\d+) 张表", "tables"),

    # —— 01_技术栈与选型 ——
    (TECH_DOC, "技术选型表 · 端点总数",
     r"全项目 \*\*(\d+) 个端点\*\*", "operations"),
    (TECH_DOC, "技术选型表 · router 数",
     r"，(\d+) 个 router", "routers"),
    (TECH_DOC, "技术选型表 · 页面路由数",
     r"(\d+) 个页面路由", "page_routes"),

    # —— 02_系统架构设计（分层图 + 目录树，锚点靠「间距」区分同词多处）——
    (ARCH_DOC, "分层图 · 页面数",
     r"(\d+) 个页面  ·  \d+ 个请求函数", "pages"),
    (ARCH_DOC, "分层图 · 请求函数数",
     r"\d+ 个页面  ·  (\d+) 个请求函数", "request_functions"),
    (ARCH_DOC, "分层图 · router 数",
     r"│  (\d+) 个 router", "routers"),
    (ARCH_DOC, "分层图 · 端点总数",
     r"个 router  ·  (\d+) 个端点", "operations"),
    (ARCH_DOC, "目录树 · 页面路由数",
     r"router\.tsx\s+(\d+) 个页面路由", "page_routes"),
    (ARCH_DOC, "目录树 · 页面文件数",
     r"pages/\s+(\d+) 个页面（详见", "pages"),
    (ARCH_DOC, "目录树 · 请求函数数",
     r"api\.ts\s+(\d+) 个请求函数", "request_functions"),

    # —— 02_代码图谱_codegraph ——
    (CODEGRAPH_DOC, "L5 分层图 · 页面数",
     r"(\d+) 页面 · \d+ 请求函数", "pages"),
    (CODEGRAPH_DOC, "L5 分层图 · 请求函数数",
     r"\d+ 页面 · (\d+) 请求函数", "request_functions"),
    (CODEGRAPH_DOC, "L4 分层图 · router 数",
     r"（(\d+) 个 router", "routers"),
    (CODEGRAPH_DOC, "L4 分层图 · 端点总数",
     r"个 router / (\d+) 端点）", "operations"),

    # —— 02_代码地图与阅读路径（续）——
    (CODEMAP_DOC, "关键文件速查 · FastAPI 装配 router 数",
     r"FastAPI 装配 (\d+) 个 router", "routers"),
    (CODEMAP_DOC, "目录树 · 请求函数数",
     r"★ (\d+) 个请求函数", "request_functions"),
    (CODEMAP_DOC, "目录树 · 页面文件数",
     r"pages/\s+(\d+) 个页面", "pages"),
]


def main() -> int:
    missing = [p for p, *_ in DECLARATIONS if not p.exists()]
    if missing:
        for p in missing:
            print(f"缺少文件：{p}")
        return 2

    m = measure()
    print("实测（来源 = 代码，离线）")
    for k, v in m.items():
        print(f"    {k:<18} {v}")

    cache: dict[Path, str] = {}
    ok = True
    print("\n声明核对")
    for path, label, pattern, key in DECLARATIONS:
        if path not in cache:
            cache[path] = path.read_text(encoding="utf-8")
        hits = re.findall(pattern, cache[path])
        rel = path.relative_to(ROOT).as_posix()
        if len(hits) != 1:
            # 0 命中 = 文档改了写法（脚本要跟着改）；>1 命中 = 锚点不唯一，结论不可信。
            print(f"    ❌ {rel} · {label}")
            print(f"       锚点命中 {len(hits)} 次（期望 1）—— 正则 `{pattern[:60]}` 需要更新")
            ok = False
            continue
        declared = int(hits[0])
        actual = m[key]
        flag = "✅" if declared == actual else "❌"
        if declared != actual:
            ok = False
        print(f"    {flag} {rel} · {label:<28} 声明 {declared:>5} / 实测 {actual:>5}")

    print("\n" + "=" * 60)
    print("OVERALL:", "PASS" if ok else "FAIL")
    if not ok:
        print("提示：改了代码导致规模变化时，**逐域回数**再更新文档（AGENTS.md 硬约束 4）；")
        print("      只改合计 / 只改一处 = 未完成。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
