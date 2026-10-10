/**
 * 商品配置第一步 —— 纯逻辑（可单测，无 IO）。
 *
 * 对应千帆发布页第一步的三个动作：
 *   1. 主图 MD5 命名（内容 MD5，天然去重）
 *   2. 图片空间搜索关键词 = 去后缀的文件名
 *   3. 一级类目树 / 四级联动 / 已选类目展示
 *
 * 本模块**刻意不碰 DOM 也不发请求** —— 交互在页面，逻辑在这里。
 */

export const IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"] as const;

/** 支持的**视频**扩展名（商品视频上传用）。 */
export const VIDEO_EXTS = [".mp4", ".mov", ".m4v", ".webm", ".avi"] as const;

/** 图片 + 视频（`md5_file_name` 的允许集）。 */
export const MEDIA_EXTS = [...IMAGE_EXTS, ...VIDEO_EXTS] as const;

/** 用**文件内容**算 MD5 作唯一文件名（用户口径：对内容取 MD5）。
 *
 * - 同内容恒同名 ⇒ 天然去重，重复上传不会堆垃圾（图片空间按名可检索）；
 * - 保留扩展名，因为平台侧按扩展名判类型。
 *
 * ⚠️ 允许集**含视频**（`MEDIA_EXTS`）：商品视频也走同一条「内容 MD5 命名」口径。
 * 早期只认图片扩展名 ⇒ 视频报「不支持的图片扩展名：.mp4」（用户报的缺陷）。
 *
 * 内容为空 ⇒ 抛错（不生成一个"空 MD5"，那会和真实文件撞名）。
 */
export function md5_file_name(content: Uint8Array, options: { ext?: string } = {}): string {
  const ext = options.ext ?? ".jpg";
  if (!content || content.length === 0) {
    throw new Error("文件内容为空，无法生成 MD5 文件名");
  }
  const suffix = ext.startsWith(".") ? ext : `.${ext}`;
  if (!(MEDIA_EXTS as readonly string[]).includes(suffix.toLowerCase())) {
    throw new Error(`不支持的媒体扩展名：${ext}`);
  }
  return `${md5Hex(content)}${suffix.toLowerCase()}`;
}

/** 16 进制 MD5（自带实现，避免为一个哈希引依赖）。 */
function md5Hex(bytes: Uint8Array): string {
  const S = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9,
    14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10, 15,
    21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
  ];
  const K = new Uint32Array(64);
  for (let i = 0; i < 64; i++) {
    K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296);
  }

  const len = bytes.length;
  const padLen = ((56 - (len + 1) % 64) + 64) % 64;
  const total = len + 1 + padLen + 8;
  const buf = new Uint8Array(total);
  buf.set(bytes);
  buf[len] = 0x80;
  const bitLen = len * 8;
  const dv = new DataView(buf.buffer);
  dv.setUint32(total - 8, bitLen >>> 0, true);
  dv.setUint32(total - 4, Math.floor(bitLen / 4294967296), true);

  let a0 = 0x67452301;
  let b0 = 0xefcdab89;
  let c0 = 0x98badcfe;
  let d0 = 0x10325476;
  const rotl = (x: number, c: number) => ((x << c) | (x >>> (32 - c))) >>> 0;

  for (let chunk = 0; chunk < total; chunk += 64) {
    const M = new Uint32Array(16);
    for (let i = 0; i < 16; i++) M[i] = dv.getUint32(chunk + i * 4, true);
    let A = a0;
    let B = b0;
    let C = c0;
    let D = d0;
    for (let i = 0; i < 64; i++) {
      let F: number;
      let g: number;
      if (i < 16) {
        F = (B & C) | (~B & D);
        g = i;
      } else if (i < 32) {
        F = (D & B) | (~D & C);
        g = (5 * i + 1) % 16;
      } else if (i < 48) {
        F = B ^ C ^ D;
        g = (3 * i + 5) % 16;
      } else {
        F = C ^ (B | ~D);
        g = (7 * i) % 16;
      }
      F = (F + A + K[i] + M[g]) >>> 0;
      A = D;
      D = C;
      C = B;
      B = (B + rotl(F, S[i])) >>> 0;
    }
    a0 = (a0 + A) >>> 0;
    b0 = (b0 + B) >>> 0;
    c0 = (c0 + C) >>> 0;
    d0 = (d0 + D) >>> 0;
  }

  const hex = (n: number) =>
    [n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  return `${hex(a0)}${hex(b0)}${hex(c0)}${hex(d0)}`;
}

