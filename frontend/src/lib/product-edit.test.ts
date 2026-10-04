/**
 * M4 商品编辑纯逻辑的 vitest 用例（设计 20）。
 *
 * ⚠️ 重点测**两条最容易错的判据**：
 * 1. `buildEditPayload` 不许把没改的字段塞 `null`（那会被后端读成**清空**）；
 * 2. `updatedSummary([])` 必须返回 `null`（不能报「已更新」而其实没动）。
 */
import { describe, expect, it } from "vitest";

import {
  ALL_GROUPS_SENTINEL,
  adoptCandidate,
  buildEditPayload,
  exportBlockReason,
  groupFilterOptions,
  groupFilterParam,
  updatedSummary,
} from "./product-edit";

describe("buildEditPayload（PATCH 语义）", () => {
  const base = { platform_account_id: 7, item_id: "ITEM-1" };

  it("只带用户真的改过的键（未收录的可选键不得进请求体）", () => {
    const payload = buildEditPayload(base, { group_id: 3 });
    expect(payload).toEqual({ platform_account_id: 7, item_id: "ITEM-1", group_id: 3 });
    // ⚠️ 关键：另外两个可编辑字段**不能出现**（出现 null = 清空）
    expect("ai_title" in payload).toBe(false);
    expect("ai_selling_point" in payload).toBe(false);
  });

  it("显式 null 是清空，会被如实带上", () => {
    const payload = buildEditPayload(base, { ai_title: null });
    expect(payload).toEqual({ platform_account_id: 7, item_id: "ITEM-1", ai_title: null });
  });

  it("空表单 = 只有身份键（后端将原样返回、updated 为空）", () => {
    expect(buildEditPayload(base, {})).toEqual(base);
  });

  it("三字段同改时全部带上", () => {
    const payload = buildEditPayload(base, {
      group_id: 2,
      ai_title: "新标题",
      ai_selling_point: "新卖点",
    });
    expect(payload.group_id).toBe(2);
    expect(payload.ai_title).toBe("新标题");
    expect(payload.ai_selling_point).toBe("新卖点");
  });
});

describe("updatedSummary（提示文案）", () => {
  it("空数组 ⇒ null（什么都不改就不提示）", () => {
    expect(updatedSummary([])).toBeNull();
    expect(updatedSummary(null)).toBeNull();
    expect(updatedSummary(undefined)).toBeNull();
  });

  it("代码名翻成中文，多项用顿号连接", () => {
    expect(updatedSummary(["group_id"])).toBe("已更新分组");
    expect(updatedSummary(["ai_title", "ai_selling_point"])).toBe("已更新AI 标题、AI 卖点");
  });

  it("表外的未知代码原样显示（不造中文名）", () => {
    expect(updatedSummary(["weird_field"])).toBe("已更新weird_field");
  });
});

describe("exportBlockReason（导出上限）", () => {
  it("超限给原因（不发请求）", () => {
    const reason = exportBlockReason(6000);
    expect(reason).toContain("6000");
    expect(reason).toContain("5000");
  });

  it("恰好等于上限可以导出（边界是 > 不是 >=）", () => {
    expect(exportBlockReason(5000)).toBeNull();
    expect(exportBlockReason(5001)).not.toBeNull();
  });

  it("空集合给「没有可导出」而不是「超限」", () => {
    expect(exportBlockReason(0)).toContain("没有可导出");
  });

  it("上限可按后端值传入（同值判据）", () => {
    expect(exportBlockReason(3, 3)).toBeNull();
    expect(exportBlockReason(4, 3)).not.toBeNull();
  });
});

describe("adoptCandidate（采纳备选）", () => {
  it("采纳一枚备选只改那一项", () => {
    const next = adoptCandidate({ group_id: 1 }, "ai_title", "选中的标题");
    expect(next).toEqual({ group_id: 1, ai_title: "选中的标题" });
  });
});

describe("分组下拉", () => {
  it("含「全部分组」与「未分组」两个特殊项", () => {
    const options = groupFilterOptions([
      { id: 3, name: "春季" },
      { id: 4, name: "夏季" },
    ]);
    expect(options.map((o) => o.label)).toEqual(["全部分组", "未分组", "春季", "夏季"]);
  });

  it("「全部分组」不发 group_id 参数", () => {
    expect(groupFilterParam(ALL_GROUPS_SENTINEL)).toBeUndefined();
    expect(groupFilterParam(0)).toBe(0);
    expect(groupFilterParam(3)).toBe(3);
  });
});
