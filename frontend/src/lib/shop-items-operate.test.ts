/**
 * 商品批量操作（上架 / 下架 / 删除）纯逻辑的 vitest 用例（2026-10-05 需求 7/8）。
 *
 * 盯两件事：
 * 1. **单次上限与后端同值**（前端放行、后端拒 = 用户点个注定失败的按钮）；
 * 2. **确认框文案不许与「删除本地台账」互相污染** —— 后者说「下次同步会写回来」，
 *    前者是真实平台写操作，说反了就等于在最危险的操作上给最放松的心理预期。
 */
import { describe, expect, it } from "vitest";

import {
  MAX_OPERATE_ITEMS,
  OPERATE_LABELS,
  batchOperateBlock,
  batchOperateHint,
} from "./shop-items-view";

describe("MAX_OPERATE_ITEMS", () => {
  it("与后端同值（改任一侧都该红）", () => {
    //: 后端真值由 `tests/backend/test_shop_item_operate.py` 直接读本文件比对；
    //: 这里钉住字面量，避免有人「顺手调一下」而没跑后端测试。
    expect(MAX_OPERATE_ITEMS).toBe(200);
  });
});

describe("OPERATE_LABELS", () => {
  it("三个动作都有中文名（缺一个会让按钮显示英文键）", () => {
    expect(Object.keys(OPERATE_LABELS).sort()).toEqual(["delete", "off_shelf", "on_shelf"]);
    expect(OPERATE_LABELS.on_shelf).toBe("上架");
    expect(OPERATE_LABELS.off_shelf).toBe("下架");
    expect(OPERATE_LABELS.delete).toBe("删除");
  });
});

describe("batchOperateBlock", () => {
  it("没勾选 ⇒ empty", () => {
    expect(batchOperateBlock([])).toBe("empty");
  });

  it("恰好上限 ⇒ 放行（边界含）", () => {
    const keys = Array.from({ length: MAX_OPERATE_ITEMS }, (_, index) => `I-${index}`);
    expect(batchOperateBlock(keys)).toBe("");
  });

  it("超一个 ⇒ too_many", () => {
    const keys = Array.from({ length: MAX_OPERATE_ITEMS + 1 }, (_, index) => `I-${index}`);
    expect(batchOperateBlock(keys)).toBe("too_many");
  });

  it("空白 id 不计入（与后端去重口径一致）", () => {
    expect(batchOperateBlock(["A", "  ", ""])).toBe("");
    expect(batchOperateBlock(["  ", ""])).toBe("empty");
  });
});

describe("batchOperateHint", () => {
  it("说清是真实平台写操作（不是只改本地）", () => {
    const text = batchOperateHint("off_shelf", 3);
    expect(text).toContain("3 个平台商品");
    expect(text).toContain("下架");
    expect(text).toContain("真实修改千帆店铺");
  });

  it("承诺「不支持的只记录不执行」（需求 7 的原话要出现在用户看得到的地方）", () => {
    expect(batchOperateHint("on_shelf", 1)).toContain("只记录、不执行");
  });

  it("删除额外警告不可恢复", () => {
    expect(batchOperateHint("delete", 1)).toContain("不可恢复");
    //: 上下架不该带这句（商品还能再上下架回去）——带上会让人以为下架也丢东西。
    expect(batchOperateHint("off_shelf", 1)).not.toContain("不可恢复");
  });

  it("**不许**出现「下次同步会写回来」（那是删除本地台账的说法）", () => {
    for (const action of ["on_shelf", "off_shelf", "delete"]) {
      const text = batchOperateHint(action, 2);
      expect(text).not.toContain("同步会写回来");
      //: 「只改本地」只能以**否定形式**出现（「不是只改本地台账」）——
      //: 肯定形式就是台账删除那套说辞，出现在写平台上会让人以为是安全操作。
      expect(text).not.toMatch(/[^是不]只改本地/);
      expect(text).toContain("不是只改本地台账");
    }
  });

  it("未知动作退化成原样键（不造中文名）", () => {
    expect(batchOperateHint("explode", 1)).toContain("explode");
  });
});