/** `abc123.jpg` → `abc123`（用户口径：**商品名称不带后缀**）。
 *
 * 用去后缀的名字去图片空间搜，才能命中刚上传的那张。
 */
export function search_keyword(file_name: string): string {
  if (!file_name) throw new Error("文件名为空");
  const low = file_name.toLowerCase();
  //: ⚠️ 用 `MEDIA_EXTS`（含视频）—— 只列图片时 `.mp4` 去不掉后缀，
  //: 视频的检索名就会带着扩展名（与图片口径不一致）。
  for (const ext of MEDIA_EXTS) {
    if (low.endsWith(ext)) return file_name.slice(0, -ext.length);
  }
  return file_name;
}

/** 是不是本模块生成的 MD5 名（32 位十六进制 + 受支持扩展名）。
 *
 * 用途：区分「我们上传的」与「用户从图片空间挑的」——后者不参与去重。
 */
export function is_md5_name(file_name: string): boolean {
  return /^[0-9a-f]{32}$/.test(search_keyword(file_name));
}

/** 一组规格类型 + 各自规格值 → **价格及库存表的行**（规格组合）。
 *
 * 用户口径（2026-10-09）：「添加规格时价格及库存列表应同步添加」——
 * 每加一个规格值，价格/库存表按**笛卡尔积**同步出行；每行带 `spec` 文本
 * （`颜色分类:白 / 尺寸:1.2m`）与稳定 `key`（同名规格值复用同一行，已填的价格/库存不丢）。
 *
 * ⚠️ 缺值口径：**任一规格类型没有值 ⇒ 不出行**（不给「待填」占位行，不造假）。
 * 只有一个规格类型时按单维出 N 行；两个类型时出 M×N 行（真机口径）。
 */
export function spec_combinations(
  specTypeValues: { name: string; values: string[] }[],
): { key: string; spec: string }[] {
  //: 只要有一个规格类型没填值，就没有组合可言（真机：两个都填齐才出行）。
  const dims = specTypeValues
    .map((d) => ({ name: d.name, values: d.values.filter((v) => v.trim() !== "") }))
    .filter((d) => d.values.length > 0);
  if (dims.length === 0) return [];

  let acc: string[][] = [[]];
  for (const dim of dims) {
    const next: string[][] = [];
    for (const prefix of acc) {
      for (const value of dim.values) next.push([...prefix, value]);
    }
    acc = next;
  }
  const names = dims.map((d) => d.name);
  return acc.map((combo) => {
    //: ⚠️ 2026-10-10：key **必须是完整 spec**（`维度名:值`，含维度名），不是只拼值
    //: （旧 `combo.join("\u0001")` 丢维度名 ⇒ 两个维度各有同名值时可能撞 key）。
    //: `spec` 含「维度名 + 值」的完整组合，笛卡尔积下**唯一**，且 `specValues` 变化后
    //: 能**重新推导**（同维度值组合 → 同 spec）⇒ 可作稳定 key 用于「按 key 对齐保留已填值」。
    const spec = combo.map((v, i) => `${names[i]}:${v}`).join(" / ");
    return { key: spec, spec };
  });
}

/** 四级类目 → 「住宅家具 > 成套家具 > 餐厅成套家具 > 餐桌+餐椅」。
 *
 * 缺值口径：空列表 → `""`（不造「未选择」这种假值，由页面决定怎么显示）。
 */
export function category_path_text(path: readonly string[]): string {
  return path.filter((p) => Boolean(p)).join(" > ");
}

/** 已上传的图对象信息（属性预测 `imageUrls` / 落库共用）。 */
export type UploadedImageLike = {
  url?: string;
  file_id?: string;
  search_name?: string;
  origin_name?: string;
  width?: number | null;
  height?: number | null;
  size_bytes?: number;
  extension?: string;
};

/** 拼平台的 `imageUrls[]` 入参（`predictProductProperty` / 提交商品共用）。
 *
 * ⚠️ 真机形状（2026-10-09 抓包）：平台**按图识别**，缺 `link` 或 `path` 的项
 * 一律跳过 —— 传残缺对象等于没传，平台会静默回空属性。
 * ⚠️ 缺值不造假：拿不到的字段就不写（不填 0、不填空串）。
 */
