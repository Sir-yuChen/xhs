/** 「导入 SKU 数据后价格库存未填充」的匹配判据（用户报 2026-10-21）。
 *
 *  现象：映射关系配好了（券后价格→售价、价格→市场价、库存→库存、SKUID→商家编码），
 *       点确认导入后规格全部建成，但**价格/库存/市场价/商家编码 全是空**。
 *
 *  ⚠️ 真根因：两侧 `spec` 的**字符串格式不同**，一条都匹配不上 ——
 *   · `buildSkuFillPlan` 产出的 spec = **行级拼接原文**
 *     （`skuSpecTextOf`：只拼值，`奇遇童话01 / 尺寸/图案任意定制`）
 *   · `spec_combinations` 产出的 spec = **带维度名前缀**
 *     （`颜色分类:奇遇童话01 / 款式:尺寸/图案任意定制`）
 *
 *  ⇒ 本测试**直接测 `lookupSpecFill`**（页面调用的就是它）。
 *    只测 `stripDimPrefix` 本身是假绿：变异「页面退回原串直接比」实测 ESCAPED，
 *    因为那条变异改的是页面，而测试没碰页面调用的函数。
 */
import { describe, expect, it } from "vitest";

import { lookupSpecFill, stripDimPrefix } from "./publish-config";

describe("导入价格库存：两侧规格串必须能对上", () => {
  it("① 带维度名的 combo 串 剥前缀 后 == 行级拼接串", () => {
    expect(
      stripDimPrefix("颜色分类:奇遇童话01 / 款式:尺寸/图案任意定制【联系客服】"),
    ).toBe("奇遇童话01 / 尺寸/图案任意定制【联系客服】");
  });

  it("② 值本身含冒号时不许被切坏（只切第一个冒号）", () => {
    expect(stripDimPrefix("颜色分类:奇遇童话01 / 款式:【适应床宽120cm】平铺款")).toBe(
      "奇遇童话01 / 【适应床宽120cm】平铺款",
    );
  });

  it("③ **核心**：格式不同的两侧必须能匹配到（否则价格库存全空）", () => {
    const fills = [
      { spec: "奇遇童话01 / 尺寸/图案任意定制【联系客服】", price: 20 },
      { spec: "奇遇童话02 / 【适应床宽120cm】", price: 30 },
    ];
    //: 页面传进来的是 combos 的 spec（**带维度名**）。
    expect(
      lookupSpecFill(fills, "颜色分类:奇遇童话01 / 款式:尺寸/图案任意定制【联系客服】")
        ?.price,
    ).toBe(20);
    expect(
      lookupSpecFill(fills, "颜色分类:奇遇童话02 / 款式:【适应床宽120cm】")?.price,
    ).toBe(30);
  });

  it("④ 原串完全相同时走快路径也能命中（不依赖剥前缀）", () => {
    const fills = [{ spec: "奇遇童话03", price: 10 }];
    expect(lookupSpecFill(fills, "奇遇童话03")?.price).toBe(10);
  });

  it("⑤ 真的对不上就返回 undefined（不瞎猜、不造假）", () => {
    const fills = [{ spec: "奇遇童话01", price: 20 }];
    expect(lookupSpecFill(fills, "颜色分类:奇遇童话99")).toBeUndefined();
  });
});
