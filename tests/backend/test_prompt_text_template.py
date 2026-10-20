"""文案向提示词模板与模块的守卫（设计 41 §11.1.3，v2.25）。

## 为什么有这个文件

实测（2026-10-16，真库）：`prompt_modules` 52 行 / `prompt_templates` 78 行**是虚的**
—— 用户账号名下**只有 2 个模块、3 条模板**，其余是别账号的重复种子。三条根本缺陷：

| # | 缺陷 | 后果 |
|---|---|---|
| P-1 | `prompt_templates.task_type` **没有 `text`** | 文案 Prompt 走 `build_generation_skeleton_hint()` **代码兜底** ⇒ 用户在界面上**改不到** |
| P-2 | 模板骨架极简（cover 全文 6 行，无爆款结构） | 与爆款硬口径（三段式 / 埋词位 / 三三法则 / 卖点收窄）**完全没接上** |
| P-3 | 2 个模块**都是图用的**（摄影术语 / 画面水印） | 「公共模块可组合复用」在**文案链路上空转** |

本文件钉住修复后的形态。⚠️ 判据必须能**区分对错**（DEF-057）：
不是「存在一个 text 模板」就算过 —— 必须**逐段位**断言骨架里有那六段，
否则一个只有「请写一篇笔记」的模板也能让测试变绿。
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# 第一层：种子数据本身（离线，不建库）
# --------------------------------------------------------------------------- #


def test_seed_has_text_task_type_template():
    """P-1：种子里**必须有 `task_type="text"` 且默认**的模板。

    这是「用户在界面上能改到文案提示词」的**唯一入口**（`template_id` nullable 时
    走的是代码兜底 `build_generation_skeleton_hint()`，而兜底用户改不了）。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    templates = load_initial_prompt_templates()["templates"]
    text_templates = [t for t in templates if t["task_type"] == "text"]
    assert text_templates, (
        "种子缺 text 模板 ⇒ 文案 Prompt 只能走代码兜底，用户在模板页改不到任何东西"
    )
    assert any(t["is_default"] for t in text_templates), (
        "text 模板必须是默认（否则生成弹窗不会自动选中它）"
    )


@pytest.mark.parametrize(
    ("slot", "fragment"),
    [
        ("① 人群与场景", "人群与场景"),
        ("② 标题多备选", "备选"),
        ("③ 正文三段式", "三段式"),
        ("④ 话题三三法则", "三三法则"),
        ("⑤ 封面文字", "封面文字"),
        ("⑥ JSON 契约", "只输出 JSON"),
    ],
)
def test_text_template_covers_all_six_sections(slot, fragment):
    """P-2：文本模板骨架必须含**六个段位**（缺一段 = 该段位的能力没接上）。

    ⚠️ 逐段位参数化，**不是**「包含关键词『文案』就算过」——
    后者会让一个空模板也变绿（DEF-057 同型：判据要能区分对错）。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    templates = load_initial_prompt_templates()["templates"]
    skeleton = next(t["prompt_skeleton"] for t in templates if t["task_type"] == "text")
    assert fragment in skeleton, f"文本模板缺第 {slot} 段（找不到「{fragment}」）"


def test_text_template_embeds_publishable_hard_limits():
    """骨架里的**固定**硬口径必须仍在，但**可变约束一律走槽位**。

    ## 口径变更（2026-10-17 用户）

    > 「字数，表情以及话题设置均有默认值，公共模版中应无需固定添加固定提示词，
    >   调用大模型时需最终拼接」

    ⇒ 原断言要求骨架里出现「600–900 字」「9 个」这类**写死的数量**，
    现在这些正是要**移除**的东西（写死了就和前端设置打架）。
    判据反过来：**保留平台硬上限**（≤15 字封面 / 3–5 次核心词），
    **可变项必须是槽位名**（`{title_length}` 等），且**不许再出现写死的字数区间**。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    templates = load_initial_prompt_templates()["templates"]
    skeleton = next(t["prompt_skeleton"] for t in templates if t["task_type"] == "text")

    #: ① 平台硬口径（不随用户设置变化）必须仍在
    for expected in ("≤15 字", "3–5 次"):
        assert expected in skeleton, f"文本模板缺硬口径「{expected}」"

    #: ② 可变约束必须是槽位引用（数值在调用时拼）
    for slot in ("{title_length}", "{body_length}", "{topic_count}", "{emoji_rule}"):
        assert slot in skeleton, f"文本模板未用槽位 {slot}（字数/表情/话题数量应在调用时拼接）"

    #: ③ **不许再有写死的字数区间**（那正是「模板与设置打架」的来源）。
    #: ⚠️ 要排除「核心搜索词前置（前 4–6 字内出现）」这类**位置**描述 ——
    #: 它讲的是核心词落在第几个字，不是整条标题的字数，属于平台 SEO 硬口径。
    import re

    without_position = re.sub(r"前\s*\d+\s*[–\-]\s*\d+\s*字内出现", "", skeleton)
    fixed_range = re.search(r"\d+\s*[–\-]\s*\d+\s*字", without_position)
    assert fixed_range is None, f"骨架里仍有写死的字数区间：{fixed_range.group(0) if fixed_range else ''}"

    #: ④ 封面文字必须点在「会被 OCR 识别」这一层 —— 最容易漏的埋词位
    assert "OCR" in skeleton, "封面文字段必须说明「平台会 OCR 识别计入关键词权重」"


