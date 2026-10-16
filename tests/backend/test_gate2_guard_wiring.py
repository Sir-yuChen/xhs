"""接线守卫：`check_gate2.py`（设计评审自检）必须真的被跑。

> ⚠️ **为什么要有这个文件**：`01_文档先行工作流.md` §2.0/2.1/2.2 与 Gate 2 清单
> 都写了「跑 `.hermes/scripts/check_gate2.py`」—— 但**当时没有任何机制保证它被跑**。
> 本项目已有前车之鉴：`library_item_filter.cjs` / `qr_session_identity.cjs`
> 写在文档里但**从未被任何用例执行**，「有脚本」≠「有验证」（见
> `test_testing_spec_guards.py` 的 `test_every_behaviour_script_is_wired_into_pytest`）。
>
> ⇒ **规矩不接线就是纸面规矩。** 本文件把那条规矩接上线。

真跑（不是「文件存在就通过」）—— 否则它自己也是纸面规矩。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".hermes" / "scripts" / "check_gate2.py"
WORKFLOW = ROOT / "docs" / "00_管理规范" / "01_文档先行工作流.md"
TEMPLATE = ROOT / "docs" / "00_管理规范" / "02_功能设计文档模板.md"
REDLINE = ROOT / "docs" / "00_管理规范" / "03_改动红线.md"
IMPACT = ROOT / ".hermes" / "scripts" / "impact_check.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


class TestGate2ScriptIsAlive:
    def test_script_exists_and_runs(self):
        """脚本存在且能跑起来（退出码 0 —— 它是「提示型」自检，不是硬门禁）。"""
        assert SCRIPT.exists(), f"自检脚本不在：{SCRIPT}"
        r = _run()
        assert r.returncode == 0, f"自检脚本跑失败：\n{r.stdout}\n{r.stderr}"
        assert "Gate 2 自检" in r.stdout, "脚本没输出预期标题 —— 可能被改坏了"

    def test_script_actually_reports_the_three_things(self):
        """三件事都要有输出（抓包快照 / 定论标题复查 / 三层声明）—— 少一件就是退化。"""
        out = _run().stdout
        assert "①" in out and "写动作" in out, "缺「平台写动作 + 抓包快照」那一段"
        assert "②" in out and "定论" in out, "缺「定论标题复查」那一段"
        assert "③" in out and "总-分-总" in out, "缺「总-分-总声明」那一段"

    def test_list_mode_works(self):
        """`--list` 只列不判（给「只想看看有哪些写接口」的人用）。"""
        r = _run("--list")
        assert r.returncode == 0
        assert "②" not in r.stdout, "--list 不应输出第②段（那是判定段）"


class TestGate2RulesAreWiredIntoTheWorkflow:
    """规范里的三件，必须在工作流文档里**真的写着**（防止文档被改回纸面）。"""

    #: ⚠️ **必须断言「节标题」本身，不能只断言「§2.1 这三个字出现过」** ——
    #: 首版就是这个错：把节标题改成「已删节 · 外部平台写接口」后守卫**照样绿**，
    #: 因为 Gate 2 清单里还引用着「（§2.1）」。变异验证当场咬出（ESCAPED）。
    #: ⇒ 宽锚点假绿（本项目已记录多次）：判据要锚在**承重的那处代码/标题**上。
    @pytest.mark.parametrize(
        "heading",
        [
            "#### §2.0 · 功能设计文档的",      # 三层必备结构
            "#### §2.1 · 外部平台写接口",       # 设计阶段抓包
            "#### §2.2 · 业务判据必须写成",     # 判据式 vs 举例式
        ],
    )
    def test_workflow_has_the_section_heading(self, heading: str):
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert heading in text, f"工作流里找不到节标题「{heading}」—— 规范被删了或改名了"

    @pytest.mark.parametrize(
        "marker",
        ["check_gate2.py", "02_功能设计文档模板.md"],
    )
    def test_workflow_names_the_artifacts(self, marker: str):
        """自检脚本 / 模板必须被工作流**点名**（否则读者不知道有这两件东西）。"""
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert marker in text, f"工作流里没有点名 {marker}"

    def test_gate2_checklist_count_matches_the_list(self):
        """Gate 2 的「N 项必查清单全部覆盖」必须与清单实际条数一致。

        ⚠️ 实测踩过（2026-10-15）：清单从七项加到十项，而 Gate 2 那句
        还写着「**七项**必查清单全部覆盖」—— **文档自己自相矛盾**，
        读者按哪句走都错。⇒ 落成机械判据。
        """
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        #: 清单标题（例：**十项必查清单**）里那个数
        import re
        m = re.search(r"\*\*([一二三四五六七八九十]+)项必查清单\*\*", text)
        assert m, "找不到「N 项必查清单」标题"
        declared = cn[m.group(1)]
        #: Gate 2 那句里的数
        m2 = re.search(r"\*\*([一二三四五六七八九十]+)项必查清单全部覆盖\*\*", text)
        assert m2, "Gate 2 里找不到「N 项必查清单全部覆盖」"
        quoted = cn[m2.group(1)]
        assert declared == quoted, (
            f"清单是「{m.group(1)}项」，而 Gate 2 写「{m2.group(1)}项」—— 两处必须同数"
        )
        #: 清单实际条数 —— ⚠️ 必须**切出清单所在区间**再数：
        #: 首版直接全文件 `^\|\s*(\d+)\s*\|` 数出 18 行（把后面所有表格的数字行都算了）
        #: ⇒ 判据太糙，自己误报。判据要能**穷举**（只数这一张表）。
        start = text.index("项必查清单**")
        end = text.index("**拆分规则**", start)
        block = text[start:end]
        rows = re.findall(r"^\|\s*(\d+)\s*\|", block, re.M)
        assert declared == len(rows), (
            f"清单标题说 {declared} 项，表格实际 {len(rows)} 行 —— 又漂了"
        )


class TestTemplateIsUsable:
    def test_template_has_the_zong_fen_zong_sections(self):
        """模板必须是「总—分—总」四段（用户 2026-10-16 口径）。

        ⚠️ 首版是「三层」（业务逻辑 / 接口设计 / 前后端交互）——
        缺「总① 总体设计」、且**前后端没分开**（用户口径要求「分开设计前后端」）。
        """
        import re
        text = TEMPLATE.read_text(encoding="utf-8", errors="replace")
        #: ⚠️⚠️ **必须锚「标题行」，不能全文找词** ——
        #: 变异实测两次 ESCAPED：
        #:  · 把 `## 2.2 底层数据结构` 改名 ⇒ 全文找「底层数据结构」仍命中**开头的说明文字**
        #:  · 把 `## 4.2 端到端链路` 删掉 ⇒ 全文找「端到端链路」仍命中**说明文字**
        #: ⇒ 判据锚在标题行（`^#{1,3} …`），说明文字不算。
        headings = re.findall(r"^#{1,3} .+$", text, re.M)
        joined = "\n".join(headings)

        for k in ("总① · 需求总体设计", "总② · 前后端关联"):
            assert any(k in h for h in headings), f"缺标题「{k}」"
        for k in ("分 · 前端设计", "分 · 后端设计"):
            assert any(k in h for h in headings), f"缺标题「{k}」"

        #: 前端段 / 后端段各自的**标题级**必备节
        assert any("底层数据结构" in h and "表" not in h for h in headings), \
            "缺前端「底层数据结构」标题"
        assert any("底层数据结构（表" in h for h in headings), \
            "缺后端「底层数据结构（表 / 模型）」标题"
        assert any("端到端链路" in h for h in headings), "缺「端到端链路」标题"
        assert any("端到端一张图" in h for h in headings), "缺总①「总体流程（端到端一张图）」标题"

    def test_template_carries_the_gate2_declaration(self):
        """模板带 `<!-- gate2: 三层 … -->` —— 声明的写法以模板为唯一来源。"""
        t = TEMPLATE.read_text(encoding="utf-8", errors="replace")
        assert "<!-- gate2:" in t
        assert "总-分-总" in t, "模板的 gate2 声明须写「总-分-总」"

    def test_template_has_real_examples_not_placeholders_only(self):
        """模板里的示例必须取自**真实踩过的坑**（否则新人抄了也不知道在防什么）。"""
        text = TEMPLATE.read_text(encoding="utf-8", errors="replace")
        assert "颜色分类" in text, "缺 spec 形态不一致的真实例子"
        assert "useCallback" in text or "Popconfirm" in text, "缺时序/竞态的真实例子"

class TestImpactCheckScriptIsAlive:
    """`impact_check.py`（改动影响反查）也必须真的能跑 —— 同「规矩不接线就是纸面规矩」。"""

    def test_script_runs_and_finds_callers(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, str(ROOT / ".hermes/scripts/impact_check.py"), "QUEUE_REGISTRY"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert "反查符号：QUEUE_REGISTRY" in out
        assert "note_queue_service.py" in out, "连注册表本体都没查到 —— 脚本退化了"
        assert "alembic" in out, (
            "没查出迁移里也引用了 QUEUE_REGISTRY —— 这正是「人极易漏」的那类连带，"
            "脚本必须能找出来"
        )

    def test_changed_mode_works(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, str(ROOT / ".hermes/scripts/impact_check.py"), "--changed"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, r.stderr
        assert "工作区干净" in r.stdout or "未提交改动" in r.stdout

    def test_workflow_names_the_tool(self):
        """工作流阶段三必须点名它（否则没人知道有这个工具）。"""
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert "impact_check.py" in text

    def test_workflow_has_the_change_impact_step(self):
        """阶段三必须保留「改动前置评估」（用户 2026-10-15 口径）。

        ⚠️ 变异验证实测：删掉这一整块时守卫**照样绿**（ESCAPED）——
        因为当时没有任何断言钉住它。⇒ 补上。
        """
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert "改动前置评估" in text, "阶段三的「改动前置评估」被删了"
        for marker in ("要改什么？", "改完连带什么？", "怎么验？"):
            assert marker in text, f"三问里缺「{marker}」"
        assert "连带排查四把尺子" in text, "缺「四把尺子」表"
        assert "常见漏项清单" in text or "五类常漏项" in text, "缺「常漏项清单」"

class TestWorkflowCheatSheet:
    """一页速查（`00_需求工作流速查.md`）——开工第一眼看的，必须有且命令真实。"""

    CHEAT = ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md"

    def test_exists(self):
        assert self.CHEAT.exists(), "速查表不在 —— 开工时只能去读 438 行的细则"

    def test_mentions_all_five_stages_and_four_gates(self):
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        for k in ("① 需求", "② 设计", "③ 开发", "④ 测试", "⑤ 沉淀"):
            assert k in t, f"速查表缺阶段 {k}"
        for g in ("Gate 1", "Gate 2", "Gate 3", "Gate 4"):
            assert g in t, f"速查表缺 {g}"

    def test_every_named_script_actually_exists(self):
        """速查表里点名的脚本**必须真实存在** —— 否则照着走会踩空。

        ⚠️ 这是「判据式」：不问「写了没」，问「指的东西在不在」。
        """
        import re
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        #: ⚠️ **只查「路径引用」（含 `/`），不查裸文件名** ——
        #: 首版把裸文件名（`check_gate2.py` 这种**概念提及**）也算进来 ⇒ **误报**。
        #: 判据太糙会自己红（本项目已重复踩过多次）：区分「提到一个名字」与「指向一个路径」。
        named = set(re.findall(r"([\w.-]+/[\w/.-]+\.(?:py|ps1|md))", t))
        #: ⚠️ **占位符不算**（`test_xxx.py` / `<名>` / `NN_` 这类是**示例形态**，不是真路径）——
        #: 首版把它们当路径 ⇒ 自己红（本项目第 N 次「判据太糙」）。
        PLACEHOLDER = ("xxx", "_x.", "<", "NN", "…")
        named = {n for n in named if not any(ph in n for ph in PLACEHOLDER)}
        missing = [n for n in named if not (ROOT / n).exists()]
        assert not missing, f"速查表引用了不存在的路径：{missing}"

    def test_mentions_the_three_heaviest_lessons(self):
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "抓包" in t and "判据式" in t and "纸面规矩" in t


    def test_workflow_has_doc_change_side_effect_list(self):
        """改「流程文档」的专属连带清单必须在（2026-10-15 助手实例：加 Gate 3 条目时
        没回头核对 Gate 4 已覆盖 ⇒ 重复）。"""
        cs = (ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md").read_text(
            encoding="utf-8", errors="replace")
        assert "改流程/规范文档时的专属连带清单" in cs, "速查表缺这张表"
        for k in ("同层的其它 Gate", "自检脚本的判据", "守卫里写死的措辞", "索引"):
            assert k in cs, f"连带清单缺「{k}」"
        wf = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert "改「流程/规范文档」本身时，连带对象与改代码不同" in wf, "工作流缺指向"

    def test_gate3_and_gate4_do_not_duplicate_link_acceptance(self):
        """Gate 3 / Gate 4 **不能对「业务链路验收」提同一件事**（用户 2026-10-15 指出重复）。

        ⚠️ 实测的原状：Gate 3 要「已在验收文档**登记**」，Gate 4 要「验收文档**已更新**（四段完整）」
        —— 措辞几乎一样 ⇒ 读者以为要跑两趟。正确分工：
        **Gate 3 只「挂上号」（登记存在），Gate 4 才写「四段完整」（真走过一遍）**。
        """
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert "挂上号" in text, "Gate 3 的「只登记存在」措辞丢了 —— 又会与 Gate 4 撞车"
        assert "四段完整**只在 Gate 4 判" in text or "只在 Gate 4 判" in text, \
            "缺「四段完整只在 Gate 4 判」这句消歧"
        assert "Gate 3 = 代码级" in text or "Gate 3（开发）" in text, "缺 Gate3/Gate4 分工表"

    def test_cheatsheet_also_disambiguates(self):
        """速查表也要写清分工（只看速查表的人占多数）。"""
        text = (ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md").read_text(
            encoding="utf-8", errors="replace")
        assert "与 Gate 3 的分工" in text
        assert "代码级" in text and "业务级" in text

class TestRedline:
    """改动红线（`03_改动红线.md` + `impact_check.py --redline`）——场景 B 的最小强制清单。"""

    REDLINE = ROOT / "docs" / "00_管理规范" / "03_改动红线.md"
    SCRIPT = ROOT / ".hermes" / "scripts" / "impact_check.py"

    CHEAT = ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md"

    def test_doc_exists_with_four_steps(self):
        t = self.REDLINE.read_text(encoding="utf-8", errors="replace")
        for k in ("① 列清单", "② 反查连带", "③ 定判据", "④ 改完验证"):
            assert k in t, f"红线缺「{k}」"

    def test_redline_rules_are_implemented_in_script(self):
        """⚠️ **规矩要能被执行** —— 触发规则必须在脚本里（否则又是纸面规矩）。"""
        src = self.SCRIPT.read_text(encoding="utf-8", errors="replace")
        assert "REDLINE_RULES" in src, "红线规则没在脚本里实现"
        assert "def redline(" in src

    def test_script_handles_chinese_paths(self):
        """⚠️⚠️ `git diff --name-only` 默认把中文路径转义成八进制 ⇒ 中文路径规则**静默失效**。

        2026-10-15 实测：不加 `core.quotepath=false` 时，「流程文档」规则对着真实改动
        返回「未命中」—— **红线形同虚设**。这是本守卫存在的唯一理由。
        """
        #: ⚠️ **不能只断言「字符串在源码里」** —— 变异实测 ESCAPED：
        #: 把 `core.quotepath=false` 从 git 命令里去掉，源码里那串还在注释/docstring 里
        #: ⇒ 静态断言照样绿，**而中文路径已经静默失效**。
        #: ⇒ 改**行为验证**：真造一个中文路径的改动，看 `--changed` 能不能列出来。
        import subprocess as sp
        probe = ROOT / "docs" / "00_管理规范" / "_redline_probe_中文.md"
        probe.write_text("probe\n", encoding="utf-8", newline="")
        try:
            r = sp.run([sys.executable, str(self.SCRIPT), "--changed"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            out = r.stdout or ""
            assert "00_管理规范" in out, (
                "`--changed` 列不出中文路径的改动（说明 core.quotepath 被去掉或失效）——\n"
                f"{out[:400]}"
            )
        finally:
            probe.unlink(missing_ok=True)

    def test_redline_rules_actually_fire(self):
        """真跑 `redline()` —— **纯函数调用，不碰任何文件**。

        ⚠️⚠️ **首版错法（2026-10-15 事故）**：先 `write_text` 改**真文件**当探针、
        再 `finally` 还原。一旦与变异脚本**并发**或**被超时打断** ⇒ `finally` 没跑完
        ⇒ **真文件留脏**（实测 `backend/app/models/note.py` 尾部被留下 `<!-- redline probe -->`，
        且 `03_改动红线.md` 停在变异态）。
        ⇒ **判据式改法**：直接 import 被测函数、传构造的路径列表 —— **零副作用、可无限并发**。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("_ic", self.SCRIPT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)

        cases = [
            (["backend/app/models/note.py"], "公共层"),
            (["docs/00_管理规范/00_需求工作流速查.md"], "流程/规范文档"),
            (["AGENTS.md"], "文档数字"),
            (["frontend/src/lib/precheck-publish.ts"], "前端契约字段"),
            (["backend/alembic/versions/x_y.py"], "数据库/迁移"),
            (["tests/backend/test_a.py"], "守卫/测试/变异"),
        ]
        for files, expect in cases:
            todos = ic.redline(files, deleted_lines=0)
            names = [t.split("]")[0][1:] for t in todos]
            assert expect in names, f"{files} 没触发「{expect}」：{names}"

        #: ⚠️ **口径已变（2026-10-16）**：原先断言「改页面文件不该触发红线」——
        #: 加入「接缝类」规则后，**改前端页面就该触发**（实测 `fd78357` 三个 bug 单测全绿）。
        #: 反面判据改为「**后端纯逻辑**改动（不涉及公共层/迁移/守卫）不该触发」。
        assert ic.redline(["backend/app/services/some_service.py"], 0) == [], \
            "改后端普通服务文件不该触发红线"
        #: 但前端页面**必须**触发「接缝类」
        seam = ic.redline(["frontend/src/pages/platforms/xhs/my-notes-page.tsx"], 0)
        assert any("接缝类" in t for t in seam), "改前端页面必须触发「接缝类」"

        #: 删除行数 >= 100 触发「删除/退场」
        assert any("删除/退场" in t for t in ic.redline(["a.py"], 100))
        assert not any("删除/退场" in t for t in ic.redline(["a.py"], 0))

    def test_no_bare_python_in_cheatsheet(self):
        """⚠️ 2026-10-16 三轮模拟第 1 轮：速查表用**裸 `python`** —— 而本机裸 python 是
        Hermes venv 的 **3.11.16**，**不是项目的 Py312**（AGENTS §2 一直用完整路径）。
        照抄 ⇒ 跑错解释器。⇒ 命令必须用 `$PY`。
        """
        import re
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        #: ⚠️ 判据是「**行首词**是裸 `python`」（不是「任何位置出现 python」）——
        #: 首版用了 `(?!-m)` 想放过 `python -m pytest`，结果**把 `python -m` 也放过了**
        #: （而那正是最常见的写法）⇒ 变异塞回裸 python 竟 ESCAPED。
        #: 正解：只看**命令起始词**是否 `python`。
        def _starts_with_bare_python(line: str) -> bool:
            cmd = line.strip()
            #: 去掉常见行内注释/提示前缀后，看起始词
            for pre in ("$ ", "# ", "> ", "| "):
                if cmd.startswith(pre):
                    cmd = cmd[len(pre):].lstrip()
            return bool(re.match(r"python(\s|$)", cmd))

        bad = [f"第 {i} 行：{l.strip()[:80]}"
               for i, l in enumerate(t.splitlines(), 1)
               if _starts_with_bare_python(l)]
        assert not bad, "速查表有裸 `python`（应用 `$PY`）：\n" + "\n".join(bad)
        #: 头部必须有 PY 定义
        assert 'PY="C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"' in t

    def test_cheatsheet_documents_pick_tests_output_shape(self):
        """⚠️ 第 1 轮模拟：文档里 `grep '\.py$'` 是**猜的** —— real 输出带注释行。"""
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "没有检测到改动文件" in t, "没写明 pick_tests 的输出形态"
        assert "别同时用" in t and "--lf" in t, "没写明 -x 与 --lf 打架"

    def test_cheatsheet_documents_the_doc_numbering_rule(self):
        """⚠️ 第 1 轮模拟：`NN_功能设计_<名>.md` 的 `NN` 怎么定没说。"""
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "取现有最大号" in t, "没写 NN 编号规则"
        assert "docs/02_设计文档/" in t


    def test_cheatsheet_documents_capture_node_concretely(self):
        """⚠️ 第 2 轮模拟：抓包节点只有 2 行注释 —— 现场下不了手。
        必须点名 ① 现成脚本 `cdp_min.py` ② 已有 926 份可复用 ③ 启动参数出处。
        """
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "cdp_min.py" in t, "没点名现成抓包脚本"
        assert "evidence_live" in t, "没提已有的抓包资产（不能复用）"
        assert "remote-debugging-port" in t, "没写怎么起 CDP Chrome"
        assert (ROOT / ".hermes" / "scripts" / "cdp_min.py").exists(), "点名的脚本不存在"

    def test_mutation_scripts_follow_one_naming_and_convention(self):
        """⚠️ 第 2 轮模拟：命名有 3 种写法 + 调用约定没写。
        必须写明统一命名 + 裸跑无参数 + 基线红会拒跑。
        """
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "mutate_verify_<域>.py" in t, "没写统一命名"
        assert "无参数" in t or "无 `--help`" in t, "没写调用约定"
        #: ⚠️ **锚整句** —— 首版写 `"基线" in t and "拒跑" in t`，两个词**分别命中**
        #: （「基线」在别处到处都是）⇒ 删掉这条仍绿（M5 ESCAPED，第 N 次「分开找词」假绿）。
        assert "基线就是红的，变异验证无意义" in t, "没写「基线红会自己拒跑」这条好设计"

    def test_the_renamed_mutation_script_matches_the_convention(self):
        """⚠️ 我上轮自己写的 `verify_redline_mutation.py` **违反了自己的命名规范**（52:1）——
        已改名 `mutate_verify_redline.py`。守卫钉住：不许再出现反例。
        """
        scripts = list((ROOT / ".hermes" / "scripts").glob("verify_*mutation*.py"))
        assert not scripts, f"变异脚本命名应统一为 `mutate_verify_<域>.py`：{[s.name for s in scripts]}"
        assert (ROOT / ".hermes" / "scripts" / "mutate_verify_redline.py").exists()


    def test_redline_covers_the_acceptance_checklists(self):
        """⚠️ 2026-10-16 三轮模拟第 3 轮（零 bug 正常过程）：改**验收清单**时红线 **0 命中**。

        真因：`REDLINE_RULES["文档数字"]` 的正则只框了 `AGENTS.md`/`README.md`/`docs/02_设计文档/`，
        **漏了 `docs/04_测试文档/`** —— 而 `verify_acceptance_links.py` 的 `LINK_DOC`
        **就是** `04_测试文档/09_…验收清单.md`。改它却不触发三校验 = **漏掉一道真防线**。
        （真实样本 `eba3e24` 就是改清单。）
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_acc", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        for f in ("docs/04_测试文档/09_第二阶段功能点全链路验收清单.md",
                  "docs/04_测试文档/10_前端页面整合验收清单.md"):
            out = " ".join(ic.redline([f], 0))
            assert "文档数字" in out, f"改 {f} 没触发三校验（验收清单是它的校验对象）"

    def test_redline_does_not_over_trigger_on_unrelated_docs(self):
        """反向：改**与校验无关**的文档（沉淀/复盘）**不该**触发三校验（防红线通胀）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_unrel", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        out = " ".join(ic.redline(["docs/05_项目沉淀/05_复盘_商品发布功能开发为何慢.md"], 0))
        assert "文档数字" not in out, "无关文档不该触发三校验"

    def test_gotcha_patterns_and_redline_rules_are_different_things(self):
        """⚠️ 第 3 轮模拟踩到的坑：`GOTCHA_PATTERNS`（反查符号时的「五类常漏项」提示）
        与 `REDLINE_RULES`（`--redline` 用）是**两个不同用途的 dict** ——
        我一开始改错了地方，改完还是 0 命中。守卫钉住：**两者都必须存在且用途不同**。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_two", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        assert hasattr(ic, "GOTCHA_PATTERNS") and hasattr(ic, "REDLINE_RULES")
        assert "① 守卫/测试写死的旧形态" in ic.GOTCHA_PATTERNS, "GOTCHA_PATTERNS 被误改"
        #: 两者的 key 集合不同（不是同一份东西抄两遍）
        assert set(ic.GOTCHA_PATTERNS) != {n for n, _, _ in ic.REDLINE_RULES}


class TestWorkflowTwoChannelsAreWired:
    """⚠️ 2026-10-16 判据式扫出的 4 个缺口 —— 必须钉住，否则下次又被冲掉。

    缺口：① 速查表完全没提「改动红线」（最常用的通道）② 工作流没引用红线文档
         ③ 四文件之间互不引用、无「先读哪个」导航 ④ 速查表里留了个字面 `\n`
    """

    CHEAT = ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md"

    def test_cheatsheet_shows_both_channels(self):
        """速查表必须画两条通道，并给出红线四步 + 自动判定命令。"""
        import re
        text = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        #: ⚠️⚠️ **锚「节标题 + 四步清单」，不能全文找词** ——
        #: 变异实测 ESCAPED：「通道 B」「改动红线」在文档里出现 **3 处**
        #: （导航表 / 流程图 / 节标题）⇒ 只改节标题时全文找词照样命中（假绿）。
        #: 判据锚到**节标题**（`^\*\*通道 B`）+ **紧跟着的四步清单**，这两处不可替代。
        m = re.search(r"^\*\*通道 B · 改动红线\*\*[\s\S]{0,400}", text, re.M)
        assert m, "速查表缺「**通道 B · 改动红线**」节标题（不是导航表/流程图里那两处提及）"
        sect = m.group(0)
        for step in ("列清单", "反查连带", "定判据", "改完验证"):
            assert step in sect, f"红线节里缺第「{step}」步"
        assert "03_改动红线.md" in sect, "红线节没指向细则文档"
        #: 通道 A 同理锚标题
        assert re.search(r"^\*\*通道 A · 五阶段\*\*", text, re.M), "缺「**通道 A · 五阶段**」节标题"
        assert "impact_check.py --changed" in text, "速查表没给自动判定命令"
        #: 例外情形（小改动触及公共层）必须写明
        assert "回设计文档同步" in text

    def test_cheatsheet_has_reading_navigation(self):
        """速查表必须有「四个文件，先读哪个」导航。"""
        text = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "先读哪个" in text
        for f in ("01_文档先行工作流.md", "02_功能设计文档模板.md", "03_改动红线.md"):
            assert f in text, f"导航缺「{f}」"

    def test_main_workflow_points_to_the_redline(self):
        """工作流首部必须分流到「通道 B」，否则读者以为所有改动都走五阶段。"""
        text = WORKFLOW.read_text(encoding="utf-8", errors="replace")
        assert "通道 B" in text and "03_改动红线.md" in text
        assert "本文是「通道 A · 新需求」的细则" in text

    def test_cheatsheet_has_no_literal_backslash_n(self):
        """⚠️ 字面 `\n` 漏改过一次（替换时转义多了一层）—— 表格里不许出现。"""
        text = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            assert "\\n" not in line, f"第 {i} 行有字面 \\n：{line[:80]}"

class TestDivisionIsByFailureMode:
    """⚠️ 2026-10-16 决定：**保持一条工作流，分流判据从「场景名」改成「失败模式」**。

    实证：commit 前缀会**反向指错** —— `fd78357`（前缀 `feat`）实为交互修正；
    `afd944f`（前缀 `fix`）实为删 5 个模块的地基变更。⇒ 按「这次改动的失败模式」分。
    """

    CHEAT = ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md"

    def test_cheatsheet_uses_failure_modes_not_scenario_names(self):
        """速查表分流判据必须是「失败模式」，且点名两个反向指错的实证。"""
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        for m in ("地基塌", "接缝错", "数据/环境", "口径变"):
            assert m in t, f"速查表缺失败模式「{m}」"
        #: 必须写明「前缀会反向指错」并给出两个实证 hash
        assert "反向指错" in t, "速查表没写「前缀会反向指错」"
        for h in ("fd78357", "afd944f"):
            assert h in t, f"速查表缺实证 {h}"

    def test_redline_is_scoped_by_failure_mode(self):
        """改动红线首部必须按失败模式划适用范围，且含「接缝类必须跑真链路」。"""
        t = REDLINE.read_text(encoding="utf-8", errors="replace")
        assert "地基塌" in t and "接缝错" in t
        assert "必须跑一条真链路" in t, "红线缺「接缝类必须跑真链路」"
        #: 要给出「单测全绿」的实证
        assert "单测全绿" in t
        assert "fd78357" in t

    def test_impact_check_flags_seam_class(self):
        """`impact_check.py` 必须对前端交互改动报「接缝类」并给真链路要求。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_t", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        out = ic.redline(["frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"], 0)
        joined = " ".join(out)
        assert "接缝类" in joined, "前端交互改动没报「接缝类」"
        assert "真链路" in joined, "接缝类没要求跑真链路"

    def test_impact_check_escalates_big_deletions(self):
        """大删除（`afd944f`）必须提示**升级走五阶段**。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_d", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        out = ic.redline(["backend/app/models/workflow.py"], 3206)
        #: ⚠️ **锚「删除/退场」那一条**，不能查整个 joined ——
        #: 变异实测 ESCAPED：删掉删除规则里的「走五阶段」后，**「公共层」规则里也有**
        #: 「建议走五阶段」⇒ 全文查「五阶段」被别处满足（假绿）。
        rule = [o for o in out if o.startswith("[删除/退场]")]
        assert rule, "大删除没报「删除/退场」"
        assert "五阶段" in rule[0], "删除/退场那条没提示升级走五阶段"

    def test_impact_check_stays_silent_for_pure_frontend_doc(self):
        """反向：纯前端交互改动**不该**命中「公共层/迁移」（避免红线通胀）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("ic_s", IMPACT)
        ic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ic)
        out = " ".join(ic.redline(["frontend/src/pages/platforms/xhs/xxx-page.tsx"], 0))
        assert "公共层" not in out
        assert "数据库/迁移" not in out

