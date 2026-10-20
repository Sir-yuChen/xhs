/**
 * 店铺违规管理纯逻辑的 vitest（设计 43，2026-10-18）。
 *
 * ⚠️ 重点测**四条最容易被改坏的判据**：
 * 1. **批量操作真可用**（2026-10-18 用户口径修正）；只在开发测试阶段用**一条数据**验；
 * 2. **申诉提交没有成功分支**（只读边界 —— 伪造回执比报错贵十倍）；
 * 3. **缺值渲染 `-`，但 `0` 不是缺值**（硬约束⑤的反向陷阱）；
 * 4. **违规原因 tag 名不 trim**（`违反法律法规` 与尾空格那组是两条 tag）。
 */
import { describe, expect, it } from "vitest";

import {
  APPEAL_NOTE_MAX,
  BATCH_ACTION_LABELS,
  BATCH_CONFIRM_ACTIONS,
  CHECKUP_TABS,
  EMPTY_TEXT,
  LOCAL_READ_MESSAGE,
  PAGE_SIZE_OPTIONS,
  SPOT_CHECK_PUNISH_STATUSES,
  VIOLATION_QUICK_FILTERS,
  VIOLATION_TABS,
  WARNING_QUICK_FILTERS,
  appealSubmitVerdict,
  appealTagColor,
  batchOutcomeText,
  canDeleteRelated,
  deleteActionLabel,
  deleteBlockedReason,
  deleteConfirmText,
  deleteOutcomeText,
  EXPORT_HEADERS,
  keepVisibleKeys,
  rowsForExport,
  setReadMarks,
  toCsv,
  isLocallyRead,
  normalizeTab,
  parseReadMarks,
  quickFilterLabel,
  reasonOptions,
  reasonTagHint,
  renderActions,
  renderEntity,
  renderPeriodEnd,
  renderText,
  renderTime,
  renderTotalText,
  statusTagColor,
  tagIdsForName,
  toggleReadMark,
  validateAppealFile,
} from "./violation-manage";

describe("子 Tab", () => {
  it("六个 Tab 的顺序与千帆后台逐字一致", () => {
    expect(VIOLATION_TABS.map((tab) => tab.label)).toEqual([
      "预警列表",
      "违规列表",
      "赔付列表",
      "仲裁列表",
      "抽检违规列表",
      "免罚机会",
    ]);
  });

  it("非法 ?tab= 回落「违规列表」，不白屏", () => {
    expect(normalizeTab("nope")).toBe("violations");
    expect(normalizeTab(null)).toBe("violations");
    expect(normalizeTab("exemptions")).toBe("exemptions");
  });

  it("店铺体检的四个分段", () => {
    expect(CHECKUP_TABS.map((tab) => tab.label)).toEqual([
      "全部",
      "违规风险",
      "负反馈风险",
      "经营建议",
    ]);
  });
});

describe("快捷筛选", () => {
  it("违规列表 8 个、顺序对齐千帆", () => {
    expect(VIOLATION_QUICK_FILTERS.map((item) => item.label)).toEqual([
      "全部",
      "待处理",
      "未读",
      "严重违规",
      "近7日违规",
      "可整改",
      "可考试",
      "可申诉",
    ]);
  });

  it("预警列表是另一套 4 个", () => {
    expect(WARNING_QUICK_FILTERS.map((item) => item.label)).toEqual([
      "全部",
      "高危预警",
      "可整改",
      "可申诉",
    ]);
  });

  it("未读/待处理带计数；计数未知时**不显示 0**", () => {
    const unread = VIOLATION_QUICK_FILTERS.find((item) => item.key === "unread")!;
    expect(quickFilterLabel(unread, { unread: 3 })).toBe("未读3");
    // 还没查回来 ⇒ 只显示标签（显示「未读0」会被读成「没有未读」）
    expect(quickFilterLabel(unread, { unread: null })).toBe("未读");
    expect(quickFilterLabel(unread, {})).toBe("未读");
  });

  it("计数为真实的 0 时照常显示", () => {
    const pending = VIOLATION_QUICK_FILTERS.find((item) => item.key === "pending")!;
    expect(quickFilterLabel(pending, { pending: 0 })).toBe("待处理0");
  });

  it("其余快捷项不带数字（即使传了 counts）", () => {
    const severe = VIOLATION_QUICK_FILTERS.find((item) => item.key === "severe")!;
    expect(quickFilterLabel(severe, { severe: 12 })).toBe("严重违规");
  });
});

