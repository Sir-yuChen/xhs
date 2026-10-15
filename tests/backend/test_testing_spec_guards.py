"""测试规范的可执行守卫 —— 把「约定」变成「红了才算数」。

这些不是普通用例，是**防止回退的钉子**。每一条都对应一次真实的踩坑：

* **禁止固定 `sleep(N>=100)`**：`add_account_modal.cjs` 里 38 次、`qr_session_identity.cjs`
  里 6 次固定 sleep，本仓实测让全量回归白等 **18.5s**，又因为每个脚本被 pytest 调 6 次
  ⇒ **96 秒纯浪费**（占全量 20%）。已改成 `waitFor(条件)`；这里保证**不许改回去**。
* **禁止新的 engine 样板**：conftest 已提供夹具，新文件再抄 `_override_database()`
  就是重复代码。既有文件在白名单里（迁移是独立工作，见规范文档的清单）。
* **`node` 必须可用**：行为脚本全部靠它，node 不在 PATH 上时应当**明确失败**，
  而不是静默 skip（静默 skip = 守卫形同虚设）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "tests" / "backend"
FRONTEND_DIR = ROOT / "tests" / "frontend"

#: 本文件自己（守卫里出现 `create_engine(` / `dependency_overrides` 字样是在**读源码**，不是在建 engine）。
THIS_FILE = Path(__file__).name

#: 固定 sleep 的阈值：>= 100ms 视为「盲等」（React 首帧 / 轮询 tick 都在几十毫秒级，
#: 100ms 以上几乎一定是没去想「到底在等什么」）。
SLEEP_FLOOR_MS = 100

#: 允许固定 sleep 的白名单 —— **必须是「真的在等真实时间」的场景**，且每条都要写明理由。
#: 新增条目只减不增；发现新的盲等一律先改 `waitFor`。
SLEEP_ALLOWLIST: dict[str, str] = {}

#: 仍在用「自建 engine」老样板的文件（迁完一个划一个，目标清零）。
#: ⚠️ 这份清单由 `test_legacy_allowlist_matches_reality` **逐条核对**：
#: 多列、漏列、列了不存在的文件都会红 —— 所以它不可能悄悄过期。
LEGACY_ENGINE_FILES: set[str] = {
    "test_account_metrics.py",
    "test_api.py",
    "test_cookie_channels.py",
    "test_initial_data.py",
    "test_login_state.py",
    "test_note_cleanup.py",
    "test_note_content_backfill.py",
    "test_note_fields.py",
    "test_note_published.py",
    "test_note_sync.py",
    "test_note_type.py",
    "test_product_note.py",
    "test_product_note_publish.py",
    "test_scheduled_jobs.py",
    "test_self_content.py",
    "test_shop_goods_sync.py",
    "test_shop_health.py",
    "test_shop_items.py",
    "test_shop_nm.py",
    "test_task_cancel.py",
    "test_uniqueness.py",
}


def _strip_comments(source: str) -> str:
    """去掉块注释与行注释，免得断言被自己的解释性注释命中（DEF-058 的教训）。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)//.*$", "", source)


def _legacy_engine_files() -> set[str]:
    """实际仍在用 `create_engine(` + `dependency_overrides` 三件套的文件。"""
    found: set[str] = set()
    for path in sorted(BACKEND_DIR.glob("test_*.py")):
        if path.name == THIS_FILE:
            continue
        source = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
        if "create_engine(" in source and "dependency_overrides" in source:
            found.add(path.name)
    return found


# --------------------------------------------------------------------------- #
# ① 前端行为脚本：不许有盲等
# --------------------------------------------------------------------------- #

def test_no_blind_sleep_in_frontend_behaviour_scripts():
    """`tests/frontend/*.cjs` 不许有固定 `sleep(N>=100)`。

    这条是本次优化的**核心守卫**：盲等既慢又 flaky，且会被 pytest 的调用次数
    成倍放大。等异步就 `waitFor(条件, {timeout})` —— 一就绪就继续。
    """
    offenders: list[str] = []
    for script in sorted(FRONTEND_DIR.glob("*.cjs")):
        if script.name in SLEEP_ALLOWLIST:
            continue
        code = _strip_comments(script.read_text(encoding="utf-8"))
        for match in re.finditer(r"sleep\(\s*(\d+)\s*\)", code):
            if int(match.group(1)) >= SLEEP_FLOOR_MS:
                offenders.append(f"{script.name}: sleep({match.group(1)})")
    assert not offenders, (
        "固定 sleep 会把回归拖慢且 flaky，改用 waitFor(条件, {timeout})。"
        f" 命中：{offenders}"
    )


