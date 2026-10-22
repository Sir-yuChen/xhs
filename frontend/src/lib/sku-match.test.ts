/**
 * SKU 图匹配的行为守卫（**定案口径 2026-10-14**）。
 *
 * 用户原话：
 *   「图片名称**不是固定样式**的，但**一定包含** sku 名」
 *   「反推后只匹配成功了第一条」
 *
 * 判据因此落成：
 *   左 = SKU 列值（**用户正则取值**后 —— 值的边界只有用户知道）
 *   右 = 图片文件名（去目录、去扩展名）
 *   命中 ⟺ **右 包含 左**
 *
 * ⚠️ 为什么不再用「两侧各跑正则 + 精确相等」：图片名样式不固定
 * （`SKU01_颜色分类(值).jpg` / `实木三斗柜_01.jpg` / `SKU-实木三斗柜.png`
 * 都可能），唯一稳定的共同点是「**一定包含 SKU 名**」。相等判据要求图片名
 * 恰好只有值，换个包就全废；而「靠反推学图片名正则」会把该条样例特有的
 * 字面量写死（`SKU01`），这正是用户报的「只匹配成功第一条」。
 *
 * 消歧（包含判据的副作用：短值 ⊂ 长值）：
 *   ① 全局**最长优先**  ② **一图一用**
 */

import { describe, expect, it } from "vitest";

import { applyExtract, imageBaseName, matchSkuRows, matchesSkuName } from "./precheck-zones";

const SKU_PATTERN = "^[^:：;；]+[:：;；](.+)$";

describe("图片名载体：只取 basename 并去扩展名", () => {
  it("剥目录 + 剥扩展名", () => {
    expect(imageBaseName("SKU/SKU01_甲.jpg")).toBe("SKU01_甲");
    expect(imageBaseName("a\\b\\SKU01_甲.PNG")).toBe("SKU01_甲");
    expect(imageBaseName("SKU01_甲")).toBe("SKU01_甲");
  });
});

describe("matchesSkuName：图片名**包含** SKU 名即命中", () => {
  it("值夹在括号里 ⇒ 命中", () => {
    expect(
      matchesSkuName(
        "颜色分类:【中古风】实木三斗柜",
        "SKU01_颜色分类(【中古风】实木三斗柜).jpg",
        SKU_PATTERN,
      ),
    ).toBe(true);
  });

  it("⚠️ **任意命名风格**都能命中（用户口径「不是固定样式」）", () => {
    const files = [
      "实木三斗柜_01.jpg",
      "SKU-实木三斗柜.png",
      "商品图-实木三斗柜-主图.jpg",
      "a/b/实木三斗柜.jpeg",
    ];
    for (const f of files) {
      expect(matchesSkuName("颜色分类:实木三斗柜", f, SKU_PATTERN), f).toBe(true);
    }
  });

  it("不含 ⇒ 不命中；空值 ⇒ 不命中", () => {
    expect(matchesSkuName("颜色分类:实木三斗柜", "SKU01_颜色分类(六斗柜).jpg", SKU_PATTERN)).toBe(
      false,
    );
    expect(matchesSkuName("", "SKU01_甲.jpg", SKU_PATTERN)).toBe(false);
  });

  it("扩展名不参与比较（`.jpg` 不算「名的一部分」）", () => {
    //: 值 `实木三斗柜` 与文件名 `实木三斗柜_01` —— 去掉 `.jpg` 后仍是包含关系。
    expect(matchesSkuName("颜色分类:实木三斗柜", "实木三斗柜_01.jpg", SKU_PATTERN)).toBe(true);
  });
});

