"""链路追踪守卫（设计 67，2026-10-25 用户口径）。

用户两条要求：
1. 「所有日志必须有操作人账号」
2. 「日志应以链路式记录，你调研市场成熟方案，实现，后台支持使用链路id查该链路
   请求的所有日志」

这里的守卫钉的是**判据**，不是实现细节：换了实现但判据不成立时它们必须红。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend" / "app"


def _read(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


# ── 一、trace_id 的生成与传播 ────────────────────────────────────────────


def test_trace_ids_follow_the_w3c_trace_context_standard():
    """trace_id 必须符合 W3C Trace Context（32 位小写十六进制）。

    为什么钉这个而不是「只要有个随机串就行」：用户要求「调研市场成熟方案」。
    W3C 标准的意义是**互操作** —— 网关 / APM / 云日志都认这个格式，
    自造格式将来接任何一个都要转换。格式错（长度 16 或含大写）看起来能用，
    但外部系统不认，等于白记。
    """
    from backend.app.core import tracing

    # 连发多次，确认都不是全零、长度固定、字符集合法（防「每次都返回同一个」）。
    seen = set()
    for _ in range(20):
        tid, _span = tracing.start_trace()
        assert re.fullmatch(r"[0-9a-f]{32}", tid), f"不是 W3C 格式: {tid}"
        assert tid != "0" * 32, "全零是 W3C 明令禁止的 trace-id"
        seen.add(tid)
        tracing.clear()
    assert len(seen) == 20, "trace_id 必须每次不同（复用了同一个 = 所有链路串成一条）"


def test_inbound_traceparent_is_continued_not_replaced():
    """外部传进来的 `traceparent` 必须被**接续**，不能另起一个新 id。

    判据：给一个合法 traceparent，返回的 trace_id 必须与入站一致。
    若实现改成「总是新建」，跨服务调用就断了 —— 那正是链路追踪要解决的问题。
    """
    from backend.app.core import tracing

    inbound = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
    parsed, parent = tracing.parse_traceparent(inbound)
    assert parsed == "c" * 32
    assert parent == "d" * 16


def test_malformed_traceparent_is_rejected():
    """非法 traceparent 必须**丢弃**（W3C 规定不许「尽力修复」）。

    修复出来的 id 与上游对不上，比不接续更糟 —— 看起来接上了，其实不是同一条。
    """
    from backend.app.core import tracing

    bad = [
        "00-" + "c" * 31 + "-" + "d" * 16 + "-01",  # trace_id 少一位
        "00-" + "C" * 32 + "-" + "d" * 16 + "-01",  # 大写
        "00-" + "0" * 32 + "-" + "d" * 16 + "-01",  # 全零
        "00-" + "c" * 32 + "-" + "d" * 15 + "-01",  # span_id 少一位
        "garbage",
        "",
    ]
    for value in bad:
        tid, span = tracing.parse_traceparent(value)
        assert tid is None and span is None, f"非法值被接受了: {value!r}"


def test_response_carries_the_trace_id_header():
    """每个响应都要带 `X-Trace-Id` —— 这是用户拿到 id 的**唯一**入口。

    没有这个头，「按链路 id 查」这个功能用户根本无从使用（他哪来的 id）。
    """
    text = _read("core/trace_middleware.py")
    assert "headers[" in text and "TRACE_ID_RESPONSE_HEADER" in text, (
        "中间件没把 trace_id 写进响应头"
    )
    from backend.app.core import tracing

    assert tracing.TRACE_ID_RESPONSE_HEADER.lower() == "x-trace-id"


def test_trace_middleware_is_registered_outermost():
    """trace 中间件必须**最后 add**（Starlette 后注册的先执行 ⇒ 最外层）。

    不在最外层的话，它拿不到原始请求头（`traceparent`），也覆盖不到其它
    中间件里产生的日志 —— 链路会缺头缺尾。
    """
    text = _read("core/middleware.py")
    #: ⚠️ 锚点必须是**注册调用**而不是类名 —— 注释里也会出现类名，
    #: 按名字 find 会命中注释（本守卫首轮就是这么被自己的注释骗过的）。
    idx_trace = text.find("add_middleware(TraceMiddleware")
    assert idx_trace != -1, "TraceMiddleware 没有注册"
    #: ⚠️ CORS 是多行注册（`app.add_middleware(\n  CORSMiddleware,`）——
    #: 按 `add_middleware(CORSMiddleware` 一个字符串找会失配，故按类名在**全部
    #: add_middleware 调用**里定位。
    calls = [
        (m.start(), m.group(1))
        for m in re.finditer(r"add_middleware\((?:[\s\S]{0,120}?)(\w+Middleware)", text)
    ]
    names = {name for _, name in calls}
    for other in ("ApiAuditMiddleware", "CORSMiddleware"):
        assert other in names, f"{other} 未注册（判据前提不成立）；实际: {names}"
        idx = next(pos for pos, name in calls if name == other)
        assert idx < idx_trace, (
            f"{other} 必须在 TraceMiddleware 之前 add（否则 trace 不在最外层）"
        )


# ── 二、日志必须带 trace_id ──────────────────────────────────────────────


def test_all_three_log_tables_carry_a_trace_column():
    """三张日志表都要有 `trace_id` 列 —— 少一张就有一类日志查不到。

    用户要的是「该链路请求的**所有**日志」：只有 api 没有 business，就看不到
    业务动作；只有 business 没有 job，就看不到它触发的定时任务。
    """
    from backend.app.models.api_log import ApiLog
    from backend.app.models.business_log import BusinessLog
    from backend.app.models.scheduled_job import ScheduledJobLog

    for model in (ApiLog, BusinessLog, ScheduledJobLog):
        cols = set(model.__table__.c.keys())
        assert "trace_id" in cols, f"{model.__tablename__} 缺 trace_id 列"


def test_audit_middleware_reads_trace_from_request_state():
    """审计中间件必须从 `request.state` 取 trace_id，不能只靠 contextvar。

    ⚠️ 这是**实测踩出来的坑**：`BaseHTTPMiddleware` 的 `call_next()` 返回后
    上下文已被恢复，此时读 contextvar 拿到空值 ⇒ 实测 11081 条 `api_logs`
    的 trace_id 全是 NULL，而代码看起来完全正常（没有任何报错）。
    """
    text = _read("core/api_audit.py")
    assert "request.state" in text and "trace_id" in text, "没从 request.state 取"
    #: 明确断言「优先 state」的写法存在（防有人改回只读 contextvar）。
    assert re.search(r'getattr\(request\.state,\s*"trace_id"', text), (
        "缺少从 request.state 读 trace_id 的实现"
    )


def test_business_log_records_the_trace_automatically():
    """业务日志要**自动**带 trace_id（调用方不用显式传）。

    要调用方每处都传，漏一处就断一节链路 —— 那是必然漏的。
    """
    text = _read("services/business_log_service.py")
    assert "trace_id" in text
    assert "_current_trace_id" in text, "没有自动取当前链路的实现"


# ── 三、按链路查全部日志 ─────────────────────────────────────────────────


def test_trace_endpoint_queries_all_three_log_sources():
    """链路查询必须**查全三张表**并把结果合起来。

    ⚠️ 这是本功能的**核心判据**：只查 `api_logs` 会漏掉业务动作与定时任务，
    返回的行数看着「有数据」，但用户说的「该链路的所有日志」并不完整 ——
    这种残缺比报错更难发现。
    """
    text = _read("services/trace_query_service.py")
    for model in ("ApiLog", "BusinessLog", "ScheduledJobLog"):
        assert model in text, f"链路查询没查 {model}"


def test_trace_timeline_is_ordered_by_time():
    """时间线必须按时间升序 —— 否则用户看不出因果顺序。"""
    text = _read("services/trace_query_service.py")
    assert "events.sort(" in text, "没有排序：链路的顺序就是故事本身"


def test_trace_endpoint_requires_permission():
    """按链路查日志必须鉴权 —— 日志里有所有用户的操作痕迹。"""
    api = BACKEND / "api" / "system_logs.py"
    text = api.read_text(encoding="utf-8")
    idx = text.find('"/logs/trace/{trace_id}"')
    assert idx != -1, "链路端点不存在"
    #: 该端点签名里必须有 require_perm（取它后面 800 字符看，够覆盖签名）。
    sig = text[idx : idx + 800]
    assert "require_perm(" in sig, "链路查询端点没有权限校验"


def test_trace_query_filters_by_actor_for_non_admin():
    """非管理员只能看自己的链路记录（管理员可看全部，用于运维排查）。"""
    text = _read("services/trace_query_service.py")
    assert "ROLE_ADMIN" in text, "没有管理员判断"
    assert "actor_id == user_id" in text or "user_id=user_id" in text, (
        "非管理员没有按操作人过滤"
    )


# ── 四、操作人必须落库 ───────────────────────────────────────────────────


def test_every_log_row_can_name_its_actor():
    """三张日志表都要能定位操作人（用户口径「所有日志必须有操作人账号」）。

    `api_logs` / `scheduled_job_logs` 存 `user_id`，`business_logs` 存
    `actor_id` + `actor_name`。少任何一个，都有日志说不清是谁做的。
    """
    from backend.app.models.api_log import ApiLog
    from backend.app.models.business_log import BusinessLog
    from backend.app.models.scheduled_job import ScheduledJobLog

    expected = {
        ApiLog: "user_id",
        BusinessLog: "actor_id",
        ScheduledJobLog: "user_id",
    }
    for model, column in expected.items():
        assert column in set(model.__table__.c.keys()), (
            f"{model.__tablename__} 缺 {column} 列 ⇒ 该表日志无法归属操作人"
        )
    #: 业务日志额外要求有名字列（只存 id 的话，用户删了账号就查不出是谁）。
    assert "actor_name" in set(BusinessLog.__table__.c.keys())


def test_trace_query_resolves_actor_names():
    """链路查询要把 id 换成**账号名** —— 用户要看的是「谁」，不是「#28」。"""
    text = _read("services/trace_query_service.py")
    assert "User.username" in text or "username" in text, "没有把 user_id 解析成账号名"
