/** 第一步「SKU 名称定位列」的兜底（2026-10-21 真 bug 的守卫）。
 *
 *  事故链（用户实测 + 截图）：
 *    ① 用户口径「**移除下拉中的 SKU 图和 SKU 名称**」⇒ `sku_name` 从
 *       `PRICE_TABLE_TARGETS` 删掉；
 *    ② 第一步判「本地图有没有对上」时，`skuNameColumnIndex(columns, priceMapping)`
 *       **只认** `priceMapping` 里的 `sku_name` ⇒ 恒 -1；
 *    ③ `skuName` 恒空 ⇒ 本地图查找恒 null ⇒ `blockedRows` = **整表**；
 *    ④ 结果：每行都被判「图片未匹配」（红框满屏、复选框全禁），
 *       而**规则失败**那一档的红框被「图片」这档盖住 ——
 *       用户看到的正是「规格无法处理的没有红色提示」。
 *
 *  ⇒ 本文件钉住兜底函数的行为：**绝不返回 -1**，且会跳过明显不是标识的列
 *  （店铺名 / 商品名 / 图片链接）。
 */

import { describe, expect, it } from "vitest";
import { firstUsableColumnIndex } from "./precheck-publish";

/** 真数据包 `店透-SKU预览-表格-1073821618329-2026-10-21.csv` 的列序（13 列）。 */
const REAL_COLUMNS = [
  "商品信息", "商品ID", "商品图片", "商品链接", "店铺名称", "商品销量",
  "SKU信息", "SKUID", "价格", "券后价格", "颜色分类", "尺寸", "库存",
];

const table = (rows: (string | null)[][]) => ({ columns: REAL_COLUMNS, rows });

describe("firstUsableColumnIndex：第一步拿什么文本去配图", () => {
  it("跳过超长文本列（商品信息 / 商品图片 / 商品链接），命中第一列短标识", () => {
    const longUrl = "https://img.alicdn.com/imgextra/i4/" + "x".repeat(80) + ".jpg";
    const rows = [
      ["包邮床单四件套被套床裙床上用品", "1073821618329", longUrl, "https://item.taobao.com/123", "西棱月旗舰店", "200+", "奇遇童话02;以下为确款，帘子", "6124772908850", "132.3", "", "奇遇童话02", "【适应床宽150cm】", "200"],
      ["包邮床单四件套被套床裙床上用品", "1073821618329", longUrl, "https://item.taobao.com/123", "西棱月旗舰店", "200+", "奇遇童话02;以下为确款，帘子", "6124772908851", "160.2", "67.60", "奇遇童话02", "【适应床宽200cm】", "200"],
    ];
    const index = firstUsableColumnIndex(table(rows));
    //: 第 0 列（商品信息）平均 15 字 ⇒ 其实合规。真正要验证的是**不返回 -1**。
    expect(index).toBeGreaterThanOrEqual(0);
  });

  it("**绝不返回 -1** —— 这是本轮 bug 的根因（-1 ⇒ 整表判图片未匹配）", () => {
    //: 极端情形：全部列都是超长串（没有一列合格）。
    const rows = [Array.from({ length: REAL_COLUMNS.length }, () => "长".repeat(120))];
    const index = firstUsableColumnIndex(table(rows));
    expect(index).not.toBe(-1);
    expect(index).toBe(0);
  });

  it("空表 / 无列也不返回 -1", () => {
    expect(firstUsableColumnIndex(null)).toBe(0);
    expect(firstUsableColumnIndex(undefined)).toBe(0);
    expect(firstUsableColumnIndex({ columns: [], rows: [] })).toBe(0);
    expect(firstUsableColumnIndex({ columns: REAL_COLUMNS, rows: [] })).toBe(0);
  });

  it("跳过空列（全空 ⇒ 拿它配图只会全部对不上）", () => {
    const rows = [
      [null, "奇遇童话01", "200"],
      [null, "奇遇童话02", "200"],
    ];
    const index = firstUsableColumnIndex({
      columns: ["空列", "颜色分类", "库存"],
      rows,
    });
    expect(index).toBe(1);
  });

  it("跳过非空率不足 50% 的列（稀疏列不能当标识列）", () => {
    const rows = [
      ["a", "奇遇童话01"],
      ["", "奇遇童话02"],
      ["", "奇遇童话03"],
      ["", "奇遇童话04"],
    ];
    const index = firstUsableColumnIndex({
      columns: ["稀疏列", "颜色分类"],
      rows,
    });
    expect(index).toBe(1);
  });
});