def test_jsdom_scripts_must_provide_waitfor():
    """用 jsdom 驱动 React 的脚本**必须**有 `waitFor`。

    精确判据是「用了 jsdom」而不是「有 await」—— 大部分脚本的 `await` 只是
    `await esbuild.build(...)` 和直接 await 一个 promise，压根没有 UI 等待，
    要求它们也带 waitFor 是噪音。真正需要 waitFor 的是那两个渲染真实组件的脚本。

    ⚠️ 判据用 `\\b` 词边界，**不能**用裸 `in`：`function waitFor` 是
    `function waitForeverX` 的子串，裸 `in` 会让「改了名」这种变异静默漏过
    （DEF-058 同型：负向断言被子串命中 ⇒ 恒真 ⇒ 守卫变成摆设）。
    """
    missing: list[str] = []
    declared = re.compile(r"\b(?:function|const|let|var)\s+waitFor\b")
    for script in sorted(FRONTEND_DIR.glob("*.cjs")):
        code = _strip_comments(script.read_text(encoding="utf-8"))
        if "jsdom" not in code.lower() and "JSDOM" not in code:
            continue
        if not declared.search(code):
            missing.append(script.name)
    assert not missing, (
        "jsdom 脚本必须提供 waitFor(条件, {timeout}) 来等渲染，不要用固定 sleep。"
        f" 命中：{missing}"
    )


# --------------------------------------------------------------------------- #
# ② 后端测试：不许再抄 engine 样板
# --------------------------------------------------------------------------- #

def test_new_test_files_must_not_duplicate_the_engine_boilerplate():
    """新测试文件不许再抄 `create_engine` + `create_all` + `dependency_overrides` 三件套。

    既有文件在 `LEGACY_ENGINE_FILES` 白名单里（迁移是独立工作，见规范文档清单）。
    **新文件进白名单 = 违规**，所以新增文件必须用 `tests/backend/conftest.py` 的夹具。
    """
    legacy = _legacy_engine_files()
    offenders = sorted(legacy - LEGACY_ENGINE_FILES)
    assert not offenders, (
        "新测试文件请用 tests/backend/conftest.py 的夹具（db_engine / db_factory / api_client），"
        f"不要自建 engine。命中：{offenders}"
    )


def test_legacy_allowlist_matches_reality():
    """白名单必须**正好**等于现实：多列 / 漏列 / 列了不存在的文件都要红。

    没有这条，白名单就会悄悄过期（本次初版就同时错了 11 处：9 条陈旧 + 2 条漏列），
    而守卫会变成一个「永远绿但什么都没守」的摆设。
    """
    actual = _legacy_engine_files()
    stale = sorted(LEGACY_ENGINE_FILES - actual)
    missing = sorted(actual - LEGACY_ENGINE_FILES)
    assert not stale, f"白名单里有过期条目（这些文件已经不抄样板了，请划掉）：{stale}"
    assert not missing, f"有文件在抄样板却没进白名单（请补登记，并排迁移）：{missing}"


def test_legacy_engine_allowlist_is_shrinking_not_growing():
    """白名单只许减不许增 —— 防止「把新文件加进白名单」变成绕过守卫的后门。"""
    assert len(LEGACY_ENGINE_FILES) <= 21, (
        "LEGACY_ENGINE_FILES 只应在迁移中变小；变大了说明有人在往里加文件。"
        f" 当前 {len(LEGACY_ENGINE_FILES)} 条"
    )


# --------------------------------------------------------------------------- #
# ③ 运行环境与覆盖面
# --------------------------------------------------------------------------- #