describe("批量操作：真可用（2026-10-18 用户口径修正）", () => {
  it("批量项目恰好四项；删除是唯一会写平台的动作", () => {
    expect(Object.keys(BATCH_ACTION_LABELS).sort()).toEqual([
      "delete_related",
      "export",
      "mark_read",
      "mark_unread",
    ]);
    // 只有「删除关联内容」写平台（先删平台再删本地，用户 2026-10-18 口径）
    const platformWriting = Object.entries(BATCH_ACTION_LABELS)
      .filter(([, label]) => /删除/.test(label))
      .map(([key]) => key);
    expect(platformWriting).toEqual(["delete_related"]);
  });

  it("危险动作都要二次确认（导出、删除）", () => {
    expect(BATCH_CONFIRM_ACTIONS.sort()).toEqual(["delete_related", "export"]);
    // 标记已读是本地动作，不该打断用户
    expect(BATCH_CONFIRM_ACTIONS).not.toContain("mark_read");
  });

  it("结果文案带上实际条数；0 条时说「没有可处理的行」而不是假装成功", () => {
    expect(batchOutcomeText("mark_read", 1)).toBe("标记为已读：已处理 1 条");
    expect(batchOutcomeText("export", 0)).toBe("没有可处理的行");
  });

  it("⚠️ 批量已读是**幂等设定**，不是逐条切换", () => {
    // 勾 3 条，其中 B3 本来就已读 ⇒ 「全部标已读」后三条都必须读；
    // 若用循环切换，B3 会被翻成未读（这是批量最容易出的错）。
    const before = setReadMarks({}, 7, ["B3"], true);
    const after = setReadMarks(before, 7, ["B1", "B2", "B3"], true);
    expect(["B1", "B2", "B3"].map((id) => isLocallyRead(after, 7, id))).toEqual([true, true, true]);

    // 反向同理：全部标未读后一条都不该残留
    const cleared = setReadMarks(after, 7, ["B1", "B2", "B3"], false);
    expect(["B1", "B2", "B3"].some((id) => isLocallyRead(cleared, 7, id))).toBe(false);
  });

  it("跨账号/空单号不串味", () => {
    const marks = setReadMarks({}, 7, ["V1", ""], true);
    expect(isLocallyRead(marks, 7, "V1")).toBe(true);
    expect(isLocallyRead(marks, 8, "V1")).toBe(false);
    expect(Object.keys(marks)).toHaveLength(1);
  });

  it("只保留当前页可见的勾选（跨页残留的 key 不许生效）", () => {
    // 用户在第 1 页勾了 A、B，翻到第 2 页（只有 C）后点批量 ⇒ 只该作用于 C 所在的可见集合，
    // A/B 不在可见集合里就必须被剔除（否则动作会落到看不见的数据上）。
    expect(keepVisibleKeys(["A", "B", "C"], ["C"])).toEqual(["C"]);
    expect(keepVisibleKeys(["A"], [])).toEqual([]);
  });

  it("导出只包含被勾选的行，且按中文表头出列", () => {
    const rows = [
      { violation_id: "V1", violation_reason: "站外导流", entity_name: "某商品", actions: ["商品下架"] },
      { violation_id: "V2", violation_reason: "违反法律法规", entity_name: "另一商品", actions: [] },
    ];
    const exported = rowsForExport(rows, ["V2"]);
    expect(exported).toHaveLength(1);
    expect(exported[0]["违规单号"]).toBe("V2");
    expect(Object.keys(exported[0]).sort()).toEqual([...EXPORT_HEADERS].sort());
  });

  it("CSV 转义：逗号/引号/换行不把列冲散", () => {
    const csv = toCsv(
      [{ 违规原因: "含,逗号", 违规对象: '含"引号"', 处罚方式: "两行\n内容" }],
      EXPORT_HEADERS
    );
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe(EXPORT_HEADERS.join(","));
    // 内部引号被双写；含逗号/换行的字段整体包引号
    expect(lines[1]).toContain('"含""引号"""');
    expect(lines[1]).toContain('"含,逗号"');
    expect(csv).toContain('"两行\n内容"');
  });
});