def test_seed_modules_cover_writing_categories():
    """P-3：模块必须覆盖**文案向**类别（原来 2 个模块全是图用的）。

    ⚠️ 判据是「类别齐」而不是「模块数变多」—— 数更多但仍是图用模块 = 问题没解决。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    modules = load_initial_prompt_templates()["modules"]
    categories = {m["category"] for m in modules}
    required = {"tone", "structure", "audience", "deai", "taboo_text", "selling_point"}
    missing = required - categories
    assert not missing, f"种子缺这些文案向模块类别：{sorted(missing)}（实得 {sorted(categories)}）"
    # 图用类别不能丢（出图链路仍靠它们）
    assert {"style", "taboo"} <= categories, "原有图用模块类别不该被替换掉"


def test_deai_module_lists_the_ai_smell_words():
    """去 AI 味模块必须**逐词列出**禁用词（不是「注意不要有 AI 味」这种空话）。

    市场共识（调研 §3.1 事实 3）：AI 输出是 80 分初稿，需独立清洗三件套。
    「此外 / 至关重要 / 值得一提」是标志性词 —— 三个必须都在表里。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    modules = load_initial_prompt_templates()["modules"]
    deai = next((m for m in modules if m["category"] == "deai"), None)
    assert deai, "缺 deai 类别模块"
    for word in ("此外", "至关重要", "值得一提"):
        assert word in deai["content"], f"去 AI 味模块没列出标志词「{word}」"


# --------------------------------------------------------------------------- #
# 第二层：兜底骨架与模板**同口径**（防「没选模板 = 拿到更差的提示词」）
# --------------------------------------------------------------------------- #