def test_node_is_available_for_behaviour_scripts():
    """node 必须在 PATH 上；缺失就**明确失败**，不许静默 skip。"""
    assert shutil.which("node"), (
        "node 不在 PATH 上 ⇒ 前端行为脚本全部无法运行。"
        "装好 node 再跑，别让守卫退化成 skip。"
    )
    result = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("v"), result.stdout


def test_conftest_fixtures_are_really_shared():
    """conftest 必须真的提供那几件套（防止有人把夹具删了还以为测试在跑）。"""
    from tests.backend import conftest as shared

    for name in ("db_engine", "db_factory", "db_session", "api_client", "client", "frontend_runner"):
        assert hasattr(shared, name), f"conftest 缺少 fixture: {name}"


def _strip_py_noise(source: str) -> str:
    """去掉三引号字符串与 `#` 行注释 —— 免得「文档字符串 / 注释里提一句」算接线证据。

    ⚠️ 2026-09-26 变异验证实录：只去文档字符串时，`test_login_state.py` 里一句
    `# 2026-09-26 接线：qr_session_identity.cjs（…）` 的注释**仍被算成接线**
    （判定只有 `script in code`，而 `script` 是裸文件名）⇒ 把真接线常量整段删掉，
    守卫依然绿。所以判定必须同时收紧到**路径形态 / 调用形态**，并去掉行注释。
    """
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"(?m)#.*$", "", source)


def _executes_node(code: str) -> bool:
    """该测试文件是否真的会拉起一个 node 进程（行为脚本的唯一执行通路）。"""
    return bool(re.search(r"subprocess\.(run|Popen)\(", code)) or "frontend_runner(" in code


def _mentions(script: str, code: str) -> bool:
    """`code` 里是否有**执行形态**的引用（裸文件名出现在注释里不算）。

    两种形态都要认：
    * `ROOT / "tests/frontend/x.cjs"` / `XXX_SCRIPT = "tests/frontend/x.cjs"`（路径形态）
    * `frontend_runner("x.cjs")`（调用形态，conftest 夹具直接吃文件名）
    """
    if f"tests/frontend/{script}" in code:
        return True
    return bool(re.search(r'frontend_runner\(\s*"' + re.escape(script) + r'"', code))


def _wired_by(script: str) -> list[str]:
    """哪些测试文件**在执行形态上**引用了 `script`，且该文件真的会跑 node。

    ⚠️ 两点都比字面看起来重要：

    * **必须排除本文件**：`THIS_FILE` 的 parametrize 参数列表里就写着这些脚本名，
      把它算进 haystack ⇒ 断言对每个条目恒真（**这条守卫 2026-09-24 建起到
      2026-09-26 一直是恒真的**：`library_item_filter.cjs` 与
      `qr_session_identity.cjs` 当时都没有任何 pytest 用例执行过）。
    * **必须去掉文档字符串与注释**，且只认执行形态（见 `_mentions`）。
    """
    hits: list[str] = []
    for path in sorted(BACKEND_DIR.glob("test_*.py")):
        if path.name == THIS_FILE:
            continue
        code = _strip_py_noise(path.read_text(encoding="utf-8", errors="ignore"))
        if _mentions(script, code) and _executes_node(code):
            hits.append(path.name)
    return hits


@pytest.mark.parametrize(
    "script",
    [
        "add_account_modal.cjs",
        "detail_message.cjs",
        "library_item_filter.cjs",
        "library_topics_merge.cjs",
        "library_topic_search.cjs",
        "login_state.cjs",
        "note_filters.cjs",
        "note_publish.cjs",
        "persisted_filters.cjs",
        "qr_session_identity.cjs",
        "schedule_center.cjs",
        "settings_view.cjs",
        "shop_items_query.cjs",
        "shop_items_view.cjs",
        "sync_gates.cjs",
    ],
)
def test_every_behaviour_script_is_wired_into_pytest(script: str):
    """每个 `.cjs` 都必须被某个测试文件的**可执行代码**引用（否则它只是个没人跑的文件）。

    DEF-063 同型：脚本存在 ≠ 在被守。`qr_session_identity.cjs` 就曾经因为
    mock 桩缺导出而**打包期即死**，整脚本长期空转还没人发现。
    """
    wired = _wired_by(script)
    assert wired, (
        f"{script} 没有被任何 pytest 文件的**可执行代码**引用 ⇒ 它不在守卫范围内。"
        "要么接线，要么删掉。（只在文档字符串 / 本守卫的参数列表里出现不算。）"
    )


