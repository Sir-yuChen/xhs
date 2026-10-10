/**
 * 商品配置第一步纯逻辑回归（设计 26）。
 *
 * 用**真实数据包的文件名与真实类目路径**写用例，不用编造的样例。
 */
import { describe, expect, it } from "vitest";

import {
  category_columns,
  category_path_text,
  flatten_category_leaves,
  is_md5_name,
  lookup_spec_image,
  md5_file_name,
  spec_combinations,
  search_keyword,
} from "./publish-config";

describe("MD5 文件名", () => {
  it("同内容恒同名 —— 这是去重的根据", () => {
    const bytes = new TextEncoder().encode("same-bytes");
    expect(md5_file_name(bytes, { ext: ".jpg" })).toBe(md5_file_name(bytes, { ext: ".jpg" }));
  });

  it("不同内容不同名", () => {
    const a = new TextEncoder().encode("a");
    const b = new TextEncoder().encode("b");
    expect(md5_file_name(a, { ext: ".jpg" })).not.toBe(md5_file_name(b, { ext: ".jpg" }));
  });

  it("保留扩展名并小写（平台按扩展名判类型）", () => {
    const bytes = new TextEncoder().encode("x");
    expect(md5_file_name(bytes, { ext: ".PNG" })).toMatch(/^[0-9a-f]{32}\.png$/);
  });

  it("空内容抛错 —— 不生成一个会和真实文件撞名的「空 MD5」", () => {
    expect(() => md5_file_name(new Uint8Array(), { ext: ".jpg" })).toThrow();
  });

  it("不支持的扩展名抛错", () => {
    expect(() => md5_file_name(new TextEncoder().encode("x"), { ext: ".exe" })).toThrow();
  });

  //: ⚠️ 回归：商品视频也走「内容 MD5 命名」—— 早期只认图片扩展名，
  //: 上传 mp4 报「不支持的图片扩展名：.mp4」（用户报的缺陷）。
  it("视频扩展名也可命名（mp4/mov 等）", () => {
    const bytes = new TextEncoder().encode("v");
    expect(md5_file_name(bytes, { ext: ".mp4" })).toMatch(/^[0-9a-f]{32}\.mp4$/);
    expect(md5_file_name(bytes, { ext: ".MOV" })).toMatch(/^[0-9a-f]{32}\.mov$/);
  });

  //: 自带实现的 MD5 必须与 Node `crypto` 逐字符一致 ——
  //: 若不一致，去重会**静默失效**（同名判断永远为假），属于最难发现的退化。
  //: 边界要覆盖分块（55/56/64 是 MD5 补位的临界长度）。
  //: 取 node:crypto —— 用**变量**拼模块名，避免 tsc 静态解析
  //: （本项目未装 @types/node，字面量 `import("node:crypto")` 会报 TS2307）。
  const nodeMd5 = async (bytes: Uint8Array): Promise<string> => {
    const spec = ["node", "crypto"].join(":");
    const mod = (await import(/* @vite-ignore */ spec)) as {
      createHash: (alg: string) => { update: (b: Uint8Array) => { digest: (enc: string) => string } };
    };
    return mod.createHash("md5").update(bytes).digest("hex");
  };

  it.each([
    "",
    "a",
    "abc",
    "hello world",
    "x".repeat(55),
    "y".repeat(56),
    "z".repeat(64),
    "小红书商品发布",
    "a".repeat(1000),
  ])("与 node:crypto 同值（len=%i 输入）", async (text) => {
    if (!text) return; //: 空内容按契约抛错，不参与比对
    const bytes = new TextEncoder().encode(text);
    const mine = md5_file_name(bytes).replace(/\.jpg$/, "");
    expect(mine).toBe(await nodeMd5(bytes));
  });
});

describe("检索名（用户口径：商品名称不带后缀）", () => {
  it("剥掉扩展名", () => {
    expect(search_keyword("d41d8cd98f00b204e9800998ecf8427e.jpg")).toBe(
      "d41d8cd98f00b204e9800998ecf8427e"
    );
  });

  it("真实数据包文件名也剥（SKU 图名带括号与中文）", () => {
    expect(search_keyword("SKU01_颜色分类(【中古风】实木三斗柜).jpeg")).toBe(
      "SKU01_颜色分类(【中古风】实木三斗柜)"
    );
    expect(search_keyword("商品属性图.png")).toBe("商品属性图");
  });

  it("没有扩展名时原样返回", () => {
    expect(search_keyword("noext")).toBe("noext");
  });

  //: ⚠️ 回归：视频的检索名也要**去后缀**（与图片同口径）——
  //: 早期只列图片扩展名 ⇒ `.mp4` 去不掉，视频检索名带着扩展名。
  it("视频文件名也剥扩展名", () => {
    expect(search_keyword("d41d8cd98f00b204e9800998ecf8427e.mp4")).toBe(
      "d41d8cd98f00b204e9800998ecf8427e"
    );
    expect(search_keyword("商品视频.MOV")).toBe("商品视频");
  });
});

describe("is_md5_name", () => {
  it("认本模块生成的名", () => {
    expect(is_md5_name("d41d8cd98f00b204e9800998ecf8427e.jpg")).toBe(true);
  });

  it("不认用户自带的业务名（避免误判成可去重）", () => {
    expect(is_md5_name("主图02.jpeg")).toBe(false);
    expect(is_md5_name("SKU01_颜色分类(【中古风】实木三斗柜).jpeg")).toBe(false);
  });
});