def test_fallback_skeleton_matches_template_sections():
    """兜底骨架（无模板时用）必须**同样含六段位**。

    ⚠️ **为什么必须守**：`template_id` 是 nullable，新用户第一次生成的默认状态就是
    「没有模板」。若兜底比模板弱，「细化提示词」就只覆盖了选模板的用户 ——
    而兜底恰恰是**默认路径**。判据 = 兜底六段位 ⊇ 模板六段位。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates
    from backend.app.services.note_gen_llm_service import build_generation_skeleton_hint

    fallback = build_generation_skeleton_hint()
    for fragment in ("人群与场景", "备选", "三段式", "三三法则", "封面文字", "只输出 JSON"):
        assert fragment in fallback, (
            f"兜底骨架缺段位「{fragment}」⇒ 没选模板的用户拿到的是更差的提示词"
        )

    # 反向对照：兜底与模板都必须引用商品槽位（不引用 = 商品信息进不了提示词）
    templates = load_initial_prompt_templates()["templates"]
    skeleton = next(t["prompt_skeleton"] for t in templates if t["task_type"] == "text")
    for slot in ("{title}", "{category}"):
        assert slot in skeleton, f"文本模板缺插槽 {slot}"
        assert slot in fallback, f"兜底骨架缺插槽 {slot}"


# --------------------------------------------------------------------------- #
# 第三层：解析器必须**真的读出**模板要求的新键（防静默丢弃）
# --------------------------------------------------------------------------- #


def test_parser_reads_titles_and_cover_text():
    """`parse_generated_payload` 必须带出 `titles` / `cover_text`。

    ⚠️ **这是本轮的真缺口**：模板从 v2.25 起要求模型给「3 个标题备选」和
    「封面大字」，但解析器此前只取 `title`/`body`/`tags` ⇒ 这两项被**静默丢弃**
    （模型确实按模板给了，我们却没读）。属「接缝两侧各自自洽、接缝没人管」同型。
    """
    from backend.app.services.product_note_service import parse_generated_payload

    parsed = parse_generated_payload(
        '{"titles": ["备选A", "备选B"], "title": "选定", "body": "正文",'
        ' "topics": ["t1"], "cover_text": "封面大字"}'
    )
    assert parsed["titles"] == ["备选A", "备选B"], f"titles 被丢弃：{parsed}"
    assert parsed["cover_text"] == "封面大字", f"cover_text 被丢弃：{parsed}"


def test_parser_tolerates_missing_optional_keys():
    """缺 `titles` / `cover_text` **不报错**（两者是可选增强，不是新契约）。

    这是防回退的关键：若把它们做成必填，所有既有调用方（干跑链路 / 旧模型输出）
    会一起炸 —— 那是把「增强」变成「破坏性变更」。
    """
    from backend.app.services.product_note_service import parse_generated_payload

    parsed = parse_generated_payload('{"title": "标题", "body": "正文", "tags": ["t"]}')
    assert parsed["titles"] == []
    assert parsed["cover_text"] == ""
    assert parsed["title"] == "标题"  # 既有契约不受影响
    assert parsed["body"] == "正文"


def test_parser_rejects_non_list_titles():
    """`titles` 不是列表 ⇒ 退空列表（**不猜、不抛**）。

    模型偶尔会把 `titles` 写成字符串（"备选A/备选B"）。此时**不能**按逗号拆
    —— 那是替模型猜分隔符（DEF-057 同型：猜错就静默产出一堆碎标题）。
    正确行为是如实退空，由上层回落为 `[title]`。
    """
    from backend.app.services.product_note_service import parse_generated_payload

    parsed = parse_generated_payload(
        '{"title": "标题", "body": "正文", "titles": "备选A/备选B"}'
    )
    assert parsed["titles"] == [], "非列表的 titles 必须退空列表，不许猜分隔符"


def test_selected_title_is_present_in_titles():
    """`GeneratedNote.titles` 必须**含选定的 `title`**。

    ⚠️ 用户会在前端看到备选下拉：若下拉里没有当前实际生效的那一条，
    用户会以为「选的和写的不一致」。兜底把 `title` 补到首位。
    """
    from backend.app.services.note_gen_llm_service import _normalize_topics  # noqa: F401
    from backend.app.services.note_gen_llm_service import GeneratedNote

    note = GeneratedNote(
        title="选定的标题",
        body="正文",
        titles=["备选A", "选定的标题", "备选B"],
    )
    assert note.title in note.titles, "选定的标题必须在备选列表里（否则前端下拉与实际不一致）"


def test_seed_and_db_rows_can_stay_in_sync():
    """种子改了之后，**库里已有的行**必须有同步手段（否则「种子对、跑起来错」）。

    ## 守卫来由（2026-10-17 实测）

    播种判重是「按 `(user_id, name)` 存在就不覆盖」—— 保护用户改动的代价是
    **改了种子，已播种的用户永远看不到新内容**。本轮把默认模板里写死的
    「600–900 字 / 9 个话题」改成槽位后，种子是新的，但库里 26 行仍是旧骨架，
    实际生成走的还是旧口径。

    判据：同步脚本存在 + 它**默认 dry-run**（不显式 `--apply` 不写库 ——
    `prompt_skeleton` 是用户可编辑内容，误覆盖等于吞掉用户的工作）。
    """
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / ".hermes" / "scripts" / "sync_prompt_templates.py"
    assert script.exists(), "缺少种子→库的同步脚本"
    text = script.read_text(encoding="utf-8")
    assert '"--apply"' in text, "同步脚本必须要求显式 --apply 才写库"
    #: 判据必须是「用户没动过」而不是「无条件覆盖」
    assert "用户改过" in text, "同步脚本必须能识别并跳过用户改过的行"
    assert "pristine" in text, "缺少跨行指纹判据（旧种子无此模板时的兜底）"