def test_wiring_guard_is_not_vacuous():
    """守卫自检：一个不存在的脚本名必须判「没接线」。

    没有这条，把判定写宽（比如又把本文件算进 haystack）就会**静默退化成恒真** ——
    这条守卫正是这么退化了整整两天。一个永远 PASS 的校验脚本和没有校验一样。
    """
    assert _wired_by("definitely_not_a_behaviour_script.cjs") == []


#: 支持 `--self-check --full`（契约 + 全部变异）的行为脚本。
#: ⚠️ 2026-10-14 拆分（用户口径「拆」）：这两个脚本的变异逐个都要 jsdom/esbuild，
#: 且 pytest 用模块级 fixture **串行**跑 ⇒ 每轮全量回归固定多花 ~3 分钟
#: （实测 `add_account_modal --full` = **2m51s**、`login_state --full` = 0.35s）。
#: 现在默认只跑契约，变异收在 `--full` 后面。
#:
#: 拆分最大的风险 = **变异被悄悄丢光**（`--full` 分支写错 / 清单被清空），
#: 那样「守卫承重」的证据链就断了，而且**契约照样全绿** ⇒ 静默退化。
#: 所以这里钉两件事：① 两个模式都真的实现；② **变异清单非空**。
FULL_MODE_SCRIPTS = ("add_account_modal.cjs", "login_state.cjs")


@pytest.mark.parametrize("script", FULL_MODE_SCRIPTS)
def test_frontend_scripts_expose_a_full_mutation_mode(script: str):
    """`--full` 必须存在，且**变异清单非空**（拆分不能把变异丢光）。

    判据读**源码**（脚本是 node 的，pytest 这边只能静态看）：
    1. 有 `process.argv.includes("--full")` —— 否则 `--full` 不生效，
       变异**永远不会被跑**，而契约仍然全绿（最坏的静默退化）；
    2. 有非空的变异清单（`VARIANTS = [...]` 或 `MODAL_VARIANTS.slice(1)`）
       —— 否则「全部变异」是个空集，`--full` 也证明不了任何事。

    ⚠️ 为什么值得一条守卫：这两个脚本的契约与变异**共用同一个入口**，
    拆分时把变异分支写错（或漏掉 `--full` 判断）不会有任何红 ——
    日常回归只会看到「契约 PASS」，丢失的是「守卫真的承重」这条结论。
    """
    src = (ROOT / "tests" / "frontend" / script).read_text(encoding="utf-8")

    assert 'process.argv.includes("--full")' in src, (
        f"{script} 没有实现 `--full` ⇒ 变异永远不会被跑（契约仍会全绿，静默退化）"
    )
    #: 变异清单：add_account_modal 用 `MODAL_VARIANTS.slice(1)`；
    #: login_state 用 `VARIANTS`（首项是契约）。
    has_list = (
        "MODAL_VARIANTS.slice(1)" in src
        or re.search(r"^const VARIANTS = \[[^\]]+\]", src, flags=re.M) is not None
    )
    assert has_list, f"{script} 找不到非空的变异清单 ⇒ `--full` 会跑一个空集"



# ---------------------------------------------------------------------------
# 记忆预算守卫（2026-09-25）
#
# 这一组守的是「会话收尾纪律」—— 此前它**只写在文档里**，没有任何机械校验，
# 于是主记忆从 8KB 漂到 12.9KB（+61%），同时 `.hermes/memory/2026-09-24.md`
# 损坏了 15% 也没人知道。规矩不接线就是纸面规矩。
# ---------------------------------------------------------------------------

