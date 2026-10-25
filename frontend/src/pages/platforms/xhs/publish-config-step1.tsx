/**
 * 商品配置第一步 —— 一比一复刻千帆「商品创建」首个页面（设计 26）。
 *
 * 真机实测的区块顺序（截图与 CDP dump 一致）：
 *   商品图片 * → 商品标题 *（+ 智能生成）→ 智能推荐行（+ 立即使用）→ 商品类目 *
 *
 * ## 与千帆的**唯一**差异（用户口径）
 * 千帆第一步点「上传图片」会弹出**图片空间抽屉**让用户挑图；
 * 本项目**不要那个抽屉** —— 选本地文件后**静默上传**到图片空间，
 * 拿回链接直接在本页预览。即「图片空间」对用户不可见。
 *
 * ## 交互链路（真机已验证）
 * ```
 * 选本地主图 → 静默上传(MD5 命名) → 拿到链接 → 预览
 *   → 平台自动出标题建议 → 点「立即使用」→ 标题落框 → 自动识别类目
 *   → 点「智能生成」可换个标题 → 再点「立即使用」
 * ```
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { hasPermission } from "../../../hooks/use-permission";

import "./publish-config-step1.css";

import { App, Button, Empty, Input, Spin, Tag, Tooltip } from "antd";
import {
  CloseOutlined,
  DeleteOutlined,
  InfoCircleOutlined,
  LeftOutlined,
  LoadingOutlined,
  PlusOutlined,
  RightOutlined,
  SearchOutlined,
} from "@ant-design/icons";

import {
  fetchPrecheckImage,
  recommendItemTitle,
  recommendProductCategory,
  searchItemDraftCategories,
  uploadPictureSpaceFile,
} from "../../../lib/api";
import type { RecommendCategoryItem } from "../../../types";
import { category_path_text, md5_file_name, search_keyword } from "../../../lib/publish-config";
import type { ItemDraftCategory } from "../../../types";
import { WaitMask } from "../../../components/ui/wait-mask";

/** 已上传的主图（链接 + 展示名）。 */
export type ConfigMainImage = {
  /** 图片空间回给的链接（预览用）。 */
  url: string;
  /** 磁盘/文件原名（仅展示与排错，不参与去重）。 */
  origin_name: string;
  /** 上传后落库的文件名（内容 MD5）。 */
  file_name: string;
  /** ⚠️ 规格图**去重键**：源预检图的 `rel_path`（用户口径 2026-10-10：同一张规格图
   *  不能被两个规格值重复选中）。只有来自预检 SKU 图的规格图才有这一项。 */
  precheck_name?: string;
  /** **源预检图相对路径**（`主图/…` 或 `SKU图/…`）—— 规格图在**选择时**就上传预览，
   *  但正式提交时后端要**统一重新上传**（与主图/详情图同链路、同去重），
   *  故 SKU 的 `image_path` 要的是**这个相对路径**，不是已上传对象的 URL。
   *  只有来自预检数据包的规格图才有这一项（手动本地上传的没有）。 */
  rel_path?: string;
  /** 去后缀检索名 —— 可按它在图片空间再查一次。 */
  search_name: string;
  /** 本地预览用的 object URL（上传成功前先本地显示，避免空窗）。 */
  local_url?: string;
  /** ⚠️ 下面几项是**标题生成**的入参材料（平台拿图链+尺寸喂模型）。 */
  file_id?: string;
  width?: number | null;
  height?: number | null;
  size_bytes?: number;
  extension?: string;
  /** ⚠️ **发布体材料**（2026-10-10 补，后端 `picture-space/upload` 返回）：
   *  上传成功后，提交/保存草稿直接传这组对象（跳过重新上传）。 */
  materialId?: string;
  path?: string;
  link?: string;
  /** 视频时长（秒；仅视频有，`videos[]` 提交体用，2026-10-11 补）。 */
  duration?: number | null;
};