describe("matchSkuRows：最长优先 + 一图一用", () => {
  const cands = [
    { fileName: "SKU01_颜色分类(实木三斗柜).jpg", relPath: "SKU/SKU01.jpg" },
    { fileName: "SKU02_颜色分类(伸缩桌+三斗柜梳妆台).jpg", relPath: "SKU/SKU02.jpg" },
    { fileName: "SKU03_颜色分类(伸缩桌+三斗柜梳妆台+云朵妆镜).jpg", relPath: "SKU/SKU03.jpg" },
  ];

  it("⚠️ 短值**不得**抢走长值的图（最长优先）", () => {
    //: ⚠️ **候选顺序必须让「长图在前」** —— 否则短值那行先命中的就是短图，
    //: 根本不发生抢占，这条用例会把「去掉最长优先」的变异放过去（实测踩过）。
    const candsLongFirst = [
      { fileName: "SKU03_颜色分类(伸缩桌+三斗柜梳妆台+云朵妆镜).jpg", relPath: "SKU/SKU03.jpg" },
      { fileName: "SKU02_颜色分类(伸缩桌+三斗柜梳妆台).jpg", relPath: "SKU/SKU02.jpg" },
    ];
    const rows = [
      { skuName: "颜色分类:伸缩桌+三斗柜梳妆台" },
      { skuName: "颜色分类:伸缩桌+三斗柜梳妆台+云朵妆镜" },
    ];
    const out = matchSkuRows(rows, candsLongFirst, SKU_PATTERN);
    //: 先到先得的话，短值(行0)会先抢走 cands[0] = SKU03（错的）；
    //: 最长优先让长值(行1)先拿 SKU03，短值只能落在 SKU02。
    expect(out[1].relPath).toBe("SKU/SKU03.jpg"); //: 长值拿到长的那张
    expect(out[0].relPath).toBe("SKU/SKU02.jpg"); //: 短值拿剩下的
  });

  it("⚠️ 用户口径 2026-10-21：**同一张图可被多行重复匹配**", () => {
    //: 这一条**取代**原先的「一图一用」：真实包 24 张图对 456 行，
    //: 同名规格（`奇遇童话 01` 反复出现）只有第一行拿到图，其余全「未匹配」。
    const rows = [{ skuName: "颜色分类:实木三斗柜" }, { skuName: "颜色分类:实木三斗柜" }];
    const out = matchSkuRows(rows, cands, SKU_PATTERN);
    expect(out[0].relPath).toBe("SKU/SKU01.jpg");
    expect(out[1].relPath).toBe("SKU/SKU01.jpg"); //: 复用同一张图
  });

  it("重复匹配下**每行仍挑最贴切的那张**（不因可复用就都指向同一张长图）", () => {
    //: 两行共用一张图是允许的（2026-10-21），但每行要挑**文件名最短**够用的：
    //: 短值行 ⊂ 两张图名 ⇒ 选短的那张；长值行只 ⊂ 长图名 ⇒ 选长那张。
    const rows = [
      { skuName: "颜色分类:伸缩桌+三斗柜梳妆台" },
      { skuName: "颜色分类:伸缩桌+三斗柜梳妆台+云朵妆镜" },
    ];
    const candsLongFirst = [
      { fileName: "SKU03_颜色分类(伸缩桌+三斗柜梳妆台+云朵妆镜).jpg", relPath: "SKU/SKU03.jpg" },
      { fileName: "SKU02_颜色分类(伸缩桌+三斗柜梳妆台).jpg", relPath: "SKU/SKU02.jpg" },
    ];
    const out = matchSkuRows(rows, candsLongFirst, SKU_PATTERN);
    expect(out[0].relPath).toBe("SKU/SKU02.jpg"); //: 短值行挑短图
    expect(out[1].relPath).toBe("SKU/SKU03.jpg"); //: 长值行挑长图
  });

  it("usedCandidates 里的图不参与（让位给手选）", () => {
    const rows = [{ skuName: "颜色分类:实木三斗柜" }];
    const out = matchSkuRows(rows, cands, SKU_PATTERN, new Set(["SKU/SKU01.jpg"]));
    expect(out[0].relPath).toBe(null);
  });

  it("值经正则取值后参与包含判断（剥列名前缀）", () => {
    const rows = [{ skuName: "颜色分类:实木三斗柜" }];
    const out = matchSkuRows(rows, cands, SKU_PATTERN);
    expect(out[0].relPath).toBe("SKU/SKU01.jpg");
    //: 列名前缀参与的话就匹配不上（文件名里是 `颜色分类(` 而非 `颜色分类:`）。
    expect(applyExtract("颜色分类:实木三斗柜", SKU_PATTERN)).toBe("实木三斗柜");
  });

  it("正则留空 ⇒ 用原值（整串包含）", () => {
    const rows = [{ skuName: "实木三斗柜" }];
    const out = matchSkuRows(rows, cands, "");
    expect(out[0].relPath).toBe("SKU/SKU01.jpg");
  });

  it("候选里没有包含关系的图 ⇒ 未命中（**不按序号兜底**）", () => {
    const rows = [{ skuName: "颜色分类:完全不同的规格" }];
    const out = matchSkuRows(rows, cands, SKU_PATTERN);
    expect(out[0].relPath).toBeNull();
  });
});

