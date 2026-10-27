/**
 * 商品列表**导出上限**的 vitest 用例（原 M4 商品编辑纯逻辑用例集）。
 *
 * ⚠️ 编辑那三组用例（`buildEditPayload` / `updatedSummary` / `adoptCandidate`）
 * **已随「编辑商品功能逻辑移除」整条删除**（2026-10-27 用户口径）：函数本身
 * 已不存在，留着用例只会 `TS2305`（模块没有该导出）。
 *
 * 只留 `exportBlockReason` —— 它与编辑无关，仍被商品列表工具栏使用。
 */
import { describe, expect, it } from "vitest";

import { exportBlockReason } from "./product-edit";

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