#: 记忆预算脚本在 `.hermes/scripts/`（不是 `.cjs`），所以不进上面那张 .cjs 清单。
# --------------------------------------------------------------------------- #
# 【已退役 2026-10-05】记忆预算守卫（`verify_memory_budget.py`）
# --------------------------------------------------------------------------- #
# 原口径（2026-09-25 立规）：`.workbuddy/memory/` 下的主记忆 ≤12KB、`REF_*.md` 单文件
# ≤25KB、单日日志 ≤25KB 且全部必须是合法 UTF-8。
#
# **为什么退场**：用户 2026-10-05 口径「项目不再使用 WorkBuddy，后续完全使用 Hermes
# 开发」⇒ 记忆不再以**文件**为真相源，改为**蒸馏入向量库**（Hindsight bank=hermes，
# 检索走 `hindsight_recall`）。约束的对象（那些文件）随之消失，判据没有载体。
#
# ⚠️ **退役 ≠ 结论被推翻**：三条实测教训（主记忆曾从声明 8KB 漂到 12.9KB 无人报警；
# 单日日志曾 7 天 368KB；`.workbuddy/memory/2026-09-24.md` UTF-8 损坏 15% 长期无人发现）
# 仍然是本仓的真教训，原文见 `docs/04_测试文档/01` 的记忆预算小节与当日日志。
# 若将来又把长期记忆落成文件，**预算 + 可解码性判据必须一起回来**（两件事：额度要有
# 机械判据、内容要能解码）。
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 业务链路验收手册自洽守卫（2026-10-04 立规 · `04_测试文档/01` §14.4）
# --------------------------------------------------------------------------- #
# `09_第二阶段功能点全链路验收清单.md` 是「用户验收主路径」。它的**链路总览表**
# 与**正文链路**是同一件事的两份写法 —— 加了链路忘改总览（或反之）是这类文档最典型
# 的漂移，肉眼看不到。规矩不接线就是纸面规矩，故落成机械判据。
LINK_VERIFY_SCRIPT = (
    ROOT / ".hermes" / "skills" / "xhs-doc-stats-verify" / "verify_acceptance_links.py"
)


#: 变异脚本（同时提供 `--baseline` / `--only` 两种模式，见下）。
LINK_MUTATE_SCRIPT = (
    ROOT / ".hermes" / "scripts" / "mutate_verify_acceptance_links.py"
)


