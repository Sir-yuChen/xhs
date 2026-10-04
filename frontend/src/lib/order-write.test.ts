/**
 * M7 订单写操作纯逻辑的 vitest（设计 16 §7.4）。
 *
 * ⚠️ 重点测**三条最容易错的判据**：
 * 1. `MAX_BATCH_SHIP` 与后端**同值**（两份数 = 前端允许的批次后端拒）；
 * 2. 门禁 `canShipOrder` / `canHandleAfterSale`（不给假入口）；
 * 3. `shipOutcomeText` **三态**（全成功 / 部分成功 / 全失败）—— 混成一句会误导。
 */
import { describe, expect, it } from "vitest";

import {
  afterSaleConfirmText,
  batchShipBlockReason,
  canHandleAfterSale,
  canShipOrder,
  HANDLEABLE_AFTERSALE_STATUS,
  LOGISTICS_COMPANIES,
  MAX_BATCH_SHIP,
  SHIPPABLE_STATUS,
  shipConfirmText,
  shipOutcomeText,
} from "./order-write";

describe("上限与状态常量（前后端同值）", () => {
  it("批量上限 = 50（与后端 order_write_service.MAX_BATCH_SHIP 相等）", () => {
    expect(MAX_BATCH_SHIP).toBe(50);
  });

  it("门禁状态词与后端同值", () => {
    expect(SHIPPABLE_STATUS).toBe("待发货");
    expect(HANDLEABLE_AFTERSALE_STATUS).toBe("待处理");
  });

  it("物流公司清单非空且 code 唯一", () => {
    expect(LOGISTICS_COMPANIES.length).toBeGreaterThan(0);
    const codes = LOGISTICS_COMPANIES.map((c) => c.code);
    expect(new Set(codes).size).toBe(codes.length);
  });
});

describe("门禁判据（不给假入口）", () => {
  it("只有待发货能发", () => {
    expect(canShipOrder("待发货")).toBe(true);
    expect(canShipOrder("已完成")).toBe(false);
    expect(canShipOrder("已关闭")).toBe(false);
    expect(canShipOrder("")).toBe(false);
    expect(canShipOrder(null)).toBe(false);
    expect(canShipOrder(undefined)).toBe(false);
  });

  it("只有待处理售后能审核", () => {
    expect(canHandleAfterSale("待处理")).toBe(true);
    expect(canHandleAfterSale("已完成")).toBe(false);
    expect(canHandleAfterSale(null)).toBe(false);
  });
});

describe("batchShipBlockReason（批量前置校验）", () => {
  it("空 ⇒ 拒", () => {
    expect(batchShipBlockReason([])).toContain("没有选择");
  });

  it("超上限 ⇒ 拒（不截断）", () => {
    const items = Array.from({ length: 51 }, (_, i) => ({
      order_no: `O-${i}`,
      tracking_no: `T-${i}`,
    }));
    const reason = batchShipBlockReason(items);
    expect(reason).toContain("51");
    expect(reason).toContain("50");
  });

  it("恰好等于上限 ⇒ 可以（边界是 > 不是 >=）", () => {
    const items = Array.from({ length: 50 }, (_, i) => ({
      order_no: `O-${i}`,
      tracking_no: `T-${i}`,
    }));
    expect(batchShipBlockReason(items)).toBeNull();
  });

  it("有单号没填运单号 ⇒ 拒并报数", () => {
    const items = [
      { order_no: "O-1", tracking_no: "T-1" },
      { order_no: "O-2", tracking_no: "" },
    ];
    expect(batchShipBlockReason(items)).toContain("1 笔还没填运单号");
  });
});

describe("确认文案（必须说清不可撤回）", () => {
  it("单笔文案含订单号 + 不可撤回", () => {
    const text = shipConfirmText([{ order_no: "O-1", tracking_no: "SF123" }]);
    expect(text).toContain("O-1");
    expect(text).toContain("SF123");
    expect(text).toContain("不可撤回");
  });

  it("多笔文案含笔数 + 不可撤回", () => {
    const text = shipConfirmText([
      { order_no: "O-1", tracking_no: "T1" },
      { order_no: "O-2", tracking_no: "T2" },
    ]);
    expect(text).toContain("2 笔");
    expect(text).toContain("不可撤回");
  });

  it("售后文案区分同意 / 拒绝", () => {
    expect(afterSaleConfirmText("agree", "AS-1", 34.8)).toContain("同意");
    expect(afterSaleConfirmText("reject", "AS-1", 34.8)).toContain("拒绝");
    expect(afterSaleConfirmText("agree", "AS-1", null)).toContain("未知");
  });
});

describe("shipOutcomeText（三态回执）", () => {
  it("全成功 ⇒ ok=true", () => {
    const out = shipOutcomeText({ succeeded_count: 3, failed_count: 0 });
    expect(out.ok).toBe(true);
    expect(out.text).toContain("已发货 3 笔");
  });

  it("部分成功 ⇒ ok=false 且**两个数都报**", () => {
    const out = shipOutcomeText({ succeeded_count: 2, failed_count: 1 });
    expect(out.ok).toBe(false);
    expect(out.text).toContain("2");
    expect(out.text).toContain("1");
    expect(out.text).toContain("部分成功");
  });

  it("全失败 ⇒ ok=false", () => {
    const out = shipOutcomeText({ succeeded_count: 0, failed_count: 4 });
    expect(out.ok).toBe(false);
    expect(out.text).toContain("失败 4 笔");
  });
});