class TestSeamClassHasAWayOut:
    """⚠️ 2026-10-16 用「商品发布」真实返工模拟跑出的**第一个真缺口**。

    模拟发现：新规则「接缝类必须跑真链路」在**用户不在场时无法执行**
    （要登录千帆 / 会写真实数据）—— 规则**没给出路** ⇒ 会在没人时卡住，
    或更糟：让人**假装跑过**（本项目已吃过「说验证过但其实没跑」的亏）。
    修法：跑不了 **允许**，但必须 ① 登记攒批清单 ② 汇报里明说「未真跑」。
    """

    CHEAT = ROOT / "docs" / "00_管理规范" / "00_需求工作流速查.md"

    def test_redline_has_the_way_out(self):
        t = REDLINE.read_text(encoding="utf-8", errors="replace")
        assert "跑不了怎么办" in t, "红线缺「跑不了怎么办」小节"
        assert "08_真机验收攒批清单.md" in t, "没给登记去处"
        assert "未真跑" in t, "没要求汇报时明说未真跑"

    def test_cheatsheet_has_the_way_out(self):
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "必须跑一条真链路" in t
        assert "跑不了" in t, "速查表缺「跑不了怎么办」"
        assert "08_真机验收攒批清单.md" in t

    def test_the_registry_file_named_actually_exists(self):
        """⚠️ 点名的路径必须**真存在** —— 否则「登记进某个不存在的清单」是空话。"""
        f = ROOT / "docs" / "04_测试文档" / "08_真机验收攒批清单.md"
        assert f.exists(), f"点名的登记去处不存在：{f}"

    def test_both_docs_name_the_same_registry(self):
        """⚠️ **同一条规则在两份文档里各写一遍 ⇒ 必须指同一个去处**（防双真相源）。"""
        import re
        a = REDLINE.read_text(encoding="utf-8", errors="replace")
        b = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "08_真机验收攒批清单.md" in a and "08_真机验收攒批清单.md" in b, \
            "两份文档必须指向同一个攒批清单"

    def test_forbids_claiming_verified_without_evidence(self):
        """必须写明「说『已验证』就要能指出运行证据」这条禁令。"""
        for t in (REDLINE.read_text(encoding="utf-8", errors="replace"),
                  self.CHEAT.read_text(encoding="utf-8", errors="replace")):
            assert "已验证" in t and "运行证据" in t