export type PublishConfigStep1Props = {
  accountId: number;
  /** 预检暂存 token（取数据包图片字节用，不暴露本地磁盘结构）。 */
  token: string;
  /** 数据包里的主图候选（走既有的 `token + rel_path` 取图，不用绝对路径）。 */
  mainImagePaths: { rel_path: string; file_name: string }[];
  images: ConfigMainImage[];
  onImagesChange: (next: ConfigMainImage[]) => void;
  title: string;
  onTitleChange: (next: string) => void;
  categoryPath: string[];
  onCategoryChange: (next: string[]) => void;
  /** 第一步是否已完成（主图 + 标题 + 类目齐备）。 */
  onReadyChange?: (ready: boolean) => void;
  /** 类目 id 变更（第二部分拉属性配置要用 id，不是路径名）。 */
  onCategoryIdChange?: (id: string) => void;
};

const TITLE_MAX = 60;

export default function PublishConfigStep1({
  accountId,
  token,
  mainImagePaths,
  images,
  onImagesChange,
  title,
  onTitleChange,
  categoryPath,
  onCategoryChange,
  onReadyChange,
  onCategoryIdChange,
}: PublishConfigStep1Props) {
  const { message } = App.useApp();

  /** 上传中（每个文件一个标记，支持并发多张）。 */
  const [uploading, setUploading] = useState<number>(0);
  /** 平台给的标题建议（点「立即使用」前的候选）。 */
  const [titleSuggest, setTitleSuggest] = useState<string>("");
  const [titlesLoading, setTitlesLoading] = useState(false);
  /** 类目搜索。 */
  const [catKeyword, setCatKeyword] = useState("");
  const [catOptions, setCatOptions] = useState<ItemDraftCategory[]>([]);
  const [catLoading, setCatLoading] = useState(false);
  /** 类目是否正在自动识别（点「立即使用」之后）。 */
  const [catRecognizing, setCatRecognizing] = useState(false);
  /** 平台推荐的类目（`getRecommendCategory`）—— 与「搜索出来的」分开存，
   *  因为它们语义不同：推荐是「平台认为你这商品属于哪类」，搜索是「找类目」。 */
  const [recommended, setRecommended] = useState<RecommendCategoryItem[]>([]);
  /** 类目搜索框默认**隐藏**，点右上角「搜索类目」才展开。 */
  const [catSearchOpen, setCatSearchOpen] = useState(false);
  /** 商品图片预览的当前下标（`null` = 未打开）。 */
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);

  /** 预览时的键盘操作：← / → 换图，Esc 关闭（与站内其它预览同口径）。 */
  useEffect(() => {
    if (previewIndex === null) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreviewIndex(null);
      else if (e.key === "ArrowLeft")
        setPreviewIndex((i) => (i === null ? null : (i - 1 + images.length) % images.length));
      else if (e.key === "ArrowRight")
        setPreviewIndex((i) => (i === null ? null : (i + 1) % images.length));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [images.length, previewIndex]);

  /** 本地 object URL 回收（避免泄漏）。 */
  const localUrlsRef = useRef<string[]>([]);
  /** 已上传主图的**最新**快照 —— 批量自动上传时逐张 append 要用它，
   *  不能读闭包里的 `images`（那是渲染时的旧值，会让前几张被覆盖）。 */
  const imagesRef = useRef<ConfigMainImage[]>(images);
  useEffect(() => {
    imagesRef.current = images;
  }, [images]);
  useEffect(() => {
    return () => {
      for (const u of localUrlsRef.current) URL.revokeObjectURL(u);
      localUrlsRef.current = [];
    };
  }, []);

  const ready = images.length > 0 && title.trim().length > 0 && categoryPath.length > 0;
  /** 类目区是否有内容（推荐 或 搜索结果）—— 决定要不要铺成一块。 */
  const hasAnyCat = recommended.length > 0 || catOptions.length > 0;
  useEffect(() => {
    onReadyChange?.(ready);
  }, [ready, onReadyChange]);

  /** 选本地主图 → **静默上传**（用户口径：选中即上传，拿到链接才进预览）。
   *
   * ⚠️ 「静默」= 不弹图片空间界面；后端上传 → 回链 → 本页预览，用户看不到中间层。
   * ⚠️ 文件名按**内容 MD5**（用户口径），同图重复选不会产生第二份。
   */
  const pickAndUpload = useCallback(
    async (
      fileIndex: number,
      picked?: File,
      options: { silent?: boolean } = {},
    ): Promise<ConfigMainImage | null> => {
      setUploading((n) => n + 1);
      try {
        let bytes = new Uint8Array();
        let blob: Blob;
        let origin: string;

        if (picked) {
          //: 用户从**本地文件对话框**选的：直接用它的字节。
          blob = picked;
          origin = picked.name;
          bytes = new Uint8Array(await picked.arrayBuffer());
        } else {
          const item = mainImagePaths[fileIndex];
          if (!item) return null;
          origin = item.file_name || item.rel_path.split(/[\\/]/).pop() || "image.jpg";
          //: 数据包路径：走既有的预检取图（`GET /{token}/image?path=…`，需鉴权）——
          //: 它已经处理了鉴权与 objectURL，别在页面里再薄一份实现。
          const objectUrl = await fetchPrecheckImage(token, item.rel_path);
          blob = await (await fetch(objectUrl)).blob();
          bytes = new Uint8Array(await blob.arrayBuffer());
        }

        const ext = (origin.match(/\.[^.]+$/) ?? [".jpg"])[0];
        //: MD5 命名在前端算好（与后端同算法），便于本地预览名与平台名一致。
        const name = md5_file_name(bytes, { ext });
        const file = new File([blob], name, { type: blob.type || "image/jpeg" });

        const localUrl = URL.createObjectURL(file);
        localUrlsRef.current.push(localUrl);

        const result = await uploadPictureSpaceFile(accountId, file);
        const next: ConfigMainImage = {
          url: result.url || localUrl,
          origin_name: origin,
          file_name: result.file_name,
          search_name: result.search_name || search_keyword(result.file_name),
          local_url: localUrl,
          //: 标题生成要用（平台把图链+尺寸喂模型，不是文件名）。
          file_id: result.file_id,
          width: result.width ?? null,
          height: result.height ?? null,
          size_bytes: result.size ?? bytes.length,
          extension: result.extension ?? ext,
        };
        //: ⚠️ 用**函数式**更新：批量自动上传时逐张 append，
        //: 闭包里的 `images` 是旧值，直接展开会把前面传到的那几张覆盖掉。
        onImagesChange([...imagesRef.current, next]);
        imagesRef.current = [...imagesRef.current, next];
        if (!options.silent) message.success(`已上传：${result.file_name}`);
        return next;
      } catch (err) {
        message.error(`上传失败：${err instanceof Error ? err.message : String(err)}`);
        return null;
      } finally {
        setUploading((n) => Math.max(0, n - 1));
      }
    },
    [accountId, images, mainImagePaths, message, onImagesChange, token]
  );

  const removeImage = (idx: number) => {
    onImagesChange(images.filter((_, i) => i !== idx));
  };

  /** 智能生成标题 —— 平台据主图出建议（真机：上传主图后即出现建议）。 */
  const genTitle = useCallback(async () => {
    if (images.length === 0) {
      message.warning("请先上传商品主图");
      return;
    }
    setTitlesLoading(true);
    try {
      //: ⚠️ 走**真 AI**（用户口径：「应为空使用 ai 自动生成」）——
      //: 绝不能拿文件名当标题（那会生成「主图02」这种值）。
      //: 真机行为：给出候选后不自动落框，等用户点「立即使用」。
      if (!hasPermission("xhs:item:aiAssist")) {
        message.warning("没有AI 推荐标题的权限");
        return;
      }
      const res = await recommendItemTitle(
        accountId,
        images.map((img) => ({
          link: img.url,
          name: img.search_name || img.origin_name,
          width: img.width ?? undefined,
          height: img.height ?? undefined,
          size: img.size_bytes,
          extension: img.extension,
          file_id: img.file_id,
        })),
      );
      const suggested = (res.title ?? "").trim().slice(0, TITLE_MAX);
      if (!suggested) {
        message.warning("AI 没有返回标题，可稍后重试");
        return;
      }
      setTitleSuggest(suggested);
    } catch (err) {
      message.error(`智能生成失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setTitlesLoading(false);
    }
  }, [accountId, images, message]);

  /** 点「立即使用」→ 标题落框 → **立刻自动识别类目**（与千帆一致）。 */
  const useTitle = useCallback(async () => {
    if (!titleSuggest) return;
    onTitleChange(titleSuggest);
    setTitleSuggest("");

    //: 用户口径：「点立即使用后应与小红书页面一致，**立刻**展示推荐类目」。
    //: ⚠️ 走平台自己的 `getRecommendCategory`（标题+主图 ⇒ 推荐类目）——
    //: 那是千帆点「立即使用」背后真正的那条 action（真机取证 2026-10-08），
    //: 用关键词搜类目是另一回事、结果也不一样。
    setCatRecognizing(true);
    try {
      const first = images[0];
      if (!hasPermission("xhs:item:aiAssist")) {
        message.warning("没有AI 推荐分类的权限");
        return;
      }
      const res = await recommendProductCategory(accountId, {
        name: titleSuggest,
        image_url: first?.url ?? "",
        uploader_info_model: first?.file_id
          ? {
              url: first.url,
              bizName: "ark",
              scene: "goods_image",
              fileId: first.file_id,
              isSecret: true,
              cloudType: 4,
            }
          : undefined,
      });
      const list = res.categories ?? [];
      setRecommended(list);
      if (list.length > 0) {
        const best = list[0];
        onCategoryChange((best.path?.length ? best.path : [best.name]).filter(Boolean));
        onCategoryIdChange?.(best.category_id ?? "");
      }
    } catch {
      //: 识别失败不阻断 —— 用户仍可手动搜索类目（缺值口径：不造假）。
      setRecommended([]);
    } finally {
      setCatRecognizing(false);
    }
  }, [accountId, images, onCategoryChange, onTitleChange, titleSuggest]);

  /** 推荐项的完整链路（缺 path 时退回单项名）。 */
  const catPathOf = useCallback(
    (c: RecommendCategoryItem): string[] => (c.path?.length ? c.path : [c.name]).filter(Boolean),
    [],
  );

  /** 该项是否 = 当前已选（比末级名，平台推荐里同名即同一类目）。 */
  const isSelected = useCallback(
    (c: RecommendCategoryItem): boolean => {
      const leaf = categoryPath[categoryPath.length - 1] ?? "";
      return leaf !== "" && leaf === c.name;
    },
    [categoryPath],
  );

  /** 手动搜类目（用户口径：支持搜索类目）。 */
  const doSearchCategory = useCallback(async () => {
    if (!catKeyword.trim()) return;
    setCatLoading(true);
    try {
      const list = await searchItemDraftCategories(accountId, catKeyword.trim());
      setCatOptions(list);
    } catch (err) {
      message.error(`类目搜索失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setCatLoading(false);
    }
  }, [accountId, catKeyword, message]);

  return (
    <div className="publish-config-step1">
      {/* ── 智能识图 * ─────────────────────────────────────
          ⚠️ 用户口径（2026-10-09）：本区从「商品图片」改名为**「智能识图」** ——
          它的作用是**给大模型提供识别用的图片**（可传主图也可传详情图），
          与「图文信息」区的「商品主图 / 商品视频 / 商品详情图」不冲突。
          图片**左上角不再展示名称角标**（用户口径：移除不展示）。 */}
      <section className="config-row">
        <div className="config-label">
          <span className="req">智能识图</span> <span className="req-star">*</span>
        </div>
        <div className="config-body">
          <div className="image-slots">
            {images.map((img, i) => (
              <div className="image-slot filled" key={`${img.file_name}-${i}`}>
                {/* ⚠️ 点缩略图**放大预览**（用户口径 2026-10-08：「商品图片支持预览」）。
                    与站内其它预览一致：大图 + 左右切换（键盘 ←/→ + 按钮）。
                    用 `<img>` + `cursor: zoom-in` 而不是 antd Image —— 后者自带
                    工具栏会与「删除」按钮抢点击区。 */}
                <img
                  src={img.local_url || img.url}
                  alt={img.origin_name}
                  className="slot-thumb"
                  onClick={() => setPreviewIndex(i)}
                />
                {/* ⚠️ 删除按钮**悬停才显形**（用户口径：右上角删图标不应常驻）。 */}
                <button
                  type="button"
                  className="slot-remove"
                  aria-label={`删除图片${i + 1}`}
                  onClick={() => removeImage(i)}
                >
                  <DeleteOutlined />
                </button>
              </div>
            ))}

            <div className="image-slot upload">
              <Tooltip title="上传本地主图（自动上传，不显示图片空间）">
                <div className="upload-inner">
                  {uploading > 0 ? <LoadingOutlined /> : <PlusOutlined />}
                  <span>上传图片</span>
                </div>
              </Tooltip>
              {/* ⚠️ 真机 `<input type=file>`：点框即选文件，选完**立刻静默上传**。
                  用隐藏 input 而不是 `<select>` —— 千帆就是点框选文件，
                  下拉在语义与观感上都不是「上传图片」。 */}
              <input
                className="upload-picker"
                type="file"
                aria-label="选择本地主图"
                accept="image/png,image/jpeg,image/jpg"
                onClick={(e) => {
                  //: 每次点都重开选择器（同一文件重选也要有反应）。
                  (e.target as HTMLInputElement).value = "";
                }}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  const idx = mainImagePaths.findIndex((p) => p.file_name === f.name);
                  void pickAndUpload(idx >= 0 ? idx : 0, f);
                }}
              />
            </div>
          </div>

          {/* ⚠️ 用户口径（2026-10-09）：说明性文案一律收进**说明图标 + Tooltip**，
              不占布局（原来两行正文说明已删除）。 */}
          <div className="image-hint-line">
            <Tooltip
              title={
                <>
                  1.建议您传入商品图片，包含：商品主图（建议包含商品主体、尺码尺寸、成分表等）、详情图片（详细描述商品信息）
                  <br />
                  2.至少包含1张1:1或3:4主图（鼠标悬停图片上可裁剪）
                </>
              }
            >
              <InfoCircleOutlined className="hint-icon" />
            </Tooltip>
            <span>智能识图（用于大模型识别商品信息）</span>
          </div>
        </div>
      </section>

      {/* ── 商品标题 * ───────────────────────────────────── */}
      <section className="config-row">
        <div className="config-label">
          <span className="req">商品标题</span> <span className="req-star">*</span>
        </div>
        <div className="config-body">
          <div className="title-line">
            <Input
              maxLength={TITLE_MAX}
              showCount
              status={title.trim() ? undefined : "error"}
              value={title}
              onChange={(e) => onTitleChange(e.target.value)}
              placeholder="系统会自动拼接品牌名称，请勿重复填写"
            />
            <Button className="smart-gen-btn" loading={titlesLoading} onClick={() => void genTitle()}>
              智能生成
            </Button>
          </div>
          {/* **表单校验**（用户口径 2026-10-10）：文案与发布助手「商品标题」那条一致。 */}
          {!title.trim() && <div className="config-field-error">请输入商品标题</div>}

          {titleSuggest && (
            <div className="title-suggest">
              <Tag className="suggest-tag">智能推荐</Tag>
              <span className="suggest-text">{titleSuggest}</span>
              <a className="use-link" onClick={() => void useTitle()}>
                ✓ 立即使用
              </a>
            </div>
          )}
        </div>
      </section>

      {/* ── 商品类目 * ───────────────────────────────────── */}
      <section className="config-row">
        <div className="config-label">
          <span className="req">商品类目</span> <span className="req-star">*</span>
        </div>
        <div className="config-body">
          {categoryPath.length === 0 && (
            <div className="config-field-error">请选择商品类目</div>
          )}
          {/* 搜索入口放**右上角**、默认隐藏（用户口径 2026-10-08）。 */}
          <div className="cat-head">
            <span className="cat-head-title">
              {categoryPath.length > 0 ? "已选类目" : "选择类目"}
            </span>
            <a className="cat-search-toggle" onClick={() => setCatSearchOpen((v) => !v)}>
              {catSearchOpen ? "收起搜索" : "搜索类目"}
            </a>
          </div>

          {catSearchOpen && (
            <div className="cat-search">
              <Input
                value={catKeyword}
                onChange={(e) => setCatKeyword(e.target.value)}
                onPressEnter={() => void doSearchCategory()}
                prefix={<SearchOutlined />}
                placeholder="请输入商品名称关键词搜索，如：帽子、面霜"
                allowClear
                autoFocus
              />
              <Button loading={catLoading} onClick={() => void doSearchCategory()}>
                搜索
              </Button>
            </div>
          )}

          {categoryPath.length > 0 && (
            <div className="cat-selected">
              <span className="cat-path">{category_path_text(categoryPath)}</span>
            </div>
          )}

          {/* ⚠️ 2026-10-20：`tip` 已移除，改由统一 `<WaitMask>` 画在最上层
              （`Spin` 的 tip 跟内容滚走；`Spin` 只保留挡交互的职责）。 */}
          <Spin spinning={false} indicator={<></>}>
            <div className={`cat-panel${hasAnyCat ? " has-result" : ""}`}>
              {recommended.length > 0 ? (
                <div className="cat-result">
                  {/* ⚠️ 平台推荐（`getRecommendCategory`）—— 与千帆一致：
                      左侧「智能推荐」标签 + 一个「已选」chip 行。 */}
                  <div className="cat-recommend">
                    <Tag className="suggest-tag">智能推荐</Tag>
                    {recommended.map((c) => (
                      <Tag
                        key={`rec-${c.category_id || c.name}`}
                        className={`cat-chip${isSelected(c) ? " active" : ""}`}
                        onClick={() => {
                          onCategoryChange(catPathOf(c));
                          onCategoryIdChange?.(c.category_id);
                        }}
                      >
                        {c.name}
                      </Tag>
                    ))}
                  </div>
                  {/* 推荐类目的完整链路列表（点一行即选中） */}
                  <div className="cat-list">
                    {recommended.map((c) => {
                      const label = category_path_text(catPathOf(c));
                      return (
                        <div
                          className={`cat-item${isSelected(c) ? " active" : ""}`}
                          key={`recrow-${c.category_id || c.name}`}
                          onClick={() => {
                            onCategoryChange(catPathOf(c));
                            onCategoryIdChange?.(c.category_id);
                          }}
                        >
                          {label}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : catOptions.length > 0 ? (
                <div className="cat-result">
                  {/* 关键词搜索结果（用户点「搜索类目」得到的） */}
                  <div className="cat-list">
                    {catOptions.map((c) => {
                      const label = category_path_text(c.path?.length ? c.path : [c.name]);
                      const leaf = categoryPath[categoryPath.length - 1] ?? "";
                      const active = leaf !== "" && label.includes(leaf);
                      return (
                        <div
                          className={`cat-item${active ? " active" : ""}`}
                          key={`row-${c.id}`}
                          onClick={() =>
                            onCategoryChange((c.path?.length ? c.path : [c.name]).filter(Boolean))
                          }
                        >
                          {label}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                /* ⚠️ 空态**只占一行**（用户口径）。原来用 antd `Empty` 是个
                   带大图标的方块，占了近百像素的空白 —— 换成一行的次要文案。 */
                <div className="cat-empty">
                  {title.trim()
                    ? "没有识别到类目，可在下方按关键词搜索"
                    : "上传图片并完成标题后为您智能识别类目"}
                </div>
              )}
            </div>
          </Spin>
          <WaitMask open={catRecognizing} title="正在识别类目" />

        </div>
      </section>

      {/* 商品图片放大预览（用户口径 2026-10-08：「商品图片支持预览」）。
          ⚠️ 左右切换只在本组（已上传的主图）内 —— 与站内其它预览同口径。 */}
      {previewIndex !== null && images[previewIndex] && (
        <div
          className="img-preview-mask"
          role="dialog"
          aria-modal="true"
          aria-label="商品图片预览"
          onClick={() => setPreviewIndex(null)}
        >
          <div className="img-preview-body" onClick={(e) => e.stopPropagation()}>
            <img
              src={images[previewIndex].local_url || images[previewIndex].url}
              alt={images[previewIndex].origin_name}
              className="img-preview-img"
            />
            <div className="img-preview-caption">
              主图{previewIndex + 1} / {images.length}
              <span className="img-preview-name">{images[previewIndex].origin_name}</span>
            </div>
          </div>

          {images.length > 1 && (
            <>
              <button
                type="button"
                className="img-preview-nav prev"
                aria-label="上一张"
                onClick={() =>
                  setPreviewIndex((previewIndex - 1 + images.length) % images.length)
                }
              >
                <LeftOutlined />
              </button>
              <button
                type="button"
                className="img-preview-nav next"
                aria-label="下一张"
                onClick={() => setPreviewIndex((previewIndex + 1) % images.length)}
              >
                <RightOutlined />
              </button>
            </>
          )}

          <button
            type="button"
            className="img-preview-close"
            aria-label="关闭预览"
            onClick={() => setPreviewIndex(null)}
          >
            <CloseOutlined />
          </button>
        </div>
      )}
    </div>
  );
}
