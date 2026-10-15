/**
 * `precheck-zones.ts` 的行为守卫（用户口径 2026-10-13）。
 *
 * 这一轮的核心风险是**静默失效**：`group` 从四值枚举变成目录名原值后，
 * 旧代码里 `m.group === "main"` 这类比较**恒不成立**，界面不会报错、
 * 只是「第二步什么都没带过来」——比抛错难查得多。
 * 所以下面每条都直接钉住「按区域名取媒体」这件事的行为。
 */

import { describe, expect, it } from "vitest";

import {
  ROOT_ZONE,
  emptyZoneMap,
  isImageValue,
  isPublishable,
  isZoneMapReady,
  mediaByZones,
  mediaKey,
  mediaOfZone,
  unsupportedOf,
  unsupportedText,
  unusedZones,
  validateZoneMap,
  zoneNamesOf,
} from "./precheck-zones";
import type { PrecheckMediaItem } from "./api";

/** 造一条媒体（只填本模块用得到的字段）。 */
function m(group: string, rel: string, extra: Partial<PrecheckMediaItem> = {}): PrecheckMediaItem {
  return {
    group,
    index: 0,
    rel_path: rel,
    file_name: rel.split("/").pop() ?? rel,
    size_bytes: 1,
    oversized: false,
    unsupported: false,
    ...extra,
  };
}

/** 包 A 的真实形态：目录名不在任何白名单里。 */
const PACKAGE_A: PrecheckMediaItem[] = [
  //: ⚠️ 交错排列是刻意的 —— 用来钉「合并多个区域时必须按区域分块，不能交错」。
  m("1比1主图", "1比1主图/1比1主图_1.jpg"),
  m("页面展示主图", "页面展示主图/页面展示主图_1.jpg"),
  m("1比1主图", "1比1主图/1比1主图_2.jpg"),
  m("SKU图", "SKU图/SKU图_01_x.jpg"),
  m("主图视频", "主图视频/主图视频_1.mp4"),
  m("详情图", "详情图/详情图_01.jpg"),
  m("详情图", "详情图/详情图_22.gif", {
    unsupported: true,
    unsupported_reason: "平台不支持 .gif 格式的图片（只收 jpg / jpeg / png）",
  }),
];

describe("区域名 = 真实目录名（不是四值枚举）", () => {
  it("按出现顺序取区域名，且保留原始形态", () => {
    expect(zoneNamesOf(PACKAGE_A)).toEqual([
      "1比1主图",
      "页面展示主图",
      "SKU图",
      "主图视频",
      "详情图",
    ]);
  });

  it("按区域名取媒体 —— 这正是旧代码恒不成立的那处", () => {
    //: 旧写法 `group === "main"` 在包 A 上恒空 ⇒ 第二步一个主图都带不进去。
    expect(mediaOfZone(PACKAGE_A, "1比1主图").length).toBe(2);
    expect(mediaOfZone(PACKAGE_A, "页面展示主图").length).toBe(1);
    expect(mediaOfZone(PACKAGE_A, "SKU图").length).toBe(1);
    expect(mediaOfZone(PACKAGE_A, "main").length).toBe(0); //: 旧枚举值取不到了
  });

  it("勾选键含区域名与相对路径（中文目录名不被拆解）", () => {
    expect(mediaKey(m("1比1主图", "1比1主图/a.jpg"))).toBe("1比1主图/1比1主图/a.jpg");
    expect(ROOT_ZONE).toBe("根目录");
  });
});

describe("多区域合并：顺序由用户选中的区域顺序决定", () => {
  it("先整块 A 区、再整块 B 区（**不是**按清单顺序交错）", () => {
    //: 这是封面顺序的判据 —— 交错会让封面变成「1比1主图1 / 页面展示主图1 / …」。
    const merged = mediaByZones(PACKAGE_A, ["1比1主图", "页面展示主图"]);
    expect(merged.map((x) => x.file_name)).toEqual([
      "1比1主图_1.jpg",
      "1比1主图_2.jpg",
      "页面展示主图_1.jpg",
    ]);
  });

  it("换个顺序 ⇒ 结果顺序跟着换（用户可调序）", () => {
    const merged = mediaByZones(PACKAGE_A, ["页面展示主图", "1比1主图"]);
    expect(merged.map((x) => x.file_name)).toEqual([
      "页面展示主图_1.jpg",
      "1比1主图_1.jpg",
      "1比1主图_2.jpg",
    ]);
  });

  it("区内保持清单顺序；不存在的区域被跳过（不报错）", () => {
    expect(mediaByZones(PACKAGE_A, ["详情图", "不存在的区"]).map((x) => x.file_name)).toEqual([
      "详情图_01.jpg",
      "详情图_22.gif",
    ]);
  });
});

