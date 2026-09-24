# -*- coding: utf-8 -*-
"""把本轮（§21 全量同步）的回归数字与演进追加进 AGENTS.md §5 的「回归」行。

该行极长（>2000 字符），Edit 工具的可读性差，故用脚本按行号精确追加。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"

OLD_COUNT = "| 回归 | **582 passed / 0 failed / 0 errors**（2026-09-22 实测）"
NEW_COUNT = "| 回归 | **583 passed / 0 failed / 0 errors**（2026-09-22 实测）"

TAIL = (
    " → **583**（设计 13 **§21 全量同步**（2026-09-22 用户口径「不要设置翻页上限，应为全量同步，"
    "同步商品类似全量同步」）：① **取消翻页上限** —— `max_pages` 缺省 = 全量，翻到**平台说「没有"
    "下一页」**为止（`has_more=false` / 游标 `-1` / 空页 / 已落库 ≥ total）；新增绝对兜底 "
    "`ABSOLUTE_PAGE_CAP=500` 防死循环（旧名 `MAX_MAX_PAGES=20` 已删）；② **限流从「判定」改「节流」** "
    "—— `CrawlRateLimiter.wait_allow()` 超限**阻塞等到窗口腾出**再继续（⚠️ 只删页数上限而不改这里 "
    "= 每轮仍只跑 ~5 页，问题原样存在）；③ **同步笔记源清单收紧**：www 源**整条退场**（它要的 URL 级 "
    "`xsec_token` **不是平台登录令牌**，是网页版主页 URL 上的查询参数，账号卡片触发时拿不到），"
    "**创作者平台会话成为唯一硬门禁** —— 没有它一律 `CREATOR_SESSION_REQUIRED` + 弹窗提示补齐，"
    "**不退化成 ark 单跑**；④ 商品同步同样全量（`/sync-goods` 是同步端点、仍保持小批次默认）；"
    "⑤ 同步提示文案统一收短。测试：删 5 条旧契约、改 9 条、新增 6 条（变异开关 `needslink`→`wwwleak`，"
    "前端行为脚本 21→18 PASS）；全量 **583 passed / 0 failed / 0 errors**）"
)


def main() -> int:
    lines = AGENTS.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if line.startswith(OLD_COUNT):
            lines[index] = line.replace("**582 passed", "**583 passed", 1).rstrip() + TAIL
            print(f"PATCHED line {index + 1}")
            break
    else:
        raise SystemExit("未找到「回归」行")
    AGENTS.write_text("\n".join(lines), encoding="utf-8")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
