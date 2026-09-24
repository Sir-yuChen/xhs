# -*- coding: utf-8 -*-
"""同步门禁迁移到「creator 强制 + www 退场」（设计 13 §21.4）后的三处收尾。

一次性脚本，跑完即弃。
1. tests/frontend/sync_gates.cjs —— 变异锚点 + A/B/C 段断言
2. tests/backend/test_page_optimizations.py —— 源码守卫 + 参数化变异名 + PASS 计数
3. tests/backend/test_note_content_backfill.py —— 限流改等待制（注入拒绝型限流器）
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CJS = ROOT / "tests/frontend/sync_gates.cjs"
OPS = ROOT / "tests/backend/test_page_optimizations.py"
BACKFILL = ROOT / "tests/backend/test_note_content_backfill.py"

# --------------------------------------------------------------------------- #
# 1) sync_gates.cjs
# --------------------------------------------------------------------------- #
CJS_PAIRS = [
    (
        '  blocked: "    blocked: sources.length === 0,",',
        '  blocked: "    blocked: !creatorOk,",',
    ),
    (
        '  needslink: "    needsLink: wwwOk && !creatorOk,",',
        '  wwwleak: "  if (creatorOk) {",',
    ),
    (
        '  // 全不可用也放行 ⇒ 门禁没了，用户点了才由后端吼\n  blocked: "    blocked: false,",',
        '  // 创作者会话不可用也放行 ⇒ 硬门禁失效，用户点了才由后端吼\n  blocked: "    blocked: false,",',
    ),
    (
        '  // PC 账号不再先录主页链接 ⇒ www 源必定 XSEC_TOKEN_REQUIRED\n  needslink: "    needsLink: false,",',
        '  // www 泄漏进源清单（creator 不可用也列源）⇒ 退化成旧的三源契约\n  wwwleak: "  if (true) {",',
    ),
]

AB_OLD_START = "  // A. **全不可用必须拦住**"
C_END_MARK = "  // D. 同步商品门禁（§19.4）"

ABC_NEW = '''  // A. **创作者平台会话是唯一硬门禁**（§21.4，2026-09-22 用户口径：
  //    「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台
  //    登录会话的提示补齐后再触发」）
  check(
    "A1 creator 不可用 ⇒ blocked（哪怕 www 是 valid）",
    noteSyncPlan(channels({ www: "valid", creator: "missing", ark: "valid" }), "creator").blocked,
    true
  );
  check(
    "A2 guest / expired 也算确定不可用 ⇒ blocked",
    noteSyncPlan(channels({ www: "valid", creator: "expired", ark: "valid" }), "creator").blocked,
    true
  );

  // B. **unknown 放行**（DEF-057：没查到 ≠ 不可用）
  check(
    "B1 creator unknown ⇒ 放行（让同步真发一次）",
    noteSyncPlan(channels(unknown), "creator").blocked,
    false
  );

  // C. 源清单（§21.4：**www 源已退场**，creator 强制）
  check(
    "C1 只 creator 可用 ⇒ sources=[creator]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "expired" }), "creator"),
    { blocked: false, needsLink: false, sources: ["creator"] }
  );
  check(
    "C2 creator + ark 可用 ⇒ sources=[creator, ark]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "valid" }), "creator").sources,
    ["creator", "ark"]
  );
  check(
    "C3 **www 永不参与**：creator 不可用 ⇒ blocked + 空 sources（www valid 也一样）",
    noteSyncPlan(channels({ www: "valid", creator: "missing", ark: "valid" }), "creator"),
    { blocked: true, needsLink: false, sources: [] }
  );
  check(
    "C4 needsLink 恒 false（主页链接录入窗已停用）",
    noteSyncPlan(channels({ www: "valid", creator: "valid", ark: "valid" }), "pc").needsLink,
    false
  );

'''

# --------------------------------------------------------------------------- #
# 2) test_page_optimizations.py
# --------------------------------------------------------------------------- #
OPS_OLD_FUNC = '''def test_note_sync_requires_profile_url_on_pc():
    """PC 账号点「同步笔记」必须先拿到主页链接（设计 13 §6.1.1 / §19.3）。

    没有这条，PC 账号在 UI 上**永远**同步不了：`user_posted` 要的是 URL 级
    `xsec_token`，而账号 Cookie 拿不到它 → R1「所有账号支持同步笔记」只在后端成立。

    §19 起「要不要链接」的判定搬进了 `lib/sync-gates.ts::noteSyncPlan`
    （needsLink = wwwOk && !creatorOk）；页面只保留接线：plan.needsLink ⇒ 链接弹窗。
    """
    source = _read(ACCOUNTS)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")
    assert "noteSyncPlan(" in source
    # 页面接线：needsLink 时必须打开链接录入弹窗，并把 URL 带给同步
    note_handler = _slice_between(source, "function handleNoteSyncClick", "function handleGoodsSyncClick")
    assert "plan.needsLink" in note_handler and "setLinkAccount(account)" in note_handler
    assert "startNoteSync(account.id, url)" in source, "链接必须随本次请求带进同步"
    assert "profileUrl" in source
    assert "xsec_token" in source, "弹窗必须说明要的是主页链接里的令牌"
    # 判定本体在 gates 里：creator 源可用时不弹（www 是冗余源），不可用时要弹
    assert "needsLink: wwwOk && !creatorOk" in gates
    # 链接只随本次请求走：不得持久化（令牌会过期，存下来就是第二套真相源）。
    assert "localStorage.setItem" not in source
    assert "sessionStorage" not in source
'''

OPS_NEW_FUNC = '''def test_note_sync_gate_is_creator_only():
    """「同步笔记」的门禁**只认创作者平台会话**（设计 13 §21.4，2026-09-22 用户口径）。

    来源：「www 源在这里是冗余的，则移除不要，使用 creator 源」+
    「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台登录
    会话的提示补齐后再触发」。

    本条守卫三件事：
    1. 判定本体在 gates 里，且**不再提 www**（写回 `wwwOk` 即回归）；
    2. 页面接线保留：`plan.blocked` ⇒ 弹补齐提示；
    3. 触发同步时**不再传 profile_url / max_pages**（两个都在 §21 里退场）。
    """
    source = _read(ACCOUNTS)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")
    assert "noteSyncPlan(" in source
    note_handler = _slice_between(source, "function handleNoteSyncClick", "function handleGoodsSyncClick")
    assert "plan.blocked" in note_handler and "setNotePrompt(account)" in note_handler
    # 门禁只认 creator：`wwwOk` 不得再出现在 gates 的决策里
    assert "wwwOk" not in gates, "www 源已退场（§21.4），决策里不许再有 wwwOk"
    assert "blocked: !creatorOk" in gates
    # 触发时不再带 URL 令牌 / 页数上限
    assert "profileUrl" not in _slice_between(
        source, "async function startNoteSync", "function handleDelete"
    ), "§21 起同步不再需要主页链接（www 源退场）"
    assert "syncAccountNotes(accountId)" in source
'''

OPS_PAIRS = [
    (
        '        ("blocked", "A1"),      # 全不可用也放行 ⇒ 门禁失效',
        '        ("blocked", "A1"),      # creator 不可用也放行 ⇒ 硬门禁失效',
    ),
    (
        '        ("needslink", "C1"),    # PC 账号不录主页链接 ⇒ www 源必定缺令牌',
        '        ("wwwleak", "C3"),      # www 泄漏进源清单 ⇒ 退化成旧的「三源」契约',
    ),
    (
        '    21 条 = A(2) + B(2) + C(6) + D(4) + E(3) + F(3) + G(1)：全不可用必须拦、\n'
        '    unknown 必须放行、单平台只同步该平台、被限流的源说「未跑完」不说「0 条」、\n'
        '    详情剩余缺值渲染 `-`。',
        '    18 条 = A(2) + B(1) + C(4) + D(4) + E(3) + F(3) + G(1)：创作者会话不可用必须拦、\n'
        '    unknown 必须放行、源清单只含 creator(+ark)（www 已退场）、被限流的源说「未跑完」\n'
        '    不说「0 条」、详情剩余缺值渲染 `-`。',
    ),
    ('    assert "21 PASS / 0 FAIL" in result.stdout', '    assert "18 PASS / 0 FAIL" in result.stdout'),
]

# --------------------------------------------------------------------------- #
# 3) test_note_content_backfill.py：限流改等待制
# --------------------------------------------------------------------------- #
BACKFILL_OLD = '''            limiter = get_rate_limiter()
            for _ in range(limiter.max_per_minute):
                assert limiter.allow(f"{creator.id}:www")
            calls: list[str] = []
'''
BACKFILL_NEW = '''            class DenyLimiter:
                """额度永远拿不到（`wait_allow` 立即超时，不真等待）。

                §21.3 起限流是**等待制**：用真限流器会让本用例真的等满一个 60 秒窗口，
                所以这里注入一个「等不到额度」的替身，钉的仍是「等不到就停、且不静默」。
                """

                def allow(self, key):  # noqa: ARG002
                    return False

                def wait_allow(self, key, **kwargs):  # noqa: ARG002
                    return False

            monkeypatch.setattr(nss, "get_rate_limiter", lambda: DenyLimiter())
            calls: list[str] = []
'''


def replace_span(text: str, start_mark: str, end_mark: str, new: str) -> str:
    start = text.index(start_mark)
    end = text.index(end_mark, start)
    return text[:start] + new + text[end:]


def main() -> int:
    cjs = CJS.read_text(encoding="utf-8")
    for old, new in CJS_PAIRS:
        assert old in cjs, old[:60]
        cjs = cjs.replace(old, new, 1)
    cjs = replace_span(cjs, AB_OLD_START, C_END_MARK, ABC_NEW)
    CJS.write_text(cjs, encoding="utf-8")
    print("WROTE sync_gates.cjs")

    ops = OPS.read_text(encoding="utf-8")
    assert OPS_OLD_FUNC in ops
    ops = ops.replace(OPS_OLD_FUNC, OPS_NEW_FUNC)
    for old, new in OPS_PAIRS:
        assert old in ops, old[:60]
        ops = ops.replace(old, new, 1)
    OPS.write_text(ops, encoding="utf-8")
    print("WROTE test_page_optimizations.py")

    back = BACKFILL.read_text(encoding="utf-8")
    assert BACKFILL_OLD in back, "backfill 锚点未命中"
    back = back.replace(BACKFILL_OLD, BACKFILL_NEW, 1)
    # 该用例现在需要 monkeypatch fixture
    back = back.replace(
        "    def test_rate_limit_stops_the_batch_and_reports_pending(self, tmp_path: Path):",
        "    def test_rate_limit_stops_the_batch_and_reports_pending(self, tmp_path: Path, monkeypatch):",
    )
    BACKFILL.write_text(back, encoding="utf-8")
    print("WROTE test_note_content_backfill.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
