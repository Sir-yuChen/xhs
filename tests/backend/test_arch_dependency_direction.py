"""架构依赖方向合同（设计 61 §四 A1，2026-10-24）。

## 三条合同（依赖只能向下：core ← services ← api）

| 合同 | 方向 | 豁免策略 |
|---|---|---|
| C1 | `core/` 不得 import `services/` / `api/` | 逐条记账（`_EXEMPT`），清一处销一条 |
| C2 | `adapters/` 不得 import `services/` / `api/` | 同上 |
| C3 | `api/` 不得跨模块 import 其他 api 模块（同包内部互引除外） | 同上 |

## 为什么是 AST 守卫而不是 import-linter 工具

项目纪律「规范要可执行，落到会红的守卫上」；守卫与既有 `test_no_unused_imports`
同型（AST 扫源码），不引第三方 CLI 依赖、跑在 pytest 里与全量回归同节奏。
判定语义对齐 import-linter 的 `forbidden` 合同：**函数级延迟 import 同样计入**
（`ast.walk` 遍历整棵树），`TYPE_CHECKING` 块同样计入——运行时不可达 ≠ 依赖不存在。

## 豁免记账纪律

* 每条豁免必须带**理由**（为什么现在还不能清、清的动作属于哪一轮）。
* `test_every_exemption_still_matches_a_violation` 钉住「豁免不许变成僵尸」：
  违规修掉了、豁免条目还留着 ⇒ 红，强迫同步销账。
* 本守卫**不负责**修违规——只让违规可见、可计数、可验收下降。
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
from pathlib import Path

import pytest



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"

#: 层名 → 允许的被依赖层（只能向下）。
_FORBIDDEN: dict[str, set[str]] = {
    "core": {"services", "api"},
    "adapters": {"services", "api"},
    # api -> api 跨模块单独判（C3，同包内部互引放行），不放这张表
}

# ---------------------------------------------------------------------------
# 豁免记账（清一处销一条；格式：(源文件 posix 相对路径, 目标模块前缀) ）
# 匹配语义 = 前缀匹配（`backend.app.services` 覆盖它的一切子模块与 from-import），
# 因为同一调用点可能两种 import 形态并存，按最粗前缀记一笔账。
# ---------------------------------------------------------------------------
_EXEMPT: dict[tuple[str, str], str] = {
    # --- C1 core -> services|api：**已清零（A5，2026-10-24）** ——
    # permission_cache / presence / business_provider_keys / env_bootstrap 四模块下沉 core
    # （它们本就是基础设施：仅依赖 core+models，被三层共用），预警值域常量下沉
    # core/llm_gateway_alert_rules，core/business_lines.py（零生产消费方的死码，
    # 唯一引用方是它自己的测试）整文件删除。此处不再有条目 = 合同 C1 满额生效。
    # --- C2 adapters -> services：**已清零（A7，2026-10-24）** ---
    # 限流器下沉 core/rate_limiter（provider 注册反转）、topic_token 下沉
    # adapters/xhs/topic_format。
    # --- C3 api 跨域（域 = 目录子树；根层互引合法故不记账，见 scan_source 的 C3 注释） ---
    ("backend/app/api/platforms/xhs/crawl.py", "backend.app.api.tasks"): "serialize_task",
    ("backend/app/api/platforms/xhs/creator.py", "backend.app.api.tasks"): "serialize_task",
    ("backend/app/api/platforms/xhs/product_notes.py", "backend.app.api.ai"): "_business_model_context / get_text_ai_client",
    ("backend/app/api/platforms/xhs/qianfan/_shared.py", "backend.app.api.ai"): "get_image_ai_client / get_text_ai_client",
    ("backend/app/api/platforms/xhs/qianfan/goods.py", "backend.app.api.ai"): "_business_model_context",
}


def _is_exempt(rel: str, target: str) -> bool:
    return any(
        rel == exempt_rel and (target == prefix or target.startswith(prefix + "."))
        for (exempt_rel, prefix) in _EXEMPT
    )


def _module_of(path: Path) -> str:
    rel = path.with_suffix("").relative_to(APP.parent.parent)
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]  # 包的 __init__.py 的模块名 = 包本身
    return ".".join(parts)


def _layer_of(module: str) -> str | None:
    if module == "backend.app" or module.startswith("backend.app."):
        rest = module[len("backend.app."):]
        top = rest.split(".")[0] if rest else ""
        return {"core": "core", "services": "services", "api": "api", "adapters": "adapters"}.get(top, None)
    return None


def _resolve_imports(module: str, node: ast.AST, *, is_package: bool = False) -> list[str]:
    """把 import 语句解析成绝对模块名（含相对 import 回溯，含 from-import 的模块本体）。

    ⚠️ 相对 import 的回溯基准：**模块** level=1 = 它所在包；**包**（`__init__.py`）
    level=1 = 包本身。搞混会把 `from . import goods` 解析到父包上去（实测踩到）。
    """
    out: list[str] = []
    if isinstance(node, ast.Import):
        out.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            parts = module.split(".")
            cut = len(parts) - node.level + (1 if is_package else 0)
            base = ".".join(parts[:cut] + ([node.module] if node.module else []))
        else:
            base = node.module or ""
        if base:
            out.append(base)
    return out


def scan_source(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """扫一段源码的越层 import（供自检复用；`module` 是源码所属的绝对模块名）。"""
    normalized = module[: -len(".__init__")] if module.endswith(".__init__") else module
    src_layer = _layer_of(normalized)
    if src_layer is None:
        return []
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        for name in _resolve_imports(normalized, node, is_package=is_package):
            tgt_layer = _layer_of(name)
            if tgt_layer is None:
                continue
            if src_layer in _FORBIDDEN and tgt_layer in _FORBIDDEN[src_layer]:
                hits.append(name)
            elif src_layer == "api" and tgt_layer == "api":
                # C3：「域内自由，跨域禁止」—— 域 = 源文件所在目录子树。
                # 根层（backend/app/api 直下）模块互引 = 同域，放行（其收敛属 A5 下沉）；
                # 子域（如 platforms/xhs）伸手到别的域（ai / tasks / publish…）= 违规。
                if is_package:
                    dir_pkg = normalized  # 包的 __init__ 的域 = 包自身
                else:
                    dir_pkg = normalized.rsplit(".", 1)[0]
                if name != dir_pkg and not name.startswith(dir_pkg + "."):
                    hits.append(name)
    return hits


def _scan_repo() -> list[tuple[str, str, str]]:
    """全仓扫描，返回 (源文件 posix 相对路径, 目标模块, 源模块) 列表。

    ⚠️ 含**间接击穿**判定（import-linter 的 "even indirectly" 口径）：受管层
    （core/services/api/adapters）import 一个非受管模块（schemas / models /
    apis 等）不算违规，但若那个中转模块自己 import 了禁区层 ⇒ 算源文件的
    间接违规 —— 中转层不是免罪通道。展开只走**非受管**模块（受管层之间的
    依赖关系由三条合同直接管，这里不递归进受管层）。
    """
    transit = _transit_map()
    results: list[tuple[str, str, str]] = []
    for path in sorted(APP.rglob("*.py")):
        module = _module_of(path)
        src_layer = _layer_of(module)
        hits = set(scan_source(
            path.read_text(encoding="utf-8"), module, is_package=path.stem == "__init__"
        ))
        if src_layer in _FORBIDDEN:
            for target in list(hits):
                if _layer_of(target) is None:  # 非受管目标 ⇒ 查它把依赖带到了哪
                    for reached in _expand_through_transit(target, transit):
                        if _layer_of(reached) in _FORBIDDEN[src_layer]:
                            hits.add(f"{target} (经中转 -> {reached})")
        rel = path.relative_to(APP.parent.parent).as_posix()
        for target in sorted(hits):
            results.append((rel, target, module))
    return results


def _transit_map() -> dict[str, set[str]]:
    """非受管模块的直接 import 表（中转展开用；进程内缓存）。"""
    global _TRANSIT_CACHE
    if _TRANSIT_CACHE is None:
        cache: dict[str, set[str]] = {}
        for path in sorted(APP.rglob("*.py")):
            module = _module_of(path)
            if _layer_of(module) is not None:
                continue  # 受管层不当中转
            is_pkg = path.stem == "__init__"
            normalized = module[:-len(".__init__")] if module.endswith(".__init__") else module
            tree = ast.parse(path.read_text(encoding="utf-8"))
            edges: set[str] = set()
            for node in ast.walk(tree):
                edges.update(_resolve_imports(normalized, node, is_package=is_pkg))
            cache[module] = edges
        _TRANSIT_CACHE = cache
    return _TRANSIT_CACHE


_TRANSIT_CACHE: dict[str, set[str]] | None = None


def _expand_through_transit(target: str, transit: dict[str, set[str]], depth: int = 6) -> set[str]:
    """从非受管模块出发，沿中转边展开，返回可达的**受管层**模块集合（限深度防环）。"""
    reached: set[str] = set()
    frontier = [target]
    seen = {target}
    for _ in range(depth):
        nxt: list[str] = []
        for mod in frontier:
            for edge in transit.get(mod, ()):
                layer = _layer_of(edge)
                if layer is not None:
                    reached.add(edge)
                elif edge not in seen:
                    seen.add(edge)
                    nxt.append(edge)
        frontier = nxt
        if not frontier:
            break
    return reached


# ---------------------------------------------------------------------------
# 守卫本体
# ---------------------------------------------------------------------------


def test_no_unregistered_dependency_violations():
    """三条合同：未记账的越层 import = 红（报错直接给出该记的账）。"""
    violations = _scan_repo()
    unregistered = [
        (rel, target) for rel, target, _mod in violations if not _is_exempt(rel, target)
    ]
    assert not unregistered, (
        "发现未记账的越层 import（依赖方向合同，设计 61 §四）。"
        "修掉它，或带理由登记进本文件的 _EXEMPT（清一处销一条）：\n"
        + "\n".join(f"  {rel} -> {target}" for rel, target in unregistered)
    )


def test_every_exemption_still_matches_a_violation():
    """豁免不许变僵尸：违规修掉了、账还挂着 ⇒ 红（强迫同步销账）。"""
    actual = [(rel, target) for rel, target, _mod in _scan_repo()]
    stale = [
        (exempt_rel, prefix)
        for (exempt_rel, prefix) in _EXEMPT
        if not any(rel == exempt_rel and (target == prefix or target.startswith(prefix + "."))
                   for rel, target in actual)
    ]
    assert not stale, (
        "以下豁免已无对应违规（违规被修掉了）——请从 _EXEMPT 销账，别让豁免清单变成僵尸：\n"
        + "\n".join(f"  {rel} -> {target}" for rel, target in stale)
    )


def test_scanner_sees_function_level_and_relative_imports():
    """自检（守卫不许空转）：函数体内 import、相对 import、TYPE_CHECKING 块都能抓到。"""
    # ① 函数级延迟 import（本项目 283 处延迟 import 的形态）
    hits = scan_source(
        textwrap.dedent(
            """
            def helper():
                from backend.app.services.account_service import latest_cookies_for_channel
                return latest_cookies_for_channel
            """
        ),
        "backend.app.core.deps",
    )
    assert hits == ["backend.app.services.account_service"]
    # ② 相对 import（`from ...services import x` 从 core 的下一级出发回溯两级）
    hits = scan_source(
        textwrap.dedent(
            """
            from ...services import presence
            """
        ),
        "backend.app.core.subpkg.mod",
    )
    assert hits == ["backend.app.services"], hits
    # ③ TYPE_CHECKING 块同样计入（运行时不可达 ≠ 依赖不存在）
    hits = scan_source(
        textwrap.dedent(
            """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from backend.app.api.notes import something
            """
        ),
        "backend.app.adapters.xhs.pc_api_adapter",
    )
    assert hits == ["backend.app.api.notes"], hits
    # ④ 反例：services -> core（向下）不违规
    hits = scan_source(
        "from backend.app.core.database import SessionLocal\n",
        "backend.app.services.account_service",
    )
    assert hits == []
    # ⑤ 间接击穿：core import schemas（非受管中转），schemas import services
    #    ⇒ 算 core 的间接违规（import-linter "even indirectly" 口径）。
    transit = {"backend.app.schemas.schedule": {"backend.app.services.schedule_triggers"}}
    reached = _expand_through_transit("backend.app.schemas.schedule", transit)
    assert reached == {"backend.app.services.schedule_triggers"}
    # ⑥ 反例：中转到的也是非禁区层（models）⇒ 空集（没有受管层到达 = 不报）
    reached = _expand_through_transit(
        "backend.app.schemas.schedule", {"backend.app.schemas.schedule": {"backend.app.models.task"}}
    )
    assert reached == set() and _layer_of("backend.app.models.task") is None


# ---------------------------------------------------------------------------
# response_model 覆盖率基线（设计 61 §四 A1 第二条守卫，A6 逐域抬升）
# ---------------------------------------------------------------------------

#: 当前基线（2026-10-24 实测 = 0）。A6 每补齐一个域就把本数字抬上去，只许升不许降。
RESPONSE_MODEL_BASELINE = 0

_HTTP_VERBS = {"get", "post", "put", "patch", "delete"}


def _count_response_model(directory: Path) -> tuple[int, int]:
    """返回 (声明了 response_model 的端点数, 端点总数)。"""
    declared = 0
    total = 0
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr in _HTTP_VERBS
                ):
                    total += 1
                    if any(kw.arg == "response_model" for kw in deco.keywords):
                        declared += 1
    return declared, total


def test_response_model_coverage_baseline():
    """OpenAPI 响应 schema 覆盖率只许升不许降（基线见 RESPONSE_MODEL_BASELINE）。"""
    declared, total = _count_response_model(APP / "api")
    assert declared >= RESPONSE_MODEL_BASELINE, (
        f"response_model 覆盖下降：{declared} < 基线 {RESPONSE_MODEL_BASELINE}"
        f"（端点总数 {total}；A6 逐域补齐后抬基线）"
    )


def test_response_model_counter_is_not_vacuous():
    """自检：计数器对合成源码真能数出来（不许恒 0 空转）。"""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "x.py"
        fake.write_text(
            textwrap.dedent(
                """
                from pydantic import BaseModel

                router = object()

                class Out(BaseModel):
                    ok: bool

                @router.get("/a", response_model=Out)
                async def a():
                    return Out(ok=True)

                @router.post("/b")
                async def b():
                    return {}
                """
            ),
            encoding="utf-8",
        )
        declared, total = _count_response_model(fake.parent)
    assert (declared, total) == (1, 2)