export function buildImageUrls(
  images: readonly UploadedImageLike[],
): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const img of images) {
    const link = String(img.url ?? "").trim();
    const fileId = String(img.file_id ?? "").trim();
    //: 平台要 `link`（签名链）或 `path`（fileId）至少一个才认这张图。
    if (!link && !fileId) continue;
    const item: Record<string, unknown> = {
      type: "spaceImage",
      name: String(img.search_name ?? img.origin_name ?? ""),
      materialId: fileId || undefined,
      path: fileId || undefined,
      link: link || undefined,
      errors: [],
    };
    if (typeof img.width === "number" && img.width > 0) item.width = img.width;
    if (typeof img.height === "number" && img.height > 0) item.height = img.height;
    if (typeof img.size_bytes === "number" && img.size_bytes > 0) item.size = img.size_bytes;
    if (img.extension) item.extension = img.extension;
    if (fileId) {
      item.key = fileId;
      item.uploaderInfoModel = {
        url: link,
        bizName: "ark",
        scene: "goods_image",
        fileId,
        isSecret: false,
        cloudType: 4,
      };
    }
    out.push(item);
  }
  return out;
}

export type CategoryNode = {
  id?: string | number;
  name?: string;
  children?: CategoryNode[];
};

/** 把平台类目树摊成叶子列表（`{id, name, path}`）。
 *
 * 平台树是嵌套 `{id, name, children[]}`；**只有叶子可选**
 * （截图里四级列最后一级才是可点选的）。
 */
export function flatten_category_leaves(node: CategoryNode): {
  id?: string | number;
  name: string;
  path: string[];
}[] {
  const out: { id?: string | number; name: string; path: string[] }[] = [];
  const walk = (item: CategoryNode, trail: string[]) => {
    const name = String(item.name ?? "");
    const here = name ? [...trail, name] : trail;
    const children = item.children ?? [];
    if (children.length === 0) {
      out.push({ id: item.id, name, path: here });
      return;
    }
    for (const ch of children) walk(ch, here);
  };
  walk(node, []);
  return out;
}

/** 已选链路 → 四级列数据（每列是该级的所有候选项）。

 * 平台是**联动列**：点了一级，二级列才出现对应候选。
 */
export function category_columns(selected_path: readonly CategoryNode[]): CategoryNode[][] {
  return selected_path.map((node) => [...(node.children ?? [])]);
}

/**
 * **唯一**的「按 SKU 名查规格图」逻辑（2026-10-10 抽成单一真相源，用户口径
 * 「两处应走同一份逻辑避免分叉」）。
 *
 * SKU 名形如 `颜色分类:斗柜 / 尺寸:1m`（多维度用 ` / ` 分隔、`规格名:值`）。
 * 逐维度拆分 → 在 `map[规格名][规格值]` 里查，取**第一个命中**的值。
 *
 * ⚠️ **规格名与规格值都 trim**：`specImages` 的 key（写入侧 `setSpecImage`）已统一
 * trim，此处查表也必须 trim，否则「值带前后空格」时 key 对不上（历史「第三步规格图
 * 空」的根因之一）。
 *
 * @param skuName SKU 行名（`颜色分类:斗柜` 或 `颜色分类:斗柜 / 尺寸:1m`）
 * @param map 规格图查表（`规格名 → 规格值 → 任意值`），调用方传 `specImageUrls` /
 *            `specImagePaths` / `specImageMaterials` 任一
 */
export function lookup_spec_image<T>(
  skuName: string,
  map: Record<string, Record<string, T>>,
): T | null {
  for (const part of String(skuName || "").split("/")) {
    //: ⚠️ 2026-10-11 真 bug：原来 `const [head, , tail] = part.split(..., 3)` 的
    //: 空位跳过了**值**（第 2 个元素），`tail` 恒 `undefined` ⇒ `value` 空 ⇒
    //: 永远 continue、规格图永远查不到（第三步「规格图不展示」的真根因，不是数据源）。
    //: 正确：`head`=规格名（冒号前）、`tail`=规格值（冒号后）。
    const [head, tail] = part.split(/(?::|：)/, 2);
    const spec = String(head ?? "").trim();
    const value = String(tail ?? "").trim();
    if (!spec || !value) continue;
    const found = map[spec]?.[value];
    if (found !== undefined && found !== null) return found;
  }
  return null;
}