def _run_link_mutator(*args: str) -> subprocess.CompletedProcess[str]:
    """跑变异脚本的某个模式。

    ⚠️ **基线也走变异脚本的 `--baseline`，不直接跑守卫脚本**：变异是**原地改文件**，
    `-n auto` 下别的 worker 可能正把变异体写在盘上 ⇒ 直接跑守卫会读到变异内容而**假红**
    （实测：7 个变异参数化在 xdist 下必红）。`--baseline` 持同一把跨进程文件锁，才是稳的。
    """
    return subprocess.run(
        [sys.executable, str(LINK_MUTATE_SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_acceptance_link_manual_is_self_consistent():
    """功能点全链路验收清单必须自洽。

    判据六件：①链路编号连续 ②总览表==正文 ③四要素齐 ④覆盖列==细目
    ⑤功能点与勾选位 1:1 且编号不重复 ⑥README + AGENTS 两处索引已登记。
    """
    result = _run_link_mutator("--baseline")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OVERALL: PASS" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ("toc_drop_last_chain", "总览表行数"),
        ("toc_wrong_step_count", "步数不符"),
        # 链路末尾允许挂「预走查实测」辅助表 ⇒ 守卫必须只认表头写着「步」的那种表
        ("step_table_counts_aux_table", "步数不符"),
        ("remove_verdict_block", "缺要素"),
        ("remove_breakpoint_block", "缺要素"),
        ("readme_unregistered", "README"),
        ("agents_unregistered", "AGENTS"),
        ("chain_number_gap", "编号不连续"),
        ("drop_functional_point", "覆盖列声明的功能点缺失"),
        ("drop_point_marker", "不是 1:1"),
        ("duplicate_functional_point", "覆盖列"),
    ],
)
def test_acceptance_link_guard_is_load_bearing(mutate: str, expected: str):
    """变异验证：**不另抄一份变异逻辑**，直接复用 `.hermes/scripts/mutate_verify_acceptance_links.py`。

    这样「守卫会不会红」这件事只有一个实现（DEF-044 教训）。脚本自己负责还原与
    收尾复核，故这里只断言它在对应变异下确实报出预期的 FAIL 文案。
    """
    result = _run_link_mutator("--only", mutate)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"[{mutate}] CAUGHT" in result.stdout, result.stdout
    assert expected in result.stdout, result.stdout


# --------------------------------------------------------------------------- #
# 模块层 Session 工厂必须全部打桩（2026-09-27 立规）
# --------------------------------------------------------------------------- #
#: 打桩清单**必须**覆盖的最小集合（判据落在具体模块上，不是「列表非空」）。
#: 这四个是「端点链路最容易走到、漏了就直接写生产库」的模块。
CRITICAL_SESSION_MODULES = {
    "backend.app.services.scheduled_job_runner",
    "backend.app.services.scheduler_service",
    "backend.app.services.note_sync_service",
    "backend.app.services.sync_task_service",
}


def test_service_session_registry_covers_every_session_owner():
    """AST 扫出来的模块清单必须真的覆盖持有方（否则打桩是空转）。

    为什么要有这条：`patch_service_sessions` 的清单是**扫出来的**，扫法本身写错
    （例如只认 `import` 不认赋值、或把 `ast.walk` 写成只看第一个节点）时它会安静地
    返回一个短清单 —— 打了桩的模块变少，测试**照样全绿**，而请求已经写进生产库。
    """
    from tests.backend.conftest import service_session_modules

    modules = set(service_session_modules())
    missing = CRITICAL_SESSION_MODULES - modules
    assert not missing, f"打桩清单漏了关键模块：{sorted(missing)}"
    for name in sorted(modules):
        module = __import__(name, fromlist=["_"])
        assert any(
            hasattr(module, attribute) for attribute in ("SessionLocal", "_session_factory")
        ), f"{name} 被列入清单却两个名字都没有 —— 打桩会空转"


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.app.services.scheduled_job_runner",
        "backend.app.services.scheduler_service",
        "backend.app.services.note_sync_service",
        "backend.app.services.account_task_service",
    ],
)
def test_patch_service_sessions_replaces_the_factory(monkeypatch, module_name):
    """变异验证：打桩必须**真的换掉**模块属性（含 `_session_factory` 快照别名）。

    `account_task_service._session_factory` 是「import 时取的另一模块属性」的**快照** ——
    只 patch `note_queue_service.SessionLocal` 对它无效。这正是最容易漏的一种持有方式，
    所以它单独占一个参数分支（漏了它 = 账号任务 worker 在测试里写生产库）。
    """
    from tests.backend.conftest import patch_service_sessions

    sentinel = object()
    patched = patch_service_sessions(monkeypatch, sentinel)

    module = __import__(module_name, fromlist=["_"])
    assert any(name.startswith(f"{module_name}.") for name in patched), patched
    # ⚠️ 两个名字不一定都在（`account_task_service` 只有 `_session_factory` 快照），
    # 所以用 `getattr` 取值：直接写 `module.SessionLocal` 会在缺属性时抛 AttributeError，
    # 失败原因变成「属性不存在」而不是「没替换成功」（判据打偏）。
    assert getattr(module, "SessionLocal", None) is sentinel or (
        getattr(module, "_session_factory", None) is sentinel
    ), f"{module_name} 未被替换：patched={patched}"

def test_terminal_statuses_agree():
    """「终态」只能有一个集合：队列侧（字面量）与同步台账（按 `STATUS_*` 组合）必须一致。

    漂移的代价很具体：队列认为任务结束了、同步台账的 `await_task_terminal` 却继续等
    ⇒ 账号卡点了同步之后，定时任务日志会**一直停在 running**（看起来像卡死），
    而任务其实早就结束了。
    """
    from backend.app.services import note_queue_service, sync_task_service

    assert set(sync_task_service.TERMINAL_STATUSES) == set(note_queue_service.TERMINAL_STATUSES), (
        "两处终态定义漂移："
        f"{sorted(sync_task_service.TERMINAL_STATUSES)} vs "
        f"{sorted(note_queue_service.TERMINAL_STATUSES)}"
    )
