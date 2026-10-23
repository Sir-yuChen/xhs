/**
 * 预检**媒体区域**的纯逻辑（设计 25 §4；用户口径 2026-10-13）。
 *
 * ## 这一轮改了什么（为什么单独成文件）
 *
 * 旧实现把「区域」当**固定四值枚举**（`main`/`video`/`sku`/`detail`），
 * 直接写在组件里做 `m.group === "main"` 这种字符串比较。
 * 用户 2026-10-13 改口径：
 *
 * > 「数据包中读取到什么就**按照读取到的文件夹分区域**，不做任何操作直接展示」
 * > 「读到什么媒体资源就展示什么」
 *
 * ⇒ 区域名 = 数据包里**一级目录名的原值**（`1比1主图` / `页面展示主图` /
 * `SKU图` / `主图视频` / `详情图` / `属性` / `根目录`）。
 * 那 4 处字符串比较全部**恒不成立** ⇒ 第二步一个主图都带不进去，
 * 而且**不报错**（最难查的静默失效）。
 *
 * 所以这些判据抽到这里：**单一实现 + 可 vitest 直测**（组件里的逻辑测不到）。
 */

import type { PrecheckKind, PrecheckMediaItem, PrecheckZoneName } from "./api";

/** 顶层散落文件（没有一级目录）所在的区域名 —— 与后端 `ROOT_ZONE` 同值。 */
export const ROOT_ZONE = "根目录";

/** 区域参考类别的展示名（只作提示，不影响任何过滤）。 */
export const KIND_LABELS: Record<PrecheckKind, string> = {
  main: "主图",
  video: "视频",
  sku: "SKU 图",
  detail: "详情图",
  other: "其它",
};

/** 从媒体清单里**按出现顺序**取区域名（保序去重）。

 *  ⚠️ 不用 `zones` 字段也能算出（后端老版本 / 测试夹具可能没给）——
 *  这样前端**不依赖后端一定下发 `zones`**（缺了也能正确分区渲染）。
 */
