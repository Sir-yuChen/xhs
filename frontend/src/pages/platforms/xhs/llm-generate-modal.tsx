/**
 * LLM 生成配置弹窗（T9 / 设计 17 §7.4、§13.4.5；**v2.27 交互重做**，设计 41 §16）。
 *
 * ## 为什么重做（用户口径：*「目前弹窗中使用极其不便、不容易理解如何使用」*）
 *
 * 旧版六个具体问题（读码实测，不是感觉）：
 *
 * | # | 问题 | 旧实现 |
 * |---|---|---|
 * | 1 | 媒体是 **6 个竖排单选、不能组合** | `Radio.Group` 6 项 —— 后端 `media_plan` 已支持组合，前端没接 |
 * | 2 | 选项名是**后端术语** | 「参考图生成」「AI 生成」—— 用户不知道选完会发生什么 |
 * | 3 | **盲选**：选了才看到参数 | `{mediaSource === "material" && …}` 参数在别处冒出来 |
 * | 4 | **主次不分** | 模板 / 模型名 / 媒体三者平铺，看不出文案模板才是主体 |
 * | 5 | **「模型名」裸输入框常驻** | `placeholder="留空即用模型配置页…"` —— 这是给开发者的 |
 * | 6 | **没有笔记类型概念** | 平台只有「图文 / 视频」两大类且**互斥**，旧版把它们混在一个列表里 |
 *
 * ## 新形态（对应上面 6 条）
 *
 * 四个**卡片区域**（用户口径 2026-10-16：*「使用卡片区域给这些区域进行分割」*、
 * *「只留标题，不要序号」*）—— 从上到下就是填写顺序：
 *
 * ```
 * ┌ 笔记类型 ─────────────────────────────────┐
 * │ ● 图文笔记（1–18 张图）  ○ 视频笔记（1 视频 + 1 封面）│
 * └──────────────────────────────────────────┘
 * ┌ 文案 ─────────────────────────────────────┐
 * │ [提示词模板 ▾]   ← 主体                    │
 * └──────────────────────────────────────────┘
 * ┌ 配图 / 视频与封面 ─────────────────────────┐
 * │ 图文：封面一行 + 内页（可叠加多行）= 笔记图序    │
 * │ 视频：**两列并排**（左 视频 | 右 封面）        │
 * └──────────────────────────────────────────┘
 * ```
 *
 * ⚠️ **③ 区按形态两套布局**，不是一套：视频笔记的「视频」与「封面」是**平级的两件东西**
 * （封面是独立上传的图，不是视频帧），串成竖排会被误读成从属关系。
 * 两套布局**共用 `SegmentCard`**（怎么摆不同、摆什么相同 —— 各写一遍必然漂移，DEF-044）。
 *
 * ⚠️ 四条纪律（沿用）：
 *   - **门禁在后端**：模板不存在 / 服务商没配 / 媒体参数不匹配 —— 都在同步段 400，
 *     这里只做「必填项在不在」的轻校验，不复制后端的业务判据（两份真相源必分叉）；
 *   - **模型完全不在这里选**（用户口径 2026-10-16：*「不允许随意设置模型名」*）：
 *     走业务点映射，弹窗**没有**模型名字段，要换模型去「模型配置」页；
 *   - **提交是入队回执**：不给「生成中…」的假进度，进度看队列卡（`LlmGenerateProgressCard`）；
 *   - **形态是单值**（平台互斥）：图文 / 视频二选一，视频笔记只留 1 个视频来源 + 封面。
 */
