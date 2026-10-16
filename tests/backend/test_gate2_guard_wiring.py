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
        assert "③" in out and "三层" in out, "缺「三层声明」那一段"

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
    def test_template_has_the_three_layers(self):
        """模板必须有三层小节（作者复制它时不会漏）。"""
        text = TEMPLATE.read_text(encoding="utf-8", errors="replace")
        assert "业务逻辑与判据" in text
        assert "接口设计" in text
        assert "前后端数据交互" in text

    def test_template_carries_the_gate2_declaration(self):
        """模板带 `<!-- gate2: 三层 … -->` —— 声明的写法以模板为唯一来源。"""
        assert "<!-- gate2:" in TEMPLATE.read_text(encoding="utf-8", errors="replace")

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
        named = set(re.findall(r"([\w/.-]+\.(?:py|ps1|md))", t))
        missing = [n for n in named if not (ROOT / n).exists()]
        assert not missing, f"速查表点名了不存在的文件：{missing}"

    def test_mentions_the_three_heaviest_lessons(self):
        t = self.CHEAT.read_text(encoding="utf-8", errors="replace")
        assert "抓包" in t and "判据式" in t and "纸面规矩" in t
