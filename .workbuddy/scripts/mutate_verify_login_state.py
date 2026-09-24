"""变异验证：登录态三层判据 + 扫码续期 + 三平台拆分（设计 13 §18 / §18.14）的守卫是不是**承重的**。

## 为什么必须做

§18 这一批守卫里有一大半是**源码级 / 关系级**断言（`'"action"' not in source`、
`offenders == []`、AST 节点类型），这类断言最容易写成「看着在查、其实永远绿」。
「断言写出来了」不等于「断言会红」—— 本项目已多次抓到假绿。

## 十九个注入

| # | 注入 | 期望变红的用例 |
|---|---|---|
| ① | L2 过关直接报 `valid`（拿两把钥匙冒充「平台确认可用」） | 列表三态 |
| ② | www 探针把「根本没问到」（`payload is None`）判成 `expired` | 红线：unknown 不冒充 expired |
| ③ | 千帆探针把「接口变了」也当成「凭据失效」 | ark 两种失败必须分开 |
| ④ | `cookie_names` 不认 JSON 形态（**DEF-059 回归**） | 两种形态等价 + 登录路径归平台 |
| ⑤ | 续期跳过身份校验（扫成别的号也照样写） | 身份不符 / 无法确认必须什么都不写 |
| ⑥ | 续期目标不带过轮询（确认轮退化成「添加账号」） | 目标跨轮存活 |
| ⑦ | 用户维度原因抄成**逐字副本**（值一样，但已是第二份实现） | **结构**守卫：只认调用 |
| ⑧ | 用户维度原因抄成**漂移副本**（值也不一样） | 复用同一份 + 结构守卫 |
| ⑨ | 假归因复活（「创作者平台没有这三类数据」），**写在另一个文件里** | 全仓扫描（AST 字符串常量） |
| ⑩ | 探针改成抛异常（丢掉「平台拒绝」与「没问到」的区分） | 探针不得 raise |
| ⑪ | 列表加载也发探针（`probe` 条件失效 ⇒ 风控） | 零平台请求 |
| ⑫ | 后端下发「动作」概念（呈现渗透进 API） | 文案/动作只有前端一份 |
| ⑬ | 前端弹窗自带第二份 detail 解析 + 「请稍后重试」 | 唯一解析口 + 不给做不到的建议 |
| ⑭ | `unknown` 与 `expired` 撞文案 | **行为**脚本必须变红 |
| ⑮ | **创作者 jar 归到 ark 平台**（观隅好物回归：假「千帆已失效」） | 三态分类 |
| ⑯ | **千帆扫码不传 `service_url`**（扫出来的是创作者令牌） | service 必须指向 ark |
| ⑰ | **千帆续期跳过店铺对账**（扫错店也照写） | 店铺不符一个字节都不写 |
| ⑱ | **绑定表来源注记写到探针前**（被探针结论整体冲掉） | 来源要说清楚 |
| ⑲ | **跨行合并方向反了**（旧信封顶掉新令牌） | 每个平台各取最新 |

⑦/⑧ 是**成对**的，也是本项目最值得看的一对：

* ⑧（漂移副本）会被「值相等」那条老断言抓到；
* ⑦（**逐字**副本）**不会** —— 值一模一样。补上的是
  `test_user_scope_reason_is_reused_not_rewritten()`：它查赋值右侧的**形态**
  （必须是 `_web_session_required_detail(...)` 的调用），字面量一律红。

⑱ 是 2026-09-21 实测抓到的真 bug：来源注记原本写在探针**之前**，被探针的
`entry["reason"] = reason` 整体覆盖 —— 用户因此看不到「这份令牌来自绑定表」。

⚠️ 锚点唯一性：每条注入的 `needle` 都先在源文件里数过出现次数（本项目规矩：
「`in source` 型断言先数目标串出现几次」）。⑥ 的两处调用因此把**前一行注释**
一起纳进锚点，避免 `replace(..., 1)` 默默改错那一处。

用完自动复原（含异常路径），最后确认基线恢复绿。

## ⚠️ 本脚本第一版自己被咬了（记下来，别再犯）

第一版按「退出码 ≠ 0 且输出尾部 900 字符里有 `failed`」判红，结果 **7 条已经变红的注入
被判成「守卫是摆设」** —— 因为 pytest 收尾清理系统 temp 会被沙箱的**批量删除守卫**拦下，
在正常汇总行**之后**追加一大段 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {...}`：
① 退出码**假性变 1**；② 汇总行被挤出尾部窗口。

⇒ 现在**一律以 `--junitxml` 为准**，顺带解决第二个坑：「pytest 没收集到用例 /
用法错误」也返回非 0，只看看退出码会把它们误判成「按预期变红」。

**变异验证脚本自己也会骗人** —— 与「校验脚本自己也会骗人」（DEF-046）同一类。

    python .workbuddy/scripts/mutate_verify_login_state.py
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"

SVC = "backend/app/services/login_state_service.py"
ACC = "backend/app/services/account_service.py"
SESS = "backend/app/api/login_sessions.py"
PC = "backend/app/api/platforms/xhs/pc.py"
PANEL = "frontend/src/components/account/qr-login-panel.tsx"
LIB = "frontend/src/lib/login-state.ts"
QFADAPTER = "backend/app/adapters/xhs/qianfan_login_adapter.py"

LS = "tests/backend/test_login_state.py"
CC = "tests/backend/test_cookie_channels.py"

CASE_LIST = f"{LS}::TestLayer12IsLocalAndComplete::test_list_endpoint_reports_all_three_local_states_without_any_probe"
CASE_PROBE_SCOPE = f"{LS}::TestLayer12IsLocalAndComplete::test_probe_only_runs_for_channels_that_are_actually_logged_in"
CASE_REDLINE = f"{LS}::TestLayer3TellsValidFromExpiredFromUnknown::test_probe_that_never_completed_stays_unknown_and_never_expired"
CASE_ARK = f"{LS}::TestLayer3TellsValidFromExpiredFromUnknown::test_ark_probe_splits_credential_error_from_other_failures"
CASE_SHAPES = f"{LS}::TestLoginPathStoresTheRightChannel::test_both_stored_shapes_are_understood"
CASE_LOGIN_CH = f"{LS}::TestLoginPathStoresTheRightChannel::test_creator_qr_login_lands_in_creator_and_pc_qr_login_in_www"
CASE_RENEW_CREATOR = f"{LS}::TestRenewalWritesOnlyTheTargetChannel::test_renew_creator_platform_uses_creator_qr_and_keeps_www"
CASE_MISMATCH = f"{LS}::TestRenewalWritesOnlyTheTargetChannel::test_renew_refuses_a_different_account_and_writes_nothing"
CASE_UNVERIFIABLE = f"{LS}::TestRenewalWritesOnlyTheTargetChannel::test_renew_refuses_when_identity_cannot_be_verified"
CASE_SAME_COPY = f"{LS}::TestNoSecondCopyOfTheDisprovenAttribution::test_user_scope_reason_is_the_same_object_not_a_second_copy"
CASE_DISPROVEN = f"{LS}::TestNoSecondCopyOfTheDisprovenAttribution::test_disproven_phrase_is_nowhere_in_the_backend"
CASE_REUSED = f"{LS}::test_user_scope_reason_is_reused_not_rewritten"
CASE_RAISE = f"{LS}::test_probe_self_profile_returns_the_raw_triple_not_a_raise"
CASE_PANEL = f"{LS}::TestQrPanelHasNoSecondDetailParser::test_panel_does_not_reparse_detail_on_its_own"
CASE_MAPPING = f"{LS}::TestFrontendBehaviourScripts::test_login_state_mapping_is_driven_for_real"
CASE_WORDING = f"{LS}::test_login_state_module_has_no_ui_wording_duplication"
CASE_ROUNDS = f"{LS}::test_target_account_id_survives_every_poll_round"
# §18.14 新增（三平台拆分 + 千帆扫码）
CASE_CREATOR_JAR = f"{CC}::TestPlatformClassification::test_creator_jar_is_creator"
CASE_MERGE = f"{CC}::TestThreePlatformsOnOneAccount::test_merge_across_rows_newest_wins_per_platform"
CASE_CREATOR_PROBE = f"{LS}::TestLayer3TellsValidFromExpiredFromUnknown::test_creator_probe_splits_the_three_outcomes"
CASE_ARK_BINDING = f"{LS}::TestLayer3TellsValidFromExpiredFromUnknown::test_qianfan_falls_back_to_binding_when_account_has_no_ark_platform"
CASE_QF_SERVICE = f"{LS}::TestQianfanQrLogin::test_qr_code_uses_the_ark_service_url"
CASE_QF_SHOP = f"{LS}::TestQianfanQrLogin::test_qianfan_renewal_refuses_a_different_shop"

# ① 的锚点：L2 过关后的取值（出现 1 次）
LAYER12_LINE = "        return STATE_UNKNOWN if has_web_session(cookies) else STATE_GUEST"

# ② 的锚点：www 探针的「请求未完成」分支（出现 1 次）
WWW_UNKNOWN_BLOCK = (
    "    if payload is None:\n"
    '        return STATE_UNKNOWN, f"检测网页版令牌未得到结论（请求未完成）：{message}"\n'
)

# ③ 的锚点：千帆「凭据失效」的窄判据（出现 1 次）
ARK_CODE_LINE = "    if code == credential_error_code:"

# ④ 的锚点：JSON 形态分支（出现 1 次）—— DEF-059 的命门
JSON_BRANCH = '    if text.startswith("{"):'

# ⑤ 的锚点：续期身份闸门（出现 1 次）
MISMATCH_GUARD = (
    "    reason = _identity_mismatch_reason(account, user_info)\n"
    "    if reason:\n"
    '        return None, "", {"code": RENEW_IDENTITY_MISMATCH, "message": reason}\n'
)

# ⑥ 的锚点：**连注释一起**（`target_account_id=state.target_account_id,` 在文件里出现 2 次，
#    另一处是把它传进续期函数；只锚那一行会依赖「谁在前面」这种脆弱前提）
CARRY_TARGET = (
    "            # 续期目标**必须带过每一轮**：状态每轮重写一次，漏掉它就会在\n"
    "            # 「扫了但还没确认」的那一轮把目标丢掉，确认轮变成添加新账号。\n"
    "            target_account_id=state.target_account_id,\n"
)

# ⑦/⑧ 的锚点：用户维度原因（出现 1 次）
REUSE_LINE = '    user_scope_reason = _web_session_required_detail(borrow_same_identity=True)["message"]'
VERBATIM_COPY = (
    "    user_scope_reason = (\n"
    '        "该账号的 Cookie 中不含网页会话（web_session），同一个人名下也没有可用的"\n'
    '        "网页会话（可能已删除或已过期）—— 我赞过的 / 我收藏的 / 互动消息这三类"\n'
    '        "只能由网页版读取。请用该账号在网页版（xiaohongshu.com）登录后重新导入 Cookie。"\n'
    "    )"
)
DRIFTED_COPY = (
    "    user_scope_reason = (\n"
    '        "该账号的 Cookie 中不含网页会话，同一个人名下也没有可用的网页会话（可能已删除或已过期）。"\n'
    '        "这三类只能由网页版读取，请重新导入 Cookie。"\n'
    "    )"
)

# ⑨ 的锚点：**另一个文件**里的字符串（`login_state_service.py` 的 guest 原因）
GUEST_REASON_LINE = (
    '        "这条网页版令牌只拿到了游客态（有 web_session 但没有 id_token），读不到本人数据，"'
)
DISPROVEN_PHRASE_LINE = '        "创作者平台没有这三类数据，读不到本人数据，"'

# ⑩ 的锚点：探针取数口（出现 1 次）
PROBE_RETURN = "        return XhsPcApiAdapter(cookies_text).get_self_profile()"
PROBE_RAISE = (
    "        success, message, payload = XhsPcApiAdapter(cookies_text).get_self_profile()\n"
    "        if not success:\n"
    "            raise RuntimeError(message)\n"
    "        return success, message, payload"
)

# ⑪ 的锚点：探针触发条件（出现 1 次）
PROBE_CONDITION = "            if probe and state == STATE_UNKNOWN:"

# ⑫ 的锚点：空状态形态（出现 1 次）
EMPTY_STATE = '    return {"state": STATE_MISSING, "reason": "", "probed": False}'

# ⑬ 的锚点：弹窗的兜底解析（出现 1 次）
PANEL_TAIL = '    return detailMessage(detail) || "二维码生成失败，请检查网络或平台登录态后重试。";'
PANEL_SECOND_PARSER = '    if (typeof detail === "string") return detail;\n    return "请稍后重试";'

# ⑭ 的锚点：`unknown` 那一行视图（出现 1 次）
LIB_UNKNOWN_ROW = (
    '  unknown: { state: "unknown", color: "#8c8c8c", label: "未检测", action: "", actionLabel: "" },'
)

# ⑮ 的锚点：平台分类顺序（出现 1 次）—— 观隅好物的命门
CLASSIFY_CREATOR = (
    "    if any(key in names for key in _CREATOR_STRONG_KEYS):\n"
    "        return CHANNEL_CREATOR\n"
)

# ⑯ 的锚点：千帆适配器的 service 指向（出现 1 次）
ARK_SERVICE_CALL = "        return XHSCreatorLoginApi(service_url=ARK_SERVICE_URL)"

# ⑰ 的锚点：千帆店铺对账闸门（出现 1 次）
SHOP_GUARD = (
    "    if (\n"
    "        binding is not None\n"
    "        and shop.get(\"shop_user_id\")\n"
    "        and (binding.shop_user_id or \"\").strip()\n"
    "        and binding.shop_user_id != shop[\"shop_user_id\"]\n"
    "    ):\n"
)

# ⑱ 的锚点：绑定表来源注记**整块**（连函数体一起，出现 1 次）。
#    ⚠️ 第一版只锚到 `if` 行为止，替换文本又把注记写回**同一位置** —— 注入等于
#    没注入（注记还在探针后面），用例当然全绿。锚点必须含函数体、替换成空才行。
BINDING_NOTE_AFTER = (
    "            # 来源注记**只在这里写一次**（探针之后）：探针会整体覆盖 reason，\n"
    "            # 写在探针前面就被冲掉了 —— 而「这份令牌来自绑定表」正是用户想知道的。\n"
    "            if from_binding and entry[\"state\"] != STATE_MISSING:\n"
    '                entry["reason"] = f"{entry[\'reason\']}（当前经千帆绑定表的令牌使用）"\n'
)
BINDING_NOTE_DROPPED = "            # 变异：来源注记整个去掉\n"

# ⑲ 的锚点：跨行合并的排序方向（出现 1 次）
MERGE_ORDER = (
    "        .order_by(\n"
    "            AccountCookieVersion.created_at.desc(),\n"
    "            AccountCookieVersion.id.desc(),\n"
    "        )\n"
)

MUTATIONS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "① L2 过关就报 `valid`（两把钥匙 ≠ 平台确认可用）",
        SVC,
        LAYER12_LINE,
        "        return STATE_VALID if has_web_session(cookies) else STATE_GUEST",
        [CASE_LIST],
    ),
    (
        "② www 探针把「根本没问到」判成 `expired`（DEF-057 同型红线）",
        SVC,
        WWW_UNKNOWN_BLOCK,
        (
            "    if payload is None:\n"
            '        return STATE_EXPIRED, f"{_REASONS[(CHANNEL_WWW, STATE_EXPIRED)]}（平台原话：{message}）"\n'
        ),
        [CASE_REDLINE],
    ),
    (
        "③ 千帆探针把「接口变了」也判成「凭据失效」",
        SVC,
        ARK_CODE_LINE,
        "    if True:  # 一律 expired",
        [CASE_ARK],
    ),
    (
        "④ `cookie_names` 不认 JSON 形态（**DEF-059 回归**）",
        ACC,
        JSON_BRANCH,
        "    if False:  # DEF-059 回归",
        [CASE_SHAPES, CASE_LOGIN_CH, CASE_RENEW_CREATOR],
    ),
    (
        "⑤ 续期跳过身份校验（扫成别的号也照样写）",
        SESS,
        MISMATCH_GUARD,
        (
            "    reason = _identity_mismatch_reason(account, user_info)\n"
            "    if False:\n"
            '        return None, "", {"code": RENEW_IDENTITY_MISMATCH, "message": reason}\n'
        ),
        [CASE_MISMATCH, CASE_UNVERIFIABLE],
    ),
    (
        "⑥ 续期目标不带过轮询（确认轮退化成「添加账号」）",
        SESS,
        CARRY_TARGET,
        (
            "            # 续期目标**必须带过每一轮**：状态每轮重写一次，漏掉它就会在\n"
            "            # 「扫了但还没确认」的那一轮把目标丢掉，确认轮变成添加新账号。\n"
            "            target_account_id=None,\n"
        ),
        [CASE_ROUNDS],
    ),
    (
        "⑦ 用户维度原因抄成**逐字副本**（值一样，但已是第二份实现）",
        PC,
        REUSE_LINE,
        VERBATIM_COPY,
        [CASE_REUSED],
    ),
    (
        "⑧ 用户维度原因抄成**漂移副本**（值也不一样）",
        PC,
        REUSE_LINE,
        DRIFTED_COPY,
        [CASE_SAME_COPY, CASE_REUSED],
    ),
    (
        "⑨ 假归因复活（「创作者平台没有这三类数据」），写在**另一个文件**里",
        SVC,
        GUEST_REASON_LINE,
        DISPROVEN_PHRASE_LINE,
        [CASE_DISPROVEN],
    ),
    (
        "⑩ 探针改成抛异常（丢掉「平台拒绝」与「没问到」的区分）",
        ACC,
        PROBE_RETURN,
        PROBE_RAISE,
        [CASE_RAISE],
    ),
    (
        "⑪ 列表加载也发探针（`probe` 条件失效 ⇒ 风控）",
        SVC,
        PROBE_CONDITION,
        "            if state == STATE_UNKNOWN:",
        [CASE_LIST, CASE_PROBE_SCOPE],
    ),
    (
        "⑫ 后端下发「动作」概念（呈现渗透进 API）",
        SVC,
        EMPTY_STATE,
        '    return {"state": STATE_MISSING, "reason": "", "probed": False, "action": "login"}',
        [CASE_WORDING],
    ),
    (
        "⑬ 弹窗自带第二份解析 + 「请稍后重试」",
        PANEL,
        PANEL_TAIL,
        PANEL_SECOND_PARSER,
        [CASE_PANEL],
    ),
    (
        "⑭ `unknown` 与 `expired` 撞文案（**行为**脚本必须变红）",
        LIB,
        LIB_UNKNOWN_ROW,
        LIB_UNKNOWN_ROW.replace('label: "未检测"', 'label: "已失效"'),
        [CASE_MAPPING],
    ),
    (
        "⑮ **创作者 jar 归到 ark 平台**（观隅好物：假「千帆已失效」）",
        ACC,
        CLASSIFY_CREATOR,
        (
            "    if any(key in names for key in _CREATOR_STRONG_KEYS):\n"
            "        return CHANNEL_ARK  # 变异：创作者 jar 也归 ark\n"
        ),
        [CASE_CREATOR_JAR, CASE_LOGIN_CH, CASE_RENEW_CREATOR],
    ),
    (
        "⑯ **千帆扫码不传 `service_url`**（扫出来的是创作者令牌）",
        QFADAPTER,
        ARK_SERVICE_CALL,
        "        return XHSCreatorLoginApi()  # 变异：service 退回创作者平台",
        [CASE_QF_SERVICE],
    ),
    (
        "⑰ **千帆续期跳过店铺对账**（扫错店也照写）",
        SESS,
        SHOP_GUARD,
        (
            "    if (\n"
            "        False\n"
            "        and binding is not None\n"
            "        and shop.get(\"shop_user_id\")\n"
            "        and (binding.shop_user_id or \"\").strip()\n"
            "        and binding.shop_user_id != shop[\"shop_user_id\"]\n"
            "    ):\n"
        ),
        [CASE_QF_SHOP],
    ),
    (
        "⑱ **绑定表来源注记被去掉**（用户看不到「这份令牌来自绑定表」）",
        SVC,
        BINDING_NOTE_AFTER,
        BINDING_NOTE_DROPPED,
        [CASE_ARK_BINDING],
    ),
    (
        "⑲ **跨行合并方向反了**（旧信封顶掉新令牌）",
        ACC,
        MERGE_ORDER,
        (
            "        .order_by(\n"
            "            AccountCookieVersion.created_at.asc(),\n"
            "            AccountCookieVersion.id.asc(),\n"
            "        )\n"
        ),
        [CASE_MERGE],
    ),
]


def run(cases: list[str]) -> tuple[int, int, int, int, str]:
    """跑一次 pytest，返回 `(收集数, 失败数, 错误数, 退出码, 输出尾部)`。

    ⚠️ **一律以 `--junitxml` 为准，不看清 stdout、也不看退出码** —— 本项目实测过两次：
    pytest 收尾清理系统 temp 时会被沙箱的**批量删除守卫**拦下，于是在正常汇总行**之后**
    追加一大段 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {...}`，
    后果有两个：① 退出码**假性变 1**；② 汇总行被挤出「输出尾部 N 字符」的窗口。
    第一版本脚本就被这两个坑同时咬到 —— 7 条明明已经变红的注入被判成「守卫是摆设」。

    用 XML 还有第二个好处：能区分「**用例失败**」与「pytest 自己出错（用法错误 / 没收集到）」——
    后者同样是非 0 退出码，直接看退出码会把它们误当成「按预期变红」。
    """
    xml_path = ROOT / ".workbuddy/tmp/mutate-one.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    if xml_path.exists():
        xml_path.unlink()
    result = subprocess.run(
        [PYTHON, "-m", "pytest", *cases, "-q", "--no-header", "-p", "no:cacheprovider",
         f"--junitxml={xml_path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = (result.stdout + result.stderr)[-900:]
    if not xml_path.exists():
        return 0, 0, 0, result.returncode, tail
    root = ET.parse(xml_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return 0, 0, 0, result.returncode, tail
    return (
        int(suite.get("tests", 0)),
        int(suite.get("failures", 0)),
        int(suite.get("errors", 0)),
        result.returncode,
        tail,
    )


def _summary(tests: int, failures: int, errors: int) -> str:
    return f"{tests} 收集 / {failures} failed / {errors} errors"


def main() -> int:
    all_cases = sorted({case for _, _, _, _, cases in MUTATIONS for case in cases})
    tests, failures, errors, code, tail = run(all_cases)
    print(f"基线复跑：{_summary(tests, failures, errors)} → {'全部通过 ✅' if code == 0 and failures == 0 and errors == 0 else '仍有红 ❌'}")
    if failures or errors:
        print(tail)

    results: list[tuple[str, bool, str]] = []
    originals: dict[str, str] = {}
    try:
        for label, rel, needle, replacement, cases in MUTATIONS:
            path = ROOT / rel
            if rel not in originals:
                originals[rel] = path.read_text(encoding="utf-8")
            source = originals[rel]
            if source.count(needle) != 1:
                results.append((label, False, f"锚点出现 {source.count(needle)} 次（要求恰好 1 次）"))
                print(f"❌ {label} → 锚点不唯一，跳过")
                continue
            path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
            try:
                tests, failures, errors, code, tail = run(cases)
            finally:
                path.write_text(source, encoding="utf-8")
            summary = _summary(tests, failures, errors)
            red = failures > 0 or errors > 0
            results.append((label, red, summary))
            print(f"{'✅' if red else '❌'} {label} → {'按预期变红' if red else '仍然全绿（守卫是摆设）'}  [{summary}]")
    finally:
        for rel, source in originals.items():
            (ROOT / rel).write_text(source, encoding="utf-8")
        print("\n[已复原全部源文件]")

    tests, failures, errors, code, tail = run(all_cases)
    baseline_ok = code == 0 and failures == 0 and errors == 0
    print(f"基线复跑：{_summary(tests, failures, errors)} → {'全部通过 ✅' if baseline_ok else '仍有红 ❌'}")
    if not baseline_ok:
        print(tail)

    caught = sum(1 for _, red, _ in results if red)
    print(f"\n变异验证结果：{caught}/{len(MUTATIONS)} 按预期变红；基线 {'绿' if baseline_ok else '红'}")
    for label, red, note in results:
        if not red:
            print(f"  ⚠️ {label}：{note}")
    return 0 if caught == len(MUTATIONS) and baseline_ok else 1


if __name__ == "__main__":
    sys.exit(main())
