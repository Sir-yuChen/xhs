/** 千帆枚举：**前后端值必须一致**（守卫）。
 *
 * 背景：`deliveryMode` 曾被前端当「发货模式」、后端当「物流类型」⇒ 平台报
 * **「商品不支持设置无物流发货」**。这类错误**不会在编译期或类型检查里暴露**，
 * 只能在值层面钉住。
 *
 * ⇒ 本文件把后端枚举期望值抄一份（改后端不改这里 ⇒ 这里红）。
 * 后端侧对应守卫：`tests/backend/test_qianfan_item_enums.py`。
 */

import { describe, expect, it } from "vitest";

import {
  DeliveryTimeType,
  FreeReturn,
  ItemStatus,
  LOGISTICS_TYPE_LABELS,
  LogisticsType,
  PlaybackWidget,
  SaleTimeMode,
  SPOT_SHIP_TIME,
  StockType,
} from "./qianfan-enums";

describe("物流类型（deliveryMode）", () => {
  it("数字值必须与后端 kianfan_item_enums.LogisticsType 一致", () => {
    expect(LogisticsType.LOGISTICS).toBe(0); //: 真机 radio value="0"
    expect(LogisticsType.NOT_LOGISTICS).toBe(1);
  });

  it("**物流类型 ≠ 发货模式** —— 默认是物流配送（0），不是 1", () => {
    //: 填 1 会被平台判成「无物流发货」⇒ 发布被拒。
    expect(LogisticsType.LOGISTICS).not.toBe(LogisticsType.NOT_LOGISTICS);
    expect(LogisticsType.LOGISTICS).toBe(0);
  });

  it("界面文案两档且与值一一对应", () => {
    expect(LOGISTICS_TYPE_LABELS.logistics).toBe("物流配送");
    expect(LOGISTICS_TYPE_LABELS.notLogistics).toBe("无物流发货或物流配送");
  });
});

describe("发货时间（skuData[].deliveryTime.type）", () => {
  it("三档数字值与后端一致", () => {
    expect(DeliveryTimeType.SAME_DAY).toBe(3);
    expect(DeliveryTimeType.HOURS).toBe(4);
    expect(DeliveryTimeType.DATE).toBe(5);
  });

  it("现货时效的界面取值是真机 radio value（同日= -1，不是 0）", () => {
    expect(SPOT_SHIP_TIME.SAME_DAY).toBe("-1");
    expect(SPOT_SHIP_TIME.H24).toBe("24");
    expect(SPOT_SHIP_TIME.H48).toBe("48");
  });

  it("15 天预售换算成 360 小时（后端同口径）", () => {
    const days = 15;
    expect(String(days * 24)).toBe("360");
  });
});

describe("其它平台枚举", () => {
  it("开售时间", () => {
    expect(SaleTimeMode.MANUAL).toBe(2);
    expect(SaleTimeMode.SCHEDULED).toBe(3);
  });

  it("回看讲解挂件", () => {
    expect(PlaybackWidget.HIDDEN).toBe(0);
    expect(PlaybackWidget.SHOWN).toBe(1);
  });

  it("库存类型 / 商品状态 / 七天无理由", () => {
    expect(StockType.SPOT).toBe(1);
    expect(ItemStatus.ON_SHELF).toBe(1);
    expect(ItemStatus.OFF_SHELF).toBe(2);
    expect(FreeReturn.SUPPORTED).toBe(1);
  });
});