describe("已读标记：仅本地切换，不同步平台", () => {
  it("标记文案逐字固定（页面 message 用它）", () => {
    expect(LOCAL_READ_MESSAGE).toBe("已读标记仅本地生效，不同步平台");
  });

  it("切换是幂等的相反操作（标记 → 取消 → 标记）", () => {
    const once = toggleReadMark({}, 8, "1421465102446313472");
    expect(isLocallyRead(once, 8, "1421465102446313472")).toBe(true);

    const twice = toggleReadMark(once, 8, "1421465102446313472");
    expect(isLocallyRead(twice, 8, "1421465102446313472")).toBe(false);

    const thrice = toggleReadMark(twice, 8, "1421465102446313472");
    expect(isLocallyRead(thrice, 8, "1421465102446313472")).toBe(true);
  });

  it("账号维度隔离：同一单号在别的账号下不受影响", () => {
    const marks = toggleReadMark({}, 8, "V1");
    expect(isLocallyRead(marks, 8, "V1")).toBe(true);
    expect(isLocallyRead(marks, 9, "V1")).toBe(false);
  });

  it("不改入参（调用方是 React state，原地改不会触发重渲染）", () => {
    const before = { "8:V1": true };
    const after = toggleReadMark(before, 8, "V1");
    expect(before).toEqual({ "8:V1": true });
    expect(after).toEqual({});
  });

  it("空单号不产生垃圾键", () => {
    expect(toggleReadMark({}, 8, "")).toEqual({});
    expect(toggleReadMark({}, 8, null)).toEqual({});
    expect(toggleReadMark({}, 8, "   ")).toEqual({});
  });

  it("坏数据退空表（一个 localStorage 值不该白屏）", () => {
    expect(parseReadMarks(null)).toEqual({});
    expect(parseReadMarks("not json")).toEqual({});
    expect(parseReadMarks("[1,2]")).toEqual({});
    expect(parseReadMarks('{"8:V1":true,"8:V2":false}')).toEqual({ "8:V1": true });
  });
});

describe("申诉：本系统不代提交", () => {
  it("判定恒为 blocked，**没有成功分支**", () => {
    const verdict = appealSubmitVerdict();
    expect(verdict.blocked).toBe(true);
    // ⚠️ 逐字断言整句（不只是「不含成功」）：变异 `appealmock` 把文案换成
    //    「申诉已提交成功…」时，`toContain("不代提交")` 也会红 —— 但只断言
    //    负面词（not contain 「成功」）会漏掉「已提交」这类说法，故正向逐字钉死。
    expect(verdict.message).toBe("本系统不代提交申诉，请在千帆后台「违规管理」中提交。");
    expect(verdict.message).not.toContain("成功");
    expect(verdict.message).not.toContain("已提交");
    expect(verdict.dashboardHint).toBe("ark.xiaohongshu.com/app-violation/violation-manage/list");
  });

  it("连调两次结果一致（不依赖任何状态，避免「第二次才成功」的假象）", () => {
    expect(appealSubmitVerdict()).toEqual(appealSubmitVerdict());
  });

  it("说明长度上限 200（千帆原文 0/200）", () => {
    expect(APPEAL_NOTE_MAX).toBe(200);
  });

  it("凭证文件校验：格式与 10MB 上限", () => {
    expect(validateAppealFile({ name: "a.PNG", size: 1024 })).toBe("");
    expect(validateAppealFile({ name: "a.rar", size: 10 * 1024 * 1024 })).toBe("");
    expect(validateAppealFile({ name: "a.gif", size: 1024 })).toContain("仅支持");
    expect(validateAppealFile({ name: "a.pdf", size: 10 * 1024 * 1024 + 1 })).toContain("10 MB");
  });
});