describe("方案 C：不合规的列出来但不可发布", () => {
  it("不合规的进清单、可被标出、且有原因文案", () => {
    const bad = unsupportedOf(PACKAGE_A);
    expect(bad.length).toBe(1);
    expect(bad[0].file_name).toBe("详情图_22.gif");
    expect(unsupportedText(bad[0])).toContain("jpg");
  });

  it("原因缺失时有兜底文案（不返回空串给用户看）", () => {
    expect(unsupportedText(m("详情图", "x.gif", { unsupported: true }))).not.toBe("");
  });

  it("可发布 = 格式合规 **且** 体积合规", () => {
    expect(isPublishable(m("详情图", "a.jpg"))).toBe(true);
    expect(isPublishable(m("详情图", "b.gif", { unsupported: true }))).toBe(false);
    expect(isPublishable(m("详情图", "c.jpg", { oversized: true }))).toBe(false);
  });
});

describe("区域映射（主图/详情图/视频 ← 多区域）", () => {
  const zones = [
    { name: "1比1主图", kind: "main" as const },
    { name: "页面展示主图", kind: "main" as const },
    { name: "SKU图", kind: "sku" as const },
    { name: "主图视频", kind: "video" as const },
    { name: "详情图", kind: "detail" as const },
  ];

  it("⚠️ **不给默认值**：初值必须是空映射（用户口径 2026-10-13 否掉了预填）", () => {
    //: 用户口径：「必须通过映射弹窗**指定**后才固定哪些是主图」。
    //: 曾经按 `kind` 猜过一版默认值 —— 预填会让用户直接点确认，
    //: 等于系统替他决定了发布位（而 `kind` 只是参考分类：包 A 的
    //: `1比1主图` 与 `页面展示主图` 都会被猜成主图，该用哪个只有用户知道）。
    const map = emptyZoneMap();
    expect(map.main).toEqual([]);
    expect(map.detail).toEqual([]);
    expect(map.video).toEqual([]);
    //: 空映射 ⇒ 必填位不满足 ⇒ **进不去第二步**（这正是「选择后才可进入」）。
    expect(isZoneMapReady(map)).toBe(false);
    expect(validateZoneMap(map)).toEqual(["请选择主图区域", "请选择详情图区域"]);
    //: 区域清单存在也不影响 —— 系统不替用户预选。
    expect(zones.length).toBeGreaterThan(0);
  });

  it("主图 / 详情图必填，视频可空", () => {
    expect(isZoneMapReady(emptyZoneMap())).toBe(false);
    expect(validateZoneMap(emptyZoneMap())).toEqual(["请选择主图区域", "请选择详情图区域"]);
    const onlyVideoEmpty = { main: ["详情图"], detail: ["详情图"], video: [] };
    expect(isZoneMapReady(onlyVideoEmpty)).toBe(true);
    expect(validateZoneMap(onlyVideoEmpty)).toEqual([]);
  });

  it("列出**未被使用**的区域（漏配要能看出来）", () => {
    const map = { main: ["1比1主图"], detail: ["详情图"], video: [] };
    expect(unusedZones(zones, map)).toEqual(["页面展示主图", "SKU图", "主图视频"]);
  });
});
/* ------------------------------------------------------------------ *
 * isImageValue（用户口径 2026-10-14）
 *
 * > 「新增排队商品 sku 表格 tab **应读到什么就展示什么**，如果为图片则
 * >   展示小图预览方式，**不应该还有线上图和本地图非表格中原始的列**」
 *
 * 判据落在**读到的东西**上，不是列名猜测 —— 同一个「图片」列在不同数据包里
 * 可能叫「图片」「主图」「商品图」，按列名枚举永远漏。
 * ------------------------------------------------------------------ */
describe("isImageValue", () => {
  it.each([
    // 本地文件名 / 带序号前缀的原始图名
    ["a.jpg", true],
    ["a.JPEG", true],
    ["a.png", true],
    ["SKU图_01_中古色-实木三斗柜.jpg", true],
    // 线上 URL（真包现状是 1688 的 gw.alicdn.com）
    ["https://gw.alicdn.com/abc.jpg", true],
    ["https://x.com/a.png?x-oss-process=image", true],
    ["https://x.com/a?imageView2/1/w/100", true],
    // 非图片
    ["", false],
    ["3", false],
    ["甲", false],
    ["A款", false],
    ["颜色分类:【中古风】实木三斗柜", false],
    // ⚠️ 非图片后缀的 URL **不算**图片列（否则每列外链都会变成缩略图位）
    ["https://x.com/a.txt", false],
    ["https://x.com/page", false],
  ])("%s → %s", (value, want) => {
    expect(isImageValue(value)).toBe(want);
  });
});
