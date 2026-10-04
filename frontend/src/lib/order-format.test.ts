/**
 * 订单纯逻辑用例（设计 16 §7，M6 · 一期只读）。
 *
 * 四条判据各有守卫，且都能被变异抓住：
 * 1. **表外状态原样显示**（不造名，DEF-057 同型）；
 * 2. **缺值 `-` 不补 0**（金额 / 百分比 / 时长 / 时间）；
 * 3. **时间不做时区换算**（后端已是上海时间，再转会整体偏 8 小时）；
 * 4. **平台脱敏要能被识别**（`includeFull` 不等于一定有原值）。
 */

import { describe, expect, it } from "vitest";

import {
  EXPORT_WARNING,
  hoursText,
  looksMasked,
  maskingNotice,
  moneyText,
  ORDER_STATUSES,
  orderItemText,
  orderItemsSummary,
  orderStatusColor,
  orderStatusText,
  percentText,
  statusCounts,
  timeText,
} from "./order-format";

describe("orderStatusText", () => {
  it("六个设计状态原样返回", () => {
    for (const s of ORDER_STATUSES) {
      expect(orderStatusText(s)).toBe(s);
    }
  });

  it("表外状态原样显示，不造名", () => {
    // 后端映射表先于前端扩展时，用户必须还能看到平台实际给了什么
    expect(orderStatusText("待揽收")).toBe("待揽收");
    expect(orderStatusText("BRAND_NEW_CODE")).toBe("BRAND_NEW_CODE");
  });

  it("空值退 -（缺值口径，不是空字符串）", () => {
    expect(orderStatusText("")).toBe("-");
    expect(orderStatusText(null)).toBe("-");
    expect(orderStatusText(undefined)).toBe("-");
    expect(orderStatusText("   ")).toBe("-");
  });
});

describe("orderStatusColor", () => {
  it("六个状态各有颜色，且互不相同语义", () => {
    const colors = ORDER_STATUSES.map((s) => orderStatusColor(s));
    expect(new Set(colors).size).toBe(ORDER_STATUSES.length);
  });

  it("表外值走 default，不是 undefined", () => {
    expect(orderStatusColor("未知态")).toBe("default");
    expect(orderStatusColor(null)).toBe("default");
  });
});

describe("缺值口径：一律 - 不补 0", () => {
  it("金额缺值显示 - 而不是 ￥0.00", () => {
    expect(moneyText(null)).toBe("-");
    expect(moneyText(undefined)).toBe("-");
    // 真实的 0 是 0，要显示出来
    expect(moneyText(0)).toBe("￥0.00");
  });

  it("金额千分位 + 两位小数", () => {
    expect(moneyText(1234.5)).toBe("￥1,234.50");
    expect(moneyText(5676)).toBe("￥5,676.00");
  });

  it("百分比缺值显示 -", () => {
    expect(percentText(null)).toBe("-");
    expect(percentText(0)).toBe("0.00%");
    expect(percentText(0.1234)).toBe("12.34%");
  });

  it("时长缺值显示 -，不是 0.0h", () => {
    expect(hoursText(null)).toBe("-");
    expect(hoursText(undefined)).toBe("-");
    expect(hoursText(0)).toBe("0.0h");
    expect(hoursText(25.34)).toBe("25.3h");
  });

  it("时间缺值显示 -", () => {
    expect(timeText(null)).toBe("-");
    expect(timeText("")).toBe("-");
  });
});

describe("timeText: 不做时区换算", () => {
  it("直接取后端给的上海时间字面量", () => {
    // ⚠️ 后端 _iso() 下发的已是上海时间；前端若用 new Date() 会整体偏 8 小时
    expect(timeText("2026-09-03T18:51:20")).toBe("09-03 18:51");
    expect(timeText("2026-09-03 18:51:20")).toBe("09-03 18:51");
  });

  it("非标准形态原样返回（不猜、不崩）", () => {
    expect(timeText("2026/09/03")).toBe("2026/09/03");
  });
});

describe("商品摘要", () => {
  it("SKU 有则带、没有则不带", () => {
    expect(orderItemText({ item_name: "商品A", sku: "红色", quantity: 2 })).toBe("商品A（红色） x2");
    expect(orderItemText({ item_name: "商品A", quantity: 1 })).toBe("商品A x1");
  });

  it("缺商品名 → 空串（由调用方过滤，不编占位名）", () => {
    expect(orderItemText({ item_name: "", sku: "红", quantity: 1 })).toBe("");
    expect(orderItemText({ item_name: null, quantity: 1 })).toBe("");
  });

  it("多件用 | 连，全缺退 -", () => {
    expect(
      orderItemsSummary([
        { item_name: "A", quantity: 1 },
        { item_name: "B", sku: "蓝", quantity: 2 },
      ]),
    ).toBe("A x1 | B（蓝） x2");
    expect(orderItemsSummary([])).toBe("-");
    expect(orderItemsSummary(null)).toBe("-");
  });
});

describe("平台脱敏识别", () => {
  it("含 * 即认为平台已脱敏", () => {
    expect(looksMasked("高*")).toBe(true);
    expect(looksMasked("189***2857")).toBe(true);
    expect(looksMasked("张三")).toBe(false);
    expect(looksMasked(null)).toBe(false);
  });

  it("平台已脱敏时提示『平台未提供完整信息』——不许承诺点开就有原值", () => {
    // ⚠️ includeFull 只是「不做二次脱敏」，不等于「一定有原值」
    expect(maskingNotice("高*", "189***2857")).toContain("平台未提供完整");
    expect(maskingNotice("张三", "13800001111")).toContain("默认脱敏");
  });

  it("导出提示必须明说含完整收件信息", () => {
    expect(EXPORT_WARNING).toContain("完整");
    expect(EXPORT_WARNING).toContain("手机号");
  });
});

describe("statusCounts", () => {
  it("每个状态都有键（缺值 0），页面不用补样板", () => {
    const counts = statusCounts({ 已完成: 3 });
    for (const s of ORDER_STATUSES) {
      expect(counts[s]).toBeTypeOf("number");
    }
    expect(counts["已完成"]).toBe(3);
    expect(counts["待付款"]).toBe(0);
  });

  it("全缺输入也不崩", () => {
    const counts = statusCounts(null);
    expect(Object.keys(counts)).toHaveLength(ORDER_STATUSES.length);
  });
});