describe("SKU 列取值正则（applyExtract）", () => {
  it("留空 ⇒ 用原值", () => {
    expect(applyExtract("颜色分类:甲", "")).toBe("颜色分类:甲");
    expect(applyExtract("颜色分类:甲", "   ")).toBe("颜色分类:甲");
  });

  it("有捕获组 ⇒ 取第 1 组；无组 ⇒ 取整串", () => {
    expect(applyExtract("颜色分类:甲", SKU_PATTERN)).toBe("甲");
    expect(applyExtract("abc123", "\\d+")).toBe("123");
  });

  it("⚠️ 没匹配上 / 语法错 ⇒ 回落原值（不返回空串，否则会静默判全表未命中）", () => {
    expect(applyExtract("甲", "^ZZZ(.+)$")).toBe("甲");
    expect(applyExtract("甲", "([")).toBe("甲");
  });

  it("⚠️ 正则里的特殊字符必须**转义**，不能被当元字符吃掉", () => {
    //: 用户口径 2026-10-14：「名称中可能存在特殊字符，**不应该去掉，转义即可**」。
    expect(applyExtract("A+B:甲", "^A\\+B[:：](.+)$")).toBe("甲");
    expect(applyExtract("颜色(分类):甲", "^颜色\\(分类\\)[:：](.+)$")).toBe("甲");
    expect(applyExtract("甲[1]:乙", "^甲\\[1\\][:：](.+)$")).toBe("乙");
  });
});

describe("真包端到端：整列都要命中（不是只有第一条）", () => {
  /** 真包 `商品ID_935947169189` 的 26 条 SKU 列值 → 图文件名。 */
  const PACK: [string, string][] = [
    ["颜色分类:【中古风】实木三斗柜", "SKU01_颜色分类(【中古风】实木三斗柜).jpg"],
    ["颜色分类:【中古风】伸缩桌+三斗柜梳妆台", "SKU02_颜色分类(【中古风】伸缩桌+三斗柜梳妆台).jpg"],
    [
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜",
      "SKU03_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜).jpg",
    ],
    [
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+妆凳",
      "SKU04_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+妆凳).jpg",
    ],
    [
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳",
      "SKU05_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳).jpg",
    ],
    ["颜色分类:【中古风】实木六斗柜", "SKU14_颜色分类(【中古风】实木六斗柜).jpg"],
  ];

  it("⚠️ 全部命中，且**每行拿到自己的那张图**（用户报的「只有第一条」不再复现）", () => {
    const rows = PACK.map(([col]) => ({ skuName: col }));
    const cands = PACK.map(([, file]) => ({ fileName: file, relPath: "SKU/" + file }));
    const out = matchSkuRows(rows, cands, SKU_PATTERN);

    expect(out.every((r) => !!r.relPath)).toBe(true);
    out.forEach((r, i) => expect(r.relPath).toBe("SKU/" + PACK[i][1]));
  });

  it("长值优先：`实木三斗柜` 不会抢走 `实木六斗柜` 的图", () => {
    //: 这两个值**互不为子串**（三≠六），但长度相同 ⇒ 各自命中各自的图。
    const rows = [
      { skuName: "颜色分类:【中古风】实木三斗柜" },
      { skuName: "颜色分类:【中古风】实木六斗柜" },
    ];
    const cands = [
      { fileName: "SKU01_颜色分类(【中古风】实木三斗柜).jpg", relPath: "a" },
      { fileName: "SKU14_颜色分类(【中古风】实木六斗柜).jpg", relPath: "b" },
    ];
    const out = matchSkuRows(rows, cands, SKU_PATTERN);
    expect(out[0].relPath).toBe("a");
    expect(out[1].relPath).toBe("b");
  });
});
