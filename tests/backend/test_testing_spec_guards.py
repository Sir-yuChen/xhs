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



# ---------------------------------------------------------------------------
# 记忆预算守卫（2026-09-25）
#
# 这一组守的是「会话收尾纪律」—— 此前它**只写在文档里**，没有任何机械校验，
# 于是主记忆从 8KB 漂到 12.9KB（+61%），同时 `.workbuddy/memory/2026-09-24.md`
# 损坏了 15% 也没人知道。规矩不接线就是纸面规矩。
# ---------------------------------------------------------------------------

#: 记忆预算脚本在 `.workbuddy/scripts/`（不是 `.cjs`），所以不进上面那张 .cjs 清单。
MEMORY_BUDGET_SCRIPT = ROOT / ".workbuddy" / "scripts" / "verify_memory_budget.py"


def _run_memory_budget(**env: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(env)
    return subprocess.run(
        [sys.executable, str(MEMORY_BUDGET_SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )


def test_memory_budget_script_is_wired_and_green():
    """主记忆 / `REF_*.md` / 日志的**预算 + 可解码性**必须全绿。

    2026-09-25 实测三件事，各自都值得一条钉子：

    ① 主记忆立了「≤8KB」却长期无人机械校验 ⇒ 实际 12.9KB。**实测地板 ≈10KB**
       （把叙述性内容全部下移到 `REF §G` 后仍要 10KB，再压只能删规则），
       故额度校准为 **12KB**（留 ~2KB 给后续新规范）。
    ② 单日日志**从来没有上限**：7 天合计 368KB / 144,537 token，单文件最高
       60.7KB。现钉 **≤25KB**（立规前的历史文件在白名单里只报不判）。
    ③ `.workbuddy/memory/2026-09-24.md` 已损坏 15%（5,529 个字符的末字节被 `?`
       顶掉 ⇒ 整份文件 UTF-8 解码抛异常）。它此前只是「文档里说按需 grep」，
       **没有任何脚本碰过它**，所以烂了很久没暴露 ⇒ 可解码性必须有判据。
    """
    result = _run_memory_budget()
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "0 FAIL" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ("memory", "主记忆 "),
        ("ref", "REF "),
        ("log", "日志超限"),
        ("utf8", "UTF-8 判据失灵"),
    ],
)
def test_memory_budget_guards_are_load_bearing(mutate: str, expected: str):
    """变异验证：额度／判据被压坏时脚本必须变红（否则它没在守任何东西）。

    断言带 `FAIL  ` **前缀**：只找 `主记忆` 的话，全绿时那行 `PASS  MEMORY.md`
    同样命中，等于什么都没断言（DEF-058 的教训）。
    """
    result = _run_memory_budget(MEMORY_BUDGET_MUTATE=mutate)
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败：stdout={result.stdout}"
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"


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