export function zoneNamesOf(media: readonly PrecheckMediaItem[]): PrecheckZoneName[] {
  const out: PrecheckZoneName[] = [];
  for (const item of media) {
    const name = String(item.group ?? "");
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

/** 某个区域下的媒体（保序 = 清单顺序）。 */
export function mediaOfZone<T extends PrecheckMediaItem>(
  media: readonly T[],
  zone: PrecheckZoneName,
): T[] {
  return media.filter((m) => m.group === zone);
}

/** 三个**媒体勾选键**的唯一实现（媒体区 / SKU 表 / 第二步都用它）。

 *  ⚠️ `group` 现在是目录名，可能含 `/` 之外的任意字符（中文 / 空格 / `图`），
 *  所以**不能**再把 `group` 当路径段去解；键就是 `group + "/" + rel_path`，
 *  与后端 `rel_path`（`<目录>/<文件名>`）拼出来的形态天然一致。
 */
export function mediaKey(item: { group: PrecheckZoneName; rel_path: string }): string {
  return `${item.group}/${item.rel_path}`;
}

/** 该区域是否**全是**不可上传的（用于给区域标题加提示）。 */
export function zoneHasUnsupported(media: readonly PrecheckMediaItem[]): boolean {
  return media.some((m) => m.unsupported);
}

/** 不可上传的媒体（**方案 C**：它们照旧在清单里，只是标出来）。 */
export function unsupportedOf(media: readonly PrecheckMediaItem[]): PrecheckMediaItem[] {
  return media.filter((m) => m.unsupported);
}

/** 不合规提示文案（红框 tooltip 用）—— **统一来源**，不在组件里各处拼。 */
export function unsupportedText(item: PrecheckMediaItem): string {
  const reason = String(item.unsupported_reason ?? "").trim();
  if (reason) return reason;
  //: 兜底：后端老版本可能没给 `unsupported_reason`。
  return `平台不支持该格式的图片（只收 jpg / jpeg / png）`;
}

/** 一条媒体**能否参与发布**（可上传 + 体积合规）。

 *  ⚠️ 判据是 `unsupported` / `oversized` 两个**后端下发的事实**，
 *  不是前端按后缀猜 —— 平台约束改了只需改后端一处。
 */
export function isPublishable(item: PrecheckMediaItem): boolean {
  return !item.unsupported && !item.oversized;
}

/** 是不是视频文件（按**后缀**判，不看区域名）。

 *  ⚠️ 必须按后缀判：区域名现在是**目录名原值**（`主图视频` / `视频`），
 *  旧写法 `item.group === "video"` 在包 A 上恒不成立 ⇒ 视频被当图片渲染
 *  （<img> 加载 mp4 ⇒ 空白框 + 无提示）。
 */
export function isVideoFile(nameOrPath: string): boolean {
  const text = String(nameOrPath ?? "").split(/[\\/]/).pop() ?? "";
  const m = text.match(/\.[A-Za-z0-9]+$/);
  const ext = (m ? m[0] : "").toLowerCase();
  return VIDEO_FILE_EXTS.has(ext);
}

/** 视频扩展名（与后端 `VIDEO_EXTS` 对齐）。 */
export const VIDEO_FILE_EXTS = new Set([
  ".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg",
  ".avi", ".mkv", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".3gp",
]);

/* ------------------------------------------------------------------ *
 * 发布位 ← 区域 的**多选映射**（用户口径 2026-10-13）
 *
 * > 「必须通过映射弹窗指定后才固定哪些是主图，哪些是详情图，
 * >   均支持合并多个第一步中的图片区域」
 *
 * ⚠️ 顺序是硬要求：主图顺序 = 封面顺序。
 * `media.filter(m => zones.includes(m.group))` 会按**清单原始顺序**把
 * 两个区的媒体**交错**输出（先 A 区一张、再 B 区一张……），
 * 而用户要的是「先整个 A 区、再整个 B 区」。所以必须**两段循环**。
 * ------------------------------------------------------------------ */

/** 按**用户选中的区域顺序**取媒体（区内保持清单顺序）。 */
export function mediaByZones<T extends PrecheckMediaItem>(
  media: readonly T[],
  zones: readonly PrecheckZoneName[],
): T[] {
  const byZone = new Map<PrecheckZoneName, T[]>();
  for (const item of media) {
    const bucket = byZone.get(item.group);
    if (bucket) bucket.push(item);
    else byZone.set(item.group, [item]);
  }
  const out: T[] = [];
  for (const zone of zones) {
    const bucket = byZone.get(zone);
    if (bucket) out.push(...bucket);
  }
  return out;
}

/** 四个**发布位**的键（区域映射弹窗的四行）。 */
export type PublishSlotKey = "main" | "detail" | "video";

/** 发布位 → 中文名（弹窗行标签）。 */
export const PUBLISH_SLOT_LABELS: Record<PublishSlotKey, string> = {
  main: "主图区域",
  detail: "详情图区域",
  video: "商品视频",
};

/** 发布位的必填性：主图 / 详情图必填，商品视频可空。 */
export const PUBLISH_SLOT_REQUIRED: Record<PublishSlotKey, boolean> = {
  main: true,
  detail: true,
  video: false,
};

/** 发布位 → 已选区域数组（`main` / `detail` / `video`）。 */
export type ZoneMap = Record<PublishSlotKey, PrecheckZoneName[]>;

/** 空映射（弹窗初值；主图/详情图留空 ⇒ 逼用户显式选）。 */
export function emptyZoneMap(): ZoneMap {
  return { main: [], detail: [], video: [] };
}

/**
 * ~~默认映射（按 `kind` 猜）~~ —— **已删除**（用户口径 2026-10-13）。
 *
 * ⚠️ 曾经按区域**参考类别**猜一版默认值填进弹窗，**已被用户否掉**：
 * 「第一步与第二步的图片资源**没有直接关系**，**必须通过映射弹窗指定**后才固定
 *   哪些是主图，哪些是详情图」。
 *
 * 预填的两个害处：
 *   ① 用户会直接点确认 ⇒ 等于系统替他决定了发布位，而 `kind` 只是**参考分类**
 *      （包 A 的 `1比1主图` 与 `页面展示主图` 都会被猜成主图，该用哪个只有用户知道）；
 *   ② 猜错时界面是「看起来填好了」的状态，比空着更难看懂 —— 空着至少知道要选。
 *
 * ⇒ 一律从 `emptyZoneMap()` 开始，逼用户显式选一遍。
 * 保留段注释是为了让「这里本来有个函数、为什么没了」可追溯（不静默删除）。
 */

/** 映射是否**可提交**（必填位非空即可；视频可为空）。 */
export function isZoneMapReady(map: ZoneMap): boolean {
  return (["main", "detail", "video"] as PublishSlotKey[]).every(
    (slot) => !PUBLISH_SLOT_REQUIRED[slot] || (map[slot]?.length ?? 0) > 0,
  );
}

/** 校验映射，返回**逐位**的问题（空数组 = 全部合法）。
 *
 *  `ctx.hasVideo`（2026-10-23 新增）：数据包里**已勾选视频**时，`video` 位**转为必填**。
 *
 *  ⚠️ 为什么不能把 `PUBLISH_SLOT_REQUIRED.video` 直接改成 `true`：
 *    用户口径 2026-10-13「**商品视频可留空**」仍然成立 —— 数据包里**没有**视频时，
 *    不该逼用户去选一个不存在的区域（那是凭空制造一个进不去的关卡）。
 *
 *  ⚠️ 为什么必须加这条：用户报障 2026-10-23「**勾选之后，使用自动上传但是还是提示
 *    数据包中没有视频**」—— 勾了视频、但区域映射里「商品视频」那栏留空
 *    ⇒ `zoneMap.video = []` ⇒ `videoPaths = []` ⇒ 自动加载跳过视频 + 文案误导。
 *    ⇒ **有视频就必须指定区域**，在弹窗点确认那一刻就拦住，而不是等自动加载时才发现。
 */
export function validateZoneMap(
  map: ZoneMap,
  ctx?: { hasVideo?: boolean },
): string[] {
  const errors: string[] = [];
  for (const slot of ["main", "detail", "video"] as PublishSlotKey[]) {
    //: 视频位：`hasVideo` 为真时转为必填（否则沿用 `PUBLISH_SLOT_REQUIRED`）
    const required =
      slot === "video" && ctx?.hasVideo ? true : PUBLISH_SLOT_REQUIRED[slot];
    if (required && (map[slot]?.length ?? 0) === 0) {
      errors.push(
        slot === "video" && ctx?.hasVideo
          ? "数据包内有已勾选的视频，请选择「商品视频」区域（或取消勾选该视频）"
          : `请选择${PUBLISH_SLOT_LABELS[slot]}`,
      );
    }
  }
  return errors;
}

/** **未被任何发布位使用**的区域名（用户口径：漏配要能看出来）。 */
export function unusedZones(
  zones: readonly { name: PrecheckZoneName }[],
  map: ZoneMap,
): PrecheckZoneName[] {
  const used = new Set<PrecheckZoneName>([
    ...(map.main ?? []),
    ...(map.detail ?? []),
    ...(map.video ?? []),
  ]);
  return zones.filter((z) => !used.has(z.name)).map((z) => z.name);
}

/* ------------------------------------------------------------------ *
 * SKU 图匹配（用户口径 2026-10-13）
 *
 * > 「指定 sku 名称列与图片名称匹配的规则，精确匹配，模糊，或使用正则表达式」
 * ------------------------------------------------------------------ */

/** 匹配方式（**两选一**）。
 *
 *  ⚠️ 用户口径 2026-10-13：「匹配规则调整为**精确和模糊**，图片名称不支持再使用
 *  正则处理后再匹配，**只能使用原始图片名称**」。
 *  ⇒ 原先的三值（`exact` / `fuzzy` / `regex`）去掉 `regex`。
 *
 *  ⚠️ 与「名称列取值正则」是**两件独立的事**：正则**只允许作用在 SKU 列**上
 *  （先取值、再比），**不允许**再对图片文件名做整形 —— 图名一律用原名比对。
 */
/** **两侧都由正则取值，再精确相等**（用户口径 2026-10-14，方案 B）。
 *
 * > 「sku 列和图片名称均通过**反推**获取到正确的 sku 名称，再进行**精确匹配**，
 * >   将正确的正则处理过的 sku 列名称和图片名称进行匹配，
 * >   也就是两者都取到**完整的 sku 列名**最后匹配」
 *
 * ## 为什么删掉了「精确 / 模糊」两档
 *
 * 原先有一档「模糊」（`looseMatchKey` 折叠分隔符/括号），还有一批
 * **代码里猜数据形态**的规则：剥 `SKU01_` / `SKU图_01_` 前缀、
 * 剥「列名(值)」外壳、剥「列名：值」外壳……
 *
 * 用户判断（2026-10-14，原话）：
 * > 「反推正则应该是**通用工具**，不可能每次出现新的符号均再次优化代码」
 *
 * 这是对的，而且那些猜测**已经开始互相打架**（实测）：图名 `SKU01_甲：乙.jpg`
 * 里的**值的冒号**被「剥列名外壳」规则当成了**列名的冒号** ⇒ 值被截成 `乙`，
 * 永远配不上。每来一个新的命名形态就得再补一条规则，且失效是**静默**的
 * （表现成「图配不上」，看不出是代码猜错了）。
 *
 * ⇒ **通用做法**：整形规则交给用户（反推工具按样例生成正则），
 * 代码只负责「跑正则 + 精确比对」，**不再有任何猜数据形态的分支**。
 * 换任何数据包都只需要用户重新反推一次正则，**不动代码**。
 *
 * ## 判据
 *
 * ```
 * 左 = applyExtract(SKU 列原值, 列取值正则)
 * 右 = applyExtract(图片文件名, 图名取值正则)
 * 命中 ⟺ 左 === 右（各自 strip 首尾空白后）
 * ```
 *
 * ⚠️ **比较只做 strip**，不做大小写/全角折叠 —— 用户口径是「完整的 sku 列名」，
 * 差一个字符就是没达到目的；要容忍书写差异由**用户的正则**表达
 * （正则本来就能写 `[（(]` 这类）。
 */

/** 图片文件名的**匹配载体**：只取 basename 并去掉扩展名。
 *
 *  用户口径 2026-10-14：「图片名称**不是固定样式**的，但**一定包含** sku 名」
 *  ⇒ 图片名侧**不需要任何正则**：不再猜前缀/外壳/分隔符，
 *  只做「文件名里有没有这个 SKU 名称」的包含判断。
 *
 *  ⚠️ 去扩展名：`xxx.jpg` 与 SKU 值比较时 `.jpg` 是噪声
 *  （用户说「名一定包含 sku 名」，扩展名不属于「名」）。
 */
export function imageBaseName(fileName: string): string {
  const base = String(fileName ?? "").split(/[\\/]/).pop() ?? "";
  return base.replace(/\.[A-Za-z0-9]+$/, "");
}

/** 单个规格名 ↔ 单个图片文件名 是否算命中（**包含**判据）。
 *
 *  左侧 = SKU 列值**经用户正则取值**后（值的边界只有用户知道）；
 *  右侧 = 图片文件名（去扩展名）。
 *
 *  ⚠️ **为什么不是相等**：图片名样式不固定（`SKU01_颜色分类(值).jpg` /
 *  `实木三斗柜_01.jpg` / `SKU-实木三斗柜.png` 都可能），
 *  唯一稳定的共同点是「**一定包含 SKU 名**」。相等判据要求图片名恰好只有值，
 *  换个包就全废。
 *
 *  ⚠️ 包含判据的**副作用**：短规格名会是长规格名的子串
 *  （`实木三斗柜` ⊂ `实木六斗柜`？否；但 `伸缩桌+三斗柜梳妆台`
 *  ⊂ `伸缩桌+三斗柜梳妆台+云朵妆镜` 是）⇒ 单看一行会串台，
 *  消歧在 `matchSkuRows` 里做（**全局最长优先 + 一图一用**）。
 */
export function matchesSkuName(
  skuColumnValue: string,
  imageFileName: string,
  skuPattern: string,
): boolean {
  const left = applyExtract(String(skuColumnValue ?? ""), skuPattern).trim();
  if (!left) return false;
  return imageBaseName(imageFileName).includes(left);
}

/** 逐行匹配（**唯一实现**：弹窗预览与上抛都用它）。
 *
 *  ## 消歧：全局最长优先 + **一图可复用**
 *
 *  包含判据下，短规格名会命中长规格名的图
 *  （值 `伸缩桌+三斗柜梳妆台` ⊂ 图名 `…伸缩桌+三斗柜梳妆台+云朵妆镜.jpg`）。
 *
 *  1. **最长优先**：把所有 (行, 图) 命中对按**值长度降序**分配 ——
 *     长值先挑，短值不会把长值该拿的图抢走；
 *  2. ⚠️ **同一张图可以被多行匹配**（用户口径 2026-10-21：见下）。
 *
 *  ## ⚠️ 2026-10-21 推翻「一图一用」（用户口径）
 *
 *  > 「SKU 图自动匹配中 SKU 图**支持重复匹配**」
 *
 *  原实现「一图一用」在真实包上暴露成**大面积未匹配**：24 张 SKU 图对 456 行，
 *  同名规格（`奇遇童话 01` 出现多次）只有第一行能拿到图，其余全部「未匹配」。
 *  而业务上「一张图对应多个同规格的 SKU」是完全正常的。
 *
 *  ⇒ 现在**只做最长优先、不做占用去重**：每一行各自挑「名字包含自己 SKU 名的
 *  最长那个候选」。最长优先仍在，只是不再有「被谁占了」的概念 ——
 *  否则取消复用后，短值行会抢走长值行该有的图。
 *
 *  ⚠️ `usedCandidates` 参数**保留但语义收窄**：它仍表示「这些图不参与」
 *  （调用方给手选让位），只是不再随着分配过程增长 —— 它**不是**占用集合。
 */
export function matchSkuRows(
  rows: readonly { skuName: string }[],
  candidates: readonly { fileName: string; relPath: string }[],
  skuPattern: string,
  usedCandidates: ReadonlySet<string> = new Set(),
): { relPath: string | null; fileName: string }[] {
  const keys = rows.map((r) => applyExtract(String(r.skuName ?? ""), skuPattern).trim());
  return keys.map((key) => pickSkuImageFor(key, candidates, usedCandidates));
}

/** **给一个 SKU 名挑一张图**（匹配的**唯一判据实现**）。
 *
 *  判据 = 候选里「文件名（去目录去扩展名）**包含**该 SKU 名」的那几张里，
 *  取**文件名最短**的（最贴合该规格；长名字的图往往是多规格合在一起的图）。
 *
 *  ⚠️ 同一张图允许被**多个 SKU 名复用**（用户口径 2026-10-21：
 *  「SKU 图自动匹配中 SKU 图**支持重复匹配**」）—— 参数 `usedCandidates`
 *  只表示「这些图不参与」（调用方给手选让位），**不是**占用集合。
 *
 *  ⚠️ 抽成独立函数是为了**能 await 一帧**：调用方（弹窗的「执行匹配」）
 *  需要先让等待蒙版落屏再跑计算，所以按行逐个 `await` 一帧即可让出主线程，
 *  而不是把整段 456 行的计算塞进一次同步调用（那时蒙版永远画不出来）。
 */
export function pickSkuImageFor(
  /** **已提取好的** SKU 名（调用方先过 `applyExtract`；这里不再二次提取，
   *  否则调用方传进来的正则会被丢掉，长值/短值的消歧判据全失效）。 */
  key: string,
  candidates: readonly { fileName: string; relPath: string }[],
  usedCandidates: ReadonlySet<string> = new Set(),
): { relPath: string | null; fileName: string } {
  const normalized = String(key ?? "").trim();
  if (!normalized) return { relPath: null, fileName: "" };
  let best = -1;
  let bestLen = Number.POSITIVE_INFINITY;
  candidates.forEach((cand, ci) => {
    if (!cand.relPath || usedCandidates.has(cand.relPath)) return;
    if (!imageBaseName(cand.fileName).includes(normalized)) return;
    const len = cand.fileName.length;
    if (len < bestLen) {
      best = ci;
      bestLen = len;
    }
  });
  if (best < 0) return { relPath: null, fileName: "" };
  return { relPath: candidates[best].relPath, fileName: candidates[best].fileName };
}

/** 编译匹配正则（不抛错）。 */
export function compileMatchPattern(pattern: string): { re: RegExp | null; error: string } {
  const text = String(pattern ?? "").trim();
  if (!text) return { re: null, error: "正则不能为空" };
  try {
    return { re: new RegExp(text), error: "" };
  } catch (error) {
    return { re: null, error: `正则语法错误：${error instanceof Error ? error.message : String(error)}` };
  }
}

/** 按**取值正则**对 SKU 列的值整形（用户口径 2026-10-13）。
 *
 * 「支持对选定的 sku 列使用正则提取后再与...图片名称进行精确或模糊的匹配」
 *
 *  * 正则**留空** ⇒ 原样返回（不提取，用整列原值）；
 *  * 有捕获组 ⇒ 取第 1 组；无组 ⇒ 取整串（与 `applyExtract` 同口径）；
 *  * 正则语法错 / 没匹配上 ⇒ **回落原值**（不返回空串）。
 *
 *  ⚠️ 最后一条是刻意的：返回空串会让该行必然判未命中，而用户看到的是
 *  「这些图就是配不上」—— 真正的原因（他的正则没命中任何一行）被藏起来了。
 *  回落原值至少让**没写错的那部分**正常工作，且未命中率一高用户会先怀疑正则。
 */
export function applyExtract(value: string, pattern: string): string {
  const raw = String(value ?? "").trim();
  const text = String(pattern ?? "").trim();
  if (!text) return raw;
  const { re } = compileMatchPattern(text);
  if (!re) return raw;
  const m = raw.match(re);
  if (!m) return raw;
  const picked = String(m.length > 1 ? m[1] : m[0]).trim();
  return picked || raw;
}

/** **一个单元格的值是不是「图片」**（用户口径 2026-10-14）。
 *
 * > 「新增排队商品 sku 表格 tab **应读到什么就展示什么**，如果为图片则
 * >   展示小图预览方式，**不应该还有线上图和本地图非表格中原始的列**」
 *
 * 判据落在**读到的东西**上，不是列名猜测 —— 同一个「图片」列在不同数据包里
 * 可能是 `图片` / `主图` / `商品图`，按列名枚举永远漏；而值本身是确定的：
 *
 *  · 后缀命中 `IMAGE_EXTS`（与后端 `shop_draft_package.IMAGE_EXTS` 同值：
 *    `jpg` / `jpeg` / `png` —— 平台真机取证的收件格式）；
 *  · 或者是一个 http(s) 图片 URL（真包现状是 1688 的 `gw.alicdn.com` 链接，
 *    路径里带后缀，但也可能是 `?x-oss-process=…` 这类无后缀的 CDN 链接
 *    ⇒ URL 分支**不要求**后缀，只看 host/路径里有没有图片特征）。
 *
 * ⚠️ **不判定本地路径是否存在** —— 那是预检的事。这里只回答「这串值该不该
 * 画成图」，为真但取不到图时由 `PrecheckThumb` 自己显示失败态。
 */
export function isImageValue(value: unknown): boolean {
  const text = String(value ?? "").trim();
  if (!text) return false;
  //: 纯本地文件名（`SKU图_01_中古色-实木三斗柜.jpg`）。
  if (/\.(jpe?g|png|gif|webp|bmp)$/i.test(text)) return true;
  //: http(s) URL：路径带图片后缀，或 CDN 常见的图片处理参数。
  if (/^https?:\/\//i.test(text)) {
    if (/\.(jpe?g|png|gif|webp|bmp)(\?|$)/i.test(text)) return true;
    if (/[?&](x-oss-process|imageView2|imageMogr2)/i.test(text)) return true;
  }
  return false;
}