import {
  App,
  Button,
  Card,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckOutlined,
  DeleteOutlined,
  FileImageOutlined,
  PlayCircleOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { hasPermission } from "../../../hooks/use-permission";

import {
  fetchMaterialFiles,
  fetchMaterialFolders,
  fetchMaterialImageObjectUrl,
  fetchSystemSettings,
  fetchXhsShopItemDetail,
  fetchPromptTemplates,
  generateLlmNote,
  materialFileUrl,
  regenNoteVersion,
  updateNoteVersion,
  publishNoteVersion,
  detailMessage,
  type LlmGenerateResult,
} from "../../../lib/api";
import { WaitMask } from "../../../components/ui/wait-mask";
import { NoteVersionHistory } from "./note-version-history";
import type {
  LlmCopyRequirements,
  LlmMediaPlanSegment,
  LlmMediaSource,
  MaterialFile,
  MaterialFolder,
  PromptTemplate,
} from "../../../types";

const { Text } = Typography;

/** 笔记形态（与后端 `NOTE_FORM_*` 同名；平台只有这两类且互斥）。 */
type NoteForm = "image" | "video";

/**
 * 媒体来源选项 —— **名字说人话 + 一行后果说明**（旧版只有术语，这是问题 2 的修法）。
 * `form` 决定它出现在哪个类型下（视频来源只在视频笔记里出现）。
 */
const MEDIA_SOURCE_OPTIONS: {
  value: LlmMediaSource;
  label: string;
  hint: string;
  form: NoteForm;
}[] = [
  {
    value: "product_image",
    label: "商品原图",
    hint: "直接用台账里的商品图，不额外生成（最快、零成本）",
    form: "image",
  },
  {
    value: "material",
    label: "本地素材",
    hint: "从素材库里勾选图片",
    form: "image",
  },
  {
    value: "ai_generate",
    label: "智能生成",
    //: ⚠️ 用户口径（2026-10-16）：*「智能生成时需要参考AI商品图相关逻辑支持选择参考图；
    //: 这个下拉参考图正常是不需要的，智能生成时在其内支持选择参考图」*
    //: —— 「参考图生成」**不再是**来源下拉里的一项（它本就是「智能生成 + 参考图」），
    //: 改为在智能生成的参数区**勾选用哪张参考图**（见 `ReferenceImagePicker`）。
    hint: "按商品信息自动出图（可填提示词、选张数，可选参考图）",
    form: "image",
  },
  {
    value: "video_material",
    label: "本地素材",
    //: ⚠️ 用户口径 2026-10-16：*「使用本地素材这无需再生成视频，后端根据商品，以及提示词，
    //: 本地素材参考生成相关的文案标题话题等」* —— 这条**不生成视频**（零成本、秒级），
    //: 只把素材里的视频当媒体，文案照走 LLM。
    hint: "用素材库里的视频，不生成（文案照旧按商品写）",
    form: "video",
  },
  {
    value: "video_ai_generate",
    label: "智能生成视频",
    //: ⚠️ 用户口径 2026-10-16：*「智能生成视频需支持选择参考图，参考图可为本地素材也可以是
    //: 商品原图，最多一共4张」* —— 参考图在段内选（与图片的「智能生成」同一套交互）。
    hint: "按商品信息文生视频（可填提示词、选参考图，异步几分钟）",
    form: "video",
  },
];

/** 每个来源的参数是否要展开（问题 3：参数**就地**展开，不再在别处冒出来）。 */
//: ⚠️ `video_reference` 已退场（用户口径：*「视频下拉应只有本地素材和智能生成视频」*）
//: —— 它与 `video_ai_generate` 带参考图是同一件事（两入口一实现 ⇒ 概念冗余）。
const SOURCES_WITH_PARAMS: LlmMediaSource[] = [
  "material",
  "ai_generate",
  "video_material",
  "video_ai_generate",
];

/** 参考图上限（用户口径 2026-10-16：*「支持多选最多4个」*）。 */
const MAX_REFERENCE_IMAGES = 4;

/**
 * 缩略图**CSS 边长**（用户口径 2026-10-17：*「小图预览都很模糊，正常应清晰」*）。
 *
 * ⚠️ **根因是格子太小，不是图不清晰**（实测坐实）：
 * 商品图原图 800×800，塞进 **56×56** 的槽位 = 把 800px 的图当 56px 用，
 * 图上的文字/细节**物理上没有足够的像素**去表达 ⇒ 必然糊。
 * 换 CDN 缩略图（`imageView2`）只省流量、**不提升清晰度**（源图信息量没变）。
 * ⇒ 真解：**把格子放大**（56 → 88）+ 按 DPR 取图（88×2=176 物理像素）。
 * ⚠️ 不要无限放大：这一行是「横向滚动带」，太高会挤掉下面的提示词输入区。
 */
const THUMB_CSS_SIZE = 100;

/** 平台硬顶（与后端 `copy_requirements.TITLE_MAX/BODY_MAX` **同值**）：
 *  标题 20 字 / 正文 1000 字。前端只是输入上限，最终仍由后端夹取。 */
const TITLE_MAX_CHARS = 20;
const BODY_MAX_CHARS = 1000;
/** 标题可填区间（与后端 `TITLE_RANGE` 同值）：低于 1 或高于硬顶都由后端夹取。 */
const TITLE_RANGE: [number, number] = [1, TITLE_MAX_CHARS];

/** 文案要求的默认值（与后端 `CopyRequirements` 默认值**逐项一致**）。
 *  ⚠️ 两处默认值不一致 = 「界面显示 8-20 字、后端按别的数字执行」，用户永远调不准。
 *  改这里必须同时改 `backend/app/services/copy_requirements.py`。 */
const DEFAULT_COPY: Required<LlmCopyRequirements> = {
  title_prompt: "",
  body_prompt: "",
  topic_prompt: "",
  title_min: 8,
  title_max: TITLE_MAX_CHARS,
  body_min: 30,
  body_max: 300,
  //: 话题默认 6–10（用户口径 2026-10-18：*「话题默认改为6-10」*）；
  //: 与后端 `CopyRequirements.topic_min/topic_max` 保持同值（两处不同步会被守卫抓）。
  topic_min: 6,
  topic_max: 10,
  emoji: "auto",
};

/**
 * 文件夹的**显示名**（末级目录名）。
 *
 * ⚠️ 用户口径 2026-10-17 截图实证：下拉里显示 `D:\work\XHS\SCK (...)` ——
 * 因为**库里的 `name` 字段本身就存着完整路径**（扫描文件夹时用路径当名称）。
 * ⇒ 显示前取末级段；路径本身仍进 `Tooltip`（要定位时还得看全路径）。
 */
function folderDisplayName(name: string): string {
  const trimmed = String(name || "").replace(/[\/]+$/, "");
  const parts = trimmed.split(/[\/]/);
  return parts[parts.length - 1] || trimmed;
}

/**
 * 缩略图地址（**渲染用**，不用于提交）。
 *
 * ⚠️ **必须走 CDN 缩略图**（2026-10-17 实测）：
 *  - 商品详情图原图是 **2480×2824 / 2480×3484**，而缩略图槽位只有 **56×56 CSS**；
 *    浏览器把 2480px 硬缩到 56px = 44 倍下采样 ⇒ 边缘糊、摩尔纹（用户口径
 *    *「小图预览都很模糊，正常应清晰」*）。
 *  - 小红书 CDN（七牛）支持 `?imageView2/2/w/112` ⇒ 直接返回 112×128、**4KB**
 *    （原图 400KB）。实测边缘能量 73.6 vs 74.3（几乎无差），但**体积降到 1/100**。
 * ⚠️ 只改**显示**地址；提交给后端的仍是原图 URL（`reference_urls` / `pick`）——
 * 用缩略图当参考图会让模型拿到模糊输入（那是另一个 bug）。
 */
function thumbUrl(url: string, cssSize = THUMB_CSS_SIZE, scale = 0): string {
  //: 只对已知的七牛/小红书图片域加参数（别的域加了可能 404）。
  if (!/xiaohongshu\.com|qimg\./.test(url)) return url;
  //: 已有查询串就不再叠（避免 `?imageView2...?imageView2...`）。
  if (url.includes("?")) return url;
  //: `scale = 0` ⇒ 按当前设备 DPR 取（用于 `src` 兜底）；
  //: `scale > 0` ⇒ 取该倍数（用于 `srcset` 的 1x/2x/3x 档）。
  const dpr =
    scale > 0
      ? scale
      : typeof window !== "undefined" && window.devicePixelRatio
        ? Math.min(Math.ceil(window.devicePixelRatio), 3)
        : 2;
  //: `imageView2/1/w/h` = **居中裁剪成正方形**（与格子形状一致）。
  //: 用 `2`（等比不裁剪）会让 2480×3484 的竖长图进方格子后再被 CSS `cover` 第二次裁切
  //: —— **两次重采样**白白损失锐度（标准做法：让 CDN 一次裁到位）。
  const side = Math.round(cssSize * dpr);
  return `${url}?imageView2/1/w/${side}/h/${side}`;
}

/**
 * 缩略图的 `srcset`（**市场标准做法**：web.dev / MDN「responsive images」）。
 *
 * ⚠️ 为什么要 `srcset` 而不是单个 `src`：
 *  同一台机器可能 1.0/1.25/1.5/2/3 倍 DPR（Windows 缩放 125% 很常见），
 *  写死一个尺寸总有某档是「不够」或「过大」：
 *  - 不够 ⇒ 浏览器把小图**放大**画 ⇒ 糊（用户看到的正是这个）；
 *  - 过大 ⇒ 浏览器把大图**缩小**画 ⇒ 理论也损失锐度、且白流量。
 *  ⇒ 给 1x/2x/3x 三档，浏览器自己挑最合适的（`x` 描述符 = 像素密度描述）。
 *
 * ⚠️ 非 CDN 图（素材的 object URL）不能加参数，返回 null ⇒ 调用方只给 `src`。
 */
function thumbSrcSet(url: string, cssSize = THUMB_CSS_SIZE): string | undefined {
  if (!/xiaohongshu\.com|qimg\./.test(url) || url.includes("?")) return undefined;
  return [1, 2, 3]
    .map((k) => `${thumbUrl(url, cssSize, k)} ${k}x`)
    .join(", ");
}

/**
 * **大图预览**（用户口径 2026-10-17：*「还缺少一个大图预览」*）。
 *
 * ⚠️ 为什么不用 antd 的 `<Image preview>`：它把「点击 = 预览」写死，
 * 而这里的点击语义是**选中**（选参考图/选图）—— 两者抢同一个手势，
 * 只能二选一。⇒ 缩略图格**点击选中**，右下角单独一个**放大角标**点击预览。
 * ⚠️ 预览图用**原图**（不是 CDN 缩略图）：看大图就是要看清细节，
 * 给缩略图等于没放大（`thumbUrl` 只服务 56~110px 的小格子）。
 */
function ImagePreviewLayer({
  preview,
  alt,
  onClose,
}: {
  /** 要预览的图片地址（空串 = 关闭）。**用原图**，不用缩略图。 */
  preview: string;
  alt: string;
  onClose: () => void;
}) {
  return (
    <Modal
      open={!!preview}
      onCancel={onClose}
      footer={null}
      width="auto"
      centered
      styles={{ body: { padding: 0 } }}
    >
      <img
        src={preview}
        alt={alt}
        style={{ display: "block", maxWidth: "82vw", maxHeight: "76vh", objectFit: "contain", borderRadius: 8 }}
      />
    </Modal>
  );
}

/**
 * **保序去重**（用户口径 2026-10-17：*「商品原图实现图片去重」*）。
 *
 * ⚠️ **必须与后端 `_product_image_urls` 同一套判据**（那边也是保序去重）：
 * 后端按 `pick` **下标**解析，前端列表若比后端多一张，就是「选第 3 张、实际出第 5 张」
 * （静默错位，最难查的那种）。真机实测：抽查 400 个商品 **400 个**都有重复图
 * （主图 10 张里有 5 张原样重复出现在详情图组，20 → 15）。
 */
/**
 * **本次生成结果**（用户口径 2026-10-18：同步等待 + 生成后可就地看）。
 *
 * 为什么单独一页而不是弹个 toast：生成要几十秒，用户等完**必须**能看到
 * 「标题写了什么 / 正文多长 / 话题几个 / 图是哪几张」—— 只报「生成完成」
 * 等于让用户自己去翻生成记录，那正是这次要消灭的往返。
 *
 * 配图**逐张列出**（不是只给个张数）：下一步用户要在这里判断
 * 「这张留着、那张换掉」，看不到图无从判断。
 */
/**
 * **本版已用媒体**的横向回显条（配图卡顶部）。
 *
 * 与下面的「段选择器」分工：
 *   · 段选择器 = **候选**（配下一次生成用什么来源）
 *   · 这条 = **成品**（这一版实际落了哪些图 / 视频、哪张是封面、哪张失败）
 *
 * ⚠️ 素材图是 `/api/...`（要鉴权）⇒ 逐张走 object URL；失败段显示红框占位 + 原因。
 */
/**
 * **智能生成段的「结果图」条**（用户口径 2026-10-19：*「如果生成失败则展示一个失败占位图，
 * tip 显示失败原因」*）。
 *
 * 为什么单独一个组件：这一段左侧是「参考图」，右侧要和它**等宽并排**
 * （`flex: 1 1 50%`），所以这里只负责渲染「这次出了什么」，
 * 不掺参考图候选那一套（否则选中态会串到别人的候选里）。
 *
 * 三种形态：
 *   · 有产出 ⇒ 缩略图横排（可滚）
 *   · 失败   ⇒ **红框虚线占位图**，图内「生成失败」+ 原因前 12 字，`Tooltip` 给全文
 *   · 没跑过 ⇒ 灰底虚线「生成后显示」
 */
function AiResultStrip({
  media,
  failure,
  onPreview,
  picked = [],
  onToggle,
}: {
  media: { url: string; local_path?: string; ok?: boolean; error?: string; source?: string }[];
  failure: string;
  onPreview?: (url: string) => void;
  /** **已选中进这篇笔记的图**（名称 = URL 末段，与商品图同一套判据）。 */
  picked?: string[];
  /** 点一下切换选中 —— 用户口径 2026-10-19：*「生成后才可以供用户选择，
   *  应是生成后选择的图片算该片笔记的图片总数」*。 */
  onToggle?: (name: string) => void;
}) {
  const [resolved, setResolved] = useState<Record<string, string>>({});
  const failed = media.filter((m) => m.url || m.local_path);

  useEffect(() => {
    let alive = true;
    const created: string[] = [];
    void (async () => {
      const map: Record<string, string> = {};
      for (const entry of failed) {
        const raw = entry.url || entry.local_path || "";
        if (!raw) continue;
        if (!raw.startsWith("/api/")) {
          map[raw] = raw;
          continue;
        }
        try {
          const objectUrl = await fetchMaterialImageObjectUrl(raw);
          if (!alive) {
            window.URL.revokeObjectURL(objectUrl);
            continue;
          }
          created.push(objectUrl);
          map[raw] = objectUrl;
        } catch {
          /* 取不到就显示原地址，不假装有图 */
        }
      }
      if (alive) setResolved(map);
    })();
    return () => {
      alive = false;
      created.forEach((url) => window.URL.revokeObjectURL(url));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failed.map((m) => m.url || m.local_path).join("|")]);

  if (failure && failed.length === 0) {
    //: ⚠️ 失败占位：**图内写「生成失败」+ 原因前 12 字**，`Tooltip` 给全文 ——
    //: 只写「失败」用户不知道怎么办；只给全文又撑破格子（用户口径：*「我看不到
    //: 生成的结果异常信息」*，所以要**看得见**，不是藏在日志里）。
    return (
      <Tooltip title={failure} placement="top">
        <div
          style={{
            width: 96,
            height: 96,
            border: "1px dashed #ff4d4f",
            borderRadius: 6,
            background: "#fff1f0",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 2,
            padding: 4,
            cursor: "help",
          }}
        >
          <Text style={{ color: "#cf1322", fontSize: 11, fontWeight: 600 }}>生成失败</Text>
          <Text style={{ color: "#a8071a", fontSize: 10, textAlign: "center", lineHeight: 1.2 }}>
            {failure.length > 12 ? `${failure.slice(0, 12)}…` : failure}
          </Text>
        </div>
      </Tooltip>
    );
  }

  if (failed.length === 0) {
    //: 还没生成过：灰底虚线（**不写「暂无」**，写明什么时候会有）
    return (
      <div
        style={{
          width: 96,
          height: 96,
          border: "1px dashed #d9d9d9",
          borderRadius: 6,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Text type="secondary" style={{ fontSize: 11 }}>
          生成后显示
        </Text>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "nowrap" }}>
      {failed.map((entry, i) => {
        const raw = entry.url || entry.local_path || "";
        const src = resolved[raw] || raw;
        //: 选中判据 = **图片名称**（URL 末段），与商品图 pick 同一口径，不按下标
        const selected = picked.includes(aiImageName(raw));
        return (
          <div
            key={`${raw}-${i}`}
            style={{
              position: "relative",
              width: 96,
              height: 96,
              flex: "0 0 auto",
              borderRadius: 6,
              overflow: "hidden",
              border: "1px solid #f0f0f0",
              cursor: onPreview ? "zoom-in" : "default",
            }}
            onClick={() => onPreview?.(raw)}
          >
            <img
              src={src}
              alt={`AI 结果 ${i + 1}`}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
            {/*: ⚠️ **选中态**（用户口径 2026-10-19：生成后由用户挑，选中才算进笔记）——
                用勾选角标而不是描边：描边在深色背景图上几乎看不见。 */}
            <div
              onClick={(event) => {
                event.stopPropagation();
                onToggle?.(aiImageName(raw));
              }}
              style={{
                position: "absolute",
                top: 2,
                right: 2,
                width: 18,
                height: 18,
                borderRadius: 4,
                background: selected ? "#1677ff" : "rgba(0,0,0,0.35)",
                border: "1px solid #fff",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
              }}
            >
              {selected ? (
                <CheckOutlined style={{ fontSize: 11, color: "#fff" }} />
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ResultMediaStrip({
  media,
  onPreview,
}: {
  media: { url: string; local_path?: string; sort_order?: number; asset_type?: string; ok?: boolean; error?: string; role?: string }[];
  onPreview?: (url: string) => void;
}) {
  const [resolved, setResolved] = useState<Record<string, string>>({});

  useEffect(() => {
    let alive = true;
    const created: string[] = [];
    void (async () => {
      const map: Record<string, string> = {};
      for (const entry of media) {
        const raw = entry.url || entry.local_path || "";
        if (!raw) continue;
        if (!raw.startsWith("/api/")) {
          map[raw] = raw;
          continue;
        }
        try {
          const objectUrl = await fetchMaterialImageObjectUrl(raw);
          if (!alive) {
            window.URL.revokeObjectURL(objectUrl);
            continue;
          }
          created.push(objectUrl);
          map[raw] = objectUrl;
        } catch {
          map[raw] = "";
        }
      }
      if (alive) setResolved(map);
    })();
    return () => {
      alive = false;
      created.forEach((url) => window.URL.revokeObjectURL(url));
    };
  }, [media]);

  return (
    <Space size={8} wrap>
      {media.map((entry, index) => {
        const raw = entry.url || entry.local_path || "";
        const src = resolved[raw] || "";
        const isVideo = (entry.asset_type || "image") === "video";
        const failed = entry.ok === false;
        return (
          <div key={`${raw}-${index}`} style={{ textAlign: "center" }}>
            {failed ? (
              <div
                title={entry.error || "生成失败"}
                style={{
                  width: THUMB_CSS_SIZE,
                  height: THUMB_CSS_SIZE,
                  borderRadius: 6,
                  border: "1px dashed #ff4d4f",
                  background: "rgba(255,77,79,0.06)",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 4,
                  padding: 4,
                  boxSizing: "border-box",
                  overflow: "hidden",
                }}
              >
                <Text style={{ color: "#ff4d4f", fontSize: 11 }}>生成失败</Text>
                <Text
                  style={{
                    color: "#ff4d4f",
                    fontSize: 10,
                    maxWidth: "100%",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {(entry.error || "未知原因").slice(0, 12)}
                </Text>
              </div>
            ) : isVideo ? (
              <div
                style={{
                  width: THUMB_CSS_SIZE,
                  height: THUMB_CSS_SIZE,
                  borderRadius: 6,
                  border: "1px solid var(--ant-color-border, #d9d9d9)",
                  background: "var(--ant-color-fill-quaternary, #fafafa)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Text style={{ fontSize: 12 }}>视频</Text>
              </div>
            ) : src ? (
              <img
                src={thumbUrl(src)}
                alt={`第 ${index + 1} 张`}
                title={raw}
                onClick={() => onPreview?.(src)}
                style={{
                  width: THUMB_CSS_SIZE,
                  height: THUMB_CSS_SIZE,
                  objectFit: "cover",
                  borderRadius: 6,
                  border: "1px solid var(--ant-color-border, #d9d9d9)",
                  display: "block",
                  cursor: onPreview ? "zoom-in" : "default",
                }}
              />
            ) : (
              <div
                style={{
                  width: THUMB_CSS_SIZE,
                  height: THUMB_CSS_SIZE,
                  borderRadius: 6,
                  border: "1px dashed var(--ant-color-border, #d9d9d9)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Text type="secondary" style={{ fontSize: 11 }}>加载中</Text>
              </div>
            )}
            <Text type="secondary" style={{ fontSize: 11 }}>
              {index === 0 ? "封面" : `第 ${index + 1} 张`}
            </Text>
          </div>
        );
      })}
    </Space>
  );
}

/**
 * **生成结果回显位**（嵌在「文案」卡的各个页签里）。
 *
 * 用户口径 2026-10-19：*「我希望的是在这里面进行回显，不是在底部新增一个区域的
 * 单独展示笔记内容」* —— 结果就长在**它对应的编辑位下面**：
 *   · 标题页签 ⇒ 显示本版标题（可改）
 *   · 正文页签 ⇒ 显示本版正文（可改）
 *   · 话题页签 ⇒ 显示本版话题（可增删改）
 * 这样「配参数 → 生成 → 就在这里看到 → 就地改 → 再重新生成」全在一个视野内，
 * 不需要往下滚去找另一块结果区。
 */
function ResultEcho({
  label,
  count,
  scope,
  regenLoading,
  onRegen,
  children,
}: {
  /** 标题行文字（**可省略**：不需要标题时连那一行都不渲染）。 */
  label?: string;
  count?: string;
  scope: "title" | "body" | "topics" | "media";
  regenLoading?: string | null;
  onRegen?: (scope: "title" | "body" | "topics" | "media") => void;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginTop: 4 }}>
      {(label || count || onRegen) && (
      <Flex align="center" gap={8} style={{ marginBottom: 4 }}>
        {label ? <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text> : null}
        {count ? (
          <Text type="secondary" style={{ fontSize: 12 }}>{count}</Text>
        ) : null}
        {onRegen ? (
          <Button
            size="small"
            type="link"
            style={{ padding: 0, fontSize: 12 }}
            loading={regenLoading === scope}
            disabled={Boolean(regenLoading) && regenLoading !== scope}
            onClick={() => onRegen(scope)}
          >
            重新生成
          </Button>
        ) : null}
      </Flex>
      )}
      {children}
    </div>
  );
}

/**
 * **可编辑回显块**（单行/多行通用）。
 *
 * 用户口径 2026-10-19：*「文案也使用回显的方式」* ——
 * 平时显示成**普通文本块**（和结果一样清爽），点一下变成输入框，
 * 失焦或按回车即提交；`Esc` 取消（还原原值）。
 *
 * 为什么不一直摆着 TextArea：结果面板以「看」为主，常年开着的输入框
 * 会让每一块都显得没写完（且会撑高面板）。
 */
function EditableBlock({
  value,
  placeholder,
  multiline = false,
  rows = 3,
  maxLength,
  maxHeight,
  onCommit,
}: {
  value: string;
  placeholder?: string;
  multiline?: boolean;
  rows?: number;
  maxLength?: number;
  maxHeight?: string;
  onCommit?: (next: string) => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  //: 外部值变了（重新生成 / 切换到别版）⇒ 同步进草稿，避免显示旧文本
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  const commit = async () => {
    const next = draft.trim();
    setEditing(false);
    if (next === value) return;
    await onCommit?.(next);
  };

  if (!editing) {
    return (
      <div
        style={{
          ...FLAT_BLOCK_STYLE,
          whiteSpace: "pre-wrap",
          cursor: "text",
          maxHeight,
          overflowY: maxHeight ? "auto" : undefined,
        }}
        onClick={() => {
          setDraft(value);
          setEditing(true);
        }}
        title="点击编辑"
      >
        {value || <Text type="secondary">{placeholder || "（空）"}</Text>}
      </div>
    );
  }

  return multiline ? (
    <Input.TextArea
      autoFocus
      value={draft}
      rows={rows}
      maxLength={maxLength}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
    />
  ) : (
    <Input
      autoFocus
      value={draft}
      maxLength={maxLength}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => void commit()}
      onPressEnter={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
    />
  );
}

/**
 * **可编辑的话题**：平时显示标签（点 × 删、末尾输入框加），
 * 与结果区其他块保持同一套「回显 + 就地改」的手感。
 */
function EditableTopics({
  topics,
  onCommit,
}: {
  topics: string[];
  onCommit?: (next: string[]) => void | Promise<void>;
}) {
  const [adding, setAdding] = useState("");
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingValue, setEditingValue] = useState("");

  const save = async (next: string[]) => {
    await onCommit?.(next);
  };

  return (
    <div style={FLAT_BLOCK_STYLE}>
      <Space size={4} wrap>
        {topics.map((topic, index) =>
          editingIndex === index ? (
            <Input
              key={`edit-${index}`}
              size="small"
              autoFocus
              style={{ width: 120 }}
              value={editingValue}
              onChange={(e) => setEditingValue(e.target.value)}
              onBlur={() => {
                const next = [...topics];
                const text = editingValue.trim();
                if (text) next[index] = text;
                else next.splice(index, 1);
                setEditingIndex(null);
                void save(next);
              }}
              onPressEnter={() => {
                const next = [...topics];
                const text = editingValue.trim();
                if (text) next[index] = text;
                else next.splice(index, 1);
                setEditingIndex(null);
                void save(next);
              }}
            />
          ) : (
            <Tag
              key={`${topic}-${index}`}
              style={{ marginInlineEnd: 0, cursor: "pointer" }}
              closable
              onClose={(e) => {
                e.preventDefault();
                void save(topics.filter((_, i) => i !== index));
              }}
              onClick={() => {
                setEditingIndex(index);
                setEditingValue(topic);
              }}
            >
              {topic}
            </Tag>
          )
        )}
        <Input
          size="small"
          style={{ width: 110 }}
          placeholder="+ 添加话题"
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
          onPressEnter={() => {
            const text = adding.trim();
            if (!text) return;
            setAdding("");
            void save([...topics, text]);
          }}
        />
      </Space>
    </div>
  );
}

/** **平铺内容块**的统一样式（标题 / 正文 / 话题共用）。
 *  用户口径 2026-10-19：*「标题正文和话题使用平铺」* —— 不套 Card 标题栏，
 *  但保留浅底 + 圆角 + 内边距，让三块仍是可辨识的独立区域。 */
const FLAT_BLOCK_STYLE: React.CSSProperties = {
  background: "var(--ant-color-fill-quaternary, #fafafa)",
  border: "1px solid var(--ant-color-border-secondary, #f0f0f0)",
  borderRadius: 6,
  padding: "8px 10px",
  fontSize: 13,
  lineHeight: 1.6,
};

/** 局部重生成的中文名（提示文案用）。 */
const SCOPE_CN: Record<string, string> = {
  title: "标题",
  body: "正文",
  topics: "话题",
  media: "配图",
  text: "文案",
  all: "整篇",
};

/** 局部重生成的**短名**（提示文案里拼句子）。⚠️ 与 TRIGGER_TYPE 简写同理：
 *  短名是排版压缩不是枚举语义 —— 字典 `sys_regenerate_scope` 存的是版本历史页
 *  的**长名**，这里刻意不入字典（设计 63 §2.4 简写口径），只吃 SCOPE_CN。 */
function scopeCn(scope: string): string {
  return SCOPE_CN[scope] ?? "";
}


function uniqueKeepOrder(urls: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const url of urls) {
    if (!seen.has(url)) {
      seen.add(url);
      out.push(url);
    }
  }
  return out;
}

/**
 * 商品原图 = **主图 + 详情图，保序去重后的合并列表**（前端唯一入口）。
 *
 * ⚠️ 两处选择器（商品原图 / 参考图）都必须走这里 —— 各写一份必然与后端漂移（DEF-044）。
 */
/**
 * 图片 URL → **图片名称**（URL 末段，去查询串/片段）。
 *
 * ⚠️ 与后端 `media_generation.product_image_name()` **同一判据**（改一处 = 改契约）。
 * `pick` 传的是名称而不是下标/整条 URL（用户口径 2026-10-18：「必须是图片名称」）：
 * 名称不依赖任何一侧的列表排序，也不受 CDN 域名/签名参数变化影响。
 */
/** **AI 结果图的名称** —— 与商品图**同一套判据**（URL 末段）。
 *  ⚠️ 不另写一份实现：两处判据一旦分叉，勾选态就会对不上（DEF-044）。 */
function aiImageName(url: string): string {
  return productImageName(url);
}

function productImageName(url: string): string {
  const text = String(url || "").trim();
  if (!text) return "";
  //: 去查询串 / 片段（同一个文件在不同签名下会有多条 URL）
  const cut = text.split("#")[0].split("?")[0].replace(/\/+$/, "");
  const parts = cut.split("/");
  return parts[parts.length - 1] || "";
}

function productImageList(productImages: string[], detailImages: string[]): string[] {
  return uniqueKeepOrder([...productImages, ...detailImages]);
}

/**
 * **媒体行里下拉的统一宽度**（用户口径 2026-10-16：*「设置一个最长下拉长度」*）。
 *
 * ⚠️ 定长而非自适应：自适应时「本地素材」窄、文件夹那条被长路径撑开，两个框长短不一
 * （截图里第一个框和第二个框宽度不同）。锁同一个值 ⇒ 无论内容多长，框永远一样长，
 * 超长内容由 antd 出省略号（`ellipsis`）。
 */
const LAYOUT_SELECT_WIDTH = 180;

/** 默认计划：商品原图当封面（与后端默认一致）。 */
function defaultSegments(): LlmMediaPlanSegment[] {
  return [{ source: "product_image", role: "cover" }];
}

/**
 * **参考图选择器**（智能生成内部，用户口径 2026-10-16）。
 *
 * ## 为什么放在「智能生成」里而不是单独一个来源
 *
 * 「参考图生成」的本质就是**智能生成 + 一张参考图**，做成下拉里的独立一项
 * 会让人以为它们互斥 —— 实际后端 `media_generation` 就是一个分支：
 * `ai_generate` **带 `reference_image_urls` 就是图生图、不带就是文生图**。
 * 两个入口调同一段代码 ⇒ 概念冗余（用户原话：*「这个下拉参考图正常是不需要的」*）。
 *
 * ## 交互
 *
 * 商品图列表里**点一张选中**（再点一次取消 = 回到纯文生图）；选中的有
 * 主色边框 + ✓（与 `MaterialPicker` 同一套视觉语言，不另立规矩）。
 * **不选**是合法状态（纯文生图），所以标题写「参考图（可选）」。
 * 商品没图时不渲染选择器、只给一句原因（缺值口径：不静默留白）。
 *
 * ⚠️ 只传**索引**上去，提交时由调用方解析成 URL（后端只认 URL 列表）。
 */
function ReferenceImagePicker({
  productImages,
  detailImages,
  materialImages,
  detailImagesNote,
  picked,
  onChange,
  onPreview,
  referenceSource,
}: {
  productImages: string[];
  detailImages: string[];
  /** 本地素材里的**图片**（用户口径 2026-10-16：*「参考图支持选本地或商品原图」*）。
   *  ⚠️ 元素是 `{ url, preview }` **成对**给的：
   *  `url` = 提交给后端的引用（`/api/xhs/materials/files/{id}/file`，后端读盘内联）；
   *  `preview` = 页面上 `<img>` 用的地址 —— 素材端点**要鉴权**，
   *  直接 `<img src="/api/...">` 发不出 Authorization ⇒ **必然 401 破图**
   *  （本仓踩过：见设计 17 §12 的素材预览口径）。所以走 object URL 预览。 */
  materialImages: { url: string; preview: string }[];
  detailImagesNote: string;
  picked: string[];
  onChange: (urls: string[]) => void;
  /** 放大看大图（用户口径 2026-10-17：*「还缺少一个大图预览」*）。 */
  onPreview: (url: string) => void;
  /** 参考图来源（由父组件持有：下拉画在「媒体来源」同一行）。 */
  referenceSource: "product" | "material";
}) {
  const [hint, setHint] = useState("");
  //: 参考图**来源**（用户口径 2026-10-17：*「参考图要有一个下拉，是选择原图还是本地素材
  //: 需指定具体文件夹」*）—— 先选来源，再在来源内挑图（与媒体来源下拉同一套心智：
  //: 「从哪儿拿」先定，「拿哪张」后选）。
  //: 默认「商品原图」（绝大多数场景用它；素材库要指定文件夹，多一步）。
  //: ⚠️ 这个下拉**由父组件 `SegmentCard` 渲染在「媒体来源」同一行**（用户口径 2026-10-17：
  //: *「下拉框放一行，并标注」*）—— 组件里只用 `source` 决定候选图，不自己画那一行。

  const productTotal = productImages.length + detailImages.length;
  /**
   * **按来源显示候选**（不再把三组混排成一条）：
   *  - `product` ⇒ 商品主图 ｜ 商品详情图（一行，竖线分隔）；
   *  - `material` ⇒ 当前文件夹下的图片素材。
   *
   * ⚠️ **为什么改成分开展示**：三组混排时用户分不清哪张是素材、哪张是商品图，
   * 而且要「选素材先选文件夹」这个动作在混排里无处安放（用户口径见上）。
   * ⚠️ 存的是 **URL**（不是下标）⇒ 不需要偏移量换算。
   */
  const cells: ({ url: string; preview: string } | null)[] =
    referenceSource === "product"
      ? productImageList(productImages, detailImages).map((u) => ({
          url: u,
          preview: u,
        }))
      : [...materialImages];
  const emptyText =
    referenceSource === "product"
      ? "这个商品还没有同步到图片"
      : "这个文件夹下没有图片素材";
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {cells.length === 0 || (cells.length === 1 && cells[0] === null) ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {emptyText}
        </Text>
      ) : (
        <div
          className="hermes-hide-scrollbar"
          style={{
            display: "flex",
            flexWrap: "nowrap",
            gap: 6,
            overflowX: "auto",
            paddingBottom: 2,
            width: "100%",
            minWidth: 0,
          }}
        >
          {cells.map((cell, position) => {
            //: `null` = 分隔位（画一条竖线，让「主图｜详情图」看得出分界）。
            if (cell === null) {
              return (
                <div
                  key={`divider-${position}`}
                  title="左边是商品主图，右边是商品详情图"
                  style={{
                    flex: "0 0 auto",
                    width: 1,
                    height: THUMB_CSS_SIZE,
                    background: "var(--ant-color-split, #f0f0f0)",
                  }}
                />
              );
            }
            const on = picked.includes(cell.url);
            const blocked = !on && picked.length >= MAX_REFERENCE_IMAGES;
            return (
              <div
                key={`${position}-${cell.url}`}
                onClick={() => {
                  if (blocked) {
                    //: 到上限 ⇒ **给提示**（口径：*「图片总数达到限制后不再支持选择进行提示」*）。
                    setHint(`最多选 ${MAX_REFERENCE_IMAGES} 张参考图，取消一张再选`);
                    return;
                  }
                  setHint("");
                  onChange(
                    on ? picked.filter((v) => v !== cell.url) : [...picked, cell.url]
                  );
                }}
                title={blocked ? `最多选 ${MAX_REFERENCE_IMAGES} 张` : ""}
                style={{
                  position: "relative",
                  width: THUMB_CSS_SIZE,
                  height: THUMB_CSS_SIZE,
                  //: ⚠️ `flexShrink: 0` —— 不加会被压扁变形（比换行更难看）。
                  flex: "0 0 auto",
                  borderRadius: 6,
                  overflow: "hidden",
                  cursor: blocked ? "not-allowed" : "pointer",
                  opacity: blocked ? 0.4 : 1,
                  border: on
                    ? "2px solid var(--ant-color-primary, #1677ff)"
                    : "1px solid var(--ant-color-border, #d9d9d9)",
                }}
              >
                {/* ⚠️ `src` 用 `preview` 而**不是** `url` —— 素材端点要鉴权，
                    直接给 `url` 会 401 破图（见 props 注释）。 */}
                {/* ⚠️ 同商品图：`srcSet` 三档 + `decoding=async`。
                    素材的 `preview` 是 object URL ⇒ `thumbSrcSet` 返回 undefined，只走 `src`。 */}
                <img
                  src={thumbUrl(cell.preview)}
                  srcSet={thumbSrcSet(cell.preview)}
                  alt={`参考图 ${position + 1}`}
                  decoding="async"
                  style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                />
                {on && (
                  <div
                    style={{
                      position: "absolute",
                      top: 0,
                      right: 0,
                      width: 18,
                      height: 18,
                      background: "var(--ant-color-primary, #1677ff)",
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      borderBottomLeftRadius: 6,
                      fontSize: 11,
                    }}
                  >
                    ✓
                  </div>
                )}
                {/* 放大角标（**点击预览大图，不是选中** —— 选中是点格身）：
                    `stopPropagation` 必须有，否则点放大也会顺带切换选中。 */}
                <div
                  onClick={(e) => {
                    e.stopPropagation();
                    onPreview(cell.url);
                  }}
                  title="查看大图"
                  style={{
                    position: "absolute",
                    bottom: 0,
                    right: 0,
                    width: 18,
                    height: 18,
                    background: "rgba(0,0,0,0.55)",
                    color: "#fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    borderTopLeftRadius: 6,
                    fontSize: 10,
                    cursor: "zoom-in",
                  }}
                >
                  ⤢
                </div>
              </div>
            );
          })}
        </div>
      )}
      {hint && (
        <Text type="warning" style={{ fontSize: 12 }}>
          {hint}
        </Text>
      )}
      {referenceSource === "product" && detailImages.length === 0 && detailImagesNote && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {detailImagesNote}
        </Text>
      )}
    </Space>
  );
}

/**
 * **商品原图选择器**（商品原图 = 主图 + 详情图，**合并成一行**展示）。
 */
function ProductImagePicker({
  productImages,
  detailImages,
  detailImagesNote,
  picked,
  max,
  onChange,
  onPreview,
}: {
  productImages: string[];
  detailImages: string[];
  detailImagesNote: string;
  /** 已选中的**图片 URL**（用户口径 2026-10-18：*「使用图片名称，不要使用下标」*
   *  —— 下标会因前后端列表口径不一致而错位，URL 是图片本身的身份，不会错）。 */
  picked: string[];
  max: number;
  onChange: (urls: string[]) => void;
  /** 点「⤢」看大图（用原图）。 */
  onPreview: (url: string) => void;
}) {
  const [hint, setHint] = useState("");
  if (productImages.length + detailImages.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        这个商品没有可用的台账图（换来源或先同步商品）
      </Text>
    );
  }
  const single = max === 1;
  /** 点某一张：对外一律用**图片名称**（`picked` 里存的也是名称）。 */
  const toggle = (url: string) => {
    const name = productImageName(url);
    if (single) {
      //: 单选：点同一张 = 取消；点别的 = **替换**（不是追加 —— 那会违反「只能选一个」）。
      setHint("");
      onChange(picked.includes(name) ? [] : [name]);
      return;
    }
    if (picked.includes(name)) {
      setHint("");
      onChange(picked.filter((v) => v !== name));
      return;
    }
    //: 多选满了 ⇒ **给提示**（口径 2026-10-16：*「图片总数达到限制后不再支持选择进行提示」*）
    //: —— 静默丢弃 = 点了没反应 = 用户以为坏了。
    if (picked.length >= max) {
      setHint(`最多选 ${max} 张，取消一张再选`);
      return;
    }
    setHint("");
    onChange([...picked, name]);
  };
  /**
   * **商品原图 = 主图 + 详情图，合并成一行**（用户口径 2026-10-16：
   * *「商品原图是包括商品主图和详情图」* + *「主图和详情图合并成一行展示」*）。
   *
   * ⚠️ 顺序 = **主图在前、详情图在后**，与后端 `_product_image_urls`
   * （`_main_image_urls + _detail_image_urls`）**逐位一致** —— `pick` 是这个
   * **合并列表的下标**，顺序不一致 ⇒ 用户选第 3 张、实际出第 5 张（静默错位）。
   * ⚠️ 中间那个 `null` 只是**视觉分隔**（画竖线），**不算进下标**
   * ⇒ 取下标时要跳过它（见下面的 `position` → `index` 换算）。
   */
  /** 去重后的合并列表（主图在前、详情图在后；与后端同一判据）。 */
  const merged = productImageList(productImages, detailImages);
  //: ⚠️ **分隔线放在主图与详情图之间**（不是末尾）：去重后主图组边界要用
  //: **去重后的主图张数**算，不能用 `productImages.length`（原图数 ≠ 去重后个数，
  //: 实测 10 张里有 5 张和详情图重复 ⇒ 边界错位、分隔线画错位置）。
  const mainUnique = uniqueKeepOrder(productImages).length;
  const strip: (string | null)[] =
    mainUnique > 0 && mainUnique < merged.length
      ? [...merged.slice(0, mainUnique), null, ...merged.slice(mainUnique)]
      : [...merged];
  const detailOffset = mainUnique;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      {/* **图片列表前的说明文案已移除**（用户口径 2026-10-19：*「选图（最多 3 张）移除，
          图片列表前面的文案全部移除」*）—— 上限各处已能看到（「最多 N 张」在生成张数旁），
          这里再写一遍是重复占位。**仅超限提示保留**（那是操作反馈，不是说明）。 */}
      {hint ? (
        <Text type="warning" style={{ fontSize: 12, flex: "0 0 auto" }}>
          {hint}
        </Text>
      ) : null}
      <div
        className="hermes-hide-scrollbar"
        style={{
          display: "flex",
          flexWrap: "nowrap",
          gap: 6,
          overflowX: "auto",
          paddingBottom: 2,
          flex: "1 1 auto",
          minWidth: 0,
        }}
      >
        {strip.map((url, position) => {
          //: `null` = 分隔位（画一条竖线，让「主图｜详情图」看得出分界）。
          if (url === null) {
            return (
              <div
                key="divider"
                title="左边是商品主图，右边是商品详情图"
                style={{
                  flex: "0 0 auto",
                  width: 1,
                  height: THUMB_CSS_SIZE,
                  background: "var(--ant-color-split, #f0f0f0)",
                }}
              />
            );
          }
          //: ⚠️ **不再换算下标**（用户口径 2026-10-18：*「使用图片名称，不要使用下标」*）——
          //: 分隔位、去重、前后端列表口径差异都可能让下标错位；URL 本身就是这张图的身份。
          const index = position < detailOffset ? position : position - 1;
          const on = picked.includes(productImageName(url));
          const blocked = !single && !on && picked.length >= max;
          return (
            <div
              key={url}
              onClick={() => toggle(url)}
              title={blocked ? `最多选 ${max} 张` : ""}
              style={{
                position: "relative",
                width: THUMB_CSS_SIZE,
                height: THUMB_CSS_SIZE,
                //: ⚠️ `flexShrink: 0` —— 不加会被压扁变形（比换行更难看）。
                flex: "0 0 auto",
                borderRadius: 6,
                overflow: "hidden",
                cursor: blocked ? "not-allowed" : "pointer",
                opacity: blocked ? 0.4 : 1,
                border: on
                  ? "2px solid var(--ant-color-primary, #1677ff)"
                  : "1px solid var(--ant-color-border, #d9d9d9)",
              }}
            >
              {/* ⚠️ 显示用**CDN 缩略图 + srcset**（见 `thumbUrl` / `thumbSrcSet`）：
                  `srcSet` 给 1x/2x/3x 三档，浏览器按真实 DPR 挑最合适的那档
                  —— 写死单一尺寸总有一档是「放大画」而发虚。 */}
              <img
                src={thumbUrl(url)}
                srcSet={thumbSrcSet(url)}
                alt={`商品图 ${index + 1}`}
                decoding="async"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
              />
              {on && (
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    right: 0,
                    width: 18,
                    height: 18,
                    background: "var(--ant-color-primary, #1677ff)",
                    color: "#fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    borderBottomLeftRadius: 6,
                    fontSize: 11,
                  }}
                >
                  ✓
                </div>
              )}
              {/* 放大角标（**点击预览大图，不是选中**；stopPropagation 防顺带切换） */}
              <div
                onClick={(e) => {
                  e.stopPropagation();
                  onPreview(url);
                }}
                title="查看大图"
                style={{
                  position: "absolute",
                  bottom: 0,
                  right: 0,
                  width: 18,
                  height: 18,
                  background: "rgba(0,0,0,0.55)",
                  color: "#fff",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  borderTopLeftRadius: 6,
                  fontSize: 10,
                  cursor: "zoom-in",
                }}
              >
                ⤢
              </div>
            </div>
          );
        })}
      </div>
      {detailImages.length === 0 && detailImagesNote && (
        <Text type="secondary" style={{ fontSize: 12, flex: "0 0 auto" }}>
          {detailImagesNote}
        </Text>
      )}
    </div>
  );
}

/**
 * 本地素材**选择器**（缩略图网格，点图即选中）。
 */
/**
 * 本地素材**选择器**（**二级联动** + 缩略图一行横滑，点图即选中）。
 *
 * ## 二级联动（用户口径 2026-10-16）
 *
 * *「本地素材库会存在多个文件夹地址，需要用户二级联动指定文件夹，文件夹下的素材
 * 依旧一行展示左右滚动，点击勾选」*
 *
 *   - **一级**：文件夹下拉（`folders` 由宿主拉，切换即回调 `onFolderChange`）；
 *   - **二级**：该文件夹下的素材，**一行 + 左右滚动**，点图勾选/取消。
 *
 * ⚠️ **文件夹里既有图片也有视频**（同批口径：*「文件夹中可能有图片也可能有视频素材」*）
 * ⇒ **不按类型过滤**，全部呈现；视频格加一个 `▶` 角标 + 文件名，避免和图片混淆。
 * ⚠️ 批量选**只对图片生效**：视频不占「图片张数」（笔记图序是图，视频另走视频链路）
 * ⇒ 点视频格给提示而不是静默加进 `file_ids`（后端 `material_file_ids` 是图 id）。
 *
 * ⚠️ 素材接口是**授权端点**（`/api/...`）—— `<img src>` 带不了 Authorization ⇒
 * 必须走 `fetchMaterialImageObjectUrl` 取 object URL（本仓踩过：直接贴 URL 恒 401 破图）。
 * ⚠️ object URL **必须 revoke**，否则长会话下泄漏（CDN 直链才不需要）。
 */
function MaterialPicker({
  files,
  selected,
  onChange,
}: {
  files: MaterialFile[];
  selected: number[];
  onChange: (ids: number[]) => void;
}) {
  const [urls, setUrls] = useState<Record<number, string>>({});
  const [failed, setFailed] = useState(false);
  //: 依赖用**内容**不用引用（`files` 每次渲染都是新数组 ⇒ 用 key 串做判据）。
  const key = files.map((f) => f.id).join(",");

  useEffect(() => {
    let revoked = false;
    const created: string[] = [];
    setFailed(false);
    void (async () => {
      try {
        const next: Record<number, string> = {};
        //: 只对**图片**取缩略图（视频没有可用缩略图端点，走文件图标占位）。
        for (const f of files.filter((x) => x.file_type !== "video").slice(0, 80)) {
          const objectUrl = await fetchMaterialImageObjectUrl(materialFileUrl(f.id));
          if (revoked) {
            window.URL.revokeObjectURL(objectUrl);
            break;
          }
          created.push(objectUrl);
          next[f.id] = objectUrl;
        }
        if (!revoked) setUrls(next);
      } catch {
        if (!revoked) setFailed(true);
      }
    })();
    return () => {
      revoked = true;
      created.forEach((url) => window.URL.revokeObjectURL(url));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** 点素材格：图片 ⇒ 切换选中；视频 ⇒ 给提示（不静默加进图片 id）。 */
  const onPick = (file: MaterialFile) => {
    if (file.file_type === "video") return;
    onChange(
      selected.includes(file.id) ? selected.filter((v) => v !== file.id) : [...selected, file.id]
    );
  };

  return (
    /* ⚠️ **只剩缩略图带**（用户口径 2026-10-16：*「文件夹 放一行，不要放两行占用页面空间」*
       + *「两个下拉放一起」*）—— 文件夹下拉**已上提到卡片头**与「来源」下拉并排，
       这里不再自带下拉；失败/空态文案**并进这一行右端**（口径 C3：提示不占独立区块）。 */
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      {failed ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          素材缩略图加载失败
        </Text>
      ) : files.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          这个文件夹还没有素材（去「笔记工坊 → 素材库」扫描）
        </Text>
      ) : (
        <div
          className="hermes-hide-scrollbar"
          style={{
            display: "flex",
            flexWrap: "nowrap",
            gap: 6,
            overflowX: "auto",
            paddingBottom: 2,
            flex: "1 1 auto",
            minWidth: 0,
          }}
        >
          {files.map((f) => {
            //: ⚠️ 视频**不占图片位** ⇒ 不参与勾选（也不进 `selected`）。
            const isVideo = f.file_type === "video";
            const picked = selected.includes(f.id);
            const url = urls[f.id];
            return (
              <Tooltip key={f.id} title={isVideo ? `${f.filename}（视频，不参与图片选择）` : f.filename}>
                <div
                  onClick={() => onPick(f)}
                  style={{
                    position: "relative",
                    width: THUMB_CSS_SIZE,
                    height: THUMB_CSS_SIZE,
                    //: ⚠️ `flexShrink: 0` —— 不加会被压扁变形（比换行更难看）。
                    flex: "0 0 auto",
                    borderRadius: 6,
                    overflow: "hidden",
                    cursor: isVideo ? "not-allowed" : "pointer",
                    //: 视频**变淡**（一眼看出「这个不能点」，不用先点一下才知道）。
                    opacity: isVideo ? 0.45 : 1,
                    border: picked
                      ? "2px solid var(--ant-color-primary, #1677ff)"
                      : "1px solid var(--ant-color-border, #d9d9d9)",
                  }}
                >
                  {url ? (
                    <img
                      src={url}
                      alt={f.filename}
                      style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                    />
                  ) : (
                    <div
                      style={{
                        width: "100%",
                        height: "100%",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        background: "var(--ant-color-fill-quaternary, #fafafa)",
                      }}
                    >
                      {isVideo ? (
                        <PlayCircleOutlined style={{ fontSize: 18, opacity: 0.5 }} />
                      ) : (
                        <FileImageOutlined style={{ fontSize: 18, opacity: 0.35 }} />
                      )}
                    </div>
                  )}
                  {/* 视频角标：**类型信号**（不只靠变淡）—— 一眼区分图/视频。 */}
                  {isVideo && (
                    <div
                      style={{
                        position: "absolute",
                        bottom: 0,
                        left: 0,
                        right: 0,
                        background: "rgba(0,0,0,0.55)",
                        color: "#fff",
                        fontSize: 9,
                        textAlign: "center",
                        lineHeight: "14px",
                      }}
                    >
                      视频
                    </div>
                  )}
                  {/* 选中角标：**形状**信号（不只靠边框颜色）—— 色弱也分得清。 */}
                  {picked && (
                    <div
                      style={{
                        position: "absolute",
                        top: 0,
                        right: 0,
                        width: 18,
                        height: 18,
                        background: "var(--ant-color-primary, #1677ff)",
                        color: "#fff",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        borderBottomLeftRadius: 6,
                        fontSize: 11,
                      }}
                    >
                      ✓
                    </div>
                  )}
                </div>
              </Tooltip>
            );
          })}
        </div>
      )}
      {/* 已选计数**并进这一行右端**（不新开一行）。 */}
      {selected.length > 0 && (
        <Text type="secondary" style={{ fontSize: 12, flex: "0 0 auto" }}>
          已选 {selected.length} 张
        </Text>
      )}
    </div>
  );
}

/** 视频笔记的默认计划：封面（商品原图）+ 一个视频段。 */
function defaultVideoSegments(): LlmMediaPlanSegment[] {
  return [
    { source: "product_image", role: "cover" },
    { source: "video_ai_generate", role: "content" },
  ];
}

/**
 * **单个媒体来源卡片**（③ 区的一行 / 一列）。
 *
 * 从原来的内联 JSX 抽出来，两个理由：
 *  1. **图文与视频两套布局共用同一份实现** —— 视频笔记是两列并排（视频 | 封面），
 *     图文笔记是竖排可叠加，但「一行里有什么」完全一样（标签 + 来源下拉 + 参数 + 预览）。
 *     两处各写一遍必然漂移（DEF-044）。
 *  2. **③ 区改成卡片区域后**，主文件只负责「怎么摆」，这个组件负责「摆什么」。
 *
 * 参数与预览**就地展开**（重做要解决的「盲选」问题）：选了什么就在下面看到什么。
 */
function SegmentCard({
  seg,
  title,
  tagColor,
  hint,
  options,
  materialFiles,
  materialFolders,
  materialFolderId,
  onMaterialFolderChange,
  videoTemplates,
  productImages,
  detailImages,
  materialImages,
  detailImagesNote,
  aiCountCap,
  imageLimit,
  onPatch,
  onRemove,
  onPreview,
  echo,
  echoOnly = false,
  picked,
  pickCap,
  failure = "",
  aiMedia = [],
}: {
  index: number;
  seg: LlmMediaPlanSegment;
  title: string;
  tagColor: string;
  hint: string;
  options: { value: LlmMediaSource; label: string }[];
  materialFiles: MaterialFile[];
  /** 素材**文件夹清单**（二级联动的一级；用户口径 2026-10-16）。 */
  materialFolders: MaterialFolder[];
  materialFolderId: number | undefined;
  onMaterialFolderChange: (id: number) => void;
  videoTemplates: PromptTemplate[];
  productImages: string[];
  /** 商品**详情图**（参考图候选第二组；单独拉，见弹窗里的 effect）。 */
  detailImages: string[];
  /** 本地素材**图片**（参考图候选第三组；每项 `{url, preview}`，见弹窗里的 effect）。 */
  materialImages: { url: string; preview: string }[];
  /** 详情图没拉到时的原因（**必须给**：少一组是事实，不装作没有详情图）。 */
  detailImagesNote: string;
  /** 智能生成段**可填上限**（总数 − 其他段已占；由弹窗算好传入）。 */
  aiCountCap: number;
  /** 笔记图片总数（配置下发；只用于文案展示）。 */
  imageLimit: number;
  onPatch: (patch: Partial<LlmMediaPlanSegment>) => void;
  /** 不传 = 不可删（封面就是这种：无封面 = 整篇失败，不给死路）。 */
  onRemove?: () => void;
  /** 点「⤢」看大图（由弹窗层持有预览 Modal）。 */
  onPreview: (url: string) => void;
  /** **这一段的成品回显**（用户口径 2026-10-19：*「不是在单独区域展示」*）——
   *  渲染在该段**内部**底部：封面段下面就是上一版的封面，内页段下面就是内页图。 */
  echo?: React.ReactNode;
  /** 这一段是**为回显补出来的**（用户没为它配来源）：只显示成品图，
   *  不显示来源选择器（配了也不会被这次生成使用）。 */
  echoOnly?: boolean;
  /** **这一版实际用到的候选图名称**（覆盖 `seg.pick`，在候选图上显示勾选态）。 */
  picked?: string[];
  /** **本段还能再选几张**（全局余量 = 总数 − 其它段已选）；
   *  不传 = 只受本段自己的上限约束。 */
  pickCap?: number;
  /** **这一段的失败原因**（生成失败时非空；`AiResultStrip` 用它显示占位图 + tooltip）。 */
  failure?: string;
  /** **这一段产出的图**（智能生成段专用，左侧并排渲染）。 */
  aiMedia?: { url: string; local_path?: string; ok?: boolean; error?: string; source?: string }[];
}) {
  //: ⚠️ `video_material` 也是视频段（本地素材直接用）—— 漏掉它会让「本地素材」
  //: 走不到视频参数区（用户选了却看不到选素材的入口）。
  const isVideo =
    seg.source === "video_material" ||
    seg.source === "video_ai_generate" ||
    seg.source === "video_reference";
  //: 参考图**来源**（商品原图 / 本地素材）—— 由这里持有，因为下拉要画在
  //: **「媒体来源」同一行**（用户口径 2026-10-17：*「下拉框放一行，并标注」*）。
  //: ⚠️ 用 `useState` 保持用户选择；切来源时不重置（切回原来源还是原选择）。
  const [referenceSource, setReferenceSource] = useState<"product" | "material">("product");
  return (
    <div
      style={{
        border: "1px solid var(--ant-color-border, #d9d9d9)",
        borderRadius: 8,
        padding: 10,
        height: "100%",
      }}
    >
      <Space style={{ width: "100%", marginBottom: 8 }} align="center" wrap={false}>
        <Tag color={tagColor} style={{ flex: "0 0 auto" }}>
          {title}
        </Tag>
        {/* ⚠️ **两个下拉定长等宽**（用户口径 2026-10-16：*「设置一个最长下拉长度」*）
            —— 原先宽度随内容自适应 ⇒ 「本地素材」窄、路径那条被撑出 `D:\work\XHS\SCK...`
            截断得很难看，两个框看起来不一样长。现在都锁 **180px**：
            框等宽、内容超长一律 `ellipsis`（省略号由 antd 自己出）。 */}
        <Select
          value={seg.source}
          style={{ width: LAYOUT_SELECT_WIDTH, flex: "0 0 auto" }}
          onChange={(value) =>
            onPatch({
              source: value,
              file_ids: undefined,
              prompt: undefined,
              count: undefined,
              template_id: undefined,
              duration: undefined,
            })
          }
          options={options}
        />
        {/* ⚠️ **素材的文件夹下拉与「来源」下拉并排**（用户口径 2026-10-16：
            *「两个下拉放一起」*）—— 原先它在下面 MaterialPicker 里自成一行，
            两个下拉上下分家（用户要先看「来源」、再往下找「文件夹」）。
            只在素材来源时出现（别的来源没有文件夹概念）。
            ⚠️ 显示的是**文件夹名**不是路径（路径长且同名文件夹会重复）——
            路径进 `Tooltip`（口径 C4：长信息放悬停）。 */}
        {seg.source === "material" && materialFolders.length > 0 && (
          <Tooltip
            title={
              materialFolders.find((f) => f.id === materialFolderId)?.path || "选择素材文件夹"
            }
          >
            <Select
              style={{ width: LAYOUT_SELECT_WIDTH, flex: "0 0 auto" }}
              value={materialFolderId}
              onChange={(value) => onMaterialFolderChange(value)}
              placeholder="选择素材文件夹"
              options={materialFolders.map((f) => ({
                value: f.id,
                //: 用 `label` 显示名字（`title` 是悬停提示）—— `Select` 会自己截断超长。
                label: `${folderDisplayName(f.name)}（${f.file_count}）`,
              }))}
            />
          </Tooltip>
        )}
        {/* ── 参考图来源（**与媒体来源同一行**，用户口径 2026-10-17：
            *「下拉框放一行，并标注」*）──
            ⚠️ 只在「智能生成图 / 智能生成视频」时出现（别的来源用不到参考图）。
            ⚠️ **要标注**：两个下拉并排时必须看得出哪个是「参考图」的来源
            —— 全靠左到右猜会选错（「参考图」这个 label 不可省）。 */}
        {(seg.source === "ai_generate" || seg.source === "video_ai_generate") && (
          <>
            <Text type="secondary" style={{ fontSize: 12, flex: "0 0 auto" }}>
              参考图
            </Text>
            {/* ⚠️ **不写 `size="small"`**（2026-10-17 截图实证：这行四个控件里
                「本地素材」比「智能生成视频」矮一截 —— 因为前者 small、后者默认）。
                同一行内所有 `Select` **必须同 size**，否则高度参差、看着像坏了。
                ⚠️ 宽度也用同一个常量 `LAYOUT_SELECT_WIDTH`（与媒体来源、素材文件夹
                对齐）—— 三个硬编码宽度（120/160/180）并排时宽度参差同样难看。 */}
            <Select
              style={{ width: LAYOUT_SELECT_WIDTH, flex: "0 0 auto" }}
              value={referenceSource}
              onChange={(value: "product" | "material") => setReferenceSource(value)}
              options={[
                { value: "product", label: "商品原图" },
                { value: "material", label: "本地素材" },
              ]}
            />
            {referenceSource === "material" && (
              <Tooltip
                title={
                  materialFolders.find((f) => f.id === materialFolderId)?.path || "选择素材文件夹"
                }
              >
                <Select
                  style={{ width: LAYOUT_SELECT_WIDTH, flex: "0 0 auto" }}
                  value={materialFolderId}
                  onChange={(value) => onMaterialFolderChange(value)}
                  placeholder="选择文件夹"
                  /* ⚠️ **显示文件夹名、不显示路径**（截图实证：下拉里出现
                     `D:\work\XHS\SCK...` 这种路径 —— 长、读不出是哪个文件夹，
                     而且同名文件夹无法区分）。路径进上面那个 `Tooltip`。 */
                  options={materialFolders.map((f) => ({
                    value: f.id,
                    label: `${folderDisplayName(f.name)}（${f.file_count}）`,
                  }))}
                />
              </Tooltip>
            )}
            <Text type="secondary" style={{ fontSize: 12, flex: "0 0 auto" }}>
              最多 {MAX_REFERENCE_IMAGES} 张
              {(seg.reference_urls?.length || 0) > 0 ? `，已选 ${seg.reference_urls?.length}` : ""}
            </Text>
          </>
        )}
        {onRemove && (
          <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={onRemove} />
        )}
        {/* **来源说明文案已移除**（用户口径 2026-10-19：*「直接用台账里的商品图，
            不额外生成（最快、零成本）文案也移除」*）——
            来源下拉的名称本身已经说清是什么，后面再挂一句解释只挤占横向空间。 */}
      </Space>

      {/* ── 参数就地展开 + 预览（全部来源都要有预览，用户口径 2026-10-16） ── */}
      {seg.source === "material" &&
        (materialFiles.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="素材库还没有图片（去「笔记工坊 → 素材库」添加并扫描）"
          />
        ) : (
        /* ⚠️ **先预览、再选择**（用户口径 2026-10-16：*「本地素材库的素材也应该支持
           预览，通过预览后进行选择」*）—— 原写法是一个只显文件名的 `Select`，
           用户**看不见图就得选**（正是弹窗重做要解决的「盲选」）。
           改为**缩略图网格**：点图即选中（边框高亮 + 角标勾），再选完。
           授权端点仍走 object URL（不 revoke 会泄漏；依赖用内容不用引用）。
           ⚠️ 不要再套一层 `Space`（选择器自带）—— 多一层 = 多一处间距、多一行空。 */
        <MaterialPicker
          files={materialFiles}
          selected={seg.file_ids || []}
          onChange={(ids) => onPatch({ file_ids: ids })}
        />
      ))}

      {seg.source === "product_image" && (
        /* ⚠️ **商品原图要能选**（用户口径 2026-10-16：*「商品原图，无法被选中」*）
           + **含主图与详情图**（同批追加：*「商品原图是包括商品主图和详情图」*）——
           原先只渲染只读预览条，且**只有主图**（`XhsShopItem.images`）。
           ⚠️ **封面限制为 1 张**（*「封面图应限制，只允许选择一个」*）：封面 1 张、内页按总数上限。
           ⚠️ 候选是**两组拼接**：下标必须与后端 `_product_image_urls` 的口径一致，
           故这里传「合并后的下标」= 主图在前、详情图在后（详见 `ProductImagePicker` 注释）。 */
        <ProductImagePicker
          productImages={productImages}
          detailImages={detailImages}
          detailImagesNote={detailImagesNote}
          picked={picked && picked.length ? picked : seg.pick || []}
          max={
            seg.role === "cover"
              ? 1
              : Math.max(0, Math.min(imageLimit, pickCap ?? imageLimit))
          }
          onChange={(pick) => onPatch({ pick })}
          onPreview={onPreview}
        />
      )}

      {seg.source === "ai_generate" && (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          {/* ⚠️ **参考图 ｜ 结果图 同一行、各 50%**（用户口径 2026-10-19：
              *「将当前的参考图与结果图方一行，各50%，依旧支持左右滚动」*）——
              左边是「挑参考」，右边是「看这次出了什么」，两件事挨着才看得出因果；
              上下堆叠时结果图要滚到下面才看得到，等于看不见。
              两边各自横向滚动（`overflow-x:auto` + `nowrap`），互不撑宽。
              ⚠️ 只在**智能生成**段这样摆：商品原图 / 本地素材的「参考」就是成品本身，
              再并一排结果图是重复。 */}
          <div style={{ display: "flex", gap: 10, alignItems: "flex-start", width: "100%" }}>
            <div style={{ flex: "1 1 50%", minWidth: 0, overflowX: "auto" }} className="hermes-hide-scrollbar">
              <ReferenceImagePicker
                productImages={productImages}
                detailImages={detailImages}
                materialImages={materialImages}
                detailImagesNote={detailImagesNote}
                picked={seg.reference_urls || []}
                onChange={(urls) => onPatch({ reference_urls: urls })}
                referenceSource={referenceSource}
                onPreview={onPreview}
              />
            </div>
            <div style={{ flex: "1 1 50%", minWidth: 0, overflowX: "auto" }} className="hermes-hide-scrollbar">
              <AiResultStrip
                failure={failure}
                media={aiMedia}
                onPreview={onPreview}
                picked={seg.pick || []}
                onToggle={(name) => {
                  //: 点一下切换：**选中才进这篇笔记**（用户口径 2026-10-19：
                  //: *「生成后才可以供用户选择，应是生成后选择的图片算该片笔记的图片总数」*）。
                  //: 选中集合就存在该段的 `pick` 里 —— 与商品图同一个字段、同一套名称判据。
                  const next = [...(seg.pick || [])];
                  const at = next.indexOf(name);
                  if (at >= 0) next.splice(at, 1);
                  else next.push(name);
                  onPatch({ pick: next });
                }}
              />
            </div>
          </div>
          <Input.TextArea
            rows={2}
            maxLength={2000}
            placeholder="图片提示词（可选）—— 例如：浅色木质桌面，俯拍，自然光"
            value={seg.prompt}
            onChange={(e) => onPatch({ prompt: e.target.value })}
          />
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              生成张数
            </Text>
            {/* ⚠️ **生成张数与笔记图数解耦**（用户口径 2026-10-19：*「大模型生成的图不一定
                全部可以用，生成后才可以供用户选择，所以它的生成张数不应该算该片笔记的
                图片总数，应是生成后选择的图片算该片笔记的图片总数」*）。
                旧口径（已被取代）：*「生成张数根据笔记需要的总图数动态计算…总数是固定的」*
                —— 那条把「备选池」当成了「占位」，生成 6 张就扣 6 个位，
                可实际用户可能只留 2 张，剩下的位白扣。
                现在固定 **1–6 张、默认 3 张**，不随总数变。 */}
            <InputNumber
              min={1}
              max={6}
              style={{ width: 90 }}
              value={Math.max(1, Math.min(seg.count ?? 3, 6))}
              onChange={(value) => onPatch({ count: Math.max(1, Math.min(value ?? 3, 6)) })}
            />
          </Space>
          {/* ⚠️ 这里原有一行「生成后才能预览」的说明文案，已按用户口径
              （2026-10-19）移除：**它说的是废话**（生成前当然没有图），
              白占一行高度，还与上方「最多 N 张」的说明挤在一起。 */}
        </Space>
      )}

      {isVideo && (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          {/* ⚠️ **智能生成视频：参考图在最上面**（用户口径 2026-10-16：
              *「智能生成视频需支持选择参考图，参考图可为本地素材也可以是商品原图，最多一共4张」*）
              —— 与图片的「智能生成」同一套交互（参考图 > 提示词），先挑参考、再写提示词。
              ⚠️ **候选只有商品原图（主图 + 详情图）**：视频模型要的是**可直接抓取的 URL**，
              而本地素材图走的是**带鉴权的 `/api/...` 端点**（`<img>` 都带不上 Authorization，
              何况第三方模型）⇒ 把素材路径发过去必然拉不到图。
              所以这里**不列素材图**（列了才是骗人：选了也生成不出来）。
              要「用素材做参考」⇒ 把那张图存成商品图 / 或改用商品图。 */}
          {seg.source === "video_ai_generate" && (
            <ReferenceImagePicker
              productImages={productImages}
              detailImages={detailImages}
              materialImages={materialImages}
              detailImagesNote={detailImagesNote}
              picked={seg.reference_urls || []}
              onChange={(urls) => onPatch({ reference_urls: urls })}
              referenceSource={referenceSource}
              onPreview={onPreview}
            />
          )}
          <Select
            allowClear
            style={{ width: "100%" }}
            placeholder="视频提示词模板（可选）"
            value={seg.template_id}
            onChange={(value) => onPatch({ template_id: value })}
            options={videoTemplates.map((t) => ({
              value: t.id,
              label: t.is_default ? `${t.name}（默认）` : t.name,
            }))}
            notFoundContent="还没有视频模板，可在「笔记工坊 → 提示词模板」新建"
          />
          <Input.TextArea
            rows={2}
            maxLength={2000}
            placeholder="视频提示词（可选）—— 例如：手持商品展示，缓慢环绕，暖光，竖版"
            value={seg.prompt}
            onChange={(e) => onPatch({ prompt: e.target.value })}
          />
          {/* 时长只对**生成**有意义（本地素材直接用，没有可设的时长）。 */}
          {seg.source === "video_ai_generate" && (
            <Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                时长（秒）
              </Text>
              <InputNumber
                min={1}
                max={10}
                style={{ width: 90 }}
                value={seg.duration ?? 5}
                onChange={(value) => onPatch({ duration: value ?? 5 })}
              />
            </Space>
          )}
          {seg.source === "video_material" && (
            /* 「本地素材」= **不生成视频**（用户口径：*「使用本地素材这无需再生成视频」*）⇒
               只给素材选择器（**选视频**），说清「文案仍按商品 + 提示词生成」。 */
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                选视频素材（文案仍按商品与提示词生成，不生成视频）
              </Text>
              <MaterialPicker
                files={materialFiles.filter((f) => f.file_type === "video")}
                selected={seg.file_ids || []}
                onChange={(ids) => onPatch({ file_ids: ids })}
              />
            </Space>
          )}
          {/* 生成类来源才提示「生成后才有」；本地素材是**现成的**，不该这么说。 */}
          {seg.source === "video_ai_generate" && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              ⓘ 生成后才有；异步任务约几分钟，失败不影响正文与封面
            </Text>
          )}
        </Space>
      )}
      {/* **这一段的成品回显**（用户口径 2026-10-19：*「不是在单独区域展示」*）——
          与文案区同一套：**配置在上、结果回显在下**，而且回显落在**它自己的段**里。 */}
      {echo}
    </div>
  );
}

export function LlmGenerateModal({
  open,
  accountId,
  itemId,
  itemTitle,
  productImages = [],
  onClose,
  onQueued,
}: {
  open: boolean;
  accountId: number | null;
  itemId: string;
  itemTitle: string;
  /** 商品主图（公开 CDN 直链，`<img src>` 直接可渲染）—— 供「商品原图」段预览。 */
  productImages?: string[];
  onClose: () => void;
  /** 入队成功后回调（宿主用它登记轮询）。 */
  onQueued?: (itemId: string) => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  //: 提示词模板 id（原先走 `Form.Item`，现在文案卡改成受控 Space + 三块 Tabs ⇒ 自己持 state）
  const [templateId, setTemplateId] = useState<number | undefined>(undefined);
  //: **文案要求**（标题 / 正文 / 话题各自的提示词 + 区间 + 表情 + 话题数量）
  const [copy, setCopy] = useState<Required<LlmCopyRequirements>>(DEFAULT_COPY);
  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [templatesLoaded, setTemplatesLoaded] = useState(false);
  /**
   * **素材文件夹选择**（用户口径 2026-10-16：*「本地素材库会存在多个文件夹地址，
   * 需要用户二级联动指定文件夹」*）—— 一级选文件夹、二级选文件夹下的图。
   *
   * ⚠️ 两个关键的「不」：
   *   - **不一次性拉全库**（多文件夹下全拉会把几百张图塞进内存 + 首屏卡死）；
   *   - **不按文件夹缓存后不刷新**（用户刚扫描完文件夹，列表还是空的 ⇒ 以为没图）。
   *     每次切文件夹**都重新拉**（素材库本身有分页，代价可接受）。
   */
  const [materialFolders, setMaterialFolders] = useState<MaterialFolder[]>([]);
  const [materialFolderId, setMaterialFolderId] = useState<number | undefined>(undefined);
  /** **当前所选文件夹下**的素材（不是全库 —— 见上面的两「不」）。 */
  const [materialFiles, setMaterialFiles] = useState<MaterialFile[]>([]);
  const [submitting, setSubmitting] = useState(false);
  //: **同步生成的已用秒数**（用户口径 2026-10-18：*「点击生成加蒙版」*）——
  //: 同步要等几十秒，没有可见的计时，用户会以为卡死然后重复点击。
  const [elapsedSec, setElapsedSec] = useState(0);
  /** 正在局部重生成哪一块（`title`/`body`/`topics`/`media`；null = 空闲）。
   *  合到这里声明是因为 `busy` 要同时看「生成中」与「重生成中」。 */
  const [regenScope, setRegenScope] = useState<string | null>(null);
  /** **是否在等后台**（生成或局部重生成任一）—— 蒙版与计时都看它。 */
  /** **编辑暂存**（用户口径 2026-10-19：*「生成记录编辑后应新增一个更新记录按钮」*）——
   *  改文案时不再「失焦即存库」：先攒在本地，点「更新记录」才写回这一版。
   *  好处：改一半反悔、或一次改三块，只落一次库（不会中途产生半成品版本内容）。
   *  `null` = 没有未提交改动。 */
  const [draft, setDraft] = useState<{
    title?: string;
    body?: string;
    topics?: string[];
  } | null>(null);

  /**
   * **当前配置 → 媒体条目**（用户口径 2026-10-19：*「更新记录按钮为保存最新的数据，
   * 勾选了新的图片未保存」*）。
   *
   * 「更新记录」保存的是**当前所见**：封面/内页各段选中的图、以及智能生成段这次产出的图。
   * 每条都带 `source` / `role` / `slot_index` —— 回显靠它们归段，丢了就全挤进封面。
   *
   * 解析规则（与生成链路同一套，不另立判据）：
   *   · `product_image` ⇒ `pick` 里存的是**图片名称**（URL 末段），反查回完整 URL
   *   · `material`      ⇒ `file_ids`，拼成 `/api/xhs/materials/files/{id}/file`
   *   · `ai_generate`   ⇒ 本次产出（`genResult.media` 里同来源的），没有就不放
   */
  const buildMediaEntries = (): {
    url: string;
    local_path?: string;
    sort_order?: number;
    asset_type?: string;
    ok?: boolean;
    source?: string;
    role?: string;
    slot_index?: number | null;
  }[] => {
    const productUrls = productImageList(productImages, detailImages);
    const byName = new Map<string, string>();
    for (const url of productUrls) byName.set(productImageName(url), url);

    const out: {
      url: string;
      local_path?: string;
      sort_order?: number;
      asset_type?: string;
      source?: string;
      role?: string;
      slot_index?: number | null;
    }[] = [];
    segments.forEach((seg, index) => {
      const role = index === 0 ? "cover" : "content";
      if (seg.source === "product_image") {
        for (const name of seg.pick || []) {
          const url = byName.get(name);
          if (!url) continue; // 找不到就不写（不造 URL）
          out.push({
            url,
            local_path: "",
            sort_order: out.length,
            asset_type: "image",
            source: "product_image",
            role,
            slot_index: index,
          });
        }
      } else if (seg.source === "material") {
        for (const id of seg.file_ids || []) {
          out.push({
            url: `/api/xhs/materials/files/${id}/file`,
            local_path: "",
            sort_order: out.length,
            asset_type: "image",
            source: "material",
            role,
            slot_index: index,
          });
        }
      } else if (seg.source === "ai_generate") {
        for (const item of genResult?.media || []) {
          if ((item.source || "") !== "ai_generate") continue;
          if (item.ok === false) continue;
          const raw = item.url || item.local_path || "";
          if (!raw) continue;
          out.push({
            url: raw,
            local_path: item.local_path || "",
            sort_order: out.length,
            asset_type: item.asset_type || "image",
            source: "ai_generate",
            role,
            slot_index: index,
          });
        }
      }
    });
    return out;
  };

  /** **把暂存的改动写回当前版本**（PATCH，不调模型、不花钱、版本号不变）。 */
  async function handleSaveDraft() {
    if (!genResult) return;
    //: **每次都写面板上的最新内容**（用户口径 2026-10-19：*「每次点击只保存更新最新内容」*）——
    //: 不依赖「有没有改动」这个判据：用户点了就是要以**当前所见**为准。
    //: 三个字段全量下发（`undefined` 表示不改的那套语义在这里不用 —— 全量覆盖更直白）。
    const payload = {
      title: draft?.title ?? (genResult.title || ""),
      body: draft?.body ?? (genResult.body || ""),
      topics: draft?.topics ?? (genResult.topics || []),
    };
    //: ⚠️ **等一拍**：点按钮时编辑框先 `onBlur` → `commit()` → `setDraft(...)`，
    //: 与点击在同一个事件循环里，直接读 `draft` 会拿到旧值（用户看到的「点了没保存最新内容」）。
    //: `await Promise.resolve()` 让那次 state 更新先落，再读出真正的最新内容。
    await Promise.resolve();
    try {
      const updated = await updateNoteVersion({
        recordId: genResult.record_id,
        ...payload,
        //: **配图也一起存**（勾了新图必须落库，否则下次采用回来还是旧的）
        media: buildMediaEntries(),
      });
      setGenResult((prev) =>
        prev
          ? {
              ...prev,
              title: updated.title ?? prev.title,
              body: updated.body ?? prev.body,
              topics: updated.topics ?? prev.topics,
            }
          : prev
      );
      setDraft(null);
      message.success("已更新记录");
      //: 列表也要跟着刷（否则弹窗里是新的、记录列表还是旧的）
      setVersionRefreshToken((v) => v + 1);
    } catch (error) {
      const detail = detailMessage(
        (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
      );
      message.error(detail || (error instanceof Error ? error.message : "更新失败"));
    }
  }

  /** 发布中（同步等待终态，期间锁按钮并显示同一个居中蒙版）。 */
  const [publishing, setPublishing] = useState(false);

  //: ⚠️ **生成与发布共用同一个居中进度弹窗**（用户口径 2026-10-19：
  //: *「点击发布笔记和生成统一使用居中进度弹窗」*）—— 两件事都是「点完要等几十秒」，
  //: 各自造一个蒙版会出现两套文案/两套计时/两种居中方式（DEF-044 同型）。
  const busy = submitting || regenScope !== null || publishing;
  /** 蒙版文案（用户口径 2026-10-18：两种等待都要有居中提示）。 */
  const waitLabel = publishing
    ? "正在发布"
    : submitting
      ? "正在生成"
      : regenScope === "media"
        ? "正在重新生成配图"
        : "正在重新生成文案";
  /**
   * **笔记形态** —— 弹窗内的**两个大类 Tab**（用户口径 2026-10-16：
   * *「使用两个大类 tab 将图文和视频笔记进行拆分」*）。
   *
   * ⚠️ **只切换「参数界面」，不切后端链路** —— 后端仍是**公用一条**
   * （`start_llm_generate_note` 入口、形态由 `media_plan` 派生 `note_form`）。
   */
  const [noteForm, setNoteForm] = useState<NoteForm>("image");
  /** 有序媒体段 = **笔记图序**（封面必须第 1 段）。 */
  const [segments, setSegments] = useState<LlmMediaPlanSegment[]>(defaultSegments());
  /**
   * **按笔记形态各存一套参数**（用户口径 2026-10-18：*「图文笔记和视频笔记的数据是不统一的，
   * 各自保存不清理」*）。
   *
   * 图文和视频是两条**互不相同**的链路（图 ≤18 张 / 视频 1 个 + 独立封面），
   * 参数天然不同：图文那套的「生成张数」对视频没意义，视频那套的「时长/封面」
   * 图文也用不上。共用一份状态的话，切过去会把另一套顶掉（用户回来发现白配了）。
   *
   * 形态 ⇒ 一套 `{ templateId, copy, segments }`。**只切读写目标，不清另一套**。
   */
  const formParamsRef = useRef(
    new Map<NoteForm, { templateId?: number; copy: Required<LlmCopyRequirements>; segments: LlmMediaPlanSegment[] }>()
  );

  /** 文件/素材相关的**临时勾选**不跨形态保留（它们属于「这一次选了什么图」）。 */
  const switchForm = (next: NoteForm) => {
    const current = noteForm;
    if (next === current) return;
    //: ① 先把当前形态的参数存进它自己的槽（**不清理**，切回来还在）
    formParamsRef.current.set(current, { templateId, copy, segments });
    //: ② 再载入目标形态的槽；没有就给它一套干净的默认值
    const saved = formParamsRef.current.get(next);
    if (saved) {
      setTemplateId(saved.templateId);
      setCopy(saved.copy);
      setSegments(saved.segments);
    } else {
      setTemplateId(undefined);
      setCopy(DEFAULT_COPY);
      setSegments(defaultSegments());
    }
    setNoteForm(next);
  };
  /**
   * 参考图候选 —— **主图组 / 详情图组分开**（用户口径 2026-10-16：
   * *「参考图方放在提示词的上面，包括主图详情图，支持多选最多4个」*）。
   *
   * ⚠️ 详情图**不在** `XhsShopItem` 里（列表载荷只有 `images` 主图）⇒ 打开弹窗时
   * 按需拉一次 `fetchXhsShopItemDetail`（与 AI 商品图链路同一个端点、同一种分组）。
   * 拉不到 ⇒ 只给主图组 + 一句原因（缺值口径：不静默留白，也不阻断弹窗）。
   */
  //: 本地素材**图片**候选（用户口径 2026-10-16：*「参考图支持选本地或商品原图，
  //: 本地素材均需要支持预览」*）—— 每项 `{url, preview}`：`url` 提交给后端，
  //: `preview` 走 object URL（素材端点要鉴权，`<img src="/api/...">` 恒 401 破图）。
  //: 大图预览（用户口径 2026-10-17：*「还缺少一个大图预览」*）—— 空串 = 不显示。
  const [previewImage, setPreviewImage] = useState("");
  //: 生成记录刷新令牌（每次入队成功 +1 ⇒ 记录列表重拉一次）。
  const [versionRefreshToken, setVersionRefreshToken] = useState(0);
  //: **弹窗顶层页签**（用户口径 2026-10-17：*「生成记录使用新 tab 展示」*）——
  //: 三个页签：图文笔记 / 视频笔记 / 生成记录。
  //: ⚠️ 用**一个** Tabs 而不是「外面一层再套一层」：两级页签在 80vw 弹窗里
  //: 会挤成两条 Tab 栏，且「生成记录」与「图文/视频」是同级的并列选项。
  const [activeTab, setActiveTab] = useState<"image" | "video" | "history">("image");
  //: **本次同步生成的结果**（用户口径 2026-10-18：同步等待）。
  //: 有值 ⇒ 多出「本次结果」页签；它在「生成」完成后自动切过去。
  //: 组件级状态而非全局：关弹窗即丢 —— 历史仍以「生成记录」页签为准。
  const [genResult, setGenResult] = useState<LlmGenerateResult | null>(null);

  /** 有未提交改动 ⇒ 「更新记录」可点。
   *  ⚠️ **与当前值逐字段比对**（2026-10-19 实测：只判 `!== undefined` 时，
   *  改完又改回去、或编辑框失焦但内容没变，按钮照样亮着并写回一次无意义的 PATCH）。 */
  const hasDraft =
    draft != null &&
    ((draft.title !== undefined && draft.title !== (genResult?.title || "")) ||
      (draft.body !== undefined && draft.body !== (genResult?.body || "")) ||
      (draft.topics !== undefined &&
        JSON.stringify(draft.topics) !== JSON.stringify(genResult?.topics || [])));

  /** **这份结果属于哪个形态**（`image` / `video`）—— 回显只在对应页签显示。
   *  用户口径 2026-10-19：*「生成记录是图文笔记，回显展示却在视频笔记中」*。
   *  ⚠️ 不按 `noteForm` 现判：用户切到另一个页签后 `noteForm` 会变，
   *  拿它判会把上一次的结果「搬」到另一个页签里去。 */
  const [genResultForm, setGenResultForm] = useState<NoteForm>("image");
  const [materialImages, setMaterialImages] = useState<
    { url: string; preview: string }[]
  >([]);
  const [detailImages, setDetailImages] = useState<string[]>([]);
  const [detailImagesNote, setDetailImagesNote] = useState("");
  /**
   * **笔记图片总数**（用户口径 2026-10-16：*「笔记图的总数是固定的…配置到 config 表中
   * 支持动态调整」*）—— 由后端下发（`settings` 的 `xhs_note_image_total_limit`），
   * 前端**不写死**：写死 = 改了配置界面还说旧数（两份真相源必分叉，DEF-044）。
   */
  const [imageLimit, setImageLimit] = useState(6);

  // 打开时拉一次模板（首次打开才拉；切商品不重复拉 —— 模板是用户级的，与商品无关）
  useEffect(() => {
    if (!open || templatesLoaded) return;
    void (async () => {
      try {
        const page = await fetchPromptTemplates({ pageSize: 100 });
        setTemplates(page.items);
        //: **自动选中默认模板**（模板已改为必选）：让用户少点一次，
        //: 同时保证「打开即有一个明确生效的模板」，而不是空着等提示。
        //: ⚠️ 只在用户**还没选过**时设（`templateId` 为空），不覆盖其手选。
        const textItems = page.items.filter((t) => t.task_type === "text");
        const preferred = textItems.find((t) => t.is_default) ?? textItems[0];
        if (preferred) {
          setTemplateId((current) => current ?? preferred.id);
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : "提示词模板加载失败");
      } finally {
        setTemplatesLoaded(true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, templatesLoaded]);

  //: **生成计时**（用户口径 2026-10-18「点击生成加蒙版」）—— 蒙版上要显示已用秒数，
  //: 否则同步等几十秒期间界面看起来是死的。
  useEffect(() => {
    if (!busy) {
      setElapsedSec(0);
      return;
    }
    const startedAt = Date.now();
    setElapsedSec(0);
    const timer = window.setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  // 打开即重置（跨商品不串：上一次的提示词 / 素材勾选不能带到下一个商品）
  useEffect(() => {
    if (open) {
      form.resetFields();
      setNoteForm("image");
      setSegments(defaultSegments());
      //: **关闭后重开必须是干净的**（用户口径 2026-10-19：*「关闭弹窗后没有清除数据，
      //: 再次打开依旧是回显的数据」*）—— 上次的结果、形态标记、选中页签、
      //: 文案要求、模板选择、两份形态参数仓**全部清掉**。
      //: 否则：上一个商品的结果会出现在新商品弹窗里（看起来像「新商品已有图」）。
      setGenResult(null);
      setGenResultForm("image");
      setActiveTab("image");
      setCopy(DEFAULT_COPY);
      setTemplateId(undefined);
      setRegenScope(null);
      setPreviewImage("");
      formParamsRef.current.clear();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, itemId]);

  // 参考图候选：**详情图要单独拉**（列表载荷里没有）—— 只给主图组会少一半可选项。
  useEffect(() => {
    if (!open || !accountId || !itemId) return;
    void (async () => {
      try {
        const detail = await fetchXhsShopItemDetail(accountId, itemId);
        setDetailImages(detail.detail_images || []);
        setDetailImagesNote("");
      } catch {
        //: 拉不到不阻断 —— 用户仍可用主图当参考；说清原因而不是静默少一组。
        setDetailImages([]);
        setDetailImagesNote("详情图未取到（可先同步商品）");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, itemId, accountId]);

  // 参考图候选之「本地素材」组：**只取图片**（视频不能当参考图），
  // 每张取一个 object URL 供预览。⚠️ 素材端点要鉴权 ⇒ 必须走 object URL（本仓踩过）。
  useEffect(() => {
    if (!open) return;
    const imgs = materialFiles.filter((f) => f.file_type !== "video");
    if (!imgs.length) {
      setMaterialImages([]);
      return;
    }
    let revoked = false;
    const created: string[] = [];
    void (async () => {
      try {
        const next: { url: string; preview: string }[] = [];
        for (const f of imgs.slice(0, 60)) {
          const objectUrl = await fetchMaterialImageObjectUrl(materialFileUrl(f.id));
          if (revoked) {
            window.URL.revokeObjectURL(objectUrl);
            break;
          }
          created.push(objectUrl);
          next.push({ url: materialFileUrl(f.id), preview: objectUrl });
        }
        if (!revoked) setMaterialImages(next);
      } catch {
        //: 取不到就不给这一组（不阻断商品图那组）；不静默假装有。
        if (!revoked) setMaterialImages([]);
      }
    })();
    return () => {
      revoked = true;
      created.forEach((url) => window.URL.revokeObjectURL(url));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, materialFiles.map((f) => f.id).join(",")]);

  // 切 Tab（形态）即换默认计划（图文 ⇒ 商品原图 1 张；视频 ⇒ 封面 + 视频）——
  // 不保留旧计划的残段：图文里留着视频段 = 界面说图文、实际发视频（形态与媒体不一致）。
  useEffect(() => {
    setSegments(noteForm === "video" ? defaultVideoSegments() : defaultSegments());
  }, [noteForm]);

  // 有素材段时才拉**文件夹清单**（不选就不发这个请求）
  //: ⚠️ 需要素材的段不止 `material`：**`video_material`（选视频素材）** 与
  //: **`video_ai_generate`（参考图可以选本地素材图）** 同样要拉清单
  //: —— 漏掉它们 ⇒ 用户切到「本地素材」时素材清单是空的（看起来像素材库没东西）。
  //: ⚠️ **`ai_generate` 也要算**（2026-10-17 截图实证：参考图来源选「本地素材」时
  //: 文件夹下拉显示「暂无数据」、下方「文件夹下没有图片素材」）。
  //: 根因：`ai_generate` 的**媒体来源**是「智能生成」，但它的**参考图**可以来自
  //: 本地素材 ⇒ 漏掉它就不拉文件夹/素材清单，用户看得见下拉却一个选项都没有
  //: （静默空态，比报错更难查）。
  //: 判据：**凡界面上会出现素材的地方都要算** —— 不是「媒体来源是素材」才算。
  const needsMaterial = segments.some(
    (s) =>
      s.source === "material" ||
      s.source === "ai_generate" ||
      s.source === "video_material" ||
      s.source === "video_ai_generate"
  );
  useEffect(() => {
    if (!open || !needsMaterial || materialFolders.length) return;
    void (async () => {
      try {
        const folders = await fetchMaterialFolders();
        setMaterialFolders(folders);
        //: 默认选中第一个文件夹（用户口径「二级联动指定文件夹」——
        //: 不给默认值的话二级是空的，用户会以为素材库没东西）。
        if (folders.length && materialFolderId === undefined) {
          setMaterialFolderId(folders[0].id);
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : "素材文件夹加载失败");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, needsMaterial, materialFolders.length]);

  // 二级：**切文件夹 ⇒ 重拉该文件夹下的素材**（用户口径 2026-10-16：
  //   *「本地素材库会存在多个文件夹地址，需要用户二级联动指定文件夹，
  //     文件夹下的素材依旧一行展示左右滚动，点击勾选」*）。
  //: ⚠️ **不按 `file_type` 过滤**（同批口径：*「文件夹中可能有图片也可能有视频素材」*）
  //: —— 原先 `.filter(f => f.file_type === "image")` 会把视频**藏掉**，
  //: 用户看得见的文件却选不到（静默丢失）。**全拿回来**，由选择器按类型分开呈现。
  //: ⚠️ **不缓存**（切回同一文件夹也重拉）：用户刚扫描完的文件才会出现。
  useEffect(() => {
    if (!open || !needsMaterial || materialFolderId === undefined) return;
    let cancelled = false;
    void (async () => {
      try {
        //: ⚠️ `page_size` 后端**硬上限 100**（传 200 ⇒ 422 `Input should be less than or equal to 100`）
        //: —— 超上限不是「自动截断」而是**直接报错**（本仓实测）。
        const page = await fetchMaterialFiles({ folderId: materialFolderId, pageSize: 100 });
        if (!cancelled) setMaterialFiles(page.items);
      } catch (error) {
        if (!cancelled) {
          setMaterialFiles([]);
          message.error(error instanceof Error ? error.message : "素材加载失败");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, needsMaterial, materialFolderId]);

  // 笔记图片总数：**后端下发**（改了配置界面跟着变；写死 = 两份真相源，DEF-044）。
  useEffect(() => {
    if (!open) return;
    void (async () => {
      try {
        const page = await fetchSystemSettings();
        const row = page.items.find((s) => s.key === "xhs_note_image_total_limit");
        const value = Number(row?.value);
        if (Number.isFinite(value) && value >= 1) setImageLimit(value);
      } catch {
        //: 读不到就用默认 6（不阻断生成；后端仍有总闸兜底）。
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /**
   * **智能生成段的可填上限** = 图片总数 − 其他段已占张数（**最低 1**）。
   *
   * ⚠️ 判据落在「其他段」= **除本段以外**的所有图片段（本段自己的旧值不算占用，
   * 否则改张数时会自我打架：填 3 之后上限只剩 3，再想填 4 就填不进去）。
   */
  //: ⚠️ **智能生成的图不预先占位**（用户口径 2026-10-19：*「大模型生成的图不一定全部可以用，
  //: 生成后才可以供用户选择，所以它的生成张数不应该算该片笔记的图片总数，应是生成后选择的
  //: 图片算该片笔记的图片总数」*）—— 生成张数只是「备选池大小」，8 张里用户可能只留 2 张；
  //: 预先按 8 扣减会让「还能放几张」凭一个还没发生的选择就变少（用户自己的话：不该算）。
  //: ⇒ 此处只统计**非 AI 段**的占用。
  /** **AI 备选池**：默认 3 张、最少 1、最多 6（用户口径 2026-10-19）——
   *  生成多少张与「这篇笔记用几张」是两件事：先出一批备选，再由用户挑。 */
  const AI_POOL_DEFAULT = 3;
  const AI_POOL_MAX = 6;

  const aiCountCap = (() => {
    const others = segments
      .filter((s) => s.source !== "ai_generate")
      .reduce((sum, s) => sum + (s.pick?.length || s.count || 1), 0);
    return Math.max(0, imageLimit - others);
  })();

  /**
   * **本段可再选几张商品图**（全局余量）。
   *
   * 用户口径 2026-10-19：*「最后选择的图数量超过了最大限制」* ——
   * 原来每段各自按 `imageLimit` 封顶，两段各选 6 张就合计 12 张，
   * 汇总后直接超笔记图片上限（平台单篇 18，配置默认 6）。
   * 这里按**全局已占**算余量：本段上限 = 总数 − 其它段已选（封面固定 1）。
   */
  /** **按来源切出这一段产出的图**（智能生成段右侧并排展示用）。
   *  不按下标切 —— 用户口径 2026-10-19：*「不要按照下标回显，按照图片名称」*；
   *  后端每条媒体都带 `source`，按它归段不会串。 */
  const segmentMediaFor = (source: string) => {
    if (!genResult || genResultForm !== noteForm) return [];
    return (genResult.media || []).filter((m) => (m.source || "product_image") === source);
  };

  /** 第 `index` 段的失败原因（`renderSegments` 已按来源归好）。 */
  const rowFailure = (index: number): string =>
    (renderSegments[index] as { failure?: string } | undefined)?.failure || "";

  const pickCapFor = (index: number): number => {
    const others = segments.reduce((sum, seg, i) => {
      if (i === index) return sum;
      //: ⚠️ **封面段不占图片总数**（用户口径 2026-10-19：*「封面不算在图片总数中」*）——
      //: 封面是笔记的第 1 张、也是列表里展示的那张，但「内页还可放几张」
      //: 不该被它挤掉：总数 6 就是 6 张内页，加封面共 7 张图。
      if (i === 0) return sum;
      //: ⚠️ **智能生成段按「生成后被选中的张数」占位**，不按生成张数
      //: （用户口径 2026-10-19：生成 8 张不一定全用，选了 2 张就只算 2 张）。
      //: 没选 = 0 占位 —— 此时它还没进这篇笔记。
      //: 商品图 / 素材：按已选张数；没选时按 1（它们不选就是「默认取前 N」）。
      if (seg.source === "ai_generate") {
        return sum + (seg.pick?.length || 0);
      }
      const used = seg.pick?.length ? seg.pick.length : Number(seg.count) || 1;
      return sum + used;
    }, 0);
    return Math.max(0, imageLimit - others);
  };

  /** **把当前这一版直接发布**。 */
  async function handlePublish() {
    const recordId = genResult?.record_id;
    if (!recordId) {
      message.warning("请先生成一篇笔记");
      return;
    }
    setPublishing(true);
    try {
      //: **同步**（用户口径 2026-10-19：*「发布笔记按钮改为同步按钮，发布成功关闭弹窗，
      //: 失败则异常提示」*）—— 后端等发布落终态才返回：成功 ⇒ 关弹窗；
      //: 失败 / 超时 ⇒ 抛错，走下面的 catch 弹平台原文（不假装成功）。
      const result = await publishNoteVersion(recordId);
      message.success(
        result.platform_note_id
          ? `发布成功（笔记 ID ${result.platform_note_id}）`
          : "发布成功"
      );
      onClose();
    } catch (error) {
      const detail = detailMessage(
        (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
      );
      message.error(detail || (error instanceof Error ? error.message : "发布失败"));
    } finally {
      setPublishing(false);
    }
  }

  /** 默认模板（同任务类型只有一个，后端保证）。 */
  const defaultTemplateId = useMemo(
    () => templates.find((t) => t.is_default && t.task_type === "text")?.id,
    [templates]
  );

  /** 当前形态可选的来源（问题 6：视频来源不出现在图文笔记里，反之亦然）。 */
  const sourceOptions = useMemo(
    () => MEDIA_SOURCE_OPTIONS.filter((o) => o.form === noteForm),
    [noteForm]
  );

  function patchSegment(index: number, patch: Partial<LlmMediaPlanSegment>) {
    setSegments((prev) => prev.map((seg, i) => (i === index ? { ...seg, ...patch } : seg)));
  }

  function addSegment() {
    // 新增段一律 `content`（封面唯一且恒排首位 —— 后端 R1 会校验，这里就不给错的入口）
    setSegments((prev) => [...prev, { source: "product_image", role: "content" }]);
  }

  /** 改文案要求的**单项**（其余保持不变）。 */
  function setCopyField<K extends keyof LlmCopyRequirements>(
    key: K,
    value: Required<LlmCopyRequirements>[K]
  ) {
    setCopy((prev) => ({ ...prev, [key]: value }));
  }

  /** 基于某版本**局部重生成**（用户口径 2026-10-17）。 */
  async function handleRegenFromVersion(
    record: { id: number; version_no: number },
    scope: "text" | "media"
  ) {
    if (!accountId) return;
    try {
      await regenNoteVersion({
        baselineRecordId: record.id,
        regenScope: scope,
        //: 只重写文案时把**当前界面选的文案要求**带上（用户可能就想换一组区间试试）；
        //: 只重配图片时不带 —— 文案要求与这次动作无关，带了反而覆盖基线快照。
        ...(scope === "text" ? { copyRequirements: copy } : {}),
      });
      message.success(
        scope === "text"
          ? `已基于第 ${record.version_no} 版重写文案（后台执行）`
          : `已基于第 ${record.version_no} 版重配图片（后台执行）`
      );
      setVersionRefreshToken((v) => v + 1);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重生成失败");
    }
  }


  /**
   * **从「本次结果」局部重生成**（用户口径 2026-10-18：*「点击本次结果中我想再次生成
   * 一个正文或标题或话题或配图」*）。
   *
   * ✅ **逐块重生成**（用户口径 2026-10-18：*「后端应支持每个文案区域单独重新生成，
   * 或图片区域单独重新生成」*）—— 后端已新增 `title` / `body` / `topics` 三个细粒度
   * scope：点「正文」只换正文，标题与话题**一个字都不变**（合并逻辑在 worker 里，
   * 见 `_merge_text_scope`）。这里**直传 scope**，不再映射成一个笼统的 `text`。
   */
  /**
   * **采用某版 ⇒ 把它展示到「本次结果」Tab**（用户口径 2026-10-19：
   * *「点击采用使用在本次结果 tab 中展示该记录生成的内容」*）。
   *
   * 语义上「采用」= 我就用这一版 ⇒ 紧接着要看/要改的就是它，
   * 所以这里**顺手切到「本次结果」**，用户不用再自己去找。
   * 之后在这个面板里点「重新生成某一块」，改的也是这一版（`record_id` 已换）。
   */
  /** **取消采用**：弹窗与这一版解绑 —— 结果区收起、表单回默认。
   *  用户口径 2026-10-19：*「采用回显只在弹窗中有效，应新增一个取消采用」*。 */
  function handleUnadopt() {
    //: **取消采用 ⇒ 当前页签回到初始状态**（用户口径 2026-10-19：*「点击取消采用则对应的
    //: tab 恢复到初始状态」*）—— 采用时把这一版的提示词/配置/配图勾选都回显了，
    //: 取消采用必须把它们**一并清掉**，否则界面留着上一版的痕迹、但已经没有对应结果，
    //: 用户会以为「取消了但数据还在」。
    //: ⚠️ 只重置**当前形态**那一套（用户口径 2026-10-18：*「图文和视频的数据各自保存
    //: 不清理」*）—— 取消采用不是「清空全部」，另一边的参数不该被连坐。
    //: ⚠️ **每项都要给新对象**（2026-10-19 实测坑）：`DEFAULT_COPY` 是模块级常量，
    //: 传同一个引用时 React 判定「没变化」⇒ 界面看着没重置（用户报「没有恢复初始化」）。
    //: 展开一层即可拿到新引用，字段值仍等同默认。
    const freshCopy = { ...DEFAULT_COPY };
    const freshSegments = defaultSegments();
    setGenResult(null);
    setDraft(null);                  // 未提交的编辑也一并丢弃
    setTemplateId(undefined);        // 回默认模板（模板加载 effect 会自动选 is_default）
    setCopy(freshCopy);              // 文案要求回默认
    setSegments(freshSegments);      // 配图段回默认（来源 / 勾选 / 张数全清）
    //: **存档槽一起清**：否则切走再切回来，旧参数从槽里又冒出来（等于没重置）
    formParamsRef.current.set(noteForm, {
      templateId: undefined,
      copy: { ...DEFAULT_COPY },
      segments: defaultSegments(),
    });
    //: **切回内容页签**（用户口径 2026-10-19：*「点击取消采用则对应的 tab 恢复到初始状态」*）
    //: —— 用户是在「生成记录」页签点取消的，不切回去就只能看着表单之外的地方，
    //: 看不到它已重置，会误以为没生效。
    setActiveTab(noteForm);
    //: 明确回执：让「确实点到了」这件事可见（否则用户分不清「没重置」与「没点中」）
    message.success("已取消采用，当前页签已回到初始状态");
  }

  function handleShowInResult(detail: {
    id: number;
    version_no: number;
    title: string;
    body: string;
    topics: string[];
    media: { url: string; local_path?: string; sort_order?: number; asset_type?: string; ok?: boolean; error?: string; role?: string; source?: string }[];
    copy_requirements: Record<string, unknown>;
    model_name: string;
    is_adopted: boolean;
    body_length: number;
    /** 这一版当时用的模板（回显到「提示词模板」下拉）。 */
    template_id?: number | null;
    /** 这一版的形态（`image` / `video`）—— 回显据此放进对应页签。 */
    note_form?: "image" | "video";
  }) {
    setGenResult({
      ok: true,
      platform_account_id: accountId,
      item_id: itemId,
      record_id: detail.id,
      note_id: "",
      title: detail.title || "",
      body: detail.body || "",
      topics: detail.topics || [],
      media: detail.media || [],
      copy_requirements: detail.copy_requirements || {},
      model_name: detail.model_name || "",
      version_no: detail.version_no,
      is_adopted: detail.is_adopted,
      body_length: detail.body_length,
      note_form: detail.note_form ?? "image",
    } as LlmGenerateResult);
    setGenResultForm(detail.note_form ?? "image");
    //: **提示词与配置也回显**（用户口径 2026-10-19：*「回显提示词，配置均要回显」*）——
    //: 采用某一版 = 接着用它的写法 ⇒ 表单要还原成「当时那一套」，
    //: 否则用户看到结果、但上面的提示词还是别的版本留下的，改完重生会跑偏。
    if (detail.copy_requirements && Object.keys(detail.copy_requirements).length) {
      setCopy({ ...DEFAULT_COPY, ...(detail.copy_requirements as Partial<typeof DEFAULT_COPY>) });
    }
    if (detail.template_id) setTemplateId(detail.template_id);

    //: ⚠️ **配图勾选与段结构都要跟着切**（2026-10-19 两轮实测 bug）：
    //:   ① 第一轮：只切了结果区，配置区勾选还停在上一个版本；
    //:   ② 第二轮：按 `source` 归段 ⇒ **同一来源的图全挤进封面**（第 26 版有
    //:      封面 + 2 个内页段，全是 `product_image`，按来源分不出来）。
    //: 正解：后端每条媒体都记了 `slot_index`（生成时它属于第几段）——
    //: **按段位还原**，段数也按它重建（用户选了几段就有几段）。
    //: 段内仍按**图片名称**记 `pick`（URL 末段，与手动勾选同一套判据，不按下标）。
    const bySlot = new Map<number, { source: string; role: string; names: string[] }>();
    for (const item of detail.media || []) {
      if (item.ok === false) continue;
      const raw = item.url || item.local_path || "";
      if (!raw) continue;
      const slot = Number(
        (item as { slot_index?: number }).slot_index ?? Number.NaN
      );
      if (!Number.isFinite(slot)) continue;
      const bucket = bySlot.get(slot) || {
        source: item.source || "product_image",
        role: item.role || (slot === 0 ? "cover" : "content"),
        names: [],
      };
      //: ⚠️ **素材段的标识是 `file_id`，不是文件名的末段**（2026-10-19 实测）：
      //: 素材 URL 形如 `/api/xhs/materials/files/6/file`，`productImageName` 会取到
      //: 最后一个路径段 `"file"` —— 每张素材都叫 "file"，候选图按 `file_ids` 勾选，
      //: 两边判据不同 ⇒ **素材段永远勾不上**（用户看到「未回显」）。
      //: 素材候选的选中态在 `seg.file_ids` 里，所以这里存 `file_id` 本身。
      const isMaterial = (item.source || "") === "material";
      const fileId = isMaterial ? Number(raw.match(/\/files\/(\d+)\/file/)?.[1] ?? "") : NaN;
      bucket.names.push(
        Number.isFinite(fileId) ? String(fileId) : productImageName(raw)
      );
      bySlot.set(slot, bucket);
    }
    if (bySlot.size) {
      const ordered = [...bySlot.entries()].sort((a, b) => a[0] - b[0]);
      setSegments((prev) => {
        const next = [...prev];
        ordered.forEach(([slot, bucket], position) => {
          //: 段位存在就用它（用户当时配了几段就是几段）；段位超出当前配置
          //: （上一版段更多）才补一段 —— 补的那段只带来源与勾选，
          //: 其余参数（素材文件夹 / 视频模板 / 提示词）本来就不在这条记录里。
          const current = next[position];
          if (current) {
            //: ⚠️ **来源也要按记录覆盖**（2026-10-19 实测 bug）：只换 `pick` 不换
            //: `source` 时，界面第 3 段还是「本地素材」而这一版第 3 段是「商品原图」
            //: ⇒ 名称在素材候选里找不到 ⇒ **一张都勾不上**（用户看到「未回显」）。
            //: 素材 / 视频段的参数（`file_ids` / `template_id` / `prompt`）这一版
            //: 没记录，来源一换就清掉 —— 留着会把别的来源的残留带进来。
            const sameSource = current.source === bucket.source;
            //: ⚠️ **素材段用 `file_ids`、商品图段用 `pick`**（两个字段两套判据）：
            //: 素材候选图按 `file_ids` 显示选中态，写进 `pick` 等于没写。
            const isMaterial = bucket.source === "material";
            next[position] = {
              ...(sameSource
                ? current
                : ({ role: current.role } as (typeof next)[number])),
              source: bucket.source as LlmMediaPlanSegment["source"],
              role: (slot === 0 ? "cover" : "content") as "cover" | "content",
              ...(isMaterial
                ? { file_ids: bucket.names.map(Number).filter((n) => n > 0), pick: [] }
                : { pick: [...bucket.names] }),
            } as (typeof next)[number];
          } else {
            next.push({
              source: bucket.source as LlmMediaPlanSegment["source"],
              role: (slot === 0 ? "cover" : "content") as "cover" | "content",
              pick: [...bucket.names],
            } as (typeof next)[number]);
          }
        });
        //: 这一版**没用到**的多余段 ⇒ 去掉（否则界面上还挂着上一版的空段，
        //: 用户会以为这一版也有那些内页）
        return next.slice(0, ordered.length);
      });
    }

    setActiveTab(noteForm);
  }

  /**
   * **就地保存文案修改**（用户口径 2026-10-19：*「文案也使用回显的方式」*）。
   *
   * 只把改过的那块发给后端（`undefined` = 不动），后端 `PATCH /versions/{id}`
   * 直接写这一行：**不调模型、不花钱、版本号不变**。
   * 存成功后同步更新面板，让「回显」始终等于「当前生效值」。
   */
  async function handleUpdateCopy(patch: {
    title?: string;
    body?: string;
    topics?: string[];
  }) {
    if (!genResult) return;
    try {
      const updated = await updateNoteVersion({
        recordId: genResult.record_id,
        ...patch,
      });
      setGenResult((prev) =>
        prev
          ? {
              ...prev,
              title: updated.title ?? prev.title,
              body: updated.body ?? prev.body,
              topics: updated.topics ?? prev.topics,
            }
          : prev
      );
      //: 生成记录列表里的标题摘要 / 字数也要跟着变
      setVersionRefreshToken((v) => v + 1);
      message.success("已保存");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    }
  }

  async function handleRegenFromResult(scope: "title" | "body" | "topics" | "media") {
    if (!genResult) return;
    const apiScope = scope;
    setRegenScope(scope);
    try {
      //: **同步等待 + 直接回填**（用户口径 2026-10-18：*「应同步接口直接回显新内容
      //: 覆盖历史的」*）—— 后端 `/versions/regen` 已改成本请求线程跑完才返回，
      //: 拿到的就是这一版的新内容，**就地覆盖面板**，不用再等版本列表刷新。
      const updated = await regenNoteVersion({
        baselineRecordId: genResult.record_id,
        regenScope: apiScope,
        ...(apiScope === "media" ? {} : { copyRequirements: copy }),
      });
      //: **只回填用户点的那一块**（用户口径 2026-10-18：*「正常点击重新生成对应部分
      //: 的内容」*）—— 面板上其余三块**一个字都不动**。
      //:
      //: ⚠️ 为什么不再「整包回填」：后端返回的是这一版**库里全文**，它与面板当前显示
      //: 的内容可能来自不同时刻（面板是上次生成时的快照）。整包覆盖会让用户看到
      //: 「只点了标题，正文也变了」—— 即使库里正文其实没动（实测 md5 一致）。
      //: 按块回填 = 界面所见即所点，不引入无关变化。
      setGenResult((prev) => {
        if (!prev) return prev;
        const next = { ...prev };
        if (apiScope === "title" && updated.title) next.title = updated.title;
        else if (apiScope === "body" && updated.body) next.body = updated.body;
        else if (apiScope === "topics" && updated.topics?.length)
          next.topics = updated.topics;
        else if (apiScope === "media" && updated.media?.length) next.media = updated.media;
        //: 版本元信息跟着这一版走（版本号不变，原地更新）。
        next.version_no = updated.version_no ?? prev.version_no;
        next.model_name = updated.model_name || prev.model_name;
        return next;
      });
      //: 版本列表也刷一次（列表里的「标题摘要 / 字数」要跟着变）。
      setVersionRefreshToken((v) => v + 1);
      message.success(`已重新生成${scopeCn(apiScope)}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重生成失败");
    } finally {
      setRegenScope(null);
    }
  }

  function removeSegment(index: number) {
    // 封面（第 0 段）不可删：无封面 = 整篇失败，前端不给这个死路
    if (index === 0) return;
    setSegments((prev) => prev.filter((_, i) => i !== index));
  }

  /** 提交前轻校验（业务判据仍在后端；这里只拦「明显填不全」的）。 */
  function localProblem(): string {
    //: **模板必选**（用户口径 2026-10-18）—— 没选就没法确定用哪套写法。
    if (!templateId) return "请先选择提示词模板";
    if (!segments.length) return "至少要有封面";
    if (segments[0].role !== "cover") return "第一段必须是封面";
    for (const [index, seg] of segments.entries()) {
      if (seg.source === "material" && !seg.file_ids?.length) {
        return `第 ${index + 1} 段（本地素材）还没选图片`;
      }
      //: 「本地素材」用视频（`video_material`）时选的是**视频素材**，判据同上但文案不同。
      if (seg.source === "video_material" && !seg.file_ids?.length) {
        return `第 ${index + 1} 段（本地素材）还没选视频`;
      }
    }
    if (noteForm === "video" && !segments.some((s) => SOURCES_WITH_PARAMS.includes(s.source) && s.source.startsWith("video"))) {
      return "视频笔记需要一个视频来源";
    }
    return "";
  }

  async function submit() {
    //: ⚠️ **采用状态（还有回显内容）下不允许生成**（用户口径 2026-10-19：
    //: *「采用中说明有回显内容不支持点击生成按钮」*）——
    //: 此时面板显示的是**某一版的既有内容**，直接生成会在它的基础上又产出一版，
    //: 用户分不清「我是在改这一版还是在新建」。要生成就先「取消采用」回到空白起点。
    if (genResult?.record_id) {
      message.warning("当前是采用的版本（有回显内容），请先点「取消采用」再生成");
      return;
    }
    if (!accountId) return;
    const problem = localProblem();
    if (problem) {
      message.error(problem);
      return;
    }
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      // 媒体按段组装：`media_plan` 是**唯一**入口（后端以它为准，见 `build_plan` 注释）。
      const mediaPlan: LlmMediaPlanSegment[] = segments.map((seg) => {
        const base: LlmMediaPlanSegment = { source: seg.source, role: seg.role };
        if (seg.source === "material") {
          if (seg.file_ids?.length) base.file_ids = seg.file_ids;
        } else if (seg.source === "product_image") {
          //: 用户选了哪几张商品图（下标）**必须下发** —— 不传 = 后端取前 6 张，
          //: 用户的选择就白做了（本仓最厌恶的「静默失效」）。
          if (seg.pick?.length) base.pick = seg.pick;
        } else if (seg.source === "ai_generate") {
          if (seg.prompt) base.prompt = seg.prompt;
          //: ⚠️ **`count` 必须下发**（2026-10-19 实测）：原来 `if (seg.count)` 在
          //: `count` 为 `undefined` 时跳过 —— 界面上「生成张数」显示 1（那是组件
          //: 的默认展示），但后端收到没这个键 ⇒ 该段一张都不出，
          //: 用户看到的是「智能生成没生成图」，且没有任何原因。
          base.count = Math.max(1, Number(seg.count) || 1);
          //: 参考图：用户勾选的是 **URL**（两组混选），直接下发 —— 后端本来就按
          //: `reference_image_urls` 收（`media_generation` 的 `_str_list` 读它）。
          //: 不勾 = 纯文生图（不下发该键，语义干净）。
          if (seg.reference_urls?.length) base.reference_image_urls = seg.reference_urls;
        } else if (seg.source === "video_material") {
          //: 本地素材视频：**选中的视频素材 id**（后端按 id 取，不前端拼 URL）。
          if (seg.file_ids?.length) base.file_ids = seg.file_ids;
          if (seg.template_id) base.template_id = seg.template_id;
          if (seg.prompt) base.prompt = seg.prompt;
        } else if (seg.source === "video_ai_generate" || seg.source === "video_reference") {
          if (seg.template_id) base.template_id = seg.template_id;
          if (seg.prompt) base.prompt = seg.prompt;
          if (seg.duration) base.duration = seg.duration;
          //: 参考图（用户口径：*「智能生成视频需支持选择参考图…最多一共4张」*）——
          //: 与图片的「智能生成」同一通道（`reference_image_urls`）。
          if (seg.reference_urls?.length) base.reference_image_urls = seg.reference_urls;
        }
        return base;
      });

      //: **同步等待**（用户口径 2026-10-18：*「不要放入队列中直接同步等待」*）。
      //: 后端跑完才返回，这里直接拿到标题/正文/话题/配图 —— 不再有「入队→轮询」那一段。
      if (!hasPermission("xhs:item:generateNote")) {
        message.warning("没有LLM 生成笔记的权限");
        return;
      }
      const result = await generateLlmNote({
        platformAccountId: accountId,
        itemId,
        templateId,
        //: ⚠️ **不下发 `modelName`**（用户口径 2026-10-16：*「不允许随意设置模型名」*）——
        //: 模型由后端**业务点映射**决定，弹窗不参与（原先这里会把用户手填的模型名传下去，
        //: 等于给映射开了个后门：写个没配的模型名运行期才 502）。
        mediaPlan,
        //: **文案要求**（用户口径 2026-10-17）：三块提示词 + 区间 + 表情 + 话题数量。
        //: 后端在**同步段**规整（区间倒挂等当场 400，不跑就拒绝）。
        copyRequirements: copy,
      });

      //: 拿到结果就地展示（**不关弹窗**）—— 用户要在这里继续「这段重写 / 这图换一张」。
      //: ⚠️ 同时记下**这一版属于哪个形态**（用户口径 2026-10-19：*「生成记录是图文笔记，
      //: 回显展示却在视频笔记中」*）—— 回显只在对应页签出现，不会跨形态显示。
      setGenResult(result);
      //: **形态以后端返回为准**（`note_form`）；缺失时才回落到当前页签。
      setGenResultForm(result.note_form ?? noteForm);
      //: **把本版实际用到的图写回配置区的勾选态**（用户口径 2026-10-19：
      //: *「在配置区域勾选本次的图片智能生成中新一个生成图的预览」*）——
      //: 这样候选缩略图里会直接标出「这一版用了哪几张」，
      //: 用户改配置时能看到起点是什么，而不是从空白开始猜。
      //: ⚠️ 只回填第 1 段（封面）能对上的 `product_image`：其它来源
      //: （智能生成 / 素材）没有「候选 = 成品」的对应关系，硬塞会把配置改乱。
      const coverNames = result.media
        .slice(0, 1)
        .filter((m) => m.ok !== false && (m.url || m.local_path))
        .map((m) => productImageName(m.url || m.local_path || ""))
        .filter(Boolean);
      if (coverNames.length) {
        patchSegment(0, { pick: coverNames });
      }
      //: 结果内嵌在当前形态页签 ⇒ 切回该页签（用户口径 2026-10-19）
      setActiveTab(noteForm);
      setVersionRefreshToken((v) => v + 1);
      onQueued?.(itemId);
      message.success(`第 ${result.version_no} 版生成完成`);
    } catch (error) {
      // 表单校验失败不是 Error（antd 已在字段下渲染原因）
      if (error instanceof Error) {
        //: ⚠️ **弹后端给的真原因，不弹 axios 的通用文案**（用户口径 2026-10-19：
        //: 点生成只看到 *「Request failed with status code 400」* —— 那是 axios 的
        //: `error.message`，后端 400 的结构化 `detail.message`（「组合图片总数 9
        //: 超过上限 6」这类**能直接改**的话）被丢掉了，用户只能猜）。
        const detail = detailMessage(
          (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
        );
        message.error(detail || error.message);
      }
    } finally {
      setSubmitting(false);
    }
  }

  /**
   * **把成品媒体按段分配**（用户口径 2026-10-19：*「不是在单独区域展示」*）。
   *
   * 每一段编辑卡下面只回显**这一段产出的**那几张：
   *   · 第 1 段（封面）⇒ 第 1 项（封面就是图序第一张）
   *   · 第 2 段起的图片段 ⇒ 按 `count` 依次切
   *   · 视频段 ⇒ 该项 `asset_type === "video"`
   *
   * ⚠️ 按 `count` 切而不是按 `source` 猜：素材段 / 商品图段可能一条 `count`
   * 就是多张（`product_image` 的 `pick` 可以选多张），按 source 归组会把
   * 后一段的图串到前一段去。
   */
  /**
   * **本版配图按图序回显**（用户口径 2026-10-19 定案：*「你来决定」* ⇒ 方案 C）。
   *
   * 回显**不再按界面当前配了几段来切**：那一版是按**当时**的配置生成的，
   * 之后改了配置，段数就对不上 —— 界面上只剩 1 段（封面）时，
   * 4 张图会全挤在封面段下，或者干脆只显示第 1 张。
   *
   * 这里改成**按图序说话**：
   *   · 第 1 张 = 封面
   *   · 第 2 张起 = 内页 1、内页 2 …（一段只放一张，图序一眼可读）
   * 段数由**这一版实际有几张图**决定，不由当前配置决定。
   * 配置区（下面那套段选择器）保持原样，只负责「下一次生成用什么」。
   */
  /**
   * **渲染用的段列表 + 每段要回显的成品图**（用户口径 2026-10-19：
   * *「回显必须配置回去的内页中回显，不要单独展示」*）。
   *
   * ## 为什么要「自动补段」
   *
   * 生成时可能**一段出了多张**（封面段 `product_image` 就能一次给 4 张），
   * 界面里却只有 1 段。若回显只按现有段切，后 3 张无处安放；
   * 若另开一块展示，又变成「单独展示」。
   *
   * 所以这里把段列表**补齐到够放这一版所有图**：
   *   · 第 1 张 → 封面段
   *   · 第 2 张起 → 内页 1 / 内页 2 …（每段一张）
   * 补出来的段**只用于回显**（`echoOnly: true`），
   * 用户没为它们配来源 ⇒ 配置区显示「未配置」，点「重新生成」也不会差遣它们。
   *
   * ## 回显落在段内
   *
   * 每段 `echo` 只放**这一段的成品图**，没有就什么都不显示（不占位）。
   */
  /**
   * **渲染用的段列表**（用户口径 2026-10-19：*「两个内页之间的图不能关联」*）。
   *
   * ## 判据：名称 **且** 该名称属于**这一段自己**的 `pick`
   *
   * 之前只按「名称 → 段」建**全局映射**，两个内页都选了同一张图时
   * 映射只剩最后一段，于是「点内页 2 的第 1 张，内页 1 的同名图也被勾上」。
   *
   * 正确判据是：**每段只看自己的 `pick` 列表**。
   * 同一个名称可以在多段里各选一次（这是允许的：两段都用同一张图），
   * 但**勾选态按段独立** —— 内页 2 勾上的，不会让内页 1 跟着勾。
   *
   * ⚠️ 勾选用**位置（段号 + 名称）**双重限定，不用全局名称表。
   */
  const renderSegments = useMemo(() => {
    const rows = segments.map((seg) => ({
      seg,
      //: **这一段自己的勾选**（原样回显用户的选择，不跨段传播）
      picked: [...(seg.pick || [])],
      //: **这一段的失败信息**（用户口径 2026-10-19：*「如果生成失败则展示一个失败占位图，
      //: tip 显示失败原因」*）—— 后端逐段隔离失败，`media_json` 里 `ok=false`
      //: 的条目带 `error`，按**来源**归回它那一段。
      failure: "",
    }));
    if (!genResult || genResultForm !== noteForm) return rows;
    for (const item of genResult.media) {
      if (item.ok !== false) continue;
      //: ⚠️ 失败段**缺 `source` 时归给智能生成段**（2026-10-19 实测：老记录
      //: `source=None`，按空串找不到任何段 ⇒ 占位图不出现，用户看到的是
      //: 「生成后显示」，像是根本没跑过。智能生成是唯一会「生成出 0 张」的来源：
      //: 商品图/素材选空了是用户自己没选，不该报失败）。
      const source = item.source || "ai_generate";
      const target = rows.findIndex((row) => row.seg.source === source);
      if (target >= 0) {
        rows[target].failure = item.error || "生成失败（未返回原因）";
      }
    }
    return rows;
  }, [genResult, genResultForm, noteForm, segments]);

  const videoTemplates = useMemo(
    () => templates.filter((t) => t.task_type === "video"),
    [templates]
  );

  return (
    <Modal
      centered
      open={open}
      title={`LLM 生成笔记 · ${itemTitle || itemId}`}
      //: 用户口径（2026-10-16）：**全屏的 80%**（视口百分比，不用固定像素）。
      //: ⚠️ 原口径是 70%（同日），**已被 80% 取代**（用户追加：*「弹窗扩大到80%」*）。
      width="80vw"
      //: ⚠️ **不能同时给 `style.top`** —— antd 的 `centered` 靠 `top:50%` 实现，
      //: 显式 top 会把它顶掉（本仓被全站守卫抓过「假居中」）。
      styles={{
        body: {
          height: "72vh",
          maxHeight: "72vh",
          overflowY: "auto",
          //: 藏滚动条外观，保留滚动能力（沿用全站口径）。
          scrollbarWidth: "none",
          msOverflowStyle: "none",
        },
      }}
      className="hermes-hide-scrollbar"
      //: **生成中禁止关闭**：同步请求已经发出，关掉弹窗也停不了它，
      //: 只会让用户以为「取消了」而结果无人接（同「结果弹窗执行中禁止关闭」口径）。
      maskClosable={false}
      closable={!busy}
      onCancel={busy ? undefined : onClose}
      footer={
        <Space>
          <Button onClick={onClose}>取消</Button>
          {/* **直接发布**（用户口径 2026-10-19：*「生成用户满意的笔记后可以直接发布」*）——
              只有拿到生成结果才可点（没有结果就没有可发的内容）；
              发布是**异步**的：点完立即返回「发布中」，终态去内容库看（同一套发布链路，
              不另造一条，也不跳过 ark 令牌 / 媒体 / 已发布这些门禁）。 */}
          <Button
            disabled={!genResult?.record_id || submitting || publishing}
            onClick={() => void handlePublish()}
          >
            发布笔记
          </Button>
          {/* **更新记录**（用户口径 2026-10-19：*「更新记录按钮不可以置灰，每次点击只保存
              更新最新内容」*）—— 与取消 / 发布 / 生成并列在弹窗底部；
              **有记录就可点**（不按有无改动置灰）：点了就把面板上的当前内容整体写回这一版，
              没有改动时写回的内容与库里一致（幂等，不产生副作用）。 */}
          <Button
            disabled={!genResult?.record_id || publishing}
            onClick={() => void handleSaveDraft()}
          >
            更新记录
          </Button>
          <Tooltip
            title={
              genResult?.record_id
                ? "当前是采用的版本（有回显内容），请先点「取消采用」再生成"
                : ""
            }
          >
            <Button
              type="primary"
              disabled={publishing}
              onClick={() => void submit()}
            >
              生成
            </Button>
          </Tooltip>
        </Space>
      }
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        {templatesLoaded && templates.length === 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            还没有提示词模板（可在「笔记工坊 → 提示词模板」新建，留空则用默认骨架）
          </Text>
        )}

        {/* **两个大类 Tab**（用户口径 2026-10-16）—— 图文 / 视频各一套参数。
            ⚠️ 只切**参数界面**，后端链路公用一条（形态仍由 `media_plan` 派生）。
            ⚠️ 形态说明**并进 Tab 标签下方一行**（口径 C2/C3：同一信息不重复、
            提示不占独立区块 —— 原写法是一条独立的 `Alert`，白占两行高度）。 */}
        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            const next = key as "image" | "video" | "history";
            setActiveTab(next);
            //: 切到图文/视频时**同步笔记形态**（后端按 `note_form` 派生，界面必须一致）
            //: 只有 image / video 是笔记形态；history / result 页签不改形态
            //: （写进去会把 noteForm 变成非法值）。
            if (next === "image" || next === "video") switchForm(next);
          }}
          items={[
            { key: "image", label: "图文笔记" },
            { key: "video", label: "视频笔记" },
            {
              key: "history",
              label: "生成记录",
              //: 记录页的内容**在这个 Tab 里渲染**（不是弹窗底部另开一块）
              children: accountId && itemId ? (
                <NoteVersionHistory
                  platformAccountId={accountId}
                  itemId={itemId}
                  refreshToken={versionRefreshToken}
                  onRegen={handleRegenFromVersion}
                  onShowInResult={handleShowInResult}
                  onUnadopt={handleUnadopt}
                  //: **采用态以父组件为准**：切页签会重挂载表格，内部 state 会丢，
                  //: 按钮就变回「采用」、用户点不到「取消」（2026-10-19 实测）。
                  adoptedRecordId={genResult?.record_id ?? null}
                />
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  缺少商品信息，无法查询生成记录
                </Text>
              ),
            },
            //: ⚠️ **独立的「本次结果」页签已取消**（用户口径 2026-10-19：
            //: *「将本地结果 tab 合并到对应的图文笔记或视频笔记中，不再单独展示一个
            //: 本次结果页」*）—— 结果直接显示在**当前形态页签内**（见下方
            //: `activeTab === noteForm && genResult` 那一块），
            //: 用户在一个页面里就能「看结果 + 改参数 + 重生成」。
          ]}
          tabBarExtraContent={
            <Text type="secondary" style={{ fontSize: 12 }}>
              {activeTab === "history"
                ? "仅显示该商品的生成记录"
                : noteForm === "video"
                  ? "1 个视频 + 1 张独立封面"
                  : "1–18 张图，第 1 张即封面"}
            </Text>
          }
        />

        {/* ⚠️ 以下「文案 / 配图 / 提交提示」三块**只在图文/视频页签显示**
            （`activeTab === "history"` 时整个隐藏 —— 记录页不该出现生成参数，
            否则用户会以为能在历史页直接点生成）。 */}
        {(activeTab === "image" || activeTab === "video") && (
          <>
        {/* 文案 —— 主体（卡片区域）
            ⚠️ 用户口径 2026-10-17：*「文案区域支持用户手动输入标题提示词和笔记正文，以及
            笔记话题，分析支持提供各自的提示词，也可以为空，设置一个各自的文字数区间和
            是否带表情，以及生成的话题数量区间，均需要限制」*
            ⇒ 三块（标题 / 正文 / 话题）各自一组参数，用 Tabs 分开（口径 C6：不混在一起）。
            ⚠️ 提示词**可空**：空 = 不额外要求（走模板默认），不是「必须填」。 */} 
        <Card
          size="small"
          title={<Text strong>文案</Text>}
          styles={{ body: { padding: 12 } }}
        >
          {/* 模板：全局一个（它决定骨架与写法，「每块一个模板」没有语义） */}
          <Space size={8} style={{ width: "100%", marginBottom: 10 }} wrap={false}>
            <Text type="secondary" style={{ fontSize: 12, flex: "0 0 auto" }}>
              <span style={{ color: "#ff4d4f" }}>*</span> 提示词模板
            </Text>
            {/*: **必选**（用户口径 2026-10-18：*「提示词模板必选」*）——
                去掉 `allowClear`：能清空就一定有「没选」的状态，而没选时后端
                只能回落到内置骨架，用户不知道实际用的是哪套写法。 */}
            <Select
              style={{ flex: "1 1 auto", minWidth: 0 }}
              placeholder="请选择提示词模板"
              status={templateId ? undefined : "warning"}
              value={templateId}
              onChange={(value) => setTemplateId(value)}
              options={templates
                .filter((t) => t.task_type === "text")
                .map((t) => ({
                  value: t.id,
                  //: ⚠️ **名字里已经带「（默认）」就不再追加**（2026-10-17 截图实证：
                  //: 显示成「商品笔记文案（默认）（默认）」—— 种子模板的 `name` 本身就
                  //: 含这个后缀（库里 8 条 text 模板全是「商品笔记文案（默认）」），
                  //: 前端再拼一次 = 重复。判据：**看名字里有没有**，不看 `is_default`
                  //: （`is_default` 是「是否默认项」的标记，与显示名里写不写无关）。
                  //: ⚠️ 判据是**具体后缀**「（默认）」而不是 `includes("默认")` ——
                  //: 用户可能把模板命名为「非默认风格」，用宽判据会把它也当成已带后缀。
                  label: /（默认）$/.test(t.name) || !t.is_default
                    ? t.name
                    : `${t.name}（默认）`,
                }))}
            />
          </Space>

          {/* **标题 / 正文 / 话题：平铺，不用 Tab**（用户口径 2026-10-19：
              *「文案区域的标题话题以及正文平铺不要使用 tab」*）——
              三块各自是「提示词 + 配置 + 本版回显」，一眼能看全，
              不用来回点页签才知道另一块填了什么。 */}
          <Space direction="vertical" size={12} style={{ width: "100%" }}>

            <div>
              <Flex align="center" gap={8} style={{ marginBottom: 6 }}>
                <Text strong style={{ fontSize: 13 }}>标题</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>不填提示词则按模板写</Text>
              </Flex>
              <Input.TextArea
                rows={2}
                maxLength={500}
                placeholder="标题提示词（可空）—— 例如：突出材质与使用场景"
                value={copy.title_prompt}
                onChange={(e) => setCopyField("title_prompt", e.target.value)}
              />
              <Flex align="center" gap={6} wrap style={{ marginTop: 6 }}>
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>字数</Text>
                <InputNumber
                  min={TITLE_RANGE[0]}
                  max={TITLE_RANGE[1]}
                  style={{ width: 72 }}
                  value={copy.title_min}
                  onChange={(v) => setCopyField("title_min", v ?? TITLE_RANGE[0])}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>–</Text>
                <InputNumber
                  min={TITLE_RANGE[0]}
                  max={TITLE_RANGE[1]}
                  style={{ width: 72 }}
                  value={copy.title_max}
                  onChange={(v) => setCopyField("title_max", v ?? TITLE_RANGE[1])}
                />
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap", marginLeft: 6 }}>
                  表情
                </Text>
                <Segmented
                  size="small"
                  value={copy.emoji}
                  onChange={(v) => setCopyField("emoji", (v ?? "auto") as "auto" | "on" | "off")}
                  options={[
                    { value: "auto", label: "自动" },
                    { value: "on", label: "带" },
                    { value: "off", label: "不带" },
                  ]}
                />
              </Flex>
              {genResult && genResultForm === noteForm && (
                <ResultEcho
                  label="本版标题"
                  scope="title"
                  regenLoading={regenScope}
                  onRegen={(scope) => void handleRegenFromResult(scope)}
                >
                  <EditableBlock
                    value={genResult.title || ""}
                    placeholder="（无标题）"
                    maxLength={TITLE_MAX_CHARS}
                    onCommit={(next) => setDraft((d) => ({ ...d, title: next }))}
                  />
                </ResultEcho>
              )}
            </div>

            <div>
              <Flex align="center" gap={8} style={{ marginBottom: 6 }}>
                <Text strong style={{ fontSize: 13 }}>正文</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>不填提示词则按模板写</Text>
              </Flex>
              <Input.TextArea
                rows={2}
                maxLength={500}
                placeholder="正文提示词（可空）—— 例如：口语化，突出小户型收纳"
                value={copy.body_prompt}
                onChange={(e) => setCopyField("body_prompt", e.target.value)}
              />
              <Flex align="center" gap={6} wrap style={{ marginTop: 6 }}>
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>字数</Text>
                <InputNumber
                  min={1}
                  max={BODY_MAX_CHARS}
                  step={50}
                  style={{ width: 80 }}
                  value={copy.body_min}
                  onChange={(v) => setCopyField("body_min", v ?? 1)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>–</Text>
                <InputNumber
                  min={1}
                  max={BODY_MAX_CHARS}
                  step={50}
                  style={{ width: 80 }}
                  value={copy.body_max}
                  onChange={(v) => setCopyField("body_max", v ?? BODY_MAX_CHARS)}
                />
                <Tooltip title={`平台上限 ${BODY_MAX_CHARS} 字`}>
                  <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>字</Text>
                </Tooltip>
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap", marginLeft: 6 }}>
                  表情
                </Text>
                <Segmented
                  size="small"
                  value={copy.emoji}
                  onChange={(v) => setCopyField("emoji", (v ?? "auto") as "auto" | "on" | "off")}
                  options={[
                    { value: "auto", label: "自动" },
                    { value: "on", label: "带" },
                    { value: "off", label: "不带" },
                  ]}
                />
              </Flex>
              {genResult && genResultForm === noteForm && (
                <ResultEcho
                  label="本版正文"
                  count={`${[...(genResult.body || "")].length} 字`}
                  scope="body"
                  regenLoading={regenScope}
                  onRegen={(scope) => void handleRegenFromResult(scope)}
                >
                  <EditableBlock
                    value={genResult.body || ""}
                    placeholder="（空）"
                    multiline
                    rows={8}
                    maxHeight="36vh"
                    onCommit={(next) => setDraft((d) => ({ ...d, body: next }))}
                  />
                </ResultEcho>
              )}
            </div>

            <div>
              <Flex align="center" gap={8} style={{ marginBottom: 6 }}>
                <Text strong style={{ fontSize: 13 }}>话题</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>不填提示词则按模板写</Text>
              </Flex>
              <Input.TextArea
                rows={2}
                maxLength={500}
                placeholder="话题提示词（可空）—— 例如：围绕家居场景与风格"
                value={copy.topic_prompt}
                onChange={(e) => setCopyField("topic_prompt", e.target.value)}
              />
              <Flex align="center" gap={6} wrap style={{ marginTop: 6 }}>
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>话题数量</Text>
                <InputNumber
                  min={1}
                  max={10}
                  style={{ width: 72 }}
                  value={copy.topic_min}
                  onChange={(v) => setCopyField("topic_min", v ?? 1)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>–</Text>
                <InputNumber
                  min={1}
                  max={10}
                  style={{ width: 72 }}
                  value={copy.topic_max}
                  onChange={(v) => setCopyField("topic_max", v ?? 6)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>个</Text>
              </Flex>
              {genResult && genResultForm === noteForm && (
                <ResultEcho
                  label="本版话题"
                  count={`${genResult.topics.length} 个`}
                  scope="topics"
                  regenLoading={regenScope}
                  onRegen={(scope) => void handleRegenFromResult(scope)}
                >
                  <EditableTopics
                    topics={genResult.topics}
                    onCommit={(next) => setDraft((d) => ({ ...d, topics: next }))}
                  />
                </ResultEcho>
              )}
            </div>

          </Space>

        </Card>

        {/* 配图 / 视频 —— 有序段 = 笔记图序（卡片区域分割，用户口径 2026-10-16） */}
        <Card
          size="small"
          title={
            <Space size={6}>
              <Text strong>{noteForm === "video" ? "视频与封面" : "配图"}</Text>
              <Text type="secondary" style={{ fontSize: 12, fontWeight: "normal" }}>
                {noteForm === "video"
                  ? "一个视频 + 一张封面（封面就是发布后列表里看到的那张图）"
                  : "从上到下就是笔记图序"}
              </Text>
            </Space>
          }
          styles={{ body: { padding: 12 } }}
        >
          {noteForm === "video" ? (
            /* ── 视频笔记：**上下布局**（封面在上、视频在下）—— 用户口径 2026-10-16：
               *「视频与封面 上下布局不要左右布局」*（原先左右并排，用户明确要改上下）。 ── */
            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              {renderSegments.map(({ seg, picked }, index) => {
                const isCover = index === 0;
                return (
                  <SegmentCard
                    key={index}
                    index={index}
                    seg={seg}
                    title={isCover ? "封面" : "视频"}
                    tagColor={isCover ? "gold" : "blue"}
                    hint=""
                    /* ⚠️ **不能从 `sourceOptions` 里筛**（`sourceOptions` 已按 `noteForm`
                       过滤成「视频形态」的选项）—— 封面那格要的是**图片形态**的选项，
                       从视频集合里筛 `form === "image"` 恒得**空数组** ⇒ `Select`
                       找不到匹配项就把**原始值 `product_image` 直接显示出来**
                       （2026-10-16 截图实证的真 bug）。
                       判据：**按需要的那类形态，从全量 `MEDIA_SOURCE_OPTIONS` 里筛**。 */
                    options={MEDIA_SOURCE_OPTIONS.filter(
                      (o) => o.form === (isCover ? "image" : "video")
                    )}
                    materialFiles={materialFiles}
                    materialFolders={materialFolders}
                    materialFolderId={materialFolderId}
                    onMaterialFolderChange={setMaterialFolderId}
                    videoTemplates={videoTemplates}
                    productImages={productImages}
                    detailImages={detailImages}
                    materialImages={materialImages}
                    detailImagesNote={detailImagesNote}
                    aiCountCap={aiCountCap}
                    imageLimit={imageLimit}
                    onPatch={(patch) => patchSegment(index, patch)}
                    onPreview={setPreviewImage}
                    picked={picked}
                    pickCap={pickCapFor(index)}
                  />
                );
              })}
            </Space>
          ) : (
            /* ── 图文笔记：封面一行 + 内页可叠加（顺序 = 图序） ── */
            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              {renderSegments.map(({ seg, picked }, index) => (
                <SegmentCard
                  key={index}
                  index={index}
                  seg={seg}
                  picked={picked}
                  pickCap={pickCapFor(index)}
                  failure={rowFailure(index)}
                  aiMedia={segmentMediaFor(seg.source)}
                  title={index === 0 ? "封面" : `内页 ${index}`}
                  tagColor={seg.role === "cover" ? "gold" : "default"}
                  hint={MEDIA_SOURCE_OPTIONS.find((o) => o.value === seg.source)?.hint || ""}
                  options={sourceOptions.filter((o) =>
                    seg.role === "cover" ? o.form === "image" : true
                  )}
                  materialFiles={materialFiles}
                  materialFolders={materialFolders}
                  materialFolderId={materialFolderId}
                  onMaterialFolderChange={setMaterialFolderId}
                  videoTemplates={videoTemplates}
                  productImages={productImages}
                  detailImages={detailImages}
                  materialImages={materialImages}
                  detailImagesNote={detailImagesNote}
                  aiCountCap={aiCountCap}
                  imageLimit={imageLimit}
                  onPatch={(patch) => patchSegment(index, patch)}
                  onPreview={setPreviewImage}
                  onRemove={seg.role === "cover" ? undefined : () => removeSegment(index)}
                />
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={addSegment} block>
                再加一个内页来源
              </Button>
            </Space>
          )}

        </Card>

        {/* ⚠️ **「高级设置」整块已移除**（用户口径 2026-10-16：*「高级设计移除，
            不允许随意设置模型名」*）—— 模型由**业务点映射**决定（`resolve_business_provider`
            按 `generate_note` → 服务商 → 默认模型），用户不该也不能在弹窗里改：
            手填模型名会绕过映射（写个没配的模型名 ⇒ 运行期才 502，
            而且同一业务点在两个地方各说各话 = 两份真相源）。
            要换模型 ⇒ 去「模型配置」页改业务点映射（唯一入口）。 */}

        <Text type="secondary" style={{ fontSize: 12 }}>
          ⚠️ 封面失败 = 整篇失败；其他来源失败不连坐
        </Text>

        {/* ⚠️ 这里原本还有一整块结果面板（标题/正文/话题/配图四张卡）。
            已按用户口径（2026-10-19）**拆掉**：结果是**回显到各自的编辑位**——
            标题结果进「文案 → 标题」页签、正文进「正文」页签、话题进「话题」页签、
            配图进「配图」卡的分段选择器。不再单独开一块展示区。 */}
          </>
        )}
      </Space>

      {/* 大图预览（**用原图**：看大图就是要看清细节，给缩略图等于没放大） */}
      <ImagePreviewLayer
        preview={previewImage}
        alt="图片预览"
        onClose={() => setPreviewImage("")}
      />

      {/* **居中等待蒙版**（用户口径 2026-10-18：*「蒙版的正在生成提示和耗时等
          应始终保持弹窗居中」*）。
          ⚠️ 不用 antd `Spin tip`：它的提示**跟着被包内容走** —— 弹窗内容比视口高时
          （这个弹窗 `body` 是 72vh 可滚动），提示会跟着滚出可视区，用户就看不到耗时了。
          这里自建 `position: absolute` 铺满**弹窗内容区**的蒙版 + `flex` 居中，
          提示位置与滚动无关，且只遮弹窗（不遮背后的商品列表）。 */}
      {/* ⚠️ 2026-10-20 用户口径「项目中所有等待，蒙版全部应走统一的公共组件」：
          原来是**自建黑底遮罩 + antd `<Spin spinning={false}>`** ⇒ 改用 `<WaitMask tone="dark" scope="panel">`。 */}
      <WaitMask
        open={busy}
        scope="panel"
        tone="dark"
        title={waitLabel}
        elapsed={elapsedSec}
        sub="可能要几十秒，请勿关闭"
      />
    </Modal>
  );
}