describe("缺值口径", () => {
  it("null / undefined / 空串 ⇒ `-`", () => {
    expect(renderText(null)).toBe(EMPTY_TEXT);
    expect(renderText(undefined)).toBe(EMPTY_TEXT);
    expect(renderText("")).toBe(EMPTY_TEXT);
    expect(renderText("   ")).toBe(EMPTY_TEXT);
  });

  it("**0 是真实值，不能变成 `-`**", () => {
    expect(renderText(0)).toBe("0");
    expect(renderText("0")).toBe("0");
  });

  it("时间戳：缺值/0/非法 ⇒ `-`", () => {
    expect(renderTime(null)).toBe(EMPTY_TEXT);
    expect(renderTime(0)).toBe(EMPTY_TEXT);
    expect(renderTime("1790510105000")).toBe(EMPTY_TEXT);
    expect(renderTime(1790510105000)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it("处罚方式：多条用 `/`，空 ⇒ `-`", () => {
    expect(renderActions(["商品下架", "商品冻结"])).toBe("商品下架/商品冻结");
    expect(renderActions([])).toBe(EMPTY_TEXT);
    expect(renderActions(null)).toBe(EMPTY_TEXT);
    expect(renderActions([""])).toBe(EMPTY_TEXT);
    expect(renderActions(["  "])).toBe(EMPTY_TEXT);
  });

  it("**编号 / 时间类缺值一律 `-`，绝不冒 0**（硬约束⑤的反向陷阱）", () => {
    // 缺值：渲染成 `-`，不是 `0` / 空串
    expect(renderText(null)).not.toBe("0");
    expect(renderTime(null)).toBe(EMPTY_TEXT);
    expect(renderTime(undefined)).toBe(EMPTY_TEXT);
    expect(renderTime("")).toBe(EMPTY_TEXT);
    // 违规对象编号缺失 ⇒ 只显示另一半；两半都缺 ⇒ `-`（不是空串）
    expect(renderEntity({}).idText).toBe(EMPTY_TEXT);
    expect(renderEntity({}).idText).not.toBe("");
    expect(renderEntity({}).name).toBe(EMPTY_TEXT);
    expect(renderEntity({}).name).not.toBe("");
    // 长期有效（4102416000000）不显示「至 … 结束」后缀，而不是显示 1970 / 0
    expect(renderPeriodEnd(4102416000000)).toBe("");
    // 0（纪元时刻）是过去的时间 ⇒ 「已结束」；这里钉的是**不当作长期**处理
    expect(renderPeriodEnd(0)).not.toContain("至");
  });

  it("违规对象：名称与编号各自缺值时只显示另一半", () => {
    expect(renderEntity({ entity_type_name: "商品", entity_id: "6a36" }).idText).toBe("商品编号: 6a36");
    expect(renderEntity({ entity_id: "6a36" }).idText).toBe("编号: 6a36");
    expect(renderEntity({}).idText).toBe(EMPTY_TEXT);
    expect(renderEntity({ entity_name: "观隅好物" }).name).toBe("观隅好物");
  });

  it("分页文案：共 N 条，M 页（总数 0 也是 1 页）", () => {
    expect(renderTotalText(56, 10)).toBe("共 56 条，6 页");
    expect(renderTotalText(0, 10)).toBe("共 0 条，1 页");
    expect(renderTotalText(10, 10)).toBe("共 10 条，1 页");
  });

  it("分页尺寸选项与千帆一致", () => {
    expect(PAGE_SIZE_OPTIONS).toEqual([10, 20, 30, 40, 50]);
  });
});

describe("状态与结束时间", () => {
  it("处罚状态按**数值**取色，不按文案", () => {
    expect(statusTagColor(2)).toBe("error"); // 处罚中
    expect(statusTagColor(1)).toBe("warning"); // 已处罚
    expect(statusTagColor(0)).toBe("default"); // 待处罚
    expect(statusTagColor(-1)).toBe("default"); // 已取消
    expect(statusTagColor(null)).toBe("default");
  });

  it("申诉状态取色", () => {
    expect(appealTagColor(0)).toBe("processing"); // 可申诉
    expect(appealTagColor(2)).toBe("success"); // 申诉成功
    expect(appealTagColor(3)).toBe("error"); // 申诉失败
    expect(appealTagColor(100)).toBe("default"); // 不可申诉
  });

  it("结束时间：长期（4102416000000）不显示后缀，已过显示「已结束」", () => {
    expect(renderPeriodEnd(4102416000000)).toBe("");
    expect(renderPeriodEnd(null)).toBe("");
    expect(renderPeriodEnd(Date.now() - 1000)).toBe("已结束");
    expect(renderPeriodEnd(Date.now() + 86_400_000)).toContain("结束");
  });
});

describe("违规原因 tag（不 trim、不去重）", () => {
  const tags = [
    { tag_name: "违反法律法规", tag_ids: ["80001101", "80001738"] },
    // 尾空格：平台真实数据里与上面是**两条** tag（真机 80005677）
    { tag_name: "违反法律法规 ", tag_ids: ["80005677"] },
    { tag_name: "站外导流", tag_ids: ["80000354", "80006709"] },
  ];

  it("下拉选项保留原始字符串（含尾空格）", () => {
    const options = reasonOptions(tags);
    expect(options).toHaveLength(3);
    expect(options[1].value).toBe("违反法律法规 ");
    // 去重或 trim 会把两条合并成一条 —— 这里必须是 3 条
    expect(new Set(options.map((item) => item.value)).size).toBe(3);
  });

  it("按名字查 tagIds 精确匹配，尾空格不是同一条", () => {
    expect(tagIdsForName(tags, "违反法律法规")).toEqual(["80001101", "80001738"]);
    expect(tagIdsForName(tags, "违反法律法规 ")).toEqual(["80005677"]);
    expect(tagIdsForName(tags, "违反法律法规\t")).toEqual([]);
  });

  it("查不到 ⇒ 空数组（不能退化成「不过滤」）", () => {
    expect(tagIdsForName(tags, "不存在的")).toEqual([]);
  });
});

describe("违规原因下拉：同名的两条必须能分辨（真机 U+200B）", () => {
  // 真机 `user_tag` 实测：一条是普通名（5 个 tagId），另一条末尾是**零宽空格**
  // （U+200B，1 个 tagId），界面上看起来完全一样。
  const tags = [
    { tag_name: "违反法律法规", tag_ids: ["80001101", "80001738", "80001764", "80001776", "80001801"] },
    { tag_name: "违反法律法规\u200b", tag_ids: ["80005677"] },
    { tag_name: "笔记存在创作质量不佳的内容 ", tag_ids: ["79548256"] },
  ];

  it("选项数量不因同名而变少（不按可见文案去重）", () => {
    expect(reasonOptions(tags)).toHaveLength(3);
    expect(new Set(reasonOptions(tags).map((item) => item.value)).size).toBe(3);
  });

  it("标签带 tag_ids 条数 —— 用户靠它区分「5 项」与「1 项」", () => {
    const options = reasonOptions(tags);
    expect(options[0].label).toContain("5 项");
    expect(options[1].label).toContain("1 项");
  });

  it("带零宽空格 / 尾空格的项额外标「含不可见字符」", () => {
    const options = reasonOptions(tags);
    expect(options[0].label).not.toContain("不可见");
    expect(options[1].label).toContain("同名（含不可见字符）");
    expect(options[2].label).toContain("同名（含不可见字符）");
  });

  it("hint 判据：零宽空格 trim 不掉，尾空格能 trim 掉 —— 两者都要认", () => {
    expect(reasonTagHint(tags[0])).toBe("");
    expect(reasonTagHint(tags[1])).toBe("同名（含不可见字符）");
    expect(reasonTagHint(tags[2])).toBe("同名（含不可见字符）");
  });

  it("value 永远是原始 tag_name（带 U+200B 也不动）", () => {
    expect(reasonOptions(tags)[1].value).toBe("违反法律法规\u200b");
    expect(tagIdsForName(tags, "违反法律法规\u200b")).toEqual(["80005677"]);
    // 普通名那组**不能**命中带零宽空格的那条
    expect(tagIdsForName(tags, "违反法律法规")).toEqual([
      "80001101",
      "80001738",
      "80001764",
      "80001776",
      "80001801",
    ]);
  });
});

describe("抽检页的另一套枚举", () => {
  it("抽检处罚状态是「待落罚/已落罚/已取消」，与违规列表的不是一套", () => {
    expect(SPOT_CHECK_PUNISH_STATUSES).toEqual(["待落罚", "已落罚", "已取消"]);
  });
});

describe("删除关联内容（设计 44：先平台，后本地）", () => {
  it("只有商品/笔记可删，其余类型给出具体原因（不是笼统「不支持」）", () => {
    expect(canDeleteRelated(1)).toBe(true);   // 商品
    expect(canDeleteRelated(3)).toBe(true);   // 笔记
    expect(canDeleteRelated(0)).toBe(false);  // 店铺
    expect(canDeleteRelated(2)).toBe(false);  // 账号
    expect(canDeleteRelated(4)).toBe(false);  // 直播间
    expect(canDeleteRelated(null)).toBe(false);

    expect(deleteBlockedReason(0)).toContain("店铺");
    expect(deleteBlockedReason(2)).toContain("账号");
    expect(deleteBlockedReason(4)).toContain("直播间");
  });

  it("删除文案按对象类型区分（用户能预期删的是哪一类）", () => {
    expect(deleteActionLabel(1)).toBe("删除关联商品");
    expect(deleteActionLabel(3)).toBe("删除关联笔记");
  });

  it("⚠️ 确认文案必须点明「先删平台」与「违规记录不删」", () => {
    const text = deleteConfirmText(3);
    expect(text).toContain("先删除");
    expect(text).toContain("平台");
    // 用户最容易误会的一条：以为删了违规记录 / 撤销了处罚
    expect(text).toContain("违规记录本身不会被删除");
    // 本地缺失时的处理也要说清（否则用户不懂为什么有的没删本地）
    expect(text).toContain("跳过");
  });

  it("结果汇总四态分别计数（platform_only 单独报，不能混进「已删除」）", () => {
    const text = deleteOutcomeText({ deleted: 2, platform_only: 1, failed: 0, skipped: 3 });
    expect(text).toContain("已删除 2 条");
    expect(text).toContain("仅删平台");
    expect(text).toContain("无可删内容");
    expect(text).not.toContain("失败");

    // 全零 ⇒ 老实说没有可处理的行
    expect(deleteOutcomeText({ deleted: 0, platform_only: 0, failed: 0, skipped: 0 }))
      .toBe("没有可处理的行");

    // 有失败必须报出来
    expect(deleteOutcomeText({ deleted: 1, platform_only: 0, failed: 2, skipped: 0 }))
      .toContain("平台删除失败");
  });
});