describe("类目路径文案", () => {
  it("与真机截图同形", () => {
    expect(
      category_path_text(["住宅家具", "成套家具", "餐厅成套家具", "餐桌+餐椅"])
    ).toBe("住宅家具 > 成套家具 > 餐厅成套家具 > 餐桌+餐椅");
  });

  it("空链路给空串（不造「未选择」假值）", () => {
    expect(category_path_text([])).toBe("");
  });

  it("跳过空段", () => {
    expect(category_path_text(["住宅家具", "", "餐厅成套家具"])).toBe(
      "住宅家具 > 餐厅成套家具"
    );
  });
});

describe("类目树摊平", () => {
  it("只把叶子当可选项 —— 中间层不可选（与联动列一致）", () => {
    const tree = {
      id: 1,
      name: "住宅家具",
      children: [
        {
          id: 2,
          name: "成套家具",
          children: [{ id: 3, name: "餐桌+餐椅", children: [] }],
        },
      ],
    };
    const leaves = flatten_category_leaves(tree);
    expect(leaves).toHaveLength(1);
    expect(leaves[0]).toEqual({ id: 3, name: "餐桌+餐椅", path: ["住宅家具", "成套家具", "餐桌+餐椅"] });
  });

  it("无 children 的节点自己就是叶子", () => {
    const leaves = flatten_category_leaves({ id: 9, name: "茶几", children: [] });
    expect(leaves[0].path).toEqual(["茶几"]);
  });
});

describe("联动列", () => {
  it("每列 = 该级候选项", () => {
    const path = [
      { name: "住宅家具", children: [{ name: "成套家具" }] },
      { name: "成套家具", children: [{ name: "餐桌+餐椅" }] },
    ];
    const cols = category_columns(path);
    expect(cols).toHaveLength(2);
    expect(cols[0][0].name).toBe("成套家具");
    expect(cols[1][0].name).toBe("餐桌+餐椅");
  });
});

describe("spec_combinations：添加规格时价格/库存同步出行（用户口径 2026-10-09）", () => {
  it("单维：N 个规格值 ⇒ N 行", () => {
    const rows = spec_combinations([{ name: "颜色分类", values: ["白", "黑"] }]);
    expect(rows.map((r) => r.spec)).toEqual(["颜色分类:白", "颜色分类:黑"]);
  });
  it("两维：笛卡尔积 M×N 行", () => {
    const rows = spec_combinations([
      { name: "颜色分类", values: ["白", "黑"] },
      { name: "尺寸", values: ["1.2m", "1.5m", "1.8m"] },
    ]);
    expect(rows).toHaveLength(6);
    expect(rows[0].spec).toBe("颜色分类:白 / 尺寸:1.2m");
    expect(rows[5].spec).toBe("颜色分类:黑 / 尺寸:1.8m");
  });
  it("空规格值应被过滤（不造「待填」占位行）", () => {
    const rows = spec_combinations([{ name: "颜色分类", values: ["白", "", "  "] }]);
    expect(rows.map((r) => r.spec)).toEqual(["颜色分类:白"]);
  });
  it("没有任何规格值 ⇒ 空数组（不覆盖外层那一行裸价）", () => {
    expect(spec_combinations([{ name: "颜色分类", values: [] }])).toEqual([]);
    expect(spec_combinations([])).toEqual([]);
  });
  it("key 稳定：同名规格值重算得到同一个 key（已填价格不丢）", () => {
    const a = spec_combinations([{ name: "颜色分类", values: ["白"] }]);
    const b = spec_combinations([{ name: "颜色分类", values: ["白", "黑"] }]);
    expect(a[0].key).toBe(b[0].key);
  });
});

describe("lookup_spec_image：按 SKU 名查规格图（第三步预览唯一查表，2026-10-11）", () => {
  const map = {
    颜色分类: { 斗柜: { url: "https://x/a.jpg" }, 白: { url: "https://x/b.jpg" } },
    尺寸: { "1m": { url: "https://x/c.jpg" } },
  } as Record<string, Record<string, { url: string }>>;

  it("单维度 SKU 名「颜色分类:斗柜」应命中", () => {
    expect(lookup_spec_image("颜色分类:斗柜", map)?.url).toBe("https://x/a.jpg");
  });
  it("多维度 SKU 名「颜色分类:白 / 尺寸:1m」取第一个命中", () => {
    expect(lookup_spec_image("颜色分类:白 / 尺寸:1m", map)?.url).toBe("https://x/b.jpg");
  });
  it("全角冒号「颜色分类：斗柜」也命中", () => {
    expect(lookup_spec_image("颜色分类：斗柜", map)?.url).toBe("https://x/a.jpg");
  });
  it("规格名/值带前后空格仍命中（两侧都 trim）", () => {
    expect(lookup_spec_image(" 颜色分类 : 斗柜 ", map)?.url).toBe("https://x/a.jpg");
  });
  it("查不到 ⇒ null（不造假图）", () => {
    expect(lookup_spec_image("颜色分类:不存在", map)).toBeNull();
    expect(lookup_spec_image("", map)).toBeNull();
  });
});
