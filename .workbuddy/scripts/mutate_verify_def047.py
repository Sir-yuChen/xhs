"""变异验证：把 DEF-047 的两个成因分别注回去，守卫必须变红。

DEF-047 是一条**时序**缺陷（详情正文到达后挂载商品变样），所以它有两条腿：
  1) 后端详情走 www feed，payload 结构上没有商品容器 → `goods` 恒 `None`
  2) 前端裸展开合并，让这个 `None` 盖掉列表行已补齐的那一份
两条腿都要有守卫，且都要能变红——只钉住其中一条，另一条回归时无人发现。

用法：python .workbuddy/scripts/mutate_verify_def047.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"

PAGE = ROOT / "frontend/src/pages/platforms/xhs/my-notes-page.tsx"
CSS = ROOT / "frontend/src/global.css"
PC = ROOT / "backend/app/api/platforms/xhs/pc.py"

PAGE_TESTS = "tests/backend/test_page_optimizations.py"
API_TESTS = "tests/backend/test_self_content.py"

# 守卫名（供断言引用）
T_MERGE = "test_note_detail_merge_keeps_the_goods_the_list_already_had"
T_ONE = "test_note_detail_renders_one_and_only_one_goods_block"
T_TITLE = "test_note_detail_goods_title_is_single_line_with_tooltip"
T_ROW = "test_goods_variant_and_price_share_one_row"
T_API = "test_note_detail_returns_the_same_goods_the_list_carries"

# (说明, 文件, 原文, 替换为, 期望变红的用例)
MUTATIONS = [
    # -- 前端：合并丢商品（这次报出现象的直接成因） --------------------------
    (
        "详情缺 goods 时不再回退到列表行（DEF-047 原样注回）",
        PAGE,
        "goods: detail.goods ?? note.goods ?? null",
        "goods: detail.goods ?? null",
        (PAGE_TESTS, T_MERGE),
    ),
    (
        "goods 一行挪到 `...detail` 之前（看着还在，运行时被覆盖）",
        PAGE,
        "    ...detail,\n    goods: detail.goods ?? note.goods ?? null,",
        "    goods: detail.goods ?? note.goods ?? null,\n    ...detail,",
        (PAGE_TESTS, T_MERGE),
    ),
    (
        "渲染改回直接遍历 shop.goods（第二块商品、第二套版式）",
        PAGE,
        "{mountedGoods.map((g) => (",
        "{(noteForDetail?.shop?.goods ?? []).map((g) => (",
        (PAGE_TESTS, T_ONE),
    ),
    # -- 前端：版式要求（单行省略 / 规格价格同行） --------------------------
    (
        "商品标题去掉 `…` + tip（长标题会换行撑高卡片）",
        PAGE,
        "ellipsis={{ tooltip: g.title || \"（无商品标题）\" }}",
        "",
        (PAGE_TESTS, T_TITLE),
    ),
    (
        "规格去掉 `…` + tip",
        PAGE,
        "ellipsis={{ tooltip: g.variant }}",
        "",
        (PAGE_TESTS, T_ROW),
    ),
    (
        "价格允许换行（脱离同行版式）—— 必须改动**价格块**内那一处",
        PAGE,
        '                                        fontSize: 14,\n                                        flex: "0 0 auto",\n                                        whiteSpace: "nowrap",',
        '                                        fontSize: 14,\n                                        flex: "0 0 auto",\n                                        whiteSpace: "normal",',
        (PAGE_TESTS, T_ROW),
    ),
    (
        "同行容器改回块级（规格与价格变成两行）",
        CSS,
        ".goods-meta-row {\n  display: flex;",
        ".goods-meta-row {\n  display: block;",
        (PAGE_TESTS, T_ROW),
    ),
    (
        "flex 子项不再允许被压窄（规格省略不掉，把价格挤出容器）",
        CSS,
        ".goods-meta-row > :first-child {\n  min-width: 0;",
        ".goods-meta-row > :first-child {\n  min-width: 200px;",
        (PAGE_TESTS, T_ROW),
    ),
    # -- 后端：详情商品回填（另一条腿） ------------------------------------
    (
        "详情不再回填商品（goods 恒 None → 前端一进详情就丢）",
        PC,
        '            if not normalized.get("goods") and shop.get("goods"):',
        '            if False:',
        (API_TESTS, T_API),
    ),
    (
        "详情改用另一套字段名合成商品（两份实现 → 形状不一致）",
        PC,
        '                    str(first.get("variant_desc") or ""),',
        '                    str(first.get("spec_desc") or ""),',
        (API_TESTS, T_API),
    ),
]


def run(test_file: str, test_name: str) -> bool:
    """返回 True 表示用例通过。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", f"{test_file}::{test_name}", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (PAGE, CSS, PC)}
    failures: list[str] = []

    try:
        for label, path, old, new, (test_file, test_name) in MUTATIONS:
            original = originals[path]
            if old not in original:
                print(f"[SKIP] {label} —— 原文没找到：{old!r}")
                failures.append(label)
                continue
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                passed = run(test_file, test_name)
            finally:
                path.write_text(original, encoding="utf-8")
            status = "红 ✅" if not passed else "还是绿的 ❌（守卫无效）"
            print(f"[{status}] {label}\n           → {test_name}")
            if passed:
                failures.append(label)
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    # 复原后再确认一次基线仍是绿的（防止脚本自己把文件改坏）。
    baseline = run(PAGE_TESTS, T_MERGE) and run(API_TESTS, T_API)
    print(f"\n复原后基线：{'绿 ✅' if baseline else '红 ❌'}")
    print(f"变异验证：{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} 条按预期变红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
