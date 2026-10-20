/**
 * 「发布商品」向导 —— antd `Steps` 三步（预检 / 配置商品参数 / 发布）。
 *
 * > 用户口径 2026-10-09：「发布商品第一步和第二步第三步不同，千帆后台发布商品时
 * >  会有两步，将这两步合并整合到当前项目的发布商品的第二步中」
 *
 * 即：**本项目保留自己的三步向导**（预检 → 配置商品参数 → 发布），
 * 而千帆后台的**两个步骤**（第一部分 图/标题/类目 + 第二部分 参数配置）
 * 在**第二步「配置商品参数」里合并**呈现（`PublishConfigStep1` + `PublishConfigStep2` 同屏）。
 *
 * | 本项目步 | 内容 | 落点 |
 * |---|---|---|
 * | 0 | **预检** | 选本地数据包文件夹 → 本地读取媒体/表格 → 违规词 OCR 红框 |
 * | 1 | **配置商品参数**（= 千帆两步合并） | 千帆第一部分（图/标题/类目）+ 第二部分（四分区参数）+ 右侧参数预览 |
 * | 2 | **发布** | 确认 → 提交 → 结果（含 `off_shelf` 回显） |
 *
 * ## 为什么是**一个**弹窗里的 Steps，而不是两个弹窗接力
 *
 * Steps 的语义是**同一条流程的进度指示**，必须共用一个容器才能表达
 * 「第 1 步 / 共 3 步」。拆成两个 Modal 会退化成「弹窗 A 关了再开弹窗 B」。
 *
 * ⚠️ **发布是真实写操作**，成功即产生在售商品。
 * ⚠️ 2026-10-11 用户**取消红线**：第 3 步的提交拆成两个语义明确的按钮 ——
 *   · 「发布商品」       直接上架、**发布后不再下架**（带 tip 二次确认）；
 *   · 「发布并下架商品」 发布成功后**立刻下架**（原红线行为）。
 *   两者**地位相同、没有主按钮**（用户明确：「用户自己选择，没有主按钮」）。
 * 原句「前端**不得**传 `off_shelf_after: false`」**已被取代**。
 * ⚠️ 弹窗宽度 80vw / 高 90%；文案禁 Markdown `**`。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popover,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Space,
  Steps,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  FolderOpenOutlined,
  SwapOutlined,
  LeftOutlined,
  LoadingOutlined,
  MinusOutlined,
  PlusOutlined,
  RightOutlined,
  SettingOutlined,
  UndoOutlined,
  ThunderboltOutlined,
  DeleteOutlined,
} from "@ant-design/icons";


import {
  executeDataPackageRule,
  fetchDataPackageRuleSets,
  type DataPackageRuleSet,
  discardPackagePrecheck,
  fetchAfterSaleRules,
  fetchCategoryProperties,
  fetchForbiddenWordGroups,
  fetchPrecheckImage,
  pickPackageFolder,
  precheckLocalPackage,
  publishShopItem,
  rescanPackagePrecheck,
  uploadProductVideo,
  type AfterSaleRule,
  type ForbiddenWordGroup,
  type PrecheckGroup,
  type PrecheckKind,
  type PrecheckMediaItem,
  type PrecheckResult,
  type PrecheckTable,
  type PrecheckProgress,
  fetchPrecheckProgress,
  fetchShippingTemplates,
  type ShippingTemplate,
  fetchLogisticsTemplates,
  type LogisticsTemplate,
  recommendItemTitle,
  recommendProductCategory,
  generateShortTitle,
  predictProperties,
  uploadPictureSpaceFiles,
  uploadPictureSpaceFile,
  createItemDraft,
} from "../../../lib/api";
import {
  applyBatchRuleToTable,
  buildPublishPayloadFromPrecheck,
  countFilledInColumn,
  mergePanelTable,
  priceColumnIndex,
  stockColumnIndex,
  type BatchEditMode,
  type BatchEditRule,

  dropTableColumn,
  MANUAL_COLUMN_PREFIX,
  nextManualColumnKey,
  canDropColumn,
  addTableColumn,} from "../../../lib/precheck-publish";
import { buildImageUrls, lookup_spec_image, md5_file_name, search_keyword } from "../../../lib/publish-config";
import {
  countUncheckedSkuMedia,
  DEFAULT_SKU_IMAGE_PATTERN,
  filterCheckedSkuMedia,
  localSkuImageList,
  lookupLocalSkuImage,
  selectableSkuMedia,
  validateSkuImagePattern,
  parseSkuName,
  passedSkuSpecs,
  skuNameColumnIndex,
  //: 2026-10-11 第五批（自动加载第 9 步：填 SKU 规格 / 价格库存 / 规格图）：
  buildSkuFillPlan,
  defaultSpecMapping,
  passedSkuRows,
  priceSourceOptions,
  withDerivedColumns,
  SKU_IMAGE_COL,
  type PriceMappingRow,
} from "../../../lib/precheck-publish";
import {
  emptyZoneMap,
  isZoneMapReady,
  isImageValue,
  mediaByZones,
  mediaOfZone,
  isVideoFile,
  applyExtract,
  matchSkuRows,
  KIND_LABELS,
  mediaKey,
  unsupportedText,
  unusedZones,
  validateZoneMap,
  zoneNamesOf,
  PUBLISH_SLOT_LABELS,
  PUBLISH_SLOT_REQUIRED,
  type PublishSlotKey,
  type ZoneMap,
} from "../../../lib/precheck-zones";
import {
  applyPatternToColumn,
  applyValuesToColumn,
  canApplyPattern,
  hasColumnGear,
  patternOutcomeText,
  previewPattern,
  restoreRows,
  snapshotKey,
  snapshotTable,
  type TableSnapshot,
} from "../../../lib/table-pattern";
import PublishConfigStep1, { type ConfigMainImage } from "./publish-config-step1";
import { DataPackageRulesModal } from "./data-package-rules-modal";

/** 一张图的上传结果（**与入参逐位对齐**，失败项也保留）。 */
type UploadOutcome = {
  /** 源预检图相对路径（定位用的是它，不是文件名）。 */
  rel_path: string;
  /** 显示用的原文件名。 */
  file_name: string;
  ok: boolean;
  image: ConfigMainImage | null;
  /** 失败原因（成功时空串）——用户要能看到「缺的是哪些张、为什么缺」。 */
  reason: string;
  /** 已取回的 File（内部用；`undefined` = 这一步就失败了）。 */
  file?: File | null;
};

/** 取字节这一侧的并发度（前端闸门）。
 *
 * ⚠️ 与后端 `xhs_image_upload_parallelism`（默认 5）**同值**：后端控制「PUT 的并发」，
 * 这里控制「同时向本机后端要几张图的字节」。旧版这里**不限流**（38 张一起要）
 * ⇒ 浏览器连接池耗尽 ⇒ 一部分 `fetch` 失败被静默丢弃（真机实测 38 变 14）。
 */
const UPLOAD_FETCH_CONCURRENCY = 5;
import PublishConfigStep2, {
  type ConfigSnapshot,
  type PriceRow,
  type PropertyField,
  type SpecAttribute,
} from "./publish-config-step2";
import type { CategoryPropertyItem, ItemPublishResult } from "../../../types";
import { WaitMask } from "../../../components/ui/wait-mask";
//: 「货号」属性项的合成与置首（用户口径 2026-10-15：货号固定是「其他属性」第一条）。
import {
  ITEM_NO_PROPERTY_ID,
  withItemNoFirst,
} from "../../../lib/publish-property-fields";

import "./product-publish-wizard.css";

const { Text, Paragraph } = Typography;


/** 毫秒 → 人类可读耗时（`1.2s` / `12.3s` / `1m05s`）。
 *
 * 用户口径（2026-10-09）：进度弹窗**每一项都要有耗时统计**。
 */
export function formatStageElapsed(ms: number | undefined): string {
  if (ms === undefined || ms === null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

/** 自动加载参数 —— **一个阶段**的执行情况（进度弹窗逐行）。
 *
 * 用户口径（2026-10-09）：「点击后，窗口居中展示弹窗执行进度列表，
 * 列出流程的每一阶段执行情况」。
 */
type AutoStageStatus = "pending" | "running" | "done" | "skipped" | "failed";

type AutoStage = {
  /** 阶段名（弹窗列表左列）。 */
  title: string;
  status: AutoStageStatus;
  /** 该阶段的结果说明（成功写产出、跳过/失败写原因）。 */
  detail?: string;
  /** 该阶段**耗时毫秒**（用户口径 2026-10-09：每一项加耗时统计）。
   *  `done`/`failed`/`skipped` 终态才有值；`running` 时是已跑时长（live 刷新）。 */
  ms?: number;
  /** 运行起点（`performance.now()`）—— 供 `running` 态实时算已跑时长。 */
  startedAt?: number;
};

/** `running` 阶段的**实时已跑时长**（配合进度弹窗的 500ms 秒表刷新）。
 *
 * ⚠️ 用 `performance.now()`（单调钟）而非 `Date.now()`，系统改时间不污染耗时。
 */
/** 阶段标题 → 该行是否有「逐张上传详情」（有则标题可点）。
 *
 * ⚠️ 判据用**title 前缀匹配**而不是阶段下标：阶段数组会随需求增删
 * （新增「上传商品视频」就动过顺序），写死下标迟早错位。
 */
export function uploadDetailKeyFor(title: string): "main" | "detail" | null {
  if (title.includes("主图")) return "main";
  if (title.includes("详情图")) return "detail";
  return null;
}

export function runningElapsedMs(stage: AutoStage, _tick: number): number {
  if (stage.status !== "running" || stage.startedAt === undefined) return 0;
  return Math.round(performance.now() - stage.startedAt);
}

/** 真机实测的规格类型候选（设计 27 §三.3）。
 *
 * ⚠️ 平台按**类目**下发；这里用实测值兜底，取不到时不阻塞用户（可选可不选）。 */
const SPEC_TYPE_OPTIONS = ["套装明细", "颜色分类", "尺寸", "款式", "材质", "是否带书架"];

/** 「未分组」的界面文案（后端用空串表示，不能让界面显示成空白）。 */
const UNGROUPED_LABEL = "未分组";

/** **SKU 图预览尺寸**（用户口径 2026-10-11：线上图与本地图**保持一致**，以线上图为准）。
 *
 * 单一常量：两个列都用它 ⇒ 改一处两边同步，不会一边 72 一边 100。 */
const SKU_THUMB_SIZE = 72;
/** SKU 图列的列宽（容纳预览 + 边距）。 */
const SKU_THUMB_COL_WIDTH = 110;


/**
 * 媒体区（用户口径 2026-10-13）。
 *
 * ⚠️ **不再固定四个** —— 区域名 = 数据包里的**一级目录名原值**
 * （包 A：`1比1主图` / `页面展示主图` / `SKU图` / `主图视频` / `详情图`；
 *   包 B：`主图` / `详情图` / `SKU` / `视频` / `属性`）。
 *
 * 用户原话：「数据包中读取到什么就按照读取到的文件夹分区域，不做任何操作直接展示」。
 * ⇒ 渲染时按 `zoneNamesOf(media)` **动态**出区块，不再查 `ZONES` 常量。
 * 旧常量已删除 —— 留着它就会有人拿它去 `filter`，而它在包 A 上恒空。
 */

export type ProductPublishWizardProps = {
  open: boolean;
  platformAccountId: number | null;
  shopName: string;
  onClose: () => void;
  /** 发布完成（父页据此刷新商品列表）。 */
  onPublished?: (result: ItemPublishResult) => void;
};

export function ProductPublishWizard({
  open,
  platformAccountId,
  shopName,
  onClose,
  onPublished,
}: ProductPublishWizardProps) {
  const { message } = App.useApp();

  const [step, setStep] = useState(0);

  // ---- 第一步：预检 ----
  const [packagePath, setPackagePath] = useState("");
  const [picking, setPicking] = useState(false);
  const [loading, setLoading] = useState(false);
  //: 无图形界面（后端 `NO_DESKTOP`）时，允许手填路径（否则地址栏 readOnly 死锁）。
  const [manualPathFallback, setManualPathFallback] = useState(false);
  //: ⚠️ 两段分开：`loadWithPath` 只**读取展示**，`runPrecheck` 才做检测。
  //: 用户口径 2026-10-08：「先将媒体和数据包全部在弹窗展示，点击预检，
  //: 预检通过才可以进去参数配置第二步」。
  const [inspected, setInspected] = useState(false);
  //: ⚠️ 预检用的**违规词分组**（用户口径 2026-10-08：「点击预检支持选择指定
  //: 违规词分组，支持多组，**默认全部分组**」）。空数组 = 全部分组。
  const [precheckGroups, setPrecheckGroups] = useState<string[]>([]);
  const [groupOptions, setGroupOptions] = useState<ForbiddenWordGroup[]>([]);
  //: 分组选择弹窗（用户口径 2026-10-08：分组选择用**弹窗**展示）。
  const [groupPickerOpen, setGroupPickerOpen] = useState(false);
  //: ⚠️ 等待蒙版（用户口径 2026-10-08：「预检时页面加蒙版避免误触，并统计耗时」）。
  //: 预检要跑 70+ 秒，期间用户能点到「预检 / 下一步 / 勾选」⇒ 必须先盖住。
  const [waitLabel, setWaitLabel] = useState<string | null>(null);
  const [waitElapsedSec, setWaitElapsedSec] = useState(0);
  const waitStartedAtRef = useRef<number | null>(null);
  const waitTickRef = useRef<number | null>(null);
  //: **执行进度**（用户口径 2026-10-09）：预检跑 OCR 时轮询后端拿 done/total。
  const [progress, setProgress] = useState<PrecheckProgress | null>(null);
  const progressTimerRef = useRef<number | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [precheck, setPrecheck] = useState<PrecheckResult | null>(null);

  /** **商品货号**（用户口径 2026-10-14）：
   *  「商品sku表格tab下新增一个商品货号参数，默认是为空必填，点击下一步时需校验非空」。
   *
   *  ⚠️ 住在**向导**（不在 `TablesPanel`）：它要参与 `goStep2` 的关卡判据，
   *  而面板会随之卸载/重挂（哪怕改成 display 隐藏，状态归属也应在判据这一层）。
   */
  const [itemNo, setItemNo] = useState("");
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [tables, setTables] = useState<PrecheckTable[]>([]);

  //: 大图预览（仅图片；`items` 是**同组**的图片，左右切换只在本组内）。
  const [preview, setPreview] = useState<{ items: PrecheckMediaItem[]; index: number } | null>(
    null,
  );

  // ---- 第二步：商品参数（设计 26 —— 一比一复刻千帆「商品创建」首屏）----
  const [title, setTitle] = useState("");
  //: 主图：选本地文件后**静默上传**图片空间，拿回的链接在此预览。
  const [configImages, setConfigImages] = useState<ConfigMainImage[]>([]);
  //: 已选类目链路（如 ["住宅家具","成套家具","餐厅成套家具","餐桌+餐椅"]）。
  const [configCategoryPath, setConfigCategoryPath] = useState<string[]>([]);
  //: 已选类目的 **id**（平台调属性配置要用 id，路径名只用于展示）。
  const [configCategoryId, setConfigCategoryId] = useState<string>("");
  //: 详情图（自动加载第 4 步 + 图文信息区「详情页图片」共用）。
  const [configDetailImages, setConfigDetailImages] = useState<ConfigMainImage[]>([]);
  //: 商品视频（自动加载第 5 步 + 图文信息区「主图视频」共用）。
  const [configVideo, setConfigVideo] = useState<ConfigMainImage | null>(null);
  //: 导购短标题（自动加载第 6 步 + 基础信息区「导购短标题」共用）。
  const [configShortTitle, setConfigShortTitle] = useState<string>("");
  //: 自动加载参数（阶段列表）运行态 + 二次确认 + **居中进度弹窗**。
  const [autoLoading, setAutoLoading] = useState(false);
  const [autoConfirmOpen, setAutoConfirmOpen] = useState(false);
  //: 进度弹窗可见性 + 每个阶段的执行情况（用户口径 2026-10-09：
  //: 「点击后，窗口居中展示弹窗执行进度列表，列出流程的每一阶段执行情况」）。
  const [autoProgress, setAutoProgress] = useState<AutoStage[]>([]);
  const [autoProgressOpen, setAutoProgressOpen] = useState(false);
  /** 「上传媒体资源失败是否重试」+「重试次数」（2026-10-11 用户口径）。 */
  //: 缺省 = **是 / 1 次**（用户口径「默认是」「默认1」）。状态放在二次确认弹窗里，
  //: 点「确认，开始自动加载」时才真正生效（弹窗 `destroyOnClose` ⇒ 每次重开都是默认值）。
  const [uploadRetryOnFailure, setUploadRetryOnFailure] = useState(true);
  const [uploadMaxRetries, setUploadMaxRetries] = useState(1);
  //: 进度弹窗的 **live 秒表**：运行中的阶段耗时逐秒涨（用户口径：每项要显示耗时）。
  //: 只在弹窗打开且还有阶段在跑时起表，跑完即停（不留常驻定时器）。
  const [autoTick, setAutoTick] = useState(0);
  useEffect(() => {
    if (!autoProgressOpen) return undefined;
    const running = autoProgress.some((s) => s.status === "running");
    if (!running) return undefined;
    const timer = window.setInterval(() => setAutoTick((n) => n + 1), 500);
    return () => window.clearInterval(timer);
  }, [autoProgressOpen, autoProgress]);
  //: ⚠️ 第二部分**进第二步就一直显示**（用户口径：「配置商品参数页面要看到
  //: 第二部分相关需要配置的参数」）。曾经写成「类目选定后才渲染」⇒ 用户
  //: 进第二步只看到第一部分，以为第二部分没做。
  //: 类目未定时参数区照常显示，只是类目相关的行（如属性）暂为空 —— 不藏整块。
  //: 平台下发的属性定义（关键 / 其他）—— 取不到就是空数组（**不造假**）。
  //: 来源：`getCategoryProperty`（真机 133KB 响应，`category.categoryPropertyInfo`）。
  const [keyProperties, setKeyProperties] = useState<PropertyField[]>([]);
  const [otherProperties, setOtherProperties] = useState<PropertyField[]>([]);
  /** 该类目是否支持「无物流发货」（物流类型与类目直接相关；`undefined` = 未下发）。 */
  const [supportNoLogistics, setSupportNoLogistics] = useState<boolean | undefined>(undefined);
  /** 售后服务规则（按类目下发；`must_support` 决定「七天无理由」是否可编辑）。 */
  const [afterSaleRules, setAfterSaleRules] = useState<AfterSaleRule[]>([]);
  //: 规格类型候选 —— **从平台取**（真机 6 项：套装明细/颜色分类/尺寸/款式/材质/是否带书架）。
  const [specTypeOptions, setSpecTypeOptions] = useState<string[]>(SPEC_TYPE_OPTIONS);
  /** **规格维度 id 映射**（`规格名 → 平台 attributeId`）。
   *  ⚠️ 真机发布要按规格**逐维度**给 `variants[].attributeId`；
   *  只留名字 ⇒ 后端只能把所有 SKU 行塞到第一个规格 id 上
   *  ⇒ 真机报「variants - 当前商品存在重复的规格，请删除重复规格后重试」。 */
  const [specAttributes, setSpecAttributes] = useState<SpecAttribute[]>([]);
  /** **第二步两份映射草稿的镜像**（用户口径 2026-10-15）。
   *
   *  ⚠️ 它是**只读镜像**：真相源在 `PublishStep2` 内部，这边只用来回答
   *  「删这一列会不会连带删掉某条映射」（第一步删列的二次确认要「点名」）。
   *  ⚠️ 用户没走到第二步时它为空 ⇒ 提示退化为通用文案，是诚实的（不猜）。
   */
  /** 售价的回落列（原价取不到时用）—— 由第二步上报，提交体取值要用。 */
  const [priceFallbackSource, setPriceFallbackSource] = useState("");
  const [mappingDraft, setMappingDraft] = useState<{
    specSources: string[];
    priceSources: string[];
    /** ⚠️ **完整的映射行**（不只是来源名单）—— 提交体要按它取列值
     *  （用户口径 2026-10-15：全部使用映射关系）。 */
    priceMapping: PriceMappingRow[];
  }>({ specSources: [], priceSources: [], priceMapping: [] });
  //: ⚠️ 规格 / 价格库存的导入**由第二步自己做**（用户口径 2026-10-11 最终版：
  //: 规格随类目变动 ⇒ 入口放在「商品规格」区域，不在自动加载流水线里）。
  //: 向导只负责提供数据源（`passedRows` / `specImportSources`）与上传函数。
  /** 属性值：**默认空**，第一部分执行完毕后按商品自动填充。 */
  const [propertyValues, setPropertyValues] = useState<Record<string, unknown>>({});
  /** 物流模板候选（平台下发，进第二步时拉一次）。 */
  const [shippingTemplates, setShippingTemplates] = useState<ShippingTemplate[]>([]);
  const [shippingTemplatesLoading, setShippingTemplatesLoading] = useState(false);
  const [defaultTemplateId, setDefaultTemplateId] = useState<string | null>(null);
  /** 物流计划（发货仓）候选（平台下发，进第二步时拉一次，与运费模板是**两个数据源**）。 */
  const [logisticsTemplates, setLogisticsTemplates] = useState<LogisticsTemplate[]>([]);
  const [logisticsTemplatesLoading, setLogisticsTemplatesLoading] = useState(false);
  //: 价格及库存的行（没选规格时给一行裸价）。
  const [priceRows, setPriceRows] = useState<PriceRow[]>([
    {
      key: "default",
      spec: "-",
      price: null,
      stock: null,
      market_price: null,
      barcode: "",
      seller_code: "",
    },
  ]);


  //: 物流模板候选 —— 进「配置商品参数」步拉一次（真机：页面一进来就有默认值）。
  useEffect(() => {
    if (!platformAccountId || step === 0) return;
    let alive = true;
    setShippingTemplatesLoading(true);
    fetchShippingTemplates(platformAccountId)
      .then((res) => {
        if (!alive) return;
        setShippingTemplates(res.templates ?? []);
        setDefaultTemplateId(res.default_template_id ?? null);
      })
      .catch(() => {
        /* 拉不到就留空，UI 显示「模板加载中…」，不造假 */
      })
      .finally(() => {
        if (alive) setShippingTemplatesLoading(false);
      });
    //: 物流计划（发货仓）候选 —— 与运费模板**并行**拉取（两个独立数据源）。
    setLogisticsTemplatesLoading(true);
    fetchLogisticsTemplates(platformAccountId)
      .then((res) => {
        if (!alive) return;
        setLogisticsTemplates(res.templates ?? []);
      })
      .catch(() => {
        /* 拉不到就留空，不造假 */
      })
      .finally(() => {
        if (alive) setLogisticsTemplatesLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [platformAccountId, step]);
  const [skus, setSkus] = useState<
    {
      /** 稳定 key（完整 spec `维度名:值`，唯一，不参与提交）。 */
      _key?: string;
      name: string;
      price_yuan: number | null;
      /** **划线价**（真机体 `price.originPrice`；用户口径「价格及库存」里的划线价列）。 */
      market_price_yuan?: number | null;
      stock: number | null;
      /** **规格图**（预检数据包里的相对路径；后端按它上传并填 `skuData[].specImage`）。 */
      image_path: string | null;
      /** **行级启用状态**（真机 `skuData[].available`；默认启用）。 */
      available?: boolean;
      /** 条形码（第二步「条形码」列）。 */
      barcode?: string;
      /** 商家编码（第二步「商家编码」列）。 */
      seller_code?: string;
      /** **规格图远程 URL**（第三步预览规格图用）。 */
      spec_image_url?: string | null;
      /** ⚠️ **该行发货时间文案**（2026-10-15 补，只「按规格设置发货时间」模式）。
       *  后端 `build_delivery_time` 据此算 `skuData[].deliveryTime`。 */
      delivery_label?: string;
      /** 现货+预售模式那列「发货时效」（`15天` / `20天` …）。 */
      ship_aging?: string;
    }[]
  >([]);

  // ---- 第三步：发布 ----
  const [submitting, setSubmitting] = useState(false);
  //: 本次提交点的是哪个按钮（`false` = 发布商品/不下架；`true` = 发布并下架）。
  //: ⚠️ 必须有这个 state：提交遮罩的文案要按它分叉 —— 旧版文案写死
  //: 「正在发布并立即下架…（红线）」，点「发布商品」时**说的是错的**
  //: （用户实测 2026-10-11：「点击发布商品按钮弹窗中的文案未改变」）。
  const [submitShelfMode, setSubmitShelfMode] = useState(true);
  //: 上传逐张结果（主图 / 详情图）—— 供「已上传 N 张」后面的**详情**入口展示
  //: （用户口径 2026-10-11：「新增一个详情点击弹窗展示对应的详情」）。
  const [uploadDetails, setUploadDetails] = useState<{
    main: UploadOutcome[];
    detail: UploadOutcome[];
  }>({ main: [], detail: [] });
  //: 当前打开的是哪一份详情（null = 不展示）。
  const [uploadDetailOpen, setUploadDetailOpen] = useState<"main" | "detail" | null>(null);
  /** **提交耗时统计**（用户口径 2026-10-10：「点击发布按钮后页面加蒙版加耗时统计」）。
   *  ⚠️ 用 `performance.now()` 单调钟（`Date.now()` 会被系统对时拨动）。 */
  const [submitElapsedSec, setSubmitElapsedSec] = useState(0);
  const [result, setResult] = useState<ItemPublishResult | null>(null);
  /** **保存草稿中**（用户口径 2026-10-10：发布弹窗补「保存草稿」按钮，千帆一致）。 */
  const [draftSaving, setDraftSaving] = useState(false);
  /** 保存草稿耗时（用户口径 2026-10-10：保存也应加蒙版统计耗时，与发布一致）。 */
  const [draftElapsedSec, setDraftElapsedSec] = useState(0);
  /** **第二步配置快照**（用户口径 2026-10-10：提交与第三步预览都要用完整参数）。
   *  ⚠️ 必须由向导持有 —— 第二步组件在切步时卸载，内部 state 会丢。 */
  const [configSnapshot, setConfigSnapshot] = useState<ConfigSnapshot | null>(null);
  /** **规格图原始对象**（`规格名 → 规格值 → ConfigMainImage`，2026-10-10）：
   *  第二步 `specImages` 的唯一真相源，第三步预览直接读它，与第二步同源。 */
  const [specImages, setSpecImages] = useState<Record<string, Record<string, ConfigMainImage>>>({});
  /** **发布助手异常提示数**（用户口径 2026-10-10：有异常 ⇒ 不允许进入下一步）。
   *  由第二步上报（`onHintCountChange`），保证与页面上看到的红色项**同源**。 */
  const [hintCount, setHintCount] = useState(0);

  /** 第一步 SKU 表格的**选择快照**（用户口径 2026-10-11 需求 4/5）。
   *
   *  ⚠️ 必须由**向导**持有、不能留在 `TablesPanel` 内部：第二步要按「预选通过
   *  的 SKU 规格」生成价格库存表，而 `TablesPanel` 是一个受控展示组件、
   *  它内部 state 会随页签切换卸载而丢。
   *
   *  `blocked` = 两图判定未通过的行（自动置灰 + 不勾选）；
   *  `picked`  = 用户在表格里勾选的行（默认全选）。
   *  两者一起喂给 `passedSkuSpecs()`（单一实现，第一步与第二步共用）。 */
  const [skuTableSelection, setSkuTableSelection] = useState<{
    blocked: number[];
    picked: number[];
  }>({ blocked: [], picked: [] });

  /** **SKU 图匹配结果**（设计：第 4 步 SKU 图自动匹配的产物）。
   *
   *  用户口径 2026-10-13：「第二步中的导入 sku 图上传的是第一步 sku 表中
   *  **已经匹配成功的**图」。
   *
   *  ⚠️ 必须由**向导**持有：匹配结果决定第二步「导入 SKU 图」的候选集，
   *  而 `TablesPanel` 的内部 state 会随页签切换卸载而丢（与 `skuTableSelection`
   *  同一条理由）。元素 = `{specName, specValue, relPath, fileName}`。
   */
  const [matchedSkuImages, setMatchedSkuImages] = useState<
    { specName: string; specValue: string; relPath: string; fileName: string }[]
  >([]);

  /** **区域映射**（用户口径 2026-10-13）：发布位 → 数据包里的媒体区域（可多选）。
   *
   *  用户原话：「第一步与第二步的图片资源**没有直接关系**，必须通过映射弹窗
   *  指定后才固定哪些是主图，哪些是详情图，均支持合并多个第一步中的图片区域」。
   *
   *  ⚠️ 这是**唯一**决定「哪张图进第二步」的判据 —— 不再有 `group === "main"`
   *  这类目录名猜测（`group` 是目录名原值，猜不准且静默失效）。
   */
  const [zoneMap, setZoneMap] = useState<ZoneMap>(() => emptyZoneMap());
  /** 区域映射弹窗是否打开（点「下一步」且预检通过时弹）。 */
  const [zoneMapOpen, setZoneMapOpen] = useState(false);
  /** **SKU 图匹配是否已成功触发**（用户口径 2026-10-13）：点「下一步」的关卡之一。
   *
   *  ⚠️ 与 `matchRows.length > 0` **不是**一回事：后者在「弹窗还没开」时也是 0，
   *  无法区分「没匹配」与「匹配了但一张没配上」—— 提示会指错方向。
   *  只在**用户真的执行过匹配**时置真；换数据包时复位。
   *
   *  ⚠️ 状态住在**向导**而不是 `TablesPanel`：它 gate 的是向导的「下一步」按钮，
   *  而 `TablesPanel` 会随页签切换卸载（放里面会丢）。 */
  const [skuMatchRan, setSkuMatchRan] = useState(false);

  //: 类目一旦确定，就拉该类目的**属性定义 + 规格类型**（设计 27）。
  //: ⚠️ 用类目的 **id** 调平台（路径名只是展示用，平台不认名字）。
  useEffect(() => {
    if (!platformAccountId || !configCategoryId) {
      setKeyProperties([]);
      setOtherProperties([]);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const data = await fetchCategoryProperties(platformAccountId, configCategoryId);
        if (!alive) return;
        //: 关键属性从 desc 里按 `key` 取（平台 `importantProp`）。
        setKeyProperties((data.key_properties ?? []).map((p) => ({ ...p, key: true })));
        //: ⚠️ **「货号」固定在「其他属性」第一位**（用户口径 2026-10-15）——
        //: 用户改类目时属性会**重载**，这条路径也必须插入（否则改一次类目
        //: 货号又消失；两条加载路径共用 `withItemNoFirst` 一份实现）。
        setOtherProperties(
          withItemNoFirst(
            (data.desc_properties ?? []).filter((p) => !p.key).map((p) => ({ ...p })),
          ),
        );
        //: 第一步填的货号 ⇒ 也回填进货号项的输入框（本路径不进自动加载流水线）。
        const no = itemNo.trim();
        if (no) setPropertyValues((prev) => ({ ...prev, [ITEM_NO_PROPERTY_ID]: no }));
        const sales = (data.sale_properties ?? []).map((p) => p.name).filter(Boolean);
        if (sales.length > 0) setSpecTypeOptions(sales);
        //: 物流类型与类目直接相关（用户口径 2026-10-09）：平台按类目下发是否支持无物流。
        setSupportNoLogistics(data.support_no_logistics ?? undefined);
      } catch {
        //: 取不到就留空 —— **不造假**（页面照常显示，属性区为空）。
        if (alive) {
          setKeyProperties([]);
          setOtherProperties([]);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [platformAccountId, configCategoryId, itemNo]);


  //: 物流模板候选 —— 进「配置商品参数」步拉一次（真机：页面一进来就有默认值）。
  //: **数据包列规则**管理弹窗（用户口径 2026-10-14）。规则是**库里的用户资产**，
  //: 由弹窗 CRUD；列头齿轮里选它执行（见 `ColumnPatternControl`）。
  const [rulesOpen, setRulesOpen] = useState(false);

  /** **打开轮次**（用户口径 2026-10-14）：
   *  「每次关闭发布商品弹窗，相关的属性弹窗中的参数需要清除干净」。
   *
   *  ⚠️ 第一步的面板现在**保持挂载**（切步骤不丢 state），于是它的内部 state
   *  （表格副本 / 取值正则 / 勾选 / 已应用匹配图）**活得过 `reset()`** ——
   *  下次打开就会看到「上一次正则处理过的列值」。
   *  修法：每次打开自增，作为 `<PrecheckStep key={openSeq}>` 的 key ⇒
   *  **打开即重挂**（内部 state 与 `reset()` 同批归零），
   *  而**步骤间切换 key 不变**（仍保留）。
   */
  const [openSeq, setOpenSeq] = useState(0);
  /** **第二步清空信号**（2026-10-15）：换数据包时递增 ⇒ 第二步清自己的 SKU 导入产物。
   *  ⚠️ 不用 `key` 重挂（会连带第一步一起重挂，实测自伤）。 */
  const [step2ResetToken, setStep2ResetToken] = useState(0);

  useEffect(() => {
    if (!platformAccountId || step === 0) return;
    let alive = true;
    setShippingTemplatesLoading(true);
    fetchShippingTemplates(platformAccountId)
      .then((res) => {
        if (!alive) return;
        setShippingTemplates(res.templates ?? []);
        setDefaultTemplateId(res.default_template_id ?? null);
      })
      .catch(() => {
        /* 拉不到就留空，UI 显示「模板加载中…」，不造假 */
      })
      .finally(() => {
        if (alive) setShippingTemplatesLoading(false);
      });
    //: 物流计划（发货仓）候选 —— 与运费模板**并行**拉取（两个独立数据源）。
    setLogisticsTemplatesLoading(true);
    fetchLogisticsTemplates(platformAccountId)
      .then((res) => {
        if (!alive) return;
        setLogisticsTemplates(res.templates ?? []);
      })
      .catch(() => {
        /* 拉不到就留空，不造假 */
      })
      .finally(() => {
        if (alive) setLogisticsTemplatesLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [platformAccountId, step]);

  /** 数据包里的媒体区域清单（保序）。
   *
   *  ⚠️ 优先用后端下发的 `zones`；**没有就从前端媒体清单现算**
   *  （`zoneNamesOf`）—— 这样不依赖后端一定给 `zones`（老响应 / 测试夹具）。
   */
  const mediaZones = useMemo(() => {
    const fromApi = precheck?.zones ?? [];
    if (fromApi.length > 0) return fromApi.map((z) => ({ name: z.name, kind: z.kind }));
    return zoneNamesOf(precheck?.media ?? []).map((name) => ({ name, kind: "other" as const }));
  }, [precheck]);

  /** 某区域下的媒体数（区域映射下拉里显示「区域名（N）」。 */
  const zoneCount = useCallback(
    (name: string) => mediaOfZone(precheck?.media ?? [], name).length,
    [precheck],
  );

  /** 加载完数据包 ⇒ 按区域类别给一版**默认映射**（用户可改；必填位为空则进不去）。 */

  const reset = useCallback(() => {
    setStep(0);
    setPackagePath("");
    setInspected(false);
    setToken(null);
    setPrecheck(null);
    setChecked({});
    setTables([]);
    setPreview(null);
    setTitle("");
    setSkus([]);
    setResult(null);
    setConfigImages([]);
    setConfigCategoryPath([]);
    setConfigCategoryId("");
    setConfigDetailImages([]);
    setConfigVideo(null);
    setConfigShortTitle("");
    //: ⚠️ 2026-10-10 补全：以下第二步配置 state 此前**漏清** ⇒ 关弹窗重开时
    //: 上一轮「商品规格 / 价格表 / 属性 / 配置快照 / 助手提示」全部残留
    //: （用户口径「只有弹窗关闭或发布成功后清空」没落实）。
    setConfigSnapshot(null);
    setSpecImages({});
    setHintCount(0);
    setSubmitElapsedSec(0);
    //: 上传详情也要清（否则重开弹窗看到上一轮的图清单）。
    setUploadDetails({ main: [], detail: [] });
    setUploadDetailOpen(null);
    setPriceRows([
      {
        key: "default",
        spec: "-",
        price: null,
        stock: null,
        market_price: null,
        barcode: "",
        seller_code: "",
      },
    ]);
    setKeyProperties([]);
    setOtherProperties([]);
    setSupportNoLogistics(undefined);
    setAfterSaleRules([]);
    setSpecAttributes([]);
    setPropertyValues({});
    setShippingTemplates([]);
    setShippingTemplatesLoading(false);
    setDefaultTemplateId(null);
    setLogisticsTemplates([]);
    setLogisticsTemplatesLoading(false);
  }, []);

  useEffect(() => {
    if (open) {
      reset();
      //: 每次打开自增 ⇒ `PrecheckStep` 强制重挂（内部 state 一并归零）。
      setOpenSeq((n) => n + 1);
    }
  }, [open, reset]);

  //: 打开弹窗就把**已有分组**拉下来（预检的分组选择器要能立刻选）。
  useEffect(() => {
    if (!open) return;
    fetchForbiddenWordGroups()
      .then(setGroupOptions)
      //: 拉不到不算错（可能一条词都没建）—— 不弹错误打断用户。
      .catch(() => setGroupOptions([]));
  }, [open]);

  /** 默认勾选：违规图 / 未验证项**不勾**，其余全勾。 */
  const applyDefaults = useCallback((data: PrecheckResult) => {
    const next: Record<string, boolean> = {};
    for (const item of data.media) {
      //: ⚠️ `has_forbidden` ⇒ 不勾**且禁选**（见 `MediaGrid` 的 `disabled`）。
      //: `ocr_ok !== true`（含 `null` = 未验证）⇒ 不勾。
      //: 把 `null` 当合规就是给视频开免检通道（红线）。
      next[`${item.group}/${item.rel_path}`] = !item.has_forbidden && item.ocr_ok === true;
    }
    setChecked(next);
    //: ⚠️ **「规格分类」固定列已整体移除**（用户口径 2026-10-20：
    //: 「商品发布移除商品sku表格中的规格分类固定列，『规格分类』固定列 相关所有逻辑移除」）。
    //: 现在只给 SKU 表插「SKU 图」一列（`withDerivedColumns` 内部只剩它）。
    setTables(
      data.tables.map((t) => {
        //: ⚠️ **初始化 `column_keys`**（后端不下发；它是前端产物）——
        //: 原始列的 key = 列名本身（它们**不是**手动列 ⇒ 前缀判据天然为 false）。
        //: 与插入「规格分类」在同一处做，保证**长度始终与 `columns` 一致**。
        const copied = {
          ...t,
          rows: t.rows.map((r) => [...r]),
          column_keys: t.columns.map((name) => String(name)),
        };
        //: ⚠️ **后处理的固定列一次插完**（用户口径 2026-10-15：
        //: 「SKU 图与规格分类属于**后处理的固定表格列**」）——
        //: 「SKU 图」的值此刻还没有（要等用户点「应用匹配结果」）⇒ 先插空列，
        //: 匹配完再**回填**（见 `applySkuMatch` 之后的接线）。
        return t.is_sku_table !== false ? withDerivedColumns(copied) : copied;
      }),
    );
    //: ⚠️ **不给区域映射默认值**（用户口径 2026-10-13）：
    //: 「第一步与第二步的图片资源**没有直接关系**，**必须通过映射弹窗指定**后才
    //:   固定哪些是主图，哪些是详情图」。
    //:
    //: 曾经按 `kind` 猜过一版默认值，**已被用户否掉** —— 预填会让用户直接点确认，
    //: 等于系统替他决定了发布位（而 `kind` 只是**参考分类**，包 A 的 `1比1主图`
    //: 与 `页面展示主图` 都会被猜成主图，实际该用哪个只有用户知道）。
    //: ⇒ 每次加载数据包都重置为空映射，逼用户显式选一遍。
    setZoneMap(emptyZoneMap());
    //: ⚠️ 换数据包 ⇒ 上一轮的「已匹配」作废（区域/列/图都换了）。
    setSkuMatchRan(false);
    //: 商品货号随数据包一起清空（默认空、必填）。
    setItemNo("");
    //: ⚠️ **不自动填标题**（用户口径 2026-10-08：「第一步预检通过后进入第二步
    //: 不要自动填写商品标题」）。标题由用户在第二步点「自动加载参数」生成、
    //: 或自己填 —— 不许拿数据包名当标题兜底（会得到「商品ID_935947169189」这种值）。
  }, []);

  /** 进入等待：起表 + 挂蒙版。**幂等**（已在等待时不重起秒表，否则耗时被清零）。 */
  const beginWait = useCallback((label: string) => {
    setWaitLabel(label);
    if (waitStartedAtRef.current === null) {
      waitStartedAtRef.current = Date.now();
      setWaitElapsedSec(0);
      if (waitTickRef.current !== null) window.clearInterval(waitTickRef.current);
      waitTickRef.current = window.setInterval(() => {
        if (waitStartedAtRef.current !== null) {
          setWaitElapsedSec(Math.floor((Date.now() - waitStartedAtRef.current) / 1000));
        }
      }, 1000);
    }
  }, []);

  /** 结束等待：**清标签 + 停表**（漏一个就是「蒙版没了数字还在跳」）。 */
  const endWait = useCallback(() => {
    setWaitLabel(null);
    waitStartedAtRef.current = null;
    if (waitTickRef.current !== null) {
      window.clearInterval(waitTickRef.current);
      waitTickRef.current = null;
    }
  }, []);

  /** 停执行进度轮询（预检结束 / 卸载时调用）。 */
  const stopProgressPolling = useCallback(() => {
    if (progressTimerRef.current !== null) {
      window.clearInterval(progressTimerRef.current);
      progressTimerRef.current = null;
    }
    setProgress(null);
  }, []);

  /** 起执行进度轮询（预检全程每 700ms 拉一次 done/total）。 */
  const startProgressPolling = useCallback(
    (tk: string) => {
      stopProgressPolling();
      progressTimerRef.current = window.setInterval(() => {
        fetchPrecheckProgress(tk)
          .then((p) => setProgress(p))
          .catch(() => undefined);
      }, 700);
    },
    [stopProgressPolling],
  );

  //: 组件卸载 / 关弹窗要停表 + 停进度轮询，否则定时器泄漏（换个页面还在跳）。
  useEffect(
    () => () => {
      endWait();
      stopProgressPolling();
    },
    [endWait, stopProgressPolling],
  );

  const loadWithPath = useCallback(
    async (rawPath: string) => {
      const path = rawPath.trim();
      if (!path) {
        void message.warning("请先选择数据包文件夹或填写目录路径");
        return;
      }
      setLoading(true);
      beginWait("读取数据包");
      try {
        //: ⚠️ 这一段**只读取展示**（不跑 OCR）——用户口径要求「先将媒体和数据包
        //: 全部在弹窗展示」，那就不该让用户先等 50 秒检测才看见东西。
        const response = await precheckLocalPackage({ packagePath: path, inspect: false });
        setToken(response.token);
        setPrecheck(response.precheck);
        applyDefaults(response.precheck);
        setInspected(false);
        //: ⚠️ **换数据包 ⇒ 两步一起强制重挂**（用户报障 2026-10-15：
        //: 「重新读取资源后，SKU 导入的数据未清空」）。
        //: 第一步的 `PrecheckStep` / `TablesPanel` 早已按 `key={openSeq}` 重挂；
        //: 第二步此前**漏了** ⇒ 它内部的 SKU 导入产物整批残留。
        //: 自增放这里（读取成功后）而不是读取前 —— 读失败不该把用户已配的东西清掉。
        //: ⚠️ `priceRows` 住在**向导层**（不被重挂清掉）⇒ 一并复位成初始那一行裸价。
        //: ⚠️ **只清第二步**（递增 `resetToken`，第二步自己 `useEffect` 响应）——
        //: **不要**动 `openSeq`：它挂在**第一步**上，自增会把第一步打回初始态
        //: （实测自伤：两行资源选择框 + 「正在读取」永不消失）。
        
        //: ⚠️ **只清第二步**（递增 `resetToken`，第二步自己 `useEffect` 响应）——
        //: **不要**动 `openSeq`：它挂在**第一步**上，自增会把第一步打回初始态
        //: （实测自伤：两行资源选择框 + 「正在读取」永不消失）。
        setStep2ResetToken((n) => n + 1);
        setPriceRows([{ key: "default", spec: "-", price: null, stock: null, market_price: null, barcode: "", seller_code: "" }]);
        void message.success(
          `数据包「${response.precheck.package_name}」已读取：` +
            `${response.precheck.media.length} 个媒体文件、${response.precheck.tables.length} 张表格`,
        );
      } catch (error) {
        void message.error(`读取数据包失败：${readReason(error, "读取失败")}`);
      } finally {
        setLoading(false);
        endWait();
      }
    },
    [applyDefaults, beginWait, endWait, message],
  );

  /**
   * 点「预检」→ 跑违规词 OCR / 视频抽帧。
   *
   * ⚠️ **预检通过才允许进第二步**（用户口径 2026-10-08）。「通过」的判据：
   * ① 检测真的跑完了（`inspected`）；② 没勾中的违规项（用户可能主动排除，
   * 那是他的选择，不拦）；③ 没有检测失败的条目（失败 = 没检过，不是合规）。
   * 有失败项就**不放行** —— 否则等于把「没检出」当过检。
   */
  const runPrecheck = useCallback(async () => {
    if (!token) {
      void message.warning("请先选择并读取数据包");
      return;
    }
    setLoading(true);
    beginWait("检测违规词");
    startProgressPolling(token);
    try {
      //: ⚠️ 带上选中的分组（空数组 = 全部分组，后端按「不过滤」处理）。
      const data = await rescanPackagePrecheck(token, true, precheckGroups);
      setPrecheck(data);
      applyDefaults(data);
      setInspected(true);
      const failed = data.media.filter((m: PrecheckMediaItem) => m.ocr_ok === false).length;
      const hits = data.media.filter((m: PrecheckMediaItem) => m.has_forbidden).length;
      if (failed > 0) {
        void message.warning(`预检完成，但有 ${failed} 个文件未能检测（见各条原因）`);
      } else {
        void message.success(
          hits > 0
            ? `预检完成：${hits} 个文件命中违规词（已置灰不可选）`
            : "预检完成：未发现违规词",
        );
      }
    } catch (error) {
      void message.error(`预检失败：${readReason(error, "预检失败")}`);
    } finally {
      setLoading(false);
      endWait();
      stopProgressPolling();
    }
  }, [
    applyDefaults,
    beginWait,
    endWait,
    message,
    precheckGroups,
    startProgressPolling,
    stopProgressPolling,
    token,
  ]);

  /** 预检是否**通过**（决定能否进第二步）。 */
  const precheckPassed = useMemo(() => {
    if (!inspected || !precheck) return false;
    //: 有检测失败的条目 ⇒ 没检过 ⇒ 不放行（把「没检出」当过检是最危险的）。
    return precheck.media.every((m) => m.ocr_ok !== false);
  }, [inspected, precheck]);

  /** 选择文件夹 → 直接读取（用户口径：「获得本地夹地址后，直接读取」）。 */
  const pickAndLoad = useCallback(async () => {
    setPicking(true);
    try {
      const picked = await pickPackageFolder();
      if (picked.cancelled || !picked.package_path) return;
      setPackagePath(picked.package_path);
      await loadWithPath(picked.package_path);
    } catch (error) {
      //: 无图形界面（后端弹不出系统选择框）⇒ 解锁地址栏让用户手填，而不是死路。
      if (readCode(error) === "NO_DESKTOP") {
        setManualPathFallback(true);
        void message.info("无法弹出文件夹选择框，请在地址栏填写绝对路径");
        return;
      }
      void message.error(`选择文件夹失败：${readReason(error, "选择失败")}`);
    } finally {
      setPicking(false);
    }
  }, [loadWithPath, message]);

  //: 「预检通过 且 被选中」的媒体（用户口径 2026-10-08）。
  //:
  //: ⚠️ 是 **与**关系，两个条件都要：
  //:   - 预检通过：`ocr_ok === true` 且**未命中违规词**（`!has_forbidden`）；
  //:     `ocr_ok === null/undefined` = 没检过（抽帧失败等）⇒ **不算通过**
  //:     （红线：不得把未扫描/未知当合规）；
  //:   - 被选中：第一步勾选了它。
  //: 只看勾选会让「预检没过但手滑勾上」的图漏进发布链路。
  const passedMedia = useMemo(
    () => (precheck?.media ?? []).filter((m) => m.ocr_ok === true && !m.has_forbidden),
    [precheck],
  );

  const selectedMedia = useMemo(
    () =>
      passedMedia.filter((m) => checked[`${m.group}/${m.rel_path}`]),
    [passedMedia, checked],
  );

  //: 第二步「商品图片」的候选 = **预检通过且被选中**、且落在**用户为该位选中的区域**里的媒体。
  //:
  //: ⚠️ 三个条件都要（用户口径 2026-10-08 / 2026-10-13）：
  //:   1. 预检通过（`ocr_ok === true` 且未命中违规词）；
  //:   2. 第一步勾选了它；
  //:   3. **落在区域映射弹窗里为该发布位选中的区域**（`zoneMap.main`）。
  //:
  //: ⚠️ 第 3 条是这一轮的核心变更（用户口径 2026-10-13）：
  //: 「第一步与第二步的图片资源**没有直接关系**，必须通过映射弹窗指定后才固定
  //:   哪些是主图，哪些是详情图，**均支持合并多个**第一步中的图片区域」。
  //: ⇒ 旧写法 `m.group === "main"` 已被取代：`group` 现在是**目录名原值**
  //: （`1比1主图` / `页面展示主图`），拿它跟 `"main"` 比**恒不成立** ⇒ 静默带不进图。
  //:
  //: ⚠️ 用 `mediaByZones`（**两段循环**）而不是一次 `filter`：
  //: 一次 filter 会按**清单原始顺序**把多个区的媒体**交错**输出，
  //: 而主图顺序 = 封面顺序，必须先整个 A 区、再整个 B 区。
  const mainImagePaths = useMemo(
    () =>
      mediaByZones(selectedMedia, zoneMap.main).map((m) => ({
        rel_path: m.rel_path,
        file_name: m.file_name,
      })),
    [selectedMedia, zoneMap.main],
  );

  //: 详情图候选（自动加载第 4 步用）：同上，按为用户选中的详情图区域合并。
  const detailImagePaths = useMemo(
    () =>
      mediaByZones(selectedMedia, zoneMap.detail).map((m) => ({
        rel_path: m.rel_path,
        file_name: m.file_name,
      })),
    [selectedMedia, zoneMap.detail],
  );

  //: 商品视频候选（自动加载第 5 步用）：按为用户选中的视频区域（可空）。
  const videoPaths = useMemo(
    () =>
      mediaByZones(selectedMedia, zoneMap.video).map((m) => ({
        rel_path: m.rel_path,
        file_name: m.file_name,
      })),
    [selectedMedia, zoneMap.video],
  );

  //: SKU 图候选（规格图选择弹窗用）。
  //:
  //: ⚠️ **与上面三个不同源**（用户口径 2026-10-13）：
  //: 「第二步中的导入 sku 图上传的是第一步 sku 表中**已经匹配成功的**图」。
  //: ⇒ 这里取的是**第 4 步 SKU 图匹配**的产物（`matchedSkuImages`），
  //: **不是**「用户选中的某个区域里的全部图」。
  //: 旧的 `m.group === "sku"` 写法两处都错：既是区域过滤（应走匹配结果），
  //: 又拿目录名跟 `"sku"` 比（包 A 的目录叫 `SKU图`，恒不成立）。
  const skuImagePaths = useMemo(
    () =>
      matchedSkuImages.map((it) => ({ rel_path: it.relPath, file_name: it.fileName })),
    [matchedSkuImages],
  );

  const payload = useMemo(
    () =>
      buildPublishPayloadFromPrecheck(
        token && precheck ? { token, media: selectedMedia, tables } : null,
        //: ⚠️ **必须传用户配的映射**（用户口径 2026-10-15：全部使用映射关系得到列值）。
        //: 不传 ⇒ 提交体按**猜的列名**取值 —— 用户改了映射也不生效，且发出去的是错价。
        mappingDraft.priceMapping,
        priceFallbackSource,
      ),
    [token, precheck, selectedMedia, tables, mappingDraft, priceFallbackSource],
  );

  /** **预选通过**的 SKU 规格集合（用户口径 2026-10-11 需求 4/5）：
   *  勾选了 **且** 两图判定通过的行。第二步据此生成价格库存表 ——
   *  「未选中的不允许进入配置商品参数」。
   *
   *  ⚠️ 判据与第一步是**同一个函数**（`passedSkuSpecs`）—— 否则第一步勾着的
   *  规格在第二步消失，用户会看到自相矛盾的界面。 */
  const passedSpecs = useMemo(
    () =>
      passedSkuSpecs(
        tables[0] ?? null,
        skuTableSelection.blocked,
        skuTableSelection.picked,
        //: ⚠️ SKU 名称列**优先走映射**（用户口径 2026-10-15：全部使用映射关系得到列值）。
        mappingDraft.priceMapping,
      ),
    [tables, skuTableSelection],
  );

  /** **预检通过且已勾选**的 SKU 整行数据（用户口径 2026-10-11 新需求）。
   *
   *  与 `passedSpecs` **同一套判据、同一个数据源**（`tables[0]` + 勾选态）——
   *  两处各写一份的话，第一步勾着的规格会在第二步的某一处凭空消失（DEF-044 同型）。
   *  `spec` 集合那个只够「过滤价格表行」；这里要**整行**（价格/库存/商家编码/本地图）。 */
  //: ⚠️ 向导层的「**已勾选且无违规**的 SKU 图」—— `TablesPanel` 里有一份同名的
  //: （那个作用于预检弹窗内部）。这里是**向导**作用域，供 `passedSkuRows` 查本地图。
  //: 判据与弹窗内**同一套函数**（`selectableSkuMedia` → `filterCheckedSkuMedia`），
  //: 否则「弹窗里勾了、到这里查不到图」两处各说各话（DEF-044 同型）。
  const wizardCleanSkuMedia = useMemo(
    () =>
      filterCheckedSkuMedia(
        //: ⚠️ 取「SKU **类**区域」（`kind === "sku"`）而不是 `group === "sku"` ——
        //: 包 A 的 SKU 目录叫 `SKU图`，拿目录名跟旧枚举值比**恒不成立**（静默空集）。
        selectableSkuMedia((precheck?.media ?? []).filter((m) => m.kind === "sku")),
        checked,
      ),
    [precheck, checked],
  );
  //: ⚠️ 用**默认正则**解析本地图（与预检弹窗同一套默认值）。
  //: 若用户在本弹窗表头改过正则（那份 state 在 `TablesPanel` 内），本处仍按默认正则
  //: 解析 —— **已知取舍**：把正则再往上提会牵动 `TablesPanel` 的 props 与预览链路
  //: （收益小、回归面大）。这里导出的只是「按名字对齐的候选」，用户可在导入弹窗里
  //: 用「待导入行」列核对行数，对不上就不会点确认。
  /** **本地 SKU 图候选** —— 数据源 = **第 4 步「SKU 图自动匹配」的结果**
   *  （`matchedSkuImages`），用户口径 2026-10-13：
   *  「第二步中的导入 sku 图上传的是第一步 sku 表中**已经匹配成功的**图」。
   *
   *  ⚠️ **旧实现走的是「按正则从文件名解析」那条路**（`wizardLocalImages` =
   *  `localSkuImageList(媒体, DEFAULT_SKU_IMAGE_PATTERN)`）—— 那条路已随
   *  「正则方案推翻」**整体移除**（用户口径 2026-10-15：
   *  「正则相关的逻辑方案已经推翻移除了」）。
   *  留着它的后果，正是用户报的那个 bug：
   *  > 「点击确认导入**没有上传**第一步预检通过的 sku 数据对应的图片」
   *  因为 `passedSkuRows` 拿到的 `localRel` 恒空 ⇒ `plan.images` 为空 ⇒
   *  **一张规格图都不上传**，而且不报错（静默）。
   *
   *  ⚠️ 形状对齐：`lookupLocalSkuImage` 找的是 `{ key, relPath }`，
   *  这里把匹配结果的 `{ specName, specValue }` 作为 key 传入 ——
   *  与匹配弹窗里「按 SKU 名称对齐」同一口径。
   */
  const wizardLocalImages = useMemo(
    () =>
      matchedSkuImages.map((it) => ({
        //: key 用**规格值**（`lookupLocalSkuImage` 内部会先试完整名、再试规格值）。
        key: it.specValue || it.relPath,
        relPath: it.relPath,
      })),
    [matchedSkuImages],
  );

  const filledSkuRows = useMemo(
    () =>
      passedSkuRows(
        tables[0] ?? null,
        skuTableSelection.blocked,
        skuTableSelection.picked,
        wizardLocalImages,
        //: ⚠️ 一律走映射（用户口径 2026-10-15）。
        mappingDraft.priceMapping,
      ),
    [tables, skuTableSelection, wizardLocalImages],
  );

  /** 弹窗「商品 SKU 映射关系」的**左侧候选** = **预检通过表的全部列**
   *  （用户口径 2026-10-20，逐字）：
   *  「第二步的导入sku数据弹窗中的商品sku的映射关系应左侧不再是规格分类而应该是
   *    **与价格库存映射关系表一致，预检通过列的所有列**」。
   *
   *  ⚠️ 三个已退场的旧实现（**不要再回到其中任何一个**）：
   *    ① `sourceSpecNames(filledSkuRows)` —— 从 `sku名称` 拆冒号猜前缀；
   *    ② 读自造的「规格分类」固定列（该列 2026-10-20 已整体移除）；
   *    ③ 任何「代码替用户分规格」的推断 —— 用户明确：
   *       「从 sku 名称按冒号取前缀这个是错的，不要改成这个逻辑，**用户使用规则自己控制**」。
   *
   *  ⇒ 候选清单 = 列名清单，与下面「价格库存映射关系表」**同一份来源**
   *  （wizard 传给第二步的 `priceSourceColumns` 就是 `tables[0].columns`），两边不再各算各的。
   */
  const mappingSources = useMemo(
    () => priceSourceOptions(tables[0]?.columns ?? []),
    [tables],
  );


  /** 取某 SKU 行的**规格图**（已上传的图片空间条目）——
   *  SKU 名 `颜色分类:斗柜 / 尺寸:1m` ⇒ 逐维度在快照的 `specImagePaths` 里查，
   *  取**第一个命中**的（真机 `skuData[].specImage` 是单个对象）。
   *  ⚠️ 用户报「image_path 规格图也是空」（2026-10-10）：此前恒 `null`。 */
  /** 取某 SKU 行的**规格图对象**（`specImages` 唯一真相源，2026-10-10 统一）——
   *  与第二步/第三步同源：都查上抛的 `specImages` 原对象，不再经 `configSnapshot`
   *  的三个字符串/对象副本（`specImagePaths`/`specImageUrls`/`specImageMaterials`），
   *  否则「提交 vs 预览 vs image_path」又是三份数据源、时序不一致就各说各话。 */
  const specImageFor = useCallback(
    (skuName: string): ConfigMainImage | null =>
      lookup_spec_image(skuName, specImages),
    [specImages],
  );

  /** image_path 相对路径（提交体 `skuData[].image_path` 用）—— 从 specImageFor 对象取 rel_path。 */
  const specImageRefFor = useCallback(
    (skuName: string): string | null => specImageFor(skuName)?.rel_path ?? null,
    [specImageFor],
  );

  /** 规格图远程 URL（`skus` 映射的 `spec_image_url` 字段，预览兜底用）。 */
  const specImageUrlFor = useCallback(
    (skuName: string): string | null => {
      const img = specImageFor(skuName);
      return img?.url || img?.local_url || null;
    },
    [specImageFor],
  );

  /** 规格图完整发布体对象（提交用）—— 直接就是 `specImageFor` 的对象本身。 */
  const specImageMaterialFor = useCallback(
    (skuName: string): ConfigMainImage | null => specImageFor(skuName),
    [specImageFor],
  );
  //: （用户实测 2026-10-10：「第三部的 sku 统计函数错误不是 26 行，我只配置了一个规格」
  //: —— 26 行来自**预检数据包的 SKU 表**，与用户在第二步实际配置的规格组合无关）。
  //: `priceRows` 由第二步按已填规格值同步（一步一组合），提交也用它 ⇒ 两处必然一致。
  useEffect(() => {
    setSkus(
      priceRows
          .filter((r) => r.spec && r.spec !== "-")
          .map((r) => ({
            //: ⚠️ 2026-10-10：`key` 已是**完整 spec**（`维度名:值`，`spec_combinations` 已改），
            //: 唯一且可重算 ⇒ 作第三步 rowKey / 跨列表对齐用，不参与提交。
            _key: r.key,
            name: r.spec,
            price_yuan: r.price,
            //: 划线价（真机体 price.originPrice）—— 原样带上去，缺值就是 null。
            market_price_yuan: r.market_price,
            stock: r.stock,
            //: **行级启用状态**（真机 `skuData[].available`；默认启用，`undefined`=启用）。
            available: r.available !== false,
            //: 条形码 / 商家编码（第二步同名列，第三步预览要展示）。
            barcode: r.barcode,
            seller_code: r.seller_code,
            //: ⚠️ 规格图：用户报「image_path 规格图也是空」（2026-10-10）。
            //: 第二步把**规格图相对路径**按 `规格名 → 规格值` 报进快照，而 SKU 名形如
            //: `颜色分类:斗柜 / 尺寸:1m` ⇒ 逐维度查表，取**第一个命中的**相对路径
            //: （后端据此统一重新上传，填 `skuData[].specImage`）。
            image_path: specImageRefFor(r.spec),
            //: 规格图远程 URL（第三步预览展示用）。
            spec_image_url: specImageUrlFor(r.spec),
            //: ⚠️ **该行的发货时间文案**（只「按规格设置发货时间」模式有值）。
            //: 后端 `build_delivery_time` 据此算 `skuData[].deliveryTime`
            //: （用户报障 2026-10-15：配了全款预售15天，发布后变成现货48小时）。
            delivery_label: r.delivery_label,
            ship_aging: r.ship_aging,
          })),
      );
  //: ⚠️ 2026-10-10：依赖加 `specImages`（`specImageRefFor`/`specImageUrlFor` 现查 `specImages`
  //: 原对象而非 `configSnapshot` 副本），否则选规格图后 `skus` 不重映射、image_path 仍空。
  }, [priceRows, configSnapshot, specImages]);

  /** **并发上传**预检暂存的多张图片（方案 B 的前端侧）。
   *
   * 用户口径（2026-10-11）：
   *  · 「图片资源实现并发上传，方案 B，并发数默认 5，支持在 config 表配置」；
   *  · 「并发上传图片时不要打乱图片的顺序，应保证最后拿到的图片顺序与上传前一致」；
   *  · 「已上传 14 张详情图…应上传 38 张，缺少的不知道是哪些张」⇒ 失败必须**报出来**。
   *
   * ## ⚠️ 两个曾经的真 bug（都在这个函数里，都被真机实测抓到）
   *
   * **① 静默丢图（38 → 14）**：旧版先 `Promise.all` 把 38 张**同时**取回字节，
   * 再过滤出成功的提交。38 路并发打同一个后端 ⇒ 浏览器连接池耗尽
   * （`ERR_INSUFFICIENT_RESOURCES`）⇒ 一部分 `fetch` 直接 reject ⇒ 被 `catch` 吞成
   * `null` ⇒ **静默丢弃 24 张**，页面上只显示「已上传 14 张」，且**不知道丢的是哪些**。
   * ⇒ 修法：**取字节也按并发数分块**（不再无限并发），且**失败项逐张记名**回传。
   *
   * **② 顺序错乱**：旧版把「取图失败」的项 `filter` 掉后再提交，后端回的
   * `results[].index` 是**压缩后数组**的下标；虽然做了映射，
   * 但兜底那条 `decodable.find(x => x.f.name === r.file_name)` 在
   * **两张内容相同的图（MD5 同名）** 时必然命中错的那一项 ⇒ 图串位。
   * ⇒ 修法：**保持位对齐**（失败项也占位，只是不提交），回填**只认 `results[].index`**，
   * 删掉那条按文件名找的兜底（它是错序的来源）。
   *
   * ⚠️ 返回**逐张结果**（不再只回成功的数组）：调用方需要知道**哪几张失败**，
   * 才能给用户一个「点开看详情」的入口（用户口径）。
   */
  const uploadManyFromPackage = useCallback(
    async (
      items: { rel_path: string; file_name: string }[]
    ): Promise<UploadOutcome[]> => {
      if (items.length === 0) return [];
      //: 逐张结果（**与 `items` 逐位对齐**，失败项也在，带原因）。
      const out: UploadOutcome[] = items.map((it) => ({
        rel_path: it.rel_path,
        file_name: it.file_name,
        ok: false,
        image: null,
        reason: "",
      }));

      //: 并发度：后端配置是权威闸门，但**取字节这一侧也要限流**（旧版不限 ⇒ 连接池耗尽）。
      //: 取值与后端默认一致（5）；`UPLOAD_FETCH_CONCURRENCY` 是前端侧闸门。
      const chunkSize = UPLOAD_FETCH_CONCURRENCY;

      //: ── 阶段 1：**分块**取字节，块内并行、块间串行（不一次打满连接池）──
      //: 每块完成后立即收集，块与块之间让连接池有机会回收。
      for (let start = 0; start < items.length; start += chunkSize) {
        const slice = items.slice(start, start + chunkSize);
        const fetched = await Promise.all(
          slice.map(async (it, k) => {
            const index = start + k;
            try {
              const objectUrl = await fetchPrecheckImage(token ?? "", it.rel_path);
              const blob = await (await fetch(objectUrl)).blob();
              const bytes = new Uint8Array(await blob.arrayBuffer());
              const ext = (it.file_name.match(/\.[^.]+$/) ?? [".jpg"])[0];
              const name = md5_file_name(bytes, { ext });
              const file = new File([blob], name, { type: blob.type || "image/jpeg" });
              //: ⚠️ objectURL 用完即释放 —— 旧版没释放，38 张会一直占内存。
              URL.revokeObjectURL(objectUrl);
              return { index, file };
            } catch (err) {
              //: ⚠️ **不再静默**：把失败原因留在这一项上（用户能看到是哪些张、为什么）。
              out[index].reason = `取图失败：${err instanceof Error ? err.message : String(err)}`;
              return null;
            }
          })
        );
        for (const one of fetched) {
          if (one) out[one.index].file = one.file;
        }
      }

      //: ── 阶段 2：把**成功取到字节**的项按原下标顺序提交 ──
      //: `slots[k]` = 提交数组第 k 项对应的 **items 原下标** ⇒ 回填时用它还原位置，
      //: 保证「最终顺序 == 上传前顺序」。
      const slots: number[] = [];
      const payload: File[] = [];
      out.forEach((o, i) => {
        if (o.file) {
          slots.push(i);
          payload.push(o.file);
        }
      });
      if (payload.length === 0) return out;

      let res: Awaited<ReturnType<typeof uploadPictureSpaceFiles>>;
      try {
        res = await uploadPictureSpaceFiles(platformAccountId ?? 0, payload);
      } catch (err) {
        const why = `批量上传失败：${err instanceof Error ? err.message : String(err)}`;
        for (const i of slots) out[i].reason = why;
        return out;
      }

      //: ── 阶段 3：回填。**只认 `results[].index`**（后端保证与入参逐位对齐）──
      //: ⚠️ 删掉旧版那条「按文件名找」的兜底：两张内容相同的图 MD5 同名时会命中错项 ⇒ 串位。
      for (const r of res.results ?? []) {
        const slot = typeof r.index === "number" ? slots[r.index] : undefined;
        if (slot === undefined) continue;
        if (!r.success) {
          out[slot].reason = r.message || "上传失败";
          continue;
        }
        out[slot].ok = true;
        out[slot].image = {
          url: r.url || "",
          origin_name: items[slot].file_name,
          file_name: r.file_name,
          search_name: r.search_name || search_keyword(r.file_name),
          file_id: r.file_id,
          width: r.width ?? null,
          height: r.height ?? null,
          size_bytes: r.size,
          extension: r.extension ?? "",
          local_url: "",
          materialId: r.materialId,
          path: r.path,
          link: r.link,
        };
      }
      return out;
    },
    [fetchPrecheckImage, platformAccountId, token]
  );

  /** 把预检暂存的**一张图片**上传到图片空间，返回主图对象。
   *
   * 与 `publish-config-step1` 的 `pickAndUpload` 同款逻辑（此处独立一份，
   * 因为自动加载已上移到本层，不再经 step1）。文件名按内容 MD5。
   * ⚠️ 视频**不走这里** —— 图片空间是图片库，视频单独走 `uploadProductVideo`。 */
  const uploadOneFromPackage = useCallback(
    async (rel_path: string, file_name: string): Promise<ConfigMainImage | null> => {
      try {
        const objectUrl = await fetchPrecheckImage(token ?? "", rel_path);
        const blob = await (await fetch(objectUrl)).blob();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        const ext = (file_name.match(/\.[^.]+$/) ?? [".jpg"])[0];
        const name = md5_file_name(bytes, { ext });
        const file = new File([blob], name, { type: blob.type || "image/jpeg" });
        const result = await uploadPictureSpaceFile(platformAccountId ?? 0, file);
        return {
          url: result.url || "",
          origin_name: file_name,
          file_name: result.file_name,
          search_name: result.search_name || search_keyword(result.file_name),
          file_id: result.file_id,
          width: result.width ?? null,
          height: result.height ?? null,
          size_bytes: result.size ?? bytes.length,
          extension: result.extension ?? ext,
          //: 发布体材料（2026-10-10 补）：提交/保存草稿直接用，跳过重新上传。
          materialId: result.materialId,
          path: result.path,
          link: result.link,
        };
      } catch {
        return null;
      }
    },
    [fetchPrecheckImage, platformAccountId, token],
  );

  /** 把任意本地**视频文件**上传（用户口径 2026-10-10：视频上传失败要支持手动上传）。
   *
   *  ⚠️ 视频**不进图片空间**（那是图片库，登记条目要 width/height）——走 `common_file`
   *  直传（与自动加载第 5 步同一个出口，DEF-044 不留两份实现）。
   *  文件名按内容 MD5（用户口径「视频和商品图一样，上传前改成 md5 唯一名称」）。 */
  const uploadVideoFile = useCallback(
    async (file: File): Promise<ConfigMainImage | null> => {
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const ext = (file.name.match(/\.[^.]+$/) ?? [".mp4"])[0];
        const name = md5_file_name(bytes, { ext });
        const named = new File([file], name, { type: file.type || "video/mp4" });
        const result = await uploadProductVideo(platformAccountId ?? 0, named);
        return {
          url: result.url || "",
          origin_name: file.name,
          file_name: result.file_name,
          search_name: search_keyword(result.file_name),
          file_id: result.file_id,
          width: null,
          height: null,
          size_bytes: result.size ?? bytes.length,
          extension: result.extension ?? ext,
        };
      } catch {
        return null;
      }
    },
    [platformAccountId],
  );

  /** 把任意本地图片文件上传到图片空间（**规格图**用；文件名按内容 MD5，用户口径）。 */
  const uploadImageFile = useCallback(
    async (file: File): Promise<ConfigMainImage | null> => {
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const ext = (file.name.match(/\.[^.]+$/) ?? [".jpg"])[0];
        const name = md5_file_name(bytes, { ext });
        const named = new File([file], name, { type: file.type || "image/jpeg" });
        const result = await uploadPictureSpaceFile(platformAccountId ?? 0, named);
        const localUrl = URL.createObjectURL(named);
        return {
          url: result.url || localUrl,
          origin_name: file.name,
          file_name: result.file_name,
          search_name: result.search_name || search_keyword(result.file_name),
          local_url: localUrl,
          file_id: result.file_id,
          width: result.width ?? null,
          height: result.height ?? null,
          size_bytes: result.size ?? bytes.length,
          extension: result.extension ?? ext,
          //: 发布体材料（2026-10-10 补）。
          materialId: result.materialId,
          path: result.path,
          link: result.link,
        };
      } catch {
        return null;
      }
    },
    [platformAccountId],
  );

  /** 把预检暂存的**一个视频**直传（走 `common_file`，**不登记图片空间**）。
   *
   * ⚠️ 视频常比图片大（本机样本 6.7MB），PUT 较慢 —— 返回 `{ok, reason}` 好把
   * 失败原因带进进度弹窗（用户口径：要能看出为什么失败）。
   */
  const uploadVideoFromPackage = useCallback(
    async (
      rel_path: string,
      file_name: string,
    ): Promise<{ ok: true; video: ConfigMainImage } | { ok: false; reason: string }> => {
      try {
        const objectUrl = await fetchPrecheckImage(token ?? "", rel_path);
        const blob = await (await fetch(objectUrl)).blob();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        const ext = (file_name.match(/\.[^.]+$/) ?? [".mp4"])[0];
        const name = md5_file_name(bytes, { ext });
        const file = new File([blob], name, { type: blob.type || "video/mp4" });
        const result = await uploadProductVideo(platformAccountId ?? 0, file);
        return {
          ok: true,
          video: {
            url: result.url || "",
            origin_name: file_name,
            file_name: result.file_name,
            search_name: search_keyword(result.file_name),
            file_id: result.file_id,
            width: result.width ?? null,
            height: result.height ?? null,
            size_bytes: result.size ?? bytes.length,
            extension: result.extension ?? ext,
            materialId: result.materialId ?? "",
            path: result.path ?? "",
            link: result.link ?? "",
            duration: result.duration ?? null,
          },
        };
      } catch (error) {
        return { ok: false, reason: readReason(error, "视频上传失败") };
      }
    },
    [fetchPrecheckImage, platformAccountId, token],
  );

  /**
   * 带重试地跑一个「成功/失败」型异步动作（2026-10-11 用户口径：
   * 「上传媒体资源失败是否重试，默认是，以及重试次数 1-3，默认 1」）。
   *
   * ⚠️ 判据是**回调自己的返回形态**（`{ok}` / 真值 / `null`），不是「抛没抛异常」——
   * 本项目里上传失败是**返回 null / ok:false**（见 `uploadOneFromPackage`），
   * 异常只是其中一条路径。只 catch 异常的重试在这条链路上等于没有。
   *
   * `maxRetries` 是**重试次数**（用户口径的 1–3），不是「总尝试次数」：
   * 取 1 ⇒ 最多跑 2 回（首跑 + 1 次重试）。关闭重试 ⇒ 只跑 1 回。
   */
  const withUploadRetry = useCallback(
    async <T,>(run: () => Promise<T | null>, isOk?: (value: T) => boolean): Promise<T | null> => {
      const attempts = uploadRetryOnFailure ? Math.max(0, Math.min(3, uploadMaxRetries)) + 1 : 1;
      let last: T | null = null;
      for (let i = 0; i < attempts; i += 1) {
        try {
          last = await run();
        } catch {
          last = null;
        }
        if (last !== null && (isOk ? isOk(last) : true)) return last;
      }
      return last;
    },
    [uploadRetryOnFailure, uploadMaxRetries],
  );

  /** 自动加载参数（用户口径 2026-10-09）：一次点确认，**全自动**跑完七步。
   * 顺序（用户给的链路，不可乱序）：
   *   1. 自动上传主图
   *   2. 生成标题
   *   3. 识别类目
   *   4. 上传详情图
   *   5. 上传商品视频
   *   6. 生成导购短标题
   *   7. 自动生成商品属性
   *
   * ⚠️ 主图 / 标题 / 类目失败会提示（但仍继续后续步骤，不硬中断）；
   *    详情图 / 视频 / 短标题 / 属性失败**静默跳过**（缺值口径：不造假）。
   */
  const autoLoadAll = useCallback(async () => {
    if (autoLoading) return;
    setAutoLoading(true);
    let generatedTitle = "";

    //: 阶段清单（用户口径 2026-10-09：「窗口居中展示弹窗执行进度列表，
    //: 列出流程的每一阶段执行情况」）—— 先一次性铺出全部阶段（pending），
    //: 每步开始置 running、结束置 done/skipped/failed，弹窗逐行实时反映。
    //: ⚠️ 第 8 阶段是**新增**的：类目定了以后按 `categoryId` 批量拉该类目的
    //: 参数（属性定义 / 规格类型 / 物流模板…）—— 真机进第二部分就是这么做的
    //: （设计 28 §十三「16 个加载型 action 只依赖 categoryId」）。
    const stages: AutoStage[] = [
      { title: "上传商品主图", status: "pending" },
      { title: "智能生成商品标题", status: "pending" },
      { title: "识别商品类目", status: "pending" },
      { title: "上传商品详情图", status: "pending" },
      { title: "上传商品视频", status: "pending" },
      { title: "生成导购短标题", status: "pending" },
      { title: "按类目加载商品参数", status: "pending" },
      { title: "智能生成商品属性", status: "pending" },
    ];
    setAutoProgress(stages.map((s) => ({ ...s })));

    //: 各阶段**耗时统计**（用户口径 2026-10-09：进度弹窗每一项要显示耗时）。
    //: `started[i]` 在该阶段置 running 时记下起点；终态时算出 `ms` 写进阶段。
    //: ⚠️ 用 `performance.now()`（单调钟）而不是 `Date.now()` —— 系统改时间会污染耗时。
    const started: (number | null)[] = stages.map(() => null);
    /** 更新某个阶段的状态 + 说明（`running` 自动起表、终态自动结表写入耗时）。 */
    const setStage = (index: number, patch: Partial<AutoStage>) => {
      if (patch.status === "running") {
        started[index] = performance.now();
        patch = { ...patch, startedAt: started[index]! };
      } else if (
        started[index] !== null &&
        (patch.status === "done" || patch.status === "failed" || patch.status === "skipped")
      ) {
        patch = { ...patch, ms: Math.round(performance.now() - started[index]!), startedAt: undefined };
        started[index] = null;
      }
      stages[index] = { ...stages[index], ...patch };
      setAutoProgress(stages.map((s) => ({ ...s })));
    };

    try {
      // 1) 主图：全部上传
      setStage(0, { status: "running" });
      //: ⚠️ 2026-10-11 用户口径：主图**并发上传**（方案 B）—— 与详情图同一出口。
      //: 旧写法串行 5 张 × 2.4s ≈ 12s（真机实测），并发后 ≈ 3s。
      const mainOutcomes = await uploadManyFromPackage(
        mainImagePaths.map((p) => ({ rel_path: p.rel_path, file_name: p.file_name }))
      );
      const uploaded: ConfigMainImage[] = mainOutcomes
        .filter((o) => o.ok && o.image)
        .map((o) => o.image as ConfigMainImage);
      //: ⚠️ **逐张结果留档**（用户口径：失败要能看到是哪些张）—— 不再只报一个数字。
      setUploadDetails((prev) => ({ ...prev, main: mainOutcomes }));
      if (uploaded.length === 0) {
        setStage(0, { status: "failed", detail: "主图全部上传失败，已停止后续阶段" });
        void message.warning("主图全部上传失败，已跳过后续步骤");
        return;
      }
      setConfigImages(uploaded);
      //: 成功的按**原顺序**取（失败项跳过，顺序不乱）。
      const mainFailed = mainOutcomes.filter((o) => !o.ok).length;
      setStage(0, {
        status: "done",
        detail:
          mainFailed > 0
            ? `已上传 ${uploaded.length} 张主图（${mainFailed} 张失败，点标题看详情）`
            : `已上传 ${uploaded.length} 张主图`,
      });

      // 2) 标题
      setStage(1, { status: "running" });
      try {
        const res = await recommendItemTitle(
          platformAccountId ?? 0,
          uploaded.map((img) => ({
            link: img.url,
            name: img.search_name || img.origin_name,
            width: img.width ?? undefined,
            height: img.height ?? undefined,
            size: img.size_bytes,
            extension: img.extension,
            file_id: img.file_id,
          })),
        );
        generatedTitle = (res.title ?? "").trim().slice(0, 60);
        if (generatedTitle) setTitle(generatedTitle);
        setStage(1, generatedTitle
          ? { status: "done", detail: generatedTitle }
          : { status: "skipped", detail: "平台未返回标题" });
      } catch (err) {
        generatedTitle = "";
        setStage(1, {
          status: "failed",
          detail: `生成失败：${err instanceof Error ? err.message : String(err)}`,
        });
      }

      // 3) 类目（标题或首图喂给平台推荐）
      //: ⚠️ 用局部变量 `recognizedCategoryId` 承接，供后续阶段（参数 / 属性）用 ——
      //: `setConfigCategoryId` 是异步的，闭包读 `configCategoryId` 拿到的是旧值
      //: （空串），会静默跳过属性生成。
      setStage(2, { status: "running" });
      let recognizedCategoryId = "";
      if (generatedTitle) {
        try {
          const first = uploaded[0];
          const res = await recommendProductCategory(platformAccountId ?? 0, {
            name: generatedTitle,
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
          if (list.length > 0) {
            const best = list[0];
            const path = (best.path?.length ? best.path : [best.name]).filter(Boolean);
            setConfigCategoryPath(path);
            recognizedCategoryId = best.category_id ?? "";
            setConfigCategoryId(recognizedCategoryId);
            setStage(2, { status: "done", detail: path.join(" > ") });
          } else {
            setStage(2, { status: "skipped", detail: "平台未返回类目" });
          }
        } catch (err) {
          setStage(2, {
            status: "failed",
            detail: `识别失败：${err instanceof Error ? err.message : String(err)}`,
          });
        }
      } else {
        setStage(2, { status: "skipped", detail: "无标题，未识别类目" });
      }

      // 4) 详情图
      setStage(3, { status: "running" });
      if (detailImagePaths.length > 0) {
        //: ⚠️ 2026-10-11 用户口径：详情图**并发上传**（方案 B）。
        //: 旧写法是 `for...await` 串行 ⇒ 38 张 × 2.5s ≈ 94s（真机实测 1m34s）。
        //: 并发 5 后 ≈ 19s。
        const detailOutcomes = await uploadManyFromPackage(
          detailImagePaths.map((p) => ({ rel_path: p.rel_path, file_name: p.file_name }))
        );
        const details: ConfigMainImage[] = detailOutcomes
          .filter((o) => o.ok && o.image)
          .map((o) => o.image as ConfigMainImage);
        setUploadDetails((prev) => ({ ...prev, detail: detailOutcomes }));
        setConfigDetailImages(details);
        //: ⚠️ **必须报出「应有几张 / 实际几张」**（用户实测：只显示「已上传 14 张」，
        //: 而数据包里有 38 张，用户既不知道少了、也不知道少的是哪些）。
        const detailFailed = detailOutcomes.filter((o) => !o.ok).length;
        const detailTotal = detailOutcomes.length;
        setStage(3, {
          status: details.length > 0 ? "done" : "failed",
          detail:
            detailTotal === 0
              ? "数据包内无详情图"
              : detailFailed > 0
                ? `已上传 ${details.length}/${detailTotal} 张详情图（${detailFailed} 张失败，点标题看详情）`
                : `已上传 ${details.length} 张详情图`,
        });
      } else {
        setStage(3, { status: "skipped", detail: "数据包内无详情图" });
      }

      // 5) 商品视频（取第一个）—— ⚠️ 走独立直传端点，不走图片空间
      setStage(4, { status: "running" });
      if (videoPaths.length > 0) {
        //: 视频上传的失败态是 `{ok:false, reason}`（**不抛异常、也不是 null**）⇒
        //: 必须把 `ok` 作为成功判据传进去，否则重试拿到的「失败结果」会被当成功。
        const res = await withUploadRetry(
          () => uploadVideoFromPackage(videoPaths[0].rel_path, videoPaths[0].file_name),
          (r) => r.ok,
        );
        if (res?.ok) {
          setConfigVideo(res.video);
          //: 用户口径（2026-10-09）：「执行进度列表展示的是视频原始名，确认程序中执行
          //:  是否在上传前修改了视频名称为唯一 md5」⇒ 进度里**同时给出改名后的 MD5 名**，
          //:  让「已改名」在界面上直接可见（原始名只作溯源）。
          setStage(4, {
            status: "done",
            detail: `${res.video.file_name}（原名 ${res.video.origin_name}）`,
          });
        } else {
          //: `res` 可能是 `null`（`withUploadRetry` 在回调抛错且用尽重试时）——
          //: 缺值不造原因，给一句稳定说明（原始原因见进度行的上一态）。
          setStage(4, { status: "failed", detail: res?.reason ?? "视频上传失败（已用尽重试）" });
        }
      } else {
        setStage(4, { status: "skipped", detail: "数据包内无商品视频" });
      }

      // 6) 导购短标题（⚠️ 入参是 title + categoryId —— 缺类目平台回空值）
      setStage(5, { status: "running" });
      if (generatedTitle && recognizedCategoryId) {
        try {
          const res = await generateShortTitle(
            platformAccountId ?? 0,
            generatedTitle,
            recognizedCategoryId,
          );
          const short = (res.short_title ?? "").trim();
          if (short) setConfigShortTitle(short);
          setStage(5, short
            ? { status: "done", detail: short }
            : { status: "skipped", detail: "平台未返回短标题" });
        } catch (err) {
          setStage(5, {
            status: "failed",
            detail: `生成失败：${err instanceof Error ? err.message : String(err)}`,
          });
        }
      } else {
        setStage(5, {
          status: "skipped",
          detail: recognizedCategoryId ? "无标题" : "缺类目，平台无法生成",
        });
      }

      // 7) 按类目批量拉该类目的参数（属性定义 / 规格类型）—— 真机进第二部分即触发
      setStage(6, { status: "running" });
      let loadedFields: PropertyField[] = [];
      if (recognizedCategoryId) {
        try {
          const data = await fetchCategoryProperties(
            platformAccountId ?? 0,
            recognizedCategoryId,
          );
          //: 属性候选项的 **value_id** 也要留（真机 `attributes[].attributeValueId` 用它）。
          const withValues = (items: CategoryPropertyItem[]) =>
            items.map((p) => ({ ...p, values: p.values ?? [] }));
          const keys = withValues(data.key_properties ?? []).map((p) => ({ ...p, key: true }));
          const others = withValues(data.desc_properties ?? []).filter((p) => !p.key);
          setKeyProperties(keys);
          //: ⚠️ **「货号」固定在「其他属性」第一位**（用户口径 2026-10-15）：
          //: 「第三步，类目生成后，商品的其他属性中没有展示货号属性，它应该是
          //:   固定的其它属性第一个」。平台 `desc_properties` **不下发**货号
          //: （真机 22 项逐名核对无它），所以由前端合成、排在首位
          //: （千帆 bundle `allProperties` 也是先塞货号再 concat 平台属性）。
          //: 见 `lib/publish-property-fields.ts`。
          const ordered = withItemNoFirst(others);
          setOtherProperties(ordered);
          //: 第一步填的商品货号 ⇒ 直接落进货号项的输入框（空则留空，不造假）。
          const no = itemNo.trim();
          if (no) setPropertyValues((prev) => ({ ...prev, [ITEM_NO_PROPERTY_ID]: no }));
          const sales = (data.sale_properties ?? [])
            .filter((p) => p.name)
            .map((p) => ({
              name: p.name,
              id: String(p.id ?? ""),
              kind: (p.kind === "multi" ? "multi" : p.kind === "text" ? "text" : "select") as
                "select" | "multi" | "text",
              customizable: p.customizable !== false,
              options: p.options ?? [],
              values: p.values ?? [],
            }));
          if (sales.length > 0) {
            setSpecTypeOptions(sales.map((x) => x.name));
            setSpecAttributes(sales);
          }
          loadedFields = [...keys, ...others];
          setStage(6, {
            status: "done",
            detail: `关键属性 ${keys.length} 项 / 其他属性 ${others.length} 项 / 规格类型 ${sales.length} 项`,
          });
          //: 顺带取**售后服务规则**（同属「按类目加载参数」；`must_support` 决定可否编辑）。
          try {
            const rules = await fetchAfterSaleRules(platformAccountId ?? 0, recognizedCategoryId);
            setAfterSaleRules(rules.rules ?? []);
          } catch {
            setAfterSaleRules([]);
          }
        } catch (err) {
          setStage(6, {
            status: "failed",
            detail: `加载失败：${err instanceof Error ? err.message : String(err)}`,
          });
        }
      } else {
        setStage(6, { status: "skipped", detail: "缺类目，无参数可加载" });
      }

      // 8) 智能生成商品属性（⚠️ **必须带图**，否则平台恒回空数组）
      setStage(7, { status: "running" });
      if (recognizedCategoryId && loadedFields.length > 0) {
        try {
          const res = await predictProperties(platformAccountId ?? 0, {
            category_id: recognizedCategoryId,
            title: generatedTitle || title,
            image_urls: buildImageUrls([...uploaded, ...configDetailImages]),
          });
          const predicted = (res.properties ?? {}) as Record<string, unknown>;
          let n = 0;
          //: ⚠️ **「货号」不再在这里覆盖**（2026-10-15 收敛为单实现，DEF-044）：
          //: 货号现在由**属性装配那一层**固定插入「其他属性」第一位
          //: （`lib/publish-property-fields.ts` 的 `withItemNoFirst`，加载属性时就把
          //:  第一步填的货号落进货号项的输入框）。
          //: 旧实现按名从 `loadedFields` 里找「货号」再覆盖 —— 而平台
          //: `desc_properties` 与 `predictProductProperty` **都不含货号** ⇒
          //: 那段匹配**恒不命中**（这正是「其他属性里看不到货号」的真因），
          //: 留着就是两套逻辑打架，故删除。
          //:
          //: 形态仍由真机抓包实证（2026-10-14）：「货号」是**商品属性**里的一项
          //: （`attributes[]`，走 `attributeId` + `attributeValue`），**不是**顶层
          //: `articleNo`；千帆 bundle 里 `attributeId` 用的就是 `"articleNo"`
          //: （`ARTICLE_NO_PROPERTY_ID`），值的来源是第一步用户填写。
          setPropertyValues((prev) => {
            const next: Record<string, unknown> = { ...prev };
            for (const f of loadedFields) {
              const v = predicted[f.name];
              if (v !== undefined && v !== null && v !== "") {
                //: ⚠️ 多选属性的值必须是**数组**（`Select mode="multiple"` 只吃数组）；
                //: 其余（下拉/文本）给标量。平台可能给字符串也可能给数组 —— 归一。
                next[f.id] =
                  f.kind === "multi"
                    ? Array.isArray(v)
                      ? v
                      : String(v).split(/[\s,，、/]+/).filter(Boolean)
                    : Array.isArray(v)
                      ? String(v[0] ?? "")
                      : String(v);
                n += 1;
              }
            }
            return next;
          });
          setStage(7, n > 0
            ? { status: "done", detail: `已填充 ${n} 项属性` }
            : { status: "skipped", detail: "平台未给出可填充的属性值" });
        } catch (err) {
          setStage(7, {
            status: "failed",
            detail: `生成失败：${err instanceof Error ? err.message : String(err)}`,
          });
        }
      } else {
        setStage(7, {
          status: "skipped",
          detail: loadedFields.length === 0 ? "该类目无可填充属性" : "缺类目",
        });
      }

      void message.success(
        `自动加载完成：${uploaded.length} 张主图、${detailImagePaths.length} 张详情图` +
          `${videoPaths.length ? "、视频" : ""}${generatedTitle ? "、标题与类目" : ""}`,
      );
    } finally {
      setAutoLoading(false);
    }
  }, [
    autoLoading,
    buildImageUrls,
    configDetailImages,
    detailImagePaths,
    generateShortTitle,
    //: ⚠️ `itemNo` 必须进依赖：加载属性时要把第一步填的货号落进「货号」属性项 ——
    //: 漏了的话用户改完货号再点「自动加载参数」仍用闭包里的旧值（静默错值）。
    itemNo,
    mainImagePaths,
    message,
    platformAccountId,
    predictProperties,
    recommendItemTitle,
    recommendProductCategory,
    title,
    uploadOneFromPackage,
    withUploadRetry,
    videoPaths,
  ]);

  //: ⚠️ 用户实测（2026-10-11）的另一半：**回第二步做了修改（如删掉报异常的图）后，
  //: 第三步顶部仍挂着上一轮的失败原因** —— 用户会以为「改了没用」。
  //: 口径：**只要提交材料发生变化，旧错误即失效**（清掉），避免拿旧结论误导重试。
  //: ⚠️ 刻意**不依赖 `result`**（只在有错误时清）——否则会与 `setResult` 形成渲染循环。
  useEffect(() => {
    //: 判据用 `published`（`ItemPublishResult` 的真实字段；成功 = true）。
    setResult((prev) => (prev && !prev.published ? null : prev));
    //: 只认这三个「提交材料」数组：它们是第二步可编辑、且直接进载荷的部分。
  }, [configImages, configDetailImages, specImages]);

  /** 提交发布。
   *
   * `shelfAfterPublish`（2026-10-11 用户新口径，**红线取消**）：
   *  · `true`  = **发布并下架**（发布成功后立刻下架 —— 原红线的行为）
   *  · `false` = **发布商品**（直接上架，发布后**不再**下架）
   *
   * ⚠️ 红线已于 2026-10-11 由用户明确取消：「发布后保持在售」成为正式业务入口
   * （不再是「只用于测试的钩子」）。因此这里会**显式传** `off_shelf_after`，
   * 而不是像旧版那样刻意不传、靠后端默认 true 兜底。
   * ⚠️ 但后端字段**默认值仍是 true** —— 任何漏传的调用方（旧前端 / 脚本 / 测试）
   * 拿到的仍是「立刻下架」这个更保守的行为，不会因为这次放开而静默留在架上。
   */
  const submit = useCallback(async (shelfAfterPublish: boolean) => {
    //: 先记下本次模式（供提交遮罩文案分叉；失败重试时也保持同一条语义）。
    setSubmitShelfMode(shelfAfterPublish);
    if (!platformAccountId) return;
    if (!payload) {
      void message.warning("请先完成第一步预检");
      return;
    }
    if (skus.length === 0) {
      void message.warning("没有可发布的 SKU，请检查数据包表格");
      return;
    }
    //: ⚠️ 类目唯一真相源 = `configCategoryId`（Step1 的 headSlot 选类目、自动加载
    //: 第 3 阶段识别类目，写的都是它）。早先这里读一个**从未被赋值**的 `category`
    //: 死 state ⇒ 门禁恒不通过（用户实测「目前无法点击进入下一步」）。
    if (!configCategoryId) {
      void message.warning("请先选择商品类目");
      return;
    }
    if (!title.trim()) {
      void message.warning("请填写商品标题");
      return;
    }
    //: 起秒表（用户口径 2026-10-10：发布按钮后要加蒙版 + 耗时统计）。
    const startedAt = performance.now();
    setSubmitElapsedSec(0);
    //: ⚠️ 用户口径（2026-10-10）：「**再次重新发布时应清除历史发布的错误信息**」——
    //: 上一轮的失败原因挂在 `result` 上，顶部 error 公告会一直显示到下次结果回来；
    //: 提交前先清空 ⇒ 重发过程中不再显示上一轮的错误。
    setResult(null);
    setSubmitting(true);
    try {
      const data = await publishShopItem({
        platform_account_id: platformAccountId,
        category_id: configCategoryId,
        name: title.trim(),
        //: ⚠️ **货号走顶层字段**（用户报障 2026-10-15：「相同商品信息和类目，
        //: 平台后台其他属性下存在货号，本地项目上传提示**无货号属性**」）。
        //:   · 真机 `itemData[0].articleNo`，后端 `ItemPublishRequest.article_no`
        //:     早就收着它（`publish.py:116`），**但前端从没传过** ⇒ 提交体里
        //:     永远没有货号（`skus[].erpCode` 是商家编码，不是货号，两回事）。
        //:   · ⚠️ **不许塞进 `attributes[]`**：那是类目属性数组，平台会把它当成
        //:     一个 `attributeId` = `articleNo` 的**类目属性**，而类目里不存在
        //:     这个属性 ⇒ 报「当前类目不存在[货号]属性」，**整个发布被拒**。
        //:     （第二步组 `attributes` 时已用 `withoutItemNo` 排除，见
        //:      `publish-config-step2.tsx`。）
        //:   · 空则**不传**（后端 `article_no or None`）—— 与「缺值留空不造假」同口径。
        ...(itemNo.trim() ? { article_no: itemNo.trim() } : {}),
        //: ⚠️ 2026-10-10（用户铁律「上传成功后提交用远程 url」）：主图/详情图传**已上传
        //: 对象**（含 materialId/path/link/url/尺寸），后端 `build_publish_image` 直接组装，
        //: **不再重新上传**。`configImages` / `configDetailImages` 是第二步上传后回传的
        //: `ConfigMainImage[]`，已持有完整发布体材料。
        main_images: configImages.map((img) => ({
          materialId: img.materialId ?? "",
          path: img.path ?? "",
          link: img.link ?? img.url ?? "",
          url: img.url ?? "",
          file_name: img.file_name,
          width: img.width ?? null,
          height: img.height ?? null,
          size: img.size_bytes ?? null,
          extension: img.extension ?? "",
        })),
        detail_images: configDetailImages.map((img) => ({
          materialId: img.materialId ?? "",
          path: img.path ?? "",
          link: img.link ?? img.url ?? "",
          url: img.url ?? "",
          file_name: img.file_name,
          width: img.width ?? null,
          height: img.height ?? null,
          size: img.size_bytes ?? null,
          extension: img.extension ?? "",
        })),
        //: ⚠️ **提交前白名单映射**（2026-10-10）：`skus` state 同时承载「提交字段」和
        //: 「第三步预览派生字段」（spec_image_url / seller_code）。真机 `skuData[]` 只认
        //: `name/price_yuan/market_price_yuan/available/stock/image_path/barcode/erpCode`，
        //: 且商家编码字段名是 **`erpCode`**（不是 seller_code）。这里挑字段 + 改名，
        //: 剥掉预览用的 `spec_image_url`，否则后端 `extra="forbid"` 报 422。
        skus: skus.map((s) => {
          const specMat = specImageMaterialFor(s.name);
          return {
            name: s.name,
            price_yuan: s.price_yuan,
            market_price_yuan: s.market_price_yuan ?? null,
            available: s.available !== false,
            stock: s.stock,
            image_path: s.image_path ?? null,
            barcode: s.barcode ?? null,
            erpCode: s.seller_code ?? null,
            //: ⚠️ 发货时间（2026-10-15 补）：后端 `build_delivery_time` 据此算
            //: `skuData[].deliveryTime`。不传 ⇒ 平台默认「现货48小时」覆盖用户配置。
            delivery_label: s.delivery_label ?? null,
            ship_aging: s.ship_aging ?? null,
            //: 规格图已上传对象（2026-10-10 补）：后端直接用，跳过重新上传。
            spec_image_material: specMat
              ? {
                  materialId: specMat.materialId ?? "",
                  //: ⚠️ 真机抓包（batch_create_item_body.json）规格图 specImage 的
                  //: path/link/fileId 是**长格式** `arkgoods/1040…`（图片空间目录 path），
                  //: 与主图的短格式 file_id 不同。这里用 `specMat.path`（长格式）。
                  path: specMat.path ?? "",
                  link: specMat.link ?? specMat.url ?? "",
                  url: specMat.url ?? "",
                  file_name: specMat.file_name,
                  width: specMat.width ?? null,
                  height: specMat.height ?? null,
                  size: specMat.size_bytes ?? null,
                  extension: specMat.extension ?? "",
                }
              : null,
          };
        }),
        precheck_token: payload.precheck_token,
        //: 商品视频已上传对象（2026-10-11 补，真机 `itemData[0].videos[]`）。
        videos: configVideo
          ? [
              {
                materialId: configVideo.materialId ?? "",
                path: configVideo.path ?? "",
                link: configVideo.link ?? configVideo.url ?? "",
                url: configVideo.url ?? "",
                extension: configVideo.extension ?? "",
                duration: configVideo.duration ?? null,
                width: configVideo.width ?? null,
                height: configVideo.height ?? null,
                size: configVideo.size_bytes ?? null,
              },
            ]
          : undefined,
        //: **本地数据包绝对路径**（2026-10-10 补）：后端优先用它拼图片绝对路径，
        //: 摆脱 `precheck_token` 的内存引用（后端重启即失效）。
        package_path: packagePath || undefined,
        //: **第二步配置快照**（用户口径 2026-10-10：提交的应是第二步配置的完整参数）。
        config: (configSnapshot ?? undefined) as Record<string, unknown> | undefined,
        //: ⚠️ 2026-10-11 起**显式传**（红线取消，按用户点的按钮决定）：
        //: `true` = 发布成功后立刻下架；`false` = 直接上架、不再下架。
        //: 旧版刻意不传靠后端默认 true；现在两个按钮语义不同，必须显式表达。
        off_shelf_after: shelfAfterPublish,
      });
      setResult(data);
      onPublished?.(data);
      if (data.published) {
        //: 成功提示**放顶部**（message 默认顶部），且发布成功后**关闭弹窗**。
        //: ⚠️ 三种终态要能分清（否则用户不知道该不该去店里看）：
        //:  · 要下架 + 真下架了 → 「已立即下架」
        //:  · 要下架 + 下架失败 → 「下架失败，请人工下架」
        //:  · **不要下架**（发布商品）→ 明说「商品已在售」——这是「留在店里」的正向告知，
        //:    不能复用「已下架」文案（否则用户以为没上架，实际挂着了）。
        //: ⚠️ 文案按平台真实语义（2026-10-11 抓包实证）：
        //:  ・要下架 → 已下架 / 下架失败（平台侧真的调了下架接口）
        //:  ・不下架 → 商品**进入审核**（不是「已在售」！真机平台提交后同样不可售，
        //:    且提交后平台不再调上架接口 ⇒ 审核通过才自动上架）。
        //:  原句「发布成功，商品已在售（未下架）」**是错的**（会让人以为已上架成功）。
        const content = shelfAfterPublish
          ? data.off_shelf
            ? "发布成功，已立即下架"
            : "发布成功但下架失败，请人工下架"
          : "发布成功，商品已提交平台审核（审核通过后自动上架）";
        void message.warning({ content, duration: 3 });
        //: 要下架但下架失败时**不关弹窗**（留着重试/人工处理）；其余终态关窗。
        const shouldClose = !shelfAfterPublish || data.off_shelf;
        if (shouldClose) {
          window.setTimeout(() => onClose(), 1200);
        }
      } else {
        //: 用户口径（2026-10-10）：失败原因**弹窗**列出（页面内不再列）。
        //: 弹窗的打开由 `PublishStep` 自己按 `result` 触发（那里才有该 state）。
        void message.error("发布未通过，请看顶部失败原因");
      }
    } catch (error) {
      void message.error(`发布商品失败：${readReason(error, "发布失败")}`);
    } finally {
      //: 终态**定格**耗时（不归零 —— 用户要能看到这次花了多久）。
      setSubmitElapsedSec((performance.now() - startedAt) / 1000);
      setSubmitting(false);
    }
    //: ⚠️ **必须带上图片数组**（2026-10-11 用户实测「回第二步删图 → 再提交仍报同一图片异常」）：
    //: `submit` 是 `useCallback`，闭包捕获声明时的变量。第二步删图走的是
    //: `onDetailImagesChange` ⇒ `setConfigDetailImages(...)`，**state 变了但依赖里没有它**
    //: ⇒ `submit` 不被重建 ⇒ 提交时仍用**删图前**的数组组装载荷 ⇒ 平台报同一个错。
    //: 而第三步预览是直接渲染 prop（不走闭包）⇒ 「预览正常、提交是旧数据」。
    //: 这类「漏依赖 = 静默用旧闭包」的 bug 不会报错，只会在真机上表现为「改了没用」。
  }, [
    configCategoryId,
    configSnapshot,
    configImages,
    configDetailImages,
    specImages,
    message,
    onClose,
    onPublished,
    payload,
    platformAccountId,
    skus,
    title,
  ]);

  //: 提交中每 500ms 刷一次耗时（终态由 finally 定格）。
  useEffect(() => {
    if (!submitting) return;
    const timer = window.setInterval(
      () => setSubmitElapsedSec((n) => n + 0.5),
      500,
    );
    return () => window.clearInterval(timer);
  }, [submitting]);

  //: 保存草稿中每 500ms 刷一次耗时（终态由 finally 定格）。
  useEffect(() => {
    if (!draftSaving) return;
    const timer = window.setInterval(
      () => setDraftElapsedSec((n) => n + 0.5),
      500,
    );
    return () => window.clearInterval(timer);
  }, [draftSaving]);

  /** 换到第二步时把预检媒体/表格收好（丢弃服务端会话前先把数据取到前端）。 */
  const goStep2 = useCallback(() => {
    if (!precheck) {
      void message.warning("请先加载数据包");
      return;
    }
    //: ⚠️ **关卡零：预检必须通过**（用户口径 2026-10-08）。
    //: 此前这条靠按钮 `disabled` 实现，但 disabled 会**吞掉点击** ⇒ 用户看不到提示。
    //: 改成点击时逐条报原因后，这条判据必须在这里补上（否则预检没过也能进）。
    if (!precheckPassed) {
      void message.warning("请先完成预检（违规词检测 / SKU 图匹配）");
      return;
    }
    //: ⚠️ **关卡一（用户口径 2026-10-13）**：「校验 SKU 图需自动匹配触发成功后
    //: 才可以点击下一步，否则提示「请先匹配 sku 图」」。
    //:
    //: ⚠️ 判据是 `skuMatchRan`（**用户真的点过并成功产出结果**），
    //: **不是** `matchRows.length > 0` —— 后者在「用户还没开弹窗」时也是 0，
    //: 无法区分「没匹配」与「匹配了但一张没配上」，会给错提示。
    //: 顺序上排在最前：违规词/映射都还没影的时候，先提示最靠近的这一步。
    if (!skuMatchRan) {
      void message.warning("请先匹配 SKU 图");
      return;
    }
    //: ⚠️ **关卡二：商品货号必填**（用户口径 2026-10-14）：
    //: 「商品sku表格tab下新增一个商品货号参数，默认是为空必填，
    //:   点击下一步时需校验非空」。
    if (!itemNo.trim()) {
      void message.warning("请先填写商品货号");
      return;
    }
    //: ⚠️ **「规格分类」必填校验已整体移除**（用户口径 2026-10-20：
    //: 「商品发布移除商品sku表格中的规格分类固定列，相关所有逻辑移除」）。
    //: 原逻辑是「第一列表头『规格分类』且勾选行不能为空」—— 列没了，校验随之退场。
    //: ⚠️ **进入第二步前的第二个关卡**（用户口径 2026-10-13）：
    //: 「第一步的图片和 sku 表预检全部通过后，点击进入第二步时**弹窗提示指定**
    //:   主图区域、详情图区域、商品视频、映射到预检通过的媒体资源区域，
    //:   **选择后才可以进入第二步**」。
    //:
    //: ⚠️ 与其它关卡**职责不同、不合并**：
    //:   关卡一 = 「SKU 图配没配」（本轮新增）；
    //:   关卡二 = 「素材合不合规」（OCR 跑完 + 没检测失败）；
    //:   关卡三 = 「哪个区当主图/详情图」（发布位映射）。
    //: 合并会让「不改映射就进不去数据包」这种歧义重新长出来。
    setZoneMapOpen(true);
  }, [message, precheck, precheckPassed, skuMatchRan, itemNo]);

  /** 区域映射弹窗确认 ⇒ **真正**进第二步。 */
  const confirmZoneMap = useCallback(() => {
    const errors = validateZoneMap(zoneMap);
    if (errors.length > 0) {
      void message.warning(errors.join("；"));
      return;
    }
    setZoneMapOpen(false);
    setStep(1);
  }, [message, zoneMap]);

  /** **保存草稿**（用户口径 2026-10-10：千帆发布页底部有「保存草稿」次按钮，
   *  与「提交商品」主按钮并列 —— 本弹窗补上，走后端 `create_draft` 只到草稿箱不上架）。
   *  ⚠️ 红线：草稿**不会上架**（`create_draft` 只调平台草稿建档，非发布）。 */
  const saveDraft = useCallback(async () => {
    if (!platformAccountId || !configCategoryId) {
      void message.warning("请先选择账号并完成类目识别");
      return;
    }
    if (!title.trim()) {
      void message.warning("请先填写商品标题");
      return;
    }
    const draftSkus = skus
      .filter((s) => s.name && s.name !== "-")
      .map((s) => {
        const specMat = specImageMaterialFor(s.name);
        return {
          name: s.name,
          price_yuan: s.price_yuan,
          market_price_yuan: s.market_price_yuan ?? null,
          stock: s.stock,
          image_path: s.image_path ?? null,
          //: 行级启用 + 条形码/商家编码（对齐真机，2026-10-10）。
          available: s.available !== false,
          barcode: s.barcode ?? null,
          erpCode: s.seller_code ?? null,
          //: 规格图已上传对象（2026-10-10 补）。
          spec_image_material: specMat
            ? {
                materialId: specMat.materialId ?? "",
                //: 同提交链路：规格图 specImage 的 path/link 用**长格式** `specMat.path`
                //: （真机抓包 `arkgoods/1040…`），不用短格式 file_id。
                path: specMat.path ?? "",
                link: specMat.link ?? specMat.url ?? "",
                url: specMat.url ?? "",
                file_name: specMat.file_name,
                width: specMat.width ?? null,
                height: specMat.height ?? null,
                size: specMat.size_bytes ?? null,
                extension: specMat.extension ?? "",
              }
            : null,
        };
      });
    setDraftSaving(true);
    setDraftElapsedSec(0);
    const startedAt = performance.now();
    try {
      const data = await createItemDraft({
        platform_account_id: platformAccountId,
        category_id: configCategoryId,
        name: title.trim(),
        //: ⚠️ 2026-10-10：保存草稿也用已上传对象（远程 url），不传相对路径。
        main_images: configImages.map((img) => ({
          materialId: img.materialId ?? "",
          path: img.path ?? "",
          link: img.link ?? img.url ?? "",
          url: img.url ?? "",
          file_name: img.file_name,
          width: img.width ?? null,
          height: img.height ?? null,
          size: img.size_bytes ?? null,
          extension: img.extension ?? "",
        })),
        detail_images: configDetailImages.map((img) => ({
          materialId: img.materialId ?? "",
          path: img.path ?? "",
          link: img.link ?? img.url ?? "",
          url: img.url ?? "",
          file_name: img.file_name,
          width: img.width ?? null,
          height: img.height ?? null,
          size: img.size_bytes ?? null,
          extension: img.extension ?? "",
        })),
        skus: draftSkus,
        //: 商品视频已上传对象（2026-10-11 补，真机 `itemData[0].videos[]`）。
        videos: configVideo
          ? [
              {
                materialId: configVideo.materialId ?? "",
                path: configVideo.path ?? "",
                link: configVideo.link ?? configVideo.url ?? "",
                url: configVideo.url ?? "",
                extension: configVideo.extension ?? "",
                duration: configVideo.duration ?? null,
                width: configVideo.width ?? null,
                height: configVideo.height ?? null,
                size: configVideo.size_bytes ?? null,
              },
            ]
          : undefined,
        precheck_token: token ?? "",
        //: **本地数据包绝对路径**（2026-10-10 补）：后端优先用它拼图片绝对路径。
        package_path: packagePath || undefined,
      });
      //: 新建草稿走 `create_product_drafts`（回 `productDraftId`），`draft_id` 有值。
      void message.success(
        data.draft_id ? `已保存到草稿箱（草稿 id ${data.draft_id}），未上架` : "已保存到草稿箱，未上架"
      );
    } catch (error) {
      void message.error(`保存草稿失败：${readReason(error, "保存草稿失败")}`);
    } finally {
      //: 终态定格耗时（与发布一致，用户能看到这次保存花了多久）。
      setDraftElapsedSec((performance.now() - startedAt) / 1000);
      setDraftSaving(false);
    }
  }, [configCategoryId, message, payload?.detail_images, payload?.main_images, platformAccountId, skus, title, token]);

  const close = useCallback(() => {
    //: 关窗即丢弃后端会话（⚠️ 只解除引用，不会删用户的目录）。
    if (token) void discardPackagePrecheck(token).catch(() => undefined);
    onClose();
  }, [onClose, token]);

  return (
    <Modal
      open={open}
      onCancel={close}
      //: 用户口径（2026-10-08）：弹窗 **宽 80% / 高 90%**（视口百分比）。
      width="80vw"
      //: ⚠️ **不能同时给 `style.top`** —— antd 的 `centered` 靠 `top:50%` 实现，
      //: 显式 top 会把它顶掉（实测被全站守卫抓住「假居中」）。
      //: 高度 90%：给标题栏 + 底部按钮留出余量，body 占 80vh
      //: （80 + 标题栏/按钮 ≈ 90% 视口）——整窗不溢出。
      styles={{
        body: {
          height: "80vh",
          maxHeight: "80vh",
          overflowY: "auto",
          //: 藏滚动条外观，保留滚动能力（用户口径 2026-10-08）。
          scrollbarWidth: "none",
          msOverflowStyle: "none",
        },
      }}
      className="hermes-hide-scrollbar"
      centered
      destroyOnHidden
      maskClosable={false}
      title="发布商品"
      footer={
        <Space>
          <Button onClick={close} disabled={submitting || draftSaving}>
            关闭
          </Button>
          {step > 0 && (
            <Button onClick={() => setStep(step - 1)} disabled={submitting || draftSaving}>
              上一步
            </Button>
          )}
          {step === 0 && (
            <Button
              type="primary"
              onClick={goStep2}
              //: ⚠️ **不在「没匹配 SKU 图」时禁用**（用户口径 2026-10-14：
              //: 「点击下一步时**未进行拦截提示**匹配 sku 图」）。
              //: 根因：此前 `disabled` 里含 `!precheckPassed`，而 disabled 的按钮
              //: **吞掉点击** ⇒ 用户既进不去、也看不到任何提示，只能猜。
              //: 项目既有口径：**门禁用「点击时提示」而非 `disabled`**
              //: ⇒ 只保留「加载中」这种真·不可点态，其余交给 `goStep2` 逐条报原因。
              disabled={loading || picking}
            >
              下一步：配置商品参数
            </Button>
          )}
          {step === 1 && (
            <Button
              type="primary"
              onClick={() => setStep(2)}
              //: ⚠️ 用户口径（2026-10-10）：「点击下一步发布时**校验助手中是否存在异常提示**，
              //: 存在则不允许进入下一步」⇒ 门禁**只看助手提示数**（唯一判据）。
              //: 此前多带的 `!configCategoryId`（类目）与 `skus.length === 0`（可提交行）
              //: 已**收进助手判据**（「商品类目」/「商品规格」两条）—— 否则会出现
              //: 「助手显示没有待处理提示，按钮却灰着」（用户实测 2026-10-10）。
              disabled={hintCount > 0}
            >
              下一步：确认发布
            </Button>
          )}
          {step === 2 && (
            <>
              {/* 用户口径（2026-10-11）：**提交按钮拆成两个**
                  「发布商品（直接上架不再下架）」/「发布并下架商品（发布成功后立刻下架）」。
                  ⚠️ 颜色口径（用户明确，同一轮追加）：
                    · **「发布商品」= 红色**（`danger`）—— 它是**唯一会留下在售商品**的动作，
                      视觉上必须是最「重」的那个，让人停一下；
                    · **「保存草稿」/「发布并下架商品」= 蓝色**（`type="primary"`）——
                      两者都不留后患（草稿不上架；发布并下架会自动收拾干净）。
                  ⚠️ **红线已于 2026-10-11 取消**：「发布后保持在售」成为正式入口。 */}
              <Button
                type="primary"
                onClick={() => void saveDraft()}
                disabled={submitting || draftSaving}
                loading={draftSaving}
              >
                保存草稿
              </Button>
              {/* 「发布商品」= 留在架上 ⇒ 红色 + tip 二次确认（用户口径：「tip二次确认」）。
                  用 Popconfirm（贴着按钮出现，不打断整页），文案明说后果。 */}
              {/* ⚠️ **Popconfirm 必须 `placement="top"`**（2026-07-11 实测隐患）：
                  默认 placement 会把气泡弹在**右侧**，「确认上架」按钮正好压在右边的
                  「发布并下架商品」上 ⇒ 用户点完确认，**手一落就又点到了下架按钮**
                  （真机现象：点「发布商品」，商品上架后立刻被下架 —— 两次点击连发）。
                  改 `top` 让气泡在按钮**上方**，确认键远离右侧的邻居按钮。
                  另加 `getPopupContainer` 锚定到 footer，避免气泡漂到别处。 */}
              <Popconfirm
                title="确认发布？"
                //: ⚠️ 文案**按平台真实行为**（2026-10-11 真机抓包 + 实测）：
                //:  `batch_create_item` 创建的商品天然进**审核**、审核通过前不可售
                //:  （真机平台自己提交后同样如此，且提交后**不再调任何上架接口**）。
                //:  原句「发布成功后商品将保持在售，不会自动下架」**与事实不符**
                //:  —— 商品不会立刻在售，会先进审核。已在下方保留原句作口径变更标记。
                description="发布后进入平台审核，通过后自动上架。"
                okText="确认上架"
                cancelText="取消"
                placement="top"
                onConfirm={() => void submit(false)}
                disabled={submitting || draftSaving}
              >
                <Button danger disabled={submitting || draftSaving} loading={submitting}>
                  发布商品
                </Button>
              </Popconfirm>
              {/* 「发布并下架」= 原红线行为 ⇒ 蓝色、不加二次确认
                  （与旧版「确认发布」的操作成本一致，不因为拆分而给老路径加门）。 */}
              <Button
                type="primary"
                onClick={() => void submit(true)}
                disabled={draftSaving}
                loading={submitting}
              >
                发布并下架商品
              </Button>
            </>
          )}
        </Space>
      }
    >
      {/* ⚠️ 等待蒙版（用户口径 2026-10-08）：「点击预检时页面加蒙版避免误触，
          并蒙版统计耗时」。
          ⚠️ 2026-10-08 二次口径：「文案应**始终保持在窗口居中**」——
          antd `Spin` 的 tip 是**跟着内容流滚**的（内容一长文案就滚出视野），
          所以改成自己的**固定遮罩**：`position: fixed` 铺满视口、
          flex 居中，文字永远在窗口正中，与滚动位置无关。 */}
      <Steps
        current={step}
        size="small"
        style={{ marginBottom: 16 }}
        items={[
          { title: "预检", description: "读取本地数据包" },
          { title: "配置商品参数", description: "标题 / 类目 / SKU" },
          { title: "发布", description: "确认并上架" },
        ]}
      />

      {/* ⚠️ **用户口径 2026-10-14**（「切换步骤后匹配好的 sku 图丢了」）：
          根因与第二步**同一个坑** —— 条件渲染 `{step === 0 && <PrecheckStep/>}`
          在切步时**卸载组件** ⇒ 内部 state（`appliedSkuImages` 匹配结果、
          `manualPicks` 手选图、勾选、表格编辑）全丢，切回来一片空白。
          修法：**保持挂载、只隐藏**（`display: none`）—— state 天然保留。

          ⚠️ 但「保持挂载」会让 `TablesPanel` 内部 state **活过 `reset()`**
          （用户口径 2026-10-14：「每次关闭发布商品弹窗，相关的属性弹窗中的参数
          需要清除干净，本次打开发现 sku 名称默认读取的不是原始表格中的值，
          是上一次正则处理过的列值」）。
          修法：`key={openSeq}` —— **每次打开弹窗换一个 key ⇒ 强制重挂**，
          内部 state 与 `reset()` 同批清空；步骤间切换 key 不变 ⇒ 仍然保留。
          ⇒ 两个诉求（切步保留 / 关窗清空）都满足。 */}
      <div key={openSeq} style={{ display: step === 0 ? undefined : "none" }}>
        <PrecheckStep
          packagePath={packagePath}
          onPick={() => void pickAndLoad()}
          picking={picking}
          loading={loading}
          manualPathFallback={manualPathFallback}
          onPackagePathChange={setPackagePath}
          onLoad={() => void loadWithPath(packagePath)}
          onPrecheck={() => void runPrecheck()}
          inspected={inspected}
          precheckPassed={precheckPassed}
          onSkuMatchRun={() => setSkuMatchRan(true)}
          mappingDraft={mappingDraft}
          onPreview={setPreview}
          precheckGroups={precheckGroups}
          onPrecheckGroupsChange={setPrecheckGroups}
          onOpenGroupPicker={() => setGroupPickerOpen(true)}
          groupOptions={groupOptions}
          token={token}
          precheck={precheck}
          checked={checked}
          onCheckedChange={setChecked}
          tables={tables}
          onTablesChange={setTables}
          onSkuSelectionChange={setSkuTableSelection}
          matchedSkuImages={matchedSkuImages}
          onSkuMatchChange={setMatchedSkuImages}
          zoneNames={mediaZones.map((z) => z.name)}
          itemNo={itemNo}
          onItemNoChange={setItemNo}
          onOpenRules={() => setRulesOpen(true)}
        />
      </div>

      {/* **数据包列规则**管理弹窗（用户口径 2026-10-14）——
          挂在**向导层**：它是「这次发布要用哪套提取规则」的配置，
          与第一步的表格数据无关（配置本身落库，切换数据包也还在）。 */}
      <DataPackageRulesModal
        open={rulesOpen}
        onClose={() => setRulesOpen(false)}
        //: 给「新增规则」的列名下拉用（省得手打；没有也能自由输入）。
        columnNames={(tables[0]?.columns ?? []).map((c) => String(c))}
      />

      {/* **区域映射弹窗**（用户口径 2026-10-13）：进入第二步的**第二个关卡**。 */}
      <ZoneMapModal
        open={zoneMapOpen}
        zones={mediaZones}
        counts={Object.fromEntries(mediaZones.map((z) => [z.name, zoneCount(z.name)]))}
        value={zoneMap}
        onChange={setZoneMap}
        onOk={confirmZoneMap}
        onCancel={() => setZoneMapOpen(false)}
      />

      {/* 用户口径（2026-10-10）：「来回步骤切换时，所有已填写的参数均应保持不清空，
          只有弹窗关闭或发布成功后清空」。
          ⚠️ 根因：条件渲染 `{step === 1 && <PublishConfigStep2/>}` 在切步时**卸载组件**
          ⇒ 内部 state（发货模式/规格/开售时间/售后/描述…）全丢。
          修法：**保持挂载、只隐藏**（`display: none`）—— state 天然保留；
          清空只发生在 `reset()`（关弹窗）与发布成功回写时。 */}
      {/* ⚠️ **换数据包要清第二步的 SKU 导入产物**（用户报障 2026-10-15：
          「重新读取资源后，SKU 导入的数据未清空」）。
          ⚠️ **第一版做法（已被推翻）**：给这个 div 挂 `key={openSeq}` 强制重挂。
          实测自伤 —— `openSeq` 自增会**连带把第一步 `PrecheckStep` 也重挂**
          （它也挂同一个 key）⇒ 第一步被打回「还没有加载数据包」的初始态，
          同时向导层的 `loading` 还在转 ⇒ 用户看到**两行资源选择框 +
          「正在读取本地数据包…」永不消失**。
          ⇒ 现做法：**不动组件树**，改传一个递增的 `resetToken`，
             由第二步**自己监听并清内部 state**（第一步完全不受影响）。 */}
      <div style={{ display: step === 1 ? undefined : "none" }}>
      {(
        /* 用户口径 2026-10-09：「第二步参数配置中的商品图片、商品标题、商品类目
           合并到下面各基础信息中」—— 即千帆第一部分（图/标题/类目）不再单独成块，
           而是作为 `headSlot` 并入第二部分的「基础信息」区最前面。
           「自动加载参数」按钮逻辑不变（仍在第一部分组件里）。 */
        <PublishConfigStep2
          resetToken={step2ResetToken}
          categoryPath={configCategoryPath}
          categoryId={configCategoryId}
          accountId={platformAccountId ?? 0}
          title={title}
          onTitleChange={setTitle}
          images={configImages}
          onImagesChange={setConfigImages}
          onConfigChange={setConfigSnapshot}
          onSpecImagesChange={setSpecImages}
          onHintCountChange={setHintCount}
          keyProperties={keyProperties}
          otherProperties={otherProperties}
          brandOptions={[]}
          specTypeOptions={specTypeOptions}
          shippingTemplates={shippingTemplates}
          shippingTemplatesLoading={shippingTemplatesLoading}
          defaultTemplateId={defaultTemplateId}
          logisticsTemplates={logisticsTemplates}
          logisticsTemplatesLoading={logisticsTemplatesLoading}
          priceRows={priceRows}
          onPriceRowsChange={setPriceRows}
          //: 需求 4：第二步只处理「预检通过的 SKU 规格」。
          passedSpecs={passedSpecs}
          //: 2026-10-11 最终版：规格导入是**商品规格区域**的独立入口（不在自动加载里
          //: —— 规格随类目变动）。向导负责提供数据源、映射默认值所依据的规格类型，
          //: 以及「上传本地图」所要的两个函数（都走向导那一份重试配置）。
          passedRows={filledSkuRows}
          specImportSources={mappingSources}
          priceSourceColumns={tables[0]?.columns ?? []}
          //: 用户改了价格库存列映射 ⇒ 按新映射**重新导出**通过行（本层持有表格与勾选态）。
          onRebuildRows={(priceMapping) =>
            passedSkuRows(
              tables[0] ?? null,
              skuTableSelection.blocked,
              skuTableSelection.picked,
              wizardLocalImages,
              priceMapping,
            )
          }
          withUploadRetry={withUploadRetry}
          detailImages={configDetailImages}
          onDetailImagesChange={setConfigDetailImages}
          video={configVideo}
          onVideoChange={setConfigVideo}
          onUploadVideoFile={uploadVideoFile}
          onUploadImageFile={uploadImageFile}
          specAttributes={specAttributes}
          //: ⚠️ **上报映射草稿**（第一步删列的二次确认要「点名」用了它的映射）。
          //: ⚠️ 依赖数组用的是 setState 的稳定引用 ⇒ 不需要 useCallback 包裹。
          onMappingDraftChange={setMappingDraft}
          skuImages={skuImagePaths}
          onUploadSkuImage={uploadOneFromPackage}
          onFetchPrecheckImage={(rel_path) => fetchPrecheckImage(token ?? "", rel_path)}
          shortTitle={configShortTitle}
          onShortTitleChange={setConfigShortTitle}
          propertyValues={propertyValues}
          onPropertyValuesChange={setPropertyValues}
          afterSaleRules={afterSaleRules}
          supportNoLogistics={supportNoLogistics}
          autoLoadSlot={
            <div className="config-toolbar">
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                loading={autoLoading}
                disabled={mainImagePaths.length === 0}
                onClick={() => setAutoConfirmOpen(true)}
              >
                自动加载参数
              </Button>
              <Text type="secondary" className="toolbar-hint">
                自动上传主图 → 生成标题 → 识别类目 → 上传详情图 → 上传视频 → 生成短标题 → 加载类目参数 → 生成属性
              </Text>
            </div>
          }
          headSlot={
            <PublishConfigStep1
              accountId={platformAccountId ?? 0}
              token={token ?? ""}
              mainImagePaths={mainImagePaths}
              images={configImages}
              onImagesChange={setConfigImages}
              title={title}
              onTitleChange={setTitle}
              categoryPath={configCategoryPath}
              onCategoryChange={setConfigCategoryPath}
              onCategoryIdChange={setConfigCategoryId}
            />
          }
        />
      )}
      </div>

      {/* ⚠️ 分组选择弹窗（用户口径：「点击预检发布商品将违规词分组选择使用弹窗展示」）。
          空选 = 全部分组（不是「一个都不选」）—— 所以「清空」按钮的文案要说清这一点。 */}
      <Modal
        open={groupPickerOpen}
        title="选择违规词检测分组"
        width="480px"
        centered
        onCancel={() => setGroupPickerOpen(false)}
        onOk={() => setGroupPickerOpen(false)}
        okText="确定"
        cancelText="取消"
        destroyOnClose
      >
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 12 }}>
          只检测勾选分组里的违规词；不勾选 = 检测全部分组。
        </Text>
        <Checkbox.Group
          style={{ display: "flex", flexDirection: "column", gap: 8 }}
          value={precheckGroups}
          onChange={(value) => setPrecheckGroups(value as string[])}
          options={groupOptions.map((g) => ({
            value: g.name,
            label: (
              <Space size={6}>
                <span>{g.name || UNGROUPED_LABEL}</span>
                <Tag>{g.count} 条</Tag>
                {/* 已停用的组：选了也不会参与匹配（后端按组过滤），标出来免得白选。 */}
                {!g.enabled && <Tag color="default">已停用，不参与检测</Tag>}
              </Space>
            ),
          }))}
        />
        {groupOptions.length === 0 && (
          <Text type="secondary">还没有任何分组（可先去商品台账的「违规词」里新增）</Text>
        )}
        <div style={{ marginTop: 12 }}>
          <Button size="small" onClick={() => setPrecheckGroups([])}>
            清空（= 检测全部分组）
          </Button>
        </div>
      </Modal>

      {preview && (
        <MediaPreview
          token={token as string}
          items={preview.items}
          index={preview.index}
          onIndexChange={(next) => setPreview({ ...preview, index: next })}
          onClose={() => setPreview(null)}
        />
      )}

      {/* 自动加载参数的二次确认（用户口径 2026-10-09：按钮在「基础信息」顶部，
          点开确认后全自动跑完各阶段）。 */}
      <Modal
        open={autoConfirmOpen}
        title="自动加载参数"
        centered
        okText="确认，开始自动加载"
        cancelText="取消"
        confirmLoading={autoLoading}
        onCancel={() => setAutoConfirmOpen(false)}
        onOk={() => {
          setAutoConfirmOpen(false);
          setAutoProgressOpen(true);
          void autoLoadAll();
        }}
        destroyOnClose
      >
        <p>将按顺序自动完成：</p>
        <ol>
          <li>上传数据包主图到图片空间（文件名按内容 MD5 生成）</li>
          <li>用主图生成商品标题并采用</li>
          <li>按标题自动识别商品类目</li>
          <li>上传数据包详情图</li>
          <li>上传数据包商品视频</li>
          <li>生成导购短标题</li>
          <li>按类目加载商品参数（属性定义 / 规格类型）</li>
          <li>按类目 + 主图智能生成商品属性</li>
        </ol>
        <p>过程中会覆盖当前的标题、类目、短标题与属性；主图/详情图会追加，不删除已有图。</p>
        {/* 2026-10-11 用户口径：上传媒体资源失败重试开关 + 重试次数（1-3，默认 1）。 */}
        <div style={{ marginTop: 12 }}>
          <Space align="center" size={8} wrap>
            <Text>上传媒体资源失败时重试</Text>
            <Radio.Group
              value={uploadRetryOnFailure ? "yes" : "no"}
              onChange={(e) => setUploadRetryOnFailure(e.target.value === "yes")}
            >
              <Radio value="yes">是</Radio>
              <Radio value="no">否</Radio>
            </Radio.Group>
            <Text>，重试次数</Text>
            <InputNumber
              min={1}
              max={3}
              step={1}
              value={uploadMaxRetries}
              disabled={!uploadRetryOnFailure}
              onChange={(value) => setUploadMaxRetries(Math.max(1, Math.min(3, Number(value) || 1)))}
              style={{ width: 80 }}
            />
          </Space>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              重试只作用于媒体上传（主图 / 详情图 / 视频）；次数为「失败后重跑的遍数」。
            </Text>
          </div>
        </div>

      </Modal>

      {/* 执行进度弹窗（用户口径 2026-10-09：「窗口居中展示弹窗执行进度列表，
          列出流程的每一阶段执行情况」）—— 居中小窗，逐阶段显示状态与结果。
          执行中不可关闭（避免误关后不知结果）；跑完可关闭。 */}
      <Modal
        open={autoProgressOpen}
        title="自动加载参数 · 执行进度"
        centered
        width={520}
        maskClosable={false}
        closable={!autoLoading}
        footer={
          <Button type="primary" disabled={autoLoading} onClick={() => setAutoProgressOpen(false)}>
            关闭
          </Button>
        }
        onCancel={() => {
          if (!autoLoading) setAutoProgressOpen(false);
        }}
      >
        <div className="auto-stage-list">
          {autoProgress.map((s, i) => (
            <div className="auto-stage-row" key={`${s.title}-${i}`}>
              <span className="auto-stage-icon">
                {s.status === "running" && <LoadingOutlined />}
                {/* ⚠️ 用户口径（2026-10-11）：「商品主图详情图，**出现上传失败则应修改为
                    红色标题和红色对号图标**」—— 阶段 status 仍是 done（流程跑完了），
                    但这一份详情里有失败项 ⇒ **对号也要变红**，不能只红标题
                    （绿勾 + 红字自相矛盾，用户第一眼看的是图标）。 */}
                {s.status === "done" &&
                  (uploadDetailKeyFor(s.title) &&
                  uploadDetails[uploadDetailKeyFor(s.title)!].some((r) => !r.ok) ? (
                    <CloseCircleFilled style={{ color: "#ff4d4f" }} />
                  ) : (
                    <CheckCircleFilled style={{ color: "#52c41a" }} />
                  ))}
                {s.status === "failed" && <CloseCircleFilled style={{ color: "#ff4d4f" }} />}
                {s.status === "skipped" && (
                  <span className="auto-stage-skip-dot" aria-label="已跳过" />
                )}
              </span>
              {/* ⚠️ 用户口径（2026-10-11）：「上传商品主图和上传商品详情图后端统计所有图片
                  上传的详情，**新增一个详情点击弹窗**展示对应的详情」
                  ⇒ 这两行的**标题**变成可点（带下划虚线提示），点开看逐张清单。 */}
              {(() => {
                const which = uploadDetailKeyFor(s.title);
                //: ⚠️ 用户口径（2026-10-11）：「已上传 14/38 张详情图（24 张失败…）
                //: **应将标题置红色**」⇒ 有失败时标题整体变红，一眼就能看到出事了。
                //: 判据 = **这一份详情里真有失败项**（不是看文案里有没有「失败」二字 ——
                //: 那样文案一改就失效；直接查数据才是真判据）。
                const hasFailure = which
                  ? uploadDetails[which].some((r) => !r.ok)
                  : false;
                return which ? (
                  <span
                    className={`auto-stage-title auto-stage-title-link${
                      hasFailure ? " auto-stage-title-failed" : ""
                    }`}
                    role="button"
                    tabIndex={0}
                    title="查看逐张上传详情"
                    onClick={() => setUploadDetailOpen(which)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") setUploadDetailOpen(which);
                    }}
                  >
                    {s.title}
                    {/* ⚠️ 「详情」小标**单独一行样式**（不与标题挤在一起）：
                        用户截图里「上传商品详情图详情」连成一串分不清。改为
                        `margin-inline-start` 拉开 + 竖线分隔，视觉上明确是附属入口。 */}
                    <span className="auto-stage-link-hint">详情</span>
                  </span>
                ) : (
                  <span className="auto-stage-title">{s.title}</span>
                );
              })()}
              <span className="auto-stage-detail" title={s.detail ?? ""}>
                {s.detail ?? (s.status === "pending" ? "等待中" : "")}
              </span>
              {/* 耗时统计（用户口径 2026-10-09）：终态显示定格耗时；运行中按秒表涨。 */}
              <span className="auto-stage-elapsed">
                {s.status === "running"
                  ? formatStageElapsed(runningElapsedMs(s, autoTick))
                  : formatStageElapsed(s.ms)}
              </span>
            </div>
          ))}
        </div>
        {/* ⚠️ 用户口径（2026-10-11）：「**在底部添加一个提示，点击标题可查看上传详情信息**」
            —— 进度弹窗底部常驻一行说明，用户才知道标题能点（只靠虚下划线提示太弱）。
            只在**真的有逐张详情**时显示（没跑过上传时不必出现这条）。 */}
        {uploadDetails.main.length + uploadDetails.detail.length > 0 ? (
          <div className="auto-stage-footer-hint">
            点击「上传商品主图」/「上传商品详情图」标题可查看逐张上传详情
          </div>
        ) : null}
      </Modal>

      {/* **上传逐张详情弹窗**（用户口径 2026-10-11：「新增一个详情点击弹窗展示对应的详情」）。
          ⚠️ 存在的理由：并发上传时**单张失败不再静默**，用户必须能看到
          「应有 N 张 / 成功 M 张 / 失败的是哪些、为什么」——否则丢了 24 张也无从发现。 */}
      <Modal
        open={uploadDetailOpen !== null}
        onCancel={() => setUploadDetailOpen(null)}
        footer={null}
        width="60vw"
        centered
        title={uploadDetailOpen === "main" ? "上传商品主图 · 逐张详情" : "上传商品详情图 · 逐张详情"}
        className="upload-detail-modal"
      >
        {(() => {
          const rows = uploadDetailOpen ? uploadDetails[uploadDetailOpen] : [];
          const okCount = rows.filter((r) => r.ok).length;
          const badCount = rows.length - okCount;
          return (
            <>
              <div className="upload-detail-summary">
                共 {rows.length} 张：成功 <b>{okCount}</b> 张
                {badCount > 0 ? (
                  <>
                    ，失败 <b style={{ color: "#ff4d4f" }}>{badCount}</b> 张
                  </>
                ) : null}
              </div>
              <Table
                size="small"
                rowKey={(r) => r.rel_path}
                dataSource={rows}
                pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
                scroll={{ y: 380 }}
                columns={[
                  {
                    title: "序号",
                    width: 64,
                    render: (_v, _r, i) => i + 1,
                  },
                  {
                    title: "文件",
                    dataIndex: "file_name",
                    //: 长名截断 + 悬停看全（与全站口径一致）。
                    render: (v: string) => (
                      <span title={v} className="upload-detail-name">
                        {v}
                      </span>
                    ),
                  },
                  {
                    title: "结果",
                    width: 90,
                    render: (_v, r) =>
                      r.ok ? (
                        <Tag color="green">成功</Tag>
                      ) : (
                        <Tag color="red">失败</Tag>
                      ),
                  },
                  {
                    title: "原因",
                    //: 成功时不显原因（留空），失败时必须说清 —— 这就是用户要的「缺哪些张」。
                    render: (_v, r) =>
                      r.ok ? (
                        <span className="upload-detail-ok">—</span>
                      ) : (
                        <span className="upload-detail-reason" title={r.reason}>
                          {r.reason}
                        </span>
                      ),
                  },
                ]}
              />
            </>
          );
        })()}
      </Modal>

      {step === 2 && (
        <PublishStep
          shopName={shopName}
          title={title}
          categoryName={configCategoryPath[configCategoryPath.length - 1] ?? ""}
          shortTitle={configShortTitle}
          config={configSnapshot}
          images={configImages}
          video={configVideo}
          detailImages={configDetailImages}
          specImages={specImages}
          priceRows={priceRows}
          skus={skus}
          result={result}
        />
      )}
      {/* **提交蒙版 + 耗时统计**（用户口径 2026-10-10：「点击发布按钮后页面加蒙版加耗时统计」）。
          与上面的等待遮罩同款（`position: fixed` 铺满视口 + 居中），
          文案带**实时耗时** ⇒ 用户看得见「在跑、跑了多久」，不会重复点「确认发布」
          （这也正是「一直转圈」的观感来源：真实上传+发布+下架两段轮询需 1.5 分钟上下）。 */}
      {submitting && (
        <WaitMask
          open
          scope="page"
          title={submitShelfMode ? "正在发布并下架商品" : "正在发布商品（保持在售）"}
          elapsed={submitElapsedSec}
          sub={
            submitShelfMode
              ? "发布成功后会立即下架；请勿关闭本窗口或重复点击。"
              : "发布成功后商品将保持在售、不会自动下架；请勿关闭本窗口或重复点击。"
          }
        />
      )}
      {/* **保存草稿蒙版 + 耗时**（用户口径 2026-10-10：保存应与发布一样加蒙版统计耗时）。 */}
      {draftSaving && (
        <WaitMask
          open
          scope="page"
          title="正在保存草稿"
          elapsed={draftElapsedSec}
          sub="保存到草稿箱（未上架）；请勿关闭本窗口或重复点击。"
        />
      )}
      {/* 等待遮罩：`position: fixed` 铺满视口 + flex 居中 ⇒
          文案**永远在窗口正中**，与弹窗内滚动位置无关
          （antd `Spin` 的 tip 跟着内容流滚，长内容时会滚出视野）。 */}
      {waitLabel !== null && (
        <WaitMask
          open
          scope="page"
          title={
            progress && progress.total > 0
              ? `${progress.phase ?? waitLabel}（${progress.done}/${progress.total}）`
              : waitLabel
          }
          elapsed={waitElapsedSec}
          extra={
            progress && progress.total > 0 ? (
              <Progress percent={progress.percent} style={{ width: 280 }} />
            ) : null
          }
        />
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ *
 * 第一步：预检
 * ------------------------------------------------------------------ */

function PrecheckStep(props: {
  packagePath: string;
  onPick: () => void;
  picking: boolean;
  loading: boolean;
  /** 无图形界面时允许手填路径（后端 `NO_DESKTOP`）。 */
  manualPathFallback: boolean;
  /** 手填路径时回写（仅 `manualPathFallback` 时用）。 */
  onPackagePathChange: (next: string) => void;
  onLoad: () => void;
  onPrecheck: () => void;
  /** 是否已跑过违规词检测。 */
  inspected: boolean;
  /** 预检是否通过（决定能否进第二步）。 */
  precheckPassed: boolean;
  /** 上报「用户已执行过 SKU 图匹配」（用户口径 2026-10-13：进第二步的关卡）。
   *
   *  ⚠️ 一路透传到向导 —— 关卡判在向导的「下一步」（`goStep2`）。 */
  onSkuMatchRun: () => void;
  /** 打开大图预览（同组内左右切换）。 */
  onPreview: (next: { items: PrecheckMediaItem[]; index: number }) => void;
  /** 预检用的违规词分组（空 = 全部分组）。 */
  precheckGroups: string[];
  onPrecheckGroupsChange: (next: string[]) => void;
  onOpenGroupPicker: () => void;
  groupOptions: ForbiddenWordGroup[];
  token: string | null;
  precheck: PrecheckResult | null;
  checked: Record<string, boolean>;
  onCheckedChange: (next: Record<string, boolean>) => void;
  tables: PrecheckTable[];
  onTablesChange: (next: PrecheckTable[]) => void;
  /** **第二步映射草稿的镜像**（用户口径 2026-10-15：删列二次确认要「点名」+
   *  SKU 名称列取值要按映射）。 */
  mappingDraft: {
    specSources: string[];
    priceSources: string[];
    priceMapping: PriceMappingRow[];
  };
  /** 上报 SKU 表的选择快照（需求 4：第二步要用同一份判据过滤）。 */
  onSkuSelectionChange: (next: { blocked: number[]; picked: number[] }) => void;
  /** **SKU 图匹配结果**（用户口径 2026-10-13：第二步「导入 SKU 图」的唯一数据源）。 */
  matchedSkuImages: { specName: string; specValue: string; relPath: string; fileName: string }[];
  onSkuMatchChange: (
    next: { specName: string; specValue: string; relPath: string; fileName: string }[],
  ) => void;
  /** 数据包里的区域名（SKU 图匹配弹窗的「图片区域」下拉候选）。 */
  zoneNames: string[];
  /** **商品货号**（用户口径 2026-10-14）：默认空、必填，`goStep2` 校验非空。
   *  ⚠️ 状态住在**向导**（判据也那一层）—— 面板只负责渲染与回传。 */
  itemNo: string;
  onItemNoChange: (next: string) => void;
  /** 打开「提取数据规则」管理弹窗（用户口径 2026-10-14）。 */
  onOpenRules?: () => void;
}) {
  const {
    packagePath, onPick, picking, loading, onLoad, manualPathFallback, onPackagePathChange,
    onPrecheck, inspected, precheckPassed, onPreview, onSkuMatchRun, mappingDraft,
    precheckGroups, onOpenGroupPicker, groupOptions,
    token, precheck, checked, onCheckedChange, tables, onTablesChange,
    onSkuSelectionChange, matchedSkuImages, onSkuMatchChange, zoneNames,
    itemNo, onItemNoChange, onOpenRules,
  } = props;

  /**
   * 区域批量勾选。
   *
   * ⚠️ **命中违规词的项永远不许被打开** —— 用户口径 2026-10-08：
   * 「命中违规词的不允许勾选至灰」。所以「全选 / 反选」也必须遵守这条，
   * 否则用户点一下「全选」就把禁选的违规项绕过去了（禁选形同虚设）。
   */
  const setZone = (keys: string[], value: boolean, locked: Set<string>) => {
    const next = { ...checked };
    for (const key of keys) {
      if (!value && locked.has(key)) continue; // 取消勾选对已锁的无意义（本来就是关的）
      next[key] = value && !locked.has(key) ? true : false;
    }
    onCheckedChange(next);
  };

  /** **因取消勾选而被移除的 SKU 图数**（用户口径 2026-10-11 要给提示）。
   *
   *  口径：媒体区里**可选的**（已检测 + 非违规）、但**未勾选**的 SKU 图数量 ——
   *  这些图已从规格列表的本地图列移除，必须让用户知道原因。 */
  const excludedByUncheck = countUncheckedSkuMedia(
    //: ⚠️ 同上：按 `kind === "sku"` 取 SKU 类区域（目录名可能叫 `SKU图`）。
    (precheck?.media ?? []).filter((m) => m.kind === "sku"),
    checked,
  );

  return (
    <div>
      <Space.Compact style={{ width: "100%", marginBottom: 12 }}>
        {/* ⚠️ 用户口径 2026-10-08：「地址栏**不允许输入**，只能预览」——
            路径唯一来源是「选择文件夹」（后端弹系统选择器）。
            ⚠️ 例外（2026-10-12）：后端 `NO_DESKTOP`（无图形界面弹不出框）时，
            解锁成本可手填 —— 否则既不让弹框又不让输 = 死路。
            用 `readOnly` 而非 `disabled`：内容仍可选中复制（预览要能看全）。 */}
        <Input
          prefix={<FolderOpenOutlined />}
          placeholder={
            manualPathFallback
              ? "填写数据包目录的绝对路径（如 D:\\商品数据包\\商品ID_123）"
              : "请点击右侧「选择文件夹」"
          }
          value={packagePath}
          readOnly={!manualPathFallback}
          onChange={(e) => onPackagePathChange(e.target.value)}
          //: readOnly 下仍允许聚焦选中，便于用户复制路径核对；手填模式放开输入。
          onKeyDown={manualPathFallback ? undefined : (e) => e.preventDefault()}
          style={{ cursor: manualPathFallback ? "text" : "default" }}
        />
        <Button icon={<FolderOpenOutlined />} onClick={onPick} loading={picking} disabled={loading}>
          选择文件夹
        </Button>
        <Button onClick={onLoad} loading={loading} disabled={!packagePath.trim()}>
          加载数据包
        </Button>
        {/* ⚠️ 分组选择走**弹窗**（用户口径 2026-10-08：「点击预检发布商品将违规词
            分组选择使用**弹窗**展示」）—— 内联多选框在工具栏里太挤，分组多了
            也没法好好列。空 = 全部分组。 */}
        <Button
          icon={<SettingOutlined />}
          onClick={onOpenGroupPicker}
          disabled={!precheck || loading}
        >
          违规词分组
          {precheckGroups.length > 0 && <Tag color="blue">{precheckGroups.length}</Tag>}
        </Button>
        <Button
          type="primary"
          onClick={onPrecheck}
          //: ⚠️ **支持重复预检**（用户口径 2026-10-08）：改完违规词库、或想再核一遍
          //: 都该能重跑。只在「没读过数据包」或「正在跑」时禁用 —— 检过之后**不禁用**。
          disabled={!precheck || loading}
          loading={inspected && loading}
        >
          {/* ⚠️ 用户口径 2026-10-13：「预检按钮只预检图片是否存在违规词，
              按钮的文案使用重新明确」。
              旧文案「预检」太泛（用户不知道在检什么），且与「SKU 图匹配」混在
              同一个概念里。改成**说清检的是什么** —— 与工具栏的「违规词分组」
              同族，一眼看出是一组。 */}
          {inspected ? "重新检测违规词" : "检测违规词"}
        </Button>
      </Space.Compact>

      {/* **检测范围（左） + 检测状态（右）= 同一行**（用户口径 2026-10-14：
          「把这一行的提示与『违规词检测范围：全部分组』一行，**一左一右**布局」）。
          ⚠️ 两条原本是上下两行（各占 18px），合并后省一行版面；
          `justifyContent: space-between` 让范围贴左、状态贴右。
          ⚠️ 告警多时不换行堆叠 —— 用 `title` 挂 Tooltip 看全（保持一行）。
          ⚠️ `marginTop: 10`（用户口径「距顶部资源选中框 **10px**」）。 */}
      {precheck && (
        <div
          style={{
            marginTop: 10,
            marginBottom: 8,
            fontSize: 12,
            lineHeight: "18px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          {/* 左：当前生效的检测范围（弹窗选完要能一眼看见）。 */}
          <Text type="secondary" style={{ fontSize: 12 }}>
            违规词检测范围：
            {precheckGroups.length === 0
              ? "全部分组"
              : precheckGroups.map((g) => g || UNGROUPED_LABEL).join("、")}
          </Text>
          {/* 右：检测状态 + （有告警时的）第一条告警。 */}
          <span style={{ textAlign: "right" }}>
            <Text
              type={!inspected ? "secondary" : precheckPassed ? "success" : "warning"}
              style={{ fontSize: 12 }}
            >
              {!inspected
                ? "尚未检测违规词"
                : precheckPassed
                  ? "违规词检测通过"
                  : "违规词检测未通过：有文件未能检测"}
            </Text>
            {precheck.warnings.length > 0 && (
              <Tooltip title={precheck.warnings.join("\n")}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {"  ·  "}
                  {precheck.warnings[0]}
                  {precheck.warnings.length > 1
                    ? `（等 ${precheck.warnings.length} 条）`
                    : ""}
                </Text>
              </Tooltip>
            )}
          </span>
        </div>
      )}

      {loading && (
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {inspected
            ? "正在按当前违规词库重新检测…"
            : precheck
              ? "正在检测违规词（图片多时要几十秒）…"
              : "正在读取本地数据包…"}
        </Paragraph>
      )}

      {/* ⚠️ 告警 Alert 已**合并进上面的状态条**（用户口径 2026-10-13：
          两条提示要合成一条）—— 这里不再单独渲染，否则又变成两行并排。 */}

      {!precheck ? (
        <Empty description="还没有加载数据包" />
      ) : (
        <Tabs
          items={[
            {
              key: "media",
              label: `媒体资源（${precheck.media.length}）`,
              children: (
                <>
                  {/* ⚠️ 用户口径 2026-10-13：「按照读取到的文件夹分区域，不做任何操作
                      直接展示」⇒ 区域**动态**来自数据包（`zoneNamesOf`），
                      不再查固定四值常量。旧写法 `ZONES.map` 在包 A 上只渲染出
                      一个区（其余 4 个目录名不在表里 ⇒ 整块消失）。 */}
                  {zoneNamesOf(precheck.media).map((zoneName) => {
                    const items = mediaOfZone(precheck.media, zoneName);
                    //: 区域名原值（中文目录名）—— 直接用，不再查 label 表。
                    const kind = items[0]?.kind;
                    const keys = items.map((m) => mediaKey(m));
                    //: ⚠️ 命中违规词的项**禁选**（用户口径 2026-10-08：「命中的不允许
                    //: 勾选至灰」）—— 三个批量按钮也必须认这份名单，否则「全选」就能绕过。
                    //: ⚠️ 用户口径 2026-10-13 方案 C：**平台不收的格式也不可选**
                    //: （它照常展示 + 红框，但默认不勾、发布跳过）。
                    const locked = new Set(
                      items
                        .filter((m) => m.has_forbidden || m.unsupported || m.oversized)
                        .map((m) => mediaKey(m)),
                    );
                    return (
                      <div key={zoneName} style={{ marginBottom: 16 }}>
                        <Space style={{ marginBottom: 8 }}>
                          <Text strong>
                            {zoneName}
                            {kind ? (
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                {" "}
                                （{KIND_LABELS[kind]}）
                              </Text>
                            ) : null}
                            （{items.filter((m) => checked[mediaKey(m)]).length}/{items.length}）
                          </Text>
                          {locked.size > 0 && (
                            <Tag color="error">{locked.size} 个不可选（违规词 / 格式 / 超限）</Tag>
                          )}
                          <Button
                            size="small"
                            onClick={() => setZone(keys, true, locked)}
                            //: 该区全被锁 ⇒ 全选无意义，置灰（避免用户点了没反应以为坏了）。
                            disabled={items.every((m) => locked.has(mediaKey(m)))}
                          >
                            全选
                          </Button>
                          <Button size="small" onClick={() => setZone(keys, false, locked)}>
                            全不选
                          </Button>
                          <Button
                            size="small"
                            onClick={() =>
                              setZone(
                                keys,
                                !items.every((m) => checked[mediaKey(m)]),
                                locked,
                              )
                            }
                          >
                            反选
                          </Button>
                        </Space>
                        <MediaGrid
                          items={items}
                          token={token as string}
                          checked={checked}
                          locked={locked}
                          onToggle={(key, value) => onCheckedChange({ ...checked, [key]: value })}
                          //: ⚠️ 预览只在**同组内**左右切换（用户口径：「支持左右切换
                          //: 不是所有」）—— 一组的上下文才有意义（主图之间比一比）。
                          onPreview={(index) => onPreview({ items, index })}
                        />
                      </div>
                    );
                  })}
                </>
              ),
            },
            {
              key: "tables",
              //: ⚠️ 用户口径（2026-10-09）：页签名从「表格」改为**「商品sku表格」**
              //: （该页签装的就是 SKU / 规格表）。
              //: 2026-10-11：属性图 OCR 已移除 ⇒ 后端不再产出「属性 KV 表」，
              //: 这里的 `kv` 过滤随之删掉（死判据留着会让以后误加回的表被静默隐藏）。
              label: `商品sku表格（${tables.length}）`,
              children: (
                <>
                  {/* ⚠️ 用户口径 2026-10-11：媒体资源里取消勾选的 SKU 图会**连带**
                      从规格列表移除 —— 必须给提示，否则用户只会看到本地图「莫名少了」
                      而不知道自己取消过勾选（静默变化比不提示更糟）。 */}
                  {/* 一行小字（与上面的状态条同款，不占版面）。 */}
                  {excludedByUncheck > 0 && (
                    <div style={{ marginBottom: 8, fontSize: 12, lineHeight: "18px" }}>
                      <Text type="warning" style={{ fontSize: 12 }}>
                        {excludedByUncheck} 张 SKU 图未勾选，已从规格列表移除
                      </Text>
                    </div>
                  )}
                {/* **商品货号**（用户口径 2026-10-14）：
                    「商品sku表格tab下新增一个商品货号参数，默认是为空**必填**，
                     点击下一步时需校验非空」。

                    ⚠️ 放在**表格之上**、与「SKU 图自动匹配」同一条工具行 ——
                    它是这次发布的**商品级参数**，不是表格里的列（不写进 `tables`，
                    否则会被当成数据包的一列渲染，与「读到什么就展示什么」冲突）。 */}
                <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
                  <Text strong style={{ fontSize: 12 }}>
                    商品货号
                  </Text>
                  <Text type="danger" style={{ fontSize: 12 }}>
                    *
                  </Text>
                  <Input
                    size="small"
                    style={{ width: 220 }}
                    value={itemNo}
                    onChange={(e) => onItemNoChange(e.target.value)}
                    placeholder="必填，例如 SP20261014001"
                  />
                  {/* **提取数据规则**（用户口径 2026-10-14）：
                      「在商品 sku 表格 tab 下新增**提取数据规则**按钮，
                       使用弹窗**全屏的 60%** 实现 CRUD」。
                      ⇒ 与「商品货号」同一条工具行（都是这次发布的**配置**，不是表格的列）。 */}
                  <Button
                    size="small"
                    icon={<SettingOutlined />}
                    style={{ marginLeft: "auto" }}
                    onClick={() => onOpenRules?.()}
                  >
                    提取数据规则
                  </Button>
                </div>
                <TablesPanel
                  token={token as string}
                  //: 需求 3：本地图列的数据源 = 数据包 `SKU/` 目录的媒体
                  //: （按**文件名里的 SKU 名称**与表格行对齐，不是按序号）。
                  skuMedia={(precheck.media ?? []).filter((m) => m.kind === "sku")}
                  //: ⚠️ 用户口径 2026-10-11：媒体资源里**取消勾选**的 SKU 图，
                  //: 对应的 SKU 规格行本地图**也要被移除** ⇒ 面板必须知道勾选状态。
                  checked={checked}
                  onSelectionChange={onSkuSelectionChange}
                  //: 用户口径 2026-10-13：匹配结果上抛（第二步「导入 SKU 图」的数据源）。
                  onSkuMatchChange={onSkuMatchChange}
                  zoneNames={zoneNames}
                  precheckPassed={precheckPassed}
                  onSkuMatchRun={onSkuMatchRun}
                  allMedia={precheck.media ?? []}
                  //: ⚠️ **把第二步的映射草稿传进第一步的表格**（用户口径 2026-10-15）——
                  //: 删除列时的二次确认要**点名**「该列已被商品 SKU 映射 / 价格库存映射使用」。
                  //: 不传的话只能给通用文案，而那正是用户会误删的原因（他不知道会影响第二步）。
                  //: ⚠️ 这两份是**第二步的 state**，第一步页签挂载时它们可能还是空的
                  //: （用户没走到第二步）—— 那时提示退化为「该列数据会一并删除」，是诚实的。
                  specImportSources={mappingDraft.specSources}
                  priceSources={mappingDraft.priceSources}
                  priceMapping={mappingDraft.priceMapping}
                  tables={tables}
                  //: ⚠️ 面板只显示 SKU 表（`tables` 的子集），改完必须**按真实下标**
                  //: 嵌回**完整数组**再落库 —— 直接上抛子集会把属性表（kv）丢掉，
                  //: 页签随之重建、面板状态被重置（用户实测：第二次改库存无响应）。
                  onTableChange={(panelIndex, next) =>
                    onTablesChange(mergePanelTable(tables, panelIndex, next))
                  }
                />
                </>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

function MediaGrid(props: {
  items: PrecheckMediaItem[];
  token: string;
  checked: Record<string, boolean>;
  /** 禁选的键（命中违规词）—— 复选框 disabled + 整块置灰。 */
  locked: Set<string>;
  onToggle: (key: string, value: boolean) => void;
  /** 点缩略图打开大图预览（同组内左右切换）。 */
  onPreview: (index: number) => void;
}) {
  const { items, token, checked, locked, onToggle, onPreview } = props;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
        gap: 8,
      }}
    >
      {items.map((item, index) => {
        const key = mediaKey(item);
        const isVideo = isVideoFile(item.file_name || item.rel_path);
        const isLocked = locked.has(key) || !!item.has_forbidden;
        //: ⚠️ **方案 C**（用户口径 2026-10-13）：平台不收的格式 / 超限的图
        //: **照常展示**，但要**红框 + 原因**（不是静默丢弃）。
        const badReason = item.has_forbidden
          ? "命中违规词，不可选"
          : item.unsupported
            ? unsupportedText(item)
            : item.oversized
              ? "超过平台单文件 5M 上限"
              : "";
        return (
          <div
            key={key}
            style={{
              //: 违规词 = 红框（既有口径，不变）；格式/超限 = 橙红框（同类但可区分）。
              border: item.has_forbidden
                ? "2px solid #ff4d4f"
                : badReason
                  ? "2px solid #ff7a45"
                  : "1px solid #f0f0f0",
              borderRadius: 6,
              padding: 6,
              //: ⚠️ 命中违规词 ⇒ 整块**置灰**（用户口径 2026-10-08：「不允许勾选**至灰**」）——
              //: 不只是默认不勾，而是要一眼看出「这个不能选」。
              background: isLocked ? "#f0f0f0" : "#fafafa",
              opacity: isLocked ? 0.72 : 1,
            }}
          >
            <div
              style={{
                position: "relative",
                width: "100%",
                aspectRatio: "1 / 1",
                cursor: "zoom-in",
              }}
              //: ⚠️ 视频**也要能点开**（用户口径 2026-10-08：「视频无法预览」是缺陷）。
              //: 之前这里是 `isVideo ? undefined : ...` —— 视频点了没反应。
              onClick={() => onPreview(index)}
              title={isVideo ? "点击预览视频" : "点击查看大图"}
            >
              <PrecheckThumb token={token} item={item} />
              {(item.hits ?? []).length > 0 && (
                <Tag color="error" style={{ position: "absolute", left: 2, top: 2 }}>
                  {(item.hits ?? []).map((h) => h.word).join("、")}
                </Tag>
              )}
              {/* ⚠️ **方案 C**（用户口径 2026-10-13）：平台不收的格式 / 超限的图
                  照常展示，但要在**卡片上**给出原因（不能只靠边框颜色让用户猜）。
                  用 Tooltip 包整卡（见下）会让文案藏在悬停里 ⇒ 这里给个角标。 */}
              {!!badReason && !item.has_forbidden && (
                <Tooltip title={badReason}>
                  <Tag
                    color="warning"
                    style={{ position: "absolute", left: 2, top: 2, fontSize: 11 }}
                  >
                    不可发布
                  </Tag>
                </Tooltip>
              )}
              {/* ⚠️ 用户口径 2026-10-09：「OCR 检测移除，页面不要展示 ocr」——
                  OCR 的内部指标（已抽 N 帧 / 未检测 / 帧识别失败）**不上屏**。
                  页面只保留**违规词命中**这一结果（那是预检的目的）。 */}
            </div>
            <Checkbox
              checked={!!checked[key]}
              //: 禁选（置灰）—— 命中违规词的项**结构上点不了**，不是靠用户自觉。
              disabled={isLocked}
              onChange={(e) => onToggle(key, e.target.checked)}
              style={{ marginTop: 6, fontSize: 12, maxWidth: "100%" }}
            >
              {/* ⚠️ 文件名可能很长（实测 `SKU21_颜色分类(【中古风】伸缩桌+六斗柜
                  梳妆台+妆凳+一字板上柜).jpg`）⇒ **单行省略**，全名放 Tooltip。
                  不能让长名字把卡片撑变形或换行把网格挤乱。 */}
              <Tooltip title={item.rel_path}>
                <span
                  style={{
                    display: "inline-block",
                    maxWidth: 110,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    verticalAlign: "bottom",
                  }}
                >
                  {item.file_name}
                </span>
              </Tooltip>
            </Checkbox>
            {item.has_forbidden && (
              <div style={{ fontSize: 11, color: "#ff4d4f" }}>命中违规词，不可选</div>
            )}
            {item.error_message && (
              <div style={{ fontSize: 11, color: "#faad14" }}>{item.error_message}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * 大图预览（用户口径 2026-10-08）。
 *
 * > 「所有媒体资源应支持大图预览，支持左右切换**不是所有**」
 *
 * 两个要点：
 * 1. **只在本组内**左右切换 —— 切的是传进来的 `items`（当前媒体区），不是
 *    全包 70 个。主图和主图比才有意义，混着 SKU 图切只会让人找不到北。
 * 2. 键盘 ← → / Esc 也要能用（鼠标点箭头是基本盘，键盘是顺手的）。
 *
 * ⚠️ 违规命中的词标在标题旁 —— 用户在大图上是来找「到底哪儿违规」的。
 */
function MediaPreview(props: {
  token: string;
  items: PrecheckMediaItem[];
  index: number;
  onIndexChange: (next: number) => void;
  onClose: () => void;
}) {
  const { token, items, index, onIndexChange, onClose } = props;
  const item = items[index];
  const total = items.length;

  const go = useCallback(
    (delta: number) => {
      if (total <= 1) return;
      onIndexChange((index + delta + total) % total);
    },
    [index, onIndexChange, total],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft") go(-1);
      else if (event.key === "ArrowRight") go(1);
      else if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, onClose]);

  if (!item) return null;

  return (
    <Modal
      open
      onCancel={onClose}
      centered
      width="80vw"
      footer={null}
      title={
        <Space>
          <Text style={{ fontSize: 13 }}>{item.file_name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {index + 1} / {total}
          </Text>
          {(item.hits ?? []).length > 0 && (
            <Tag color="error">{(item.hits ?? []).map((h) => h.word).join("、")}</Tag>
          )}
        </Space>
      }
      styles={{
        body: {
          height: "70vh",
          overflowY: "auto",
          padding: 0,
          scrollbarWidth: "none",
          msOverflowStyle: "none",
        },
      }}
      className="hermes-hide-scrollbar"
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#000",
        }}
      >
        <PreviewBody token={token} item={item} />
        {total > 1 && (
          <>
            <Button
              shape="circle"
              icon={<LeftOutlined />}
              onClick={() => go(-1)}
              style={{ position: "absolute", left: 12, top: "50%" }}
              aria-label="上一张"
            />
            <Button
              shape="circle"
              icon={<RightOutlined />}
              onClick={() => go(1)}
              style={{ position: "absolute", right: 12, top: "50%" }}
              aria-label="下一张"
            />
          </>
        )}
      </div>
    </Modal>
  );
}

/** 浏览器放不了这个视频格式时的说明。 */
function VideoUnsupportedHint(props: { item: PrecheckMediaItem }) {
  const { item } = props;
  return (
    <Alert
      type="warning"
      showIcon
      style={{ maxWidth: 520 }}
      message="当前浏览器不支持直接播放这个视频格式"
      description={`可以下载后用本地播放器打开。该文件已完成违规词检测，结果不受预览影响。`}
    />
  );
}

/** 单张缩略图（鉴权取流 → blob URL）。
 *
 * `onSize`（用户口径 2026-10-11 需求 5）：图片**本地解码后**回报真实 pixel 尺寸 ——
 * 这是「零下载比对」里**本地一侧**的判据来源（线上那侧取平台下发的宽高）。 */
function PrecheckThumb(props: { token: string; item: PrecheckMediaItem }) {
  const { token, item } = props;
  const [src, setSrc] = useState<string | null>(null);
  //: ⚠️ 判「是不是视频」用**后缀**，不用 `group === "video"` ——
  //: `group` 现在是目录名（`主图视频` / `视频`），跟旧枚举值比恒不成立。
  const isVideo = isVideoFile(item.file_name || item.rel_path);

  //: ⚠️ 视频也要取流 —— 缩略图出**首帧**，比一块灰底写「视频」有用得多
  //: （用户要一眼看出这是哪个视频）。
  useEffect(() => {
    let revoked: string | null = null;
    let alive = true;
    fetchPrecheckImage(token, item.rel_path)
      .then((url) => {
        if (!alive) {
          URL.revokeObjectURL(url);
          return;
        }
        revoked = url;
        setSrc(url);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [token, item.rel_path]);

  if (!src) return <div style={fillStyle}>{isVideo ? "视频" : "加载中…"}</div>;

  if (isVideo) {
    return (
      <video
        src={src}
        //: 首帧 + 不自动播（缩略图位只做预览，播放交给大图预览弹窗）
        preload="metadata"
        muted
        playsInline
        style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
      />
    );
  }
  return (
    <img
      src={src}
      alt={item.file_name}
      style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
    />
  );
}

/**
 * **线上图**小图预览（需求 3）。
 *
 * 线上图是 xlsx `图片` 列里的 URL（真包现状是 1688 的 `gw.alicdn.com`），
 * 不能走预检的 `/image`（那个端点只服务**暂存包内**的本地路径，越界会 404），
 * 所以直接当 `<img src>` 用 —— 平台是公开图床。
 *
 * ⚠️ 用户口径 2026-10-11（**推翻**上一版）：**放弃图片内容比对** ——
 * 「将拆分后的 sku 名称去查本地 sku 图名进行对齐即为通过了预检」。
 * 所以这里**只剩预览**：不发 HEAD、不算 MD5、零网络请求（风控风险归零）。
 */
function SkuImageThumb(props: { token: string; onlineUrl: string }) {
  const { onlineUrl } = props;
  if (!onlineUrl) {
    return (
      <Text type="secondary" style={{ fontSize: 11 }}>
        无
      </Text>
    );
  }
  return (
    <img
      src={onlineUrl}
      alt="线上图"
      style={{
        width: SKU_THUMB_SIZE,
        height: SKU_THUMB_SIZE,
        objectFit: "contain",
        border: "1px solid #f0f0f0",
        borderRadius: 4,
        background: "#fafafa",
        cursor: "zoom-in",
      }}
    />
  );
}

/** 表格里**一列的批量修改条**（价格 / 库存各一条，用户口径 2026-10-09）。
 *
 * 三种模式：
 *   - 加减固定值：原值 + N（N 可负）
 *   - 倍数 + 固定值：原值 × K + N
 *   - 统一改固定值：一律 = N
 *
 * ⚠️ 列可任选（表里除了「原价」「库存」还有「计算价格」等列，不写死两列）；
 *    列识别仅用于给默认列，与 `tableToSkus` 共用同一套别名。
 * ⚠️ 直接应用，**无二次确认弹窗**（用户口径 2026-10-09：移除批量修改的二次弹窗提醒）；
 *    改完给一条轻提示告知改了什么。
 */
function BatchColumnBar(props: {
  table: PrecheckTable;
  /** 当前勾选的行下标（空 = 未勾选 ⇒ 整列改）。 */
  pickedRows?: readonly number[];
  /** ⚠️ 价格库存映射（默认选中列要按用户配的映射，**不猜列名**）。 */
  priceMapping?: readonly PriceMappingRow[];
  onApply: (column: number, rule: BatchEditRule, onlyRows?: readonly number[]) => void;
}) {
  const { table, pickedRows, onApply } = props;
  const { message } = App.useApp();
  //: 列**可任选**（用户口径 2026-10-09：表里除了「原价」「库存」还有「计算价格」
  //:  等更多列，不该写死两列）；默认落在能认出的价格 / 库存列上，认不出就用第一列。
  //: ⚠️ **走映射**（用户口径 2026-10-15：所有点均走映射，移除写死逻辑）——
  //: 默认落在用户映射的「售价 / 库存」源列上；没配就退到第 1 列（**不猜列名**）。
  const priceCol = priceColumnIndex(table.columns, props.priceMapping ?? []);
  const stockCol = stockColumnIndex(table.columns, props.priceMapping ?? []);
  const defaultCol = priceCol >= 0 ? priceCol : stockCol >= 0 ? stockCol : 0;
  const [column, setColumn] = useState<number>(defaultCol);

  if (table.columns.length === 0) return null;

  //: 勾了行 ⇒ 作用域是这些行；没勾 ⇒ 整列（undefined）。
  const scope = pickedRows && pickedRows.length > 0 ? pickedRows : undefined;
  //: 价格列下限 0（不为负）；其它列不裁剪。
  const min = column === priceCol ? 0 : undefined;

  /** 落改（**无确认弹窗**，直接应用 + 一条轻提示）。 */
  const applyDirectly = (text: string, run: () => void) => {
    run();
    message.success(text);
  };

  return (
    <BatchColumnControl
      table={table}
      column={column}
      columns={table.columns}
      onColumnChange={setColumn}
      scope={scope}
      min={min}
      onApply={onApply}
      applyDirectly={applyDirectly}
    />
  );
}

/* ------------------------------------------------------------------ *
 * 表格列的**正则取值**（用户口径 2026-10-13）
 *
 * > 「表格列通过正则对原数据中的列取值操作后**覆盖原始列**」
 * > 「默认覆盖全部，如果存勾选行则**只覆盖勾选行**」
 * > 「**数值列不支持**只能读取原始值」
 *
 * 三条落地要点：
 *   ① **覆盖原列**（不做派生列）—— 用户已明确否掉「原列只读 + 派生列」；
 *   ② 作用域与 `BatchColumnBar` **同一套语义**（没勾选 = 整列；勾选 = 那些行）；
 *   ③ 破坏性动作 ⇒ **先预览再应用**，且**能撤销**（快照还原）。
 * ------------------------------------------------------------------ */


function ColumnPatternControl(props: {
  table: PrecheckTable;
  /** 该表在完整数组里的真实下标（撤销快照的键要用它）。 */
  tableIndex: number;
  /** 当前勾选的行（空 = 整列）。 */
  pickedRows?: readonly number[];
  /** 应用正则（覆盖原列）。 */
  onApply: (column: number, pattern: string, onlyRows?: readonly number[]) => void;
  /** **直接落值**（数据包规则执行：后端已算好逐行结果，前端不再过正则）。
   *  ⚠️ 与 `onApply` 分开：那条走「正则 → 匹配 → 覆盖」，这条是「结果已知，照写」。
   *  共用同一份**快照**（都进快照 ⇒ 一次「还原原始值」全撤）。 */
  onApplyValues?: (
    column: number,
    changed: readonly { rowIndex: number; after: string | null }[],
  ) => void;
  /** 撤销：把**这一列**还原到快照（用户口径 2026-10-15：「恢复数据应只恢复
   *  当前列而不是全表」）。入参 = 该列下标 —— 快照按「表 + 列」存，
   *  还原也必须指名哪一列，否则会撤错列。 */
  onRestore: (column: number) => void;
  /** 该列是否有可撤销的快照（**按列**判，不是按表）。 */
  canRestore: boolean;
  /** **从哪一列点开的**（用户口径 2026-10-14：「列默认为**当前列**」）——
   *  表头每列都挂一个入口，点哪列就以哪列为默认；弹窗里**仍可切换列**。 */
  initialColumn?: number;
  /** **自带触发按钮**（顶部工具条用）还是**只渲染弹窗**（表头图标用）。
   *  默认 `true`（顶部模式）；表头模式传 `false` —— 图标由表头自己画。 */
  withTrigger?: boolean;
  /** **受控开关**（表头模式：由表头图标驱动）。 */
  open?: boolean;
  onOpenChange?: (next: boolean) => void;
  /** 锁列（用户口径 2026-10-14）：「每一列的弹窗均是独立的，每次打开
   *  只展示当前列的正则」⇒ 打开后不显示列选择器，只对这一列生效。
   *  ⚠️ 与「仍支持选择」的关系：那是「顶部单入口」时代的诉求；现在入口下沉到
   *  每列表头、每列一个实例，再让用户在里面换列会与「贴着这枚图标」矛盾。 */
  /** 把正则结果**填入「规格分类」新列**（用户口径 2026-10-14）。
   *  入参是该列的预览结果（`rowIndex` + `after`），由调用方决定存哪。 */
}) {
  const {
    table, pickedRows, onApply, onApplyValues, onRestore, canRestore,
    initialColumn, withTrigger = true, open: openProp, onOpenChange,
  } = props;
  //: ⚠️ `modal` 用于**二次确认**（还原这一列）—— 项目口径：确认类弹窗走
  //: `App.useApp()` 的 `modal`（静态 `Modal.confirm` 不吃 ConfigProvider 主题）。
  const { message, modal } = App.useApp();
  const [openLocal, setOpenLocal] = useState(false);
  const open = openProp ?? openLocal;
  const setOpen = (v: boolean) => {
    setOpenLocal(v);
    onOpenChange?.(v);
  };
  const [column, setColumn] = useState<number>(initialColumn ?? 0);
  //: ⚠️ 表头每列各挂一个入口 ⇒ **点开哪列就以哪列为默认**。
  //: 弹窗是复用同一个实例的（`open` 由父控），所以点开时要**跟着 `initialColumn` 走** ——
  //: 否则第二次点别的表头仍停在上一列（用户会以为点错了）。
  useEffect(() => {
    if (!open) return;
    if (initialColumn !== undefined) setColumn(initialColumn);
  }, [open, initialColumn]);

  //: ⚠️ **每一列都变「可选」**（用户口径 2026-10-14：「所有列均表头有设置图标」）
  //: ⇒ 不再按 `canApplyPattern`（文本列）过滤。
  const options = table.columns.map((name, i) => ({ value: i, label: `${name}` }));

  /* ---- **数据包规则**（用户口径 2026-10-14）--------------------------------
   * 「在表格列的设置图标弹窗中**只展示启用的**数据包和规则，
   *   **先选择数据包类型再选择指定的列**」
   *
   * ⚠️ 每次打开**现拉**（不做前端缓存）：用户口径「数据包规则调整后**实时生效**」
   * —— 缓存会让他在管理弹窗改完、这里还看到旧状态。
   * ⚠️ 只请求**启用中的包 + 其中启用中的规则**（后端 `enabled_only` + `with_rules`），
   * 前端不再自己过滤（过滤口径写两遍必然漂移）。 */
  const [ruleSets, setRuleSets] = useState<DataPackageRuleSet[]>([]);
  const [pickedSetId, setPickedSetId] = useState<number | null>(null);
  const [pickedRuleId, setPickedRuleId] = useState<number | null>(null);
  const [ruleBusy, setRuleBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setPickedRuleId(null);
    let alive = true;
    void (async () => {
      try {
        const rows = await fetchDataPackageRuleSets(true, true);
        if (alive) setRuleSets(rows);
      } catch {
        //: ⚠️ 拉不到**静默**（这只是弹窗里的一个可选区块）—— 不让它把整个
        //: 正则功能挡住；用户点「执行规则」时会再报一次。
        if (alive) setRuleSets([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [open]);


  const pickedSet = ruleSets.find((r) => r.id === pickedSetId) ?? null;
  const pickedRule =
    (pickedSet?.rules ?? []).find((r) => r.id === pickedRuleId) ?? null;

  /** **来源列** —— 一个控件，两个 op 共用（用户口径 2026-10-15）：
   *  · `math_scale`  ⇒ 语义「**计算基础列**」（按哪一列算）；
   *  · `copy_column` ⇒ 语义「**来源列**」（把哪一列复制过来）。
   *
   *  用户原话（运算）：「涉及到计算时是需要一个前提条件为基于指定列，如果选择的
   *  规则是运算类的规则则需要**指定计算的基础列**，该列在表头列的设置图标 tip
   *  弹窗中指定，**默认为当前列**，可选择指定为其它列，**下拉只展示数值列**」。
   *  用户原话（复制）：「新增一个复制列值的操作规则，如果为复制列值规则，
   *  则在 tip 中指定需要将**哪一列复制到当前列**」。
   *
   *  ⚠️ **下拉项一律显示中文表头名**（用户口径 2026-10-15 二次追加：
   *  「本身应默认展示为计算价格中文表头」）—— 显示下标（`3`）用户根本不知道是哪列。
   *  ⚠️ 默认 = **当前列**（`null` 即「就是本列」），其显示名同样走表头名。
   *  ⚠️ **换规则要清掉已选**（否则带着上一条规则的列去算，静默错值）。 */
  const [sourceColumn, setSourceColumn] = useState<number | null>(null);
  /**
   * **所有规则都支持选择「数据源列」**（用户口径 2026-10-20：「所有规则均支持选择
   * 数据源列，默认为当前列，比如图片中的计算基础列，全部统一为数据源列，
   * 优化为所有规则均支持选择数据源列」）。
   *
   * ⚠️ 早先只有 `math_scale` / `copy_column` / `copy_extract` 才显示该下拉，
   * 提取类 / 固定文本「读的就是当前列自分」所以没给。用户要求统一：
   * 任何规则都能指定「从哪一列取值」，不指定 = 当前列（向后兼容，行为不变）。
   */
  const needsSourceColumn = true;
  //: 「照搬整格」语义（提示文案用「复制到」而不是「按…计算」）。
  const isCopyRule =
    pickedRule?.op === "copy_column" || pickedRule?.op === "copy_extract";
  /** 来源列清单（**两个 op 的筛选口径不同**，用户口径 2026-10-15）：
   *
   *  · `math_scale`（计算基础列）⇒ **只展示数值列**（用户原话）。列也**显示中文表头名**。
   *  · `copy_column`（来源列）    ⇒ **全部列** —— 复制不要求源列是数值
   *    （复制一列文本到当前列是完全合理的用法）。
   *
   *  ⚠️ 数值列的判据是 `column_kinds === "numeric"`（后端 `COLUMN_KINDS` 同集合，
   *  写 `"number"` 会恒空且不报错）。
   *  ⚠️ **当前列本身要始终在选项里**：它是默认值，若它被判成非数值列，
   *  下拉会「显示当前列却不在选项里」（antd 会把 value 原样渲染成裸 `3`）——
   *  那正是用户看到的「下拉显示 3」的成因之一。⇒ 钉住当前列一定入选项。 */
  //: ⚠️ **全部列都可当数据源**（不再按 op 过滤成「只数值列」）：
  //: 用户口径「所有规则均支持选择数据源列」⇒ 提取类也可能想从别的列取值，
  //: 复制类本身就不限类型。真选错列由用户自己负责（下拉已显示中文表头名）。
  const columnOptions = table.columns.map((name, i) => ({
    value: i,
    label: name || `第 ${i + 1} 列`,
  }));
  //: ⚠️ 换规则 / 换列 ⇒ 基础列回默认（否则带着上一条规则的列去算，静默错值）。
  useEffect(() => {
    setSourceColumn(null);
  }, [pickedRuleId, column]);

  /** 执行选中的规则（后端算；结果**覆盖当前列**并进快照，可一并撤销）。 */
  const runPickedRule = useCallback(async () => {
    if (!pickedSet || !pickedRule) return;
    setRuleBusy(true);
    try {
      //: ⚠️ 运算类：读**基础列**的值（默认当前列）；提取类：读当前列自己。
      //: 「把 A 列 ×1.7 写到 B 列」就靠这两处不同 —— 读 `readCol`、写 `column`。
      const readCol = needsSourceColumn ? (sourceColumn ?? column) : column;
      //: ⚠️ **默认作用于全表；有勾选时只对勾选行生效**（用户口径 2026-10-15：
      //: 「所有规则的执行均是默认当前列的所有数据行，如果存在勾选行则只对勾选行
      //:   执行有效，不存在勾选行默认全部行」）。
      //: ⚠️ 判据与 `applyColumnPattern` 的 `scope` **同一套**（空数组 = 全表）。
      const scope =
        pickedRows && pickedRows.length > 0 ? new Set(pickedRows) : null;
      const allValues = table.rows.map((r) => r[readCol] ?? null);
      const values = scope
        ? allValues.filter((_, i) => scope.has(i))
        : allValues;
      //: ⚠️ `values` 是**筛过的** ⇒ 后端回来的 `res.values[i]` 对应的是
      //: **第 i 个被选中的行**，不是第 i 行！必须用它把下标映射回去，
      //: 否则「勾了第 3 行」会写到第 1 行（静默改错行，比不生效更糟）。
      const targetRows = scope ? [...scope] : table.rows.map((_, i) => i);
      const res = await executeDataPackageRule({
        set_id: pickedSet.id,
        rule_id: pickedRule.id,
        values,
        source_column: readCol,
      });
      //: 覆盖成后端算出的结果（`null` = 该行提不出 ⇒ **留空**，不是保留原值）。
      const changes = res.values
        //: ⚠️ **下标映射**：`values` 是筛过的 ⇒ 第 i 个结果属于 `targetRows[i]`。
        .map((v, i) => ({ rowIndex: targetRows[i] ?? i, after: v }))
        //: ⚠️ 跳过没变化的行 —— 否则「点了执行但结果一样」也会写满快照。
        .filter((c) => (table.rows[c.rowIndex]?.[column] ?? null) !== c.after);
      onApplyValues?.(column, changes);
      const where = scope ? `（勾选的 ${targetRows.length} 行）` : `（全部 ${res.total} 行）`;
      message.success(
        `已按「${res.summary}」处理 ${res.total - res.missed} 行${where}` +
          (res.missed ? `，${res.missed} 行未匹配` : ""),
      );
      if (res.reason) message.warning(res.reason);
      setOpen(false);
    } catch (error) {
      void message.error(
        `执行规则失败：${
          (error as { response?: { data?: { detail?: { message?: string } } } })?.response
            ?.data?.detail?.message ?? "请稍后重试"
        }`,
      );
    } finally {
      setRuleBusy(false);
    }
    //: ⚠️ **依赖数组必须包含 `needsSourceColumn` / `sourceColumn` / `pickedRows`** ——
  //: 漏了它们，`readCol` 会一直用**首次渲染时**的闭包值（`sourceColumn = null`）
  //: ⇒ 用户选了「原价」也照旧读**当前列**（若当前列本身是空的 ⇒ 整列按 0 ⇒ 结果全 0）。
  //: 这是本项目实测踩到的真 bug：界面显示选了「原价」，实际读的是「计算价格」。
  //: ⚠️ 教训：`useCallback` 的依赖数组要覆盖**回调体里读到的每一个 props/state**，
  //: 不能只看「参数里用到的」。
}, [
  pickedSet,
  pickedRule,
  table.rows,
  table.columns,
  column,
  sourceColumn,
  needsSourceColumn,
  pickedRows,
  onApplyValues,
  message,
]);

  if (table.columns.length === 0) return null;

  return (
    <Popover
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      placement="bottomLeft"
      content={
        <div style={{ width: 460 }}>
          {/* ══════ **数据包规则**（用户口径 2026-10-14）══════
              「先选择**数据包类型**再选择指定的列」「只展示**启用的**数据包和规则」
              「数据包规则调整后**实时生效**」。
              ⚠️ 与下面的正则区块是**两条路**：这条路是**配置好、可复用**的
              （领域用户选两下即可）；正则是**临时、单次**的高级手段。 */}
          <div style={{ marginBottom: 4 }}>
            <Text strong style={{ fontSize: 12 }}>
              数据包规则
            </Text>
            <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
              选「数据包类型 → 列规则」后执行
            </Text>
          </div>
          {ruleSets.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              还没有启用的规则（可在表格上方的「提取数据规则」里配置）
            </Text>
          ) : (
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <Space size={6} style={{ width: "100%" }}>
                <Select
                  size="small"
                  style={{ width: 200 }}
                  placeholder="选择数据包类型"
                  value={pickedSetId ?? undefined}
                  onChange={(v) => {
                    setPickedSetId(v ?? null);
                    //: 换包 ⇒ 清掉已选规则（否则会留着上一个包的规则 id）。
                    setPickedRuleId(null);
                  }}
                  //: ⚠️ **只列启用的包**（后端已按 `enabled_only` 过滤）。
                  options={ruleSets.map((r) => ({ label: r.name, value: r.id }))}
                />
                <Select
                  size="small"
                  style={{ width: 220 }}
                  placeholder={pickedSetId ? "选择列规则" : "先选数据包类型"}
                  disabled={!pickedSetId}
                  value={pickedRuleId ?? undefined}
                  onChange={(v) => setPickedRuleId(v ?? null)}
                  //: ⚠️ 每条形如「规则名称 · 摘要」—— 用户看到的是
                  //: 「哪个包 + 哪条规则 + 做什么」，一步到位（设计 40 §5.3）。
                  //: ⚠️ **规则不绑列**（用户口径 2026-10-14）⇒ 这里不再显示列名；
                  //: 用在哪一列由**当前点的表头**决定（下方文案已写明）。
                  options={(pickedSet?.rules ?? []).map((r) => ({
                    label: `${r.rule_name} · ${r.summary}`,
                    value: r.id,
                  }))}
                />
              </Space>
              {/* **数据源列**（用户口径 2026-10-20：「所有规则均支持选择数据源列，
                  默认为当前列…全部统一为数据源列」）——
                  不再区分「计算基础列 / 来源列」，统一叫**数据源列**，所有规则都有。 */}
              {needsSourceColumn && (
                <Space size={6} align="center" style={{ marginTop: 6 }}>
                  <Text style={{ fontSize: 12 }}>数据源列</Text>
                  <Select
                    size="small"
                    style={{ width: 180 }}
                    //: ⚠️ 默认 = **当前列**（口径原文「默认为当前列」）。
                    //: 显示的是**该列的中文表头名**（不是下标）。
                    value={sourceColumn ?? column}
                    onChange={(v) => setSourceColumn(v)}
                    options={columnOptions}
                    placeholder="选择列"
                  />
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {isCopyRule ? "把这一列复制到当前列" : "按这一列取值"}
                  </Text>
                </Space>
              )}
              {/* ⚠️ **诊断行**（用户口径 2026-10-15：运算结果全 0，需现场可核）——
                  把「读的是哪一列 + 该列前几行的原始值」显示出来。
                  用户一眼就能看出是「读错列」还是「那列本来就空」，
                  不必再来回猜（前几轮就是靠猜浪费了好几轮）。
                  ⚠️ 2026-10-20 起所有规则都显示（都支持选数据源列，读错列同样要能现场核）。 */}
              {needsSourceColumn && (
                <Text type="secondary" style={{ fontSize: 11, display: "block" }}>
                  {`读取：第 ${(sourceColumn ?? column) + 1} 列「${
                    table.columns[sourceColumn ?? column] ?? "?"
                  }」｜共 ${table.rows.length} 行｜前 3 行原值：${table.rows
                    .slice(0, 3)
                    .map((r) => JSON.stringify(r[sourceColumn ?? column] ?? null))
                    .join("、")}`}
                </Text>
              )}
              {/* ⚠️ 规则**不绑列** ⇒ 这里只说明「会作用到当前这一列」，
                  不再做「规则列 ≠ 当前列」的警告（那条判据随列绑定一起退场）。 */}
              {pickedRule && (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {isCopyRule
                    ? `将把「${table.columns[sourceColumn ?? column] ?? ""}」列的值复制到「${
                        table.columns[column] ?? ""
                      }」列`
                    : needsSourceColumn
                      ? `将把「${pickedRule.rule_name}」按「${
                          table.columns[sourceColumn ?? column] ?? ""
                        }」列计算，写到「${table.columns[column] ?? ""}」列`
                      : `将把「${pickedRule.rule_name}」应用到「${
                          table.columns[column] ?? ""
                        }」列`}
                </Text>
              )}
              <Button
                size="small"
                type="primary"
                disabled={!pickedRule}
                loading={ruleBusy}
                onClick={() => void runPickedRule()}
              >
                执行规则
              </Button>
            </Space>
          )}
        </div>
      }
    >
      {/* **触发按钮**只在顶部工具条模式渲染（用户口径 2026-10-14：
          「将正则取值不要放在顶部，放在**每一列的表头**使用设置图标」）。
          表头模式传 `withTrigger={false}` —— 图标与开关由表头自己画。 */}
      {/* **设置图标 + 还原原始值**（用户口径 2026-10-14：「还原原始值与设置图标放一起」）。
          ⇒ 两者同处一个 `Space`（贴着该列表头/该处入口），不再让「还原」孤零零
          挂在表格上方的工具条里。
          ⚠️ 「还原」的作用域是**当前列**（用户口径 2026-10-15：
          「恢复数据应只恢复**当前列**而不是全表」）—— 快照按「表 + 列」存，
          撤的只是这一列；别列改过的东西留着。`Tooltip` / 确认框都要说清这一点。 */}
      <Space size={2}>
        <Button
          type="text"
          size="small"
          //: ⚠️ 表头模式（`withTrigger={false}`）**只画一枚小齿轮** ——
          //: 用户的反馈原话是「sku 名称列的设置图标**很大**且**存在正则取值文本**，
          //: 应统一**只需要设置图标**即可」⇒ 尺寸压到 `fontSize 11` + 零内边距 +
          //: `minWidth 0`（antd 的 text 按钮默认有最小宽度，会把它撑大）。
          style={
            withTrigger
              ? { padding: 0, height: 16 }
              : { padding: 0, height: 14, minWidth: 0, lineHeight: 1 }
          }
          title="正则取值"
          onClick={() => setOpen(true)}
        >
          <SettingOutlined style={{ fontSize: withTrigger ? undefined : 11 }} />
          {withTrigger ? " 正则取值" : null}
        </Button>
        {canRestore && (
          <Tooltip title="还原该列原始值（只还原当前列）">
            <Button
              type="text"
              size="small"
              style={{ padding: 0, height: 16 }}
              //: ⚠️ **二次确认**（用户口径 2026-10-14：「还原列值需二次确认」）——
              //: 被撤掉的原值不可逆（快照只留一份），误点一次这一列的成果就没了。
              //: ⚠️ 文案必须点明**只还原当前列**（用户口径 2026-10-15）—— 旧文案
              //: 写的是「丢弃**本表所有列**」，按列还原后那样说会吓到用户（也说不准）。
              onClick={() => {
                modal.confirm({
                  title: "还原原始值",
                  content: "将只还原当前列，恢复为包内原始数据，其他列保持不变。确定？",
                  okText: "还原",
                  okButtonProps: { danger: true },
                  cancelText: "取消",
                  //: ⚠️ 回传**这一列**（`column` 是弹窗当前列）—— 快照按「表 + 列」存。
                  onOk: () => onRestore(column),
                });
              }}
            >
              <UndoOutlined style={{ fontSize: withTrigger ? 13 : 11 }} />
            </Button>
          </Tooltip>
        )}
      </Space>
    </Popover>
  );
}

/* ------------------------------------------------------------------ *
 * **SKU 图自动匹配弹窗**（用户口径 2026-10-13）
 *
 * > 「sku表tab下新增 sku 图自动按钮；弹窗展示匹配 sku 数据与 sku 图进行批量的规则，
 * >   先下拉选择预检通过且勾选的图片媒体区域也就是指定 sku 图区域
 * >   然后指定 sku 名称列与图片名称匹配的规则，精确匹配，模糊，或使用正则表达式」
 *
 * 与预检按钮**职责分离**（用户口径）：
 *   · 「检测违规词」= 素材是否合规 → 门禁；
 *   · 「SKU 图自动匹配」= 规格行 ↔ 哪张图 → **不拦在第一步**（走第二步助手提示）。
 * ------------------------------------------------------------------ */

/** 匹配结果的一行（弹窗预览 + 上抛给第二步）。 */
export type SkuMatchRow = {
  rowIndex: number;
  /** 规格行的显示名（表格原值）。 */
  skuName: string;
  /** 匹配到的图（`null` = 未命中）。 */
  relPath: string | null;
  fileName: string;
  /** **提取后**的 SKU 名称（用户口径 2026-10-13：「触发与 sku 名称通过正则表达式
   *  提取文本并在列表中展示提取后的文本」）。
   *
   *  ⚠️ 与 `skuName`（列原值）**都要展示**：只给原值用户看不出正则起了什么作用，
   *  只给提取值他对照不回原文 —— 两个都给，一眼就知道正则对不对。 */
  extracted: string;
  /** 命中方式（`manual` = 用户手选）。 */
  via: "regex" | "manual" | "none";
};

function SkuMatchModal(props: {
  open: boolean;
  /** 预检 token（缩略图取流用）。 */
  token: string;
  /** 「图片区域」候选项（用户口径：先选**预检通过且勾选**的媒体区域）。 */
  zoneOptions: { value: string; label: string }[];
  /** 选中的区域（决定候选图集合）。 */
  zones: string[];
  onZonesChange: (next: string[]) => void;
  /** 「SKU 名称列」候选（表里的列名）。 */
  columnOptions: { value: number; label: string }[];
  column: number;
  onColumnChange: (next: number) => void;
  /** **图片名称取值正则**（用户口径 2026-10-14 方案 B）：与 SKU 列正则同构 ——
   *  两侧都先用各自的**反推正则**取到「完整的 SKU 名称」，再精确匹配。
   *  ⇒ 代码里**不再有任何猜数据形态的分支**（剥 `SKU01_` 前缀 / 剥「列名(值)」外壳），
   *  换包只需用户重新反推一次正则，不动代码。 */
  /** 图片名称的**反推**入口（与 SKU 列同一套工具，样例来自图片文件名）。 */
  /** SKU 名称列的正则**提取**（可选）：先对选定列的值跑这个正则取出捕获组，
   *  再用结果去和图名匹配（用户口径 2026-10-13：「支持对选定的 sku 列使用正则
   *  提取后再与使用指定正则规则的图片名称进行精确或模糊的匹配」）。
   *
   *  ⚠️ 这与「匹配方式=正则」是**两件独立的事**：
   *   * 本项管**怎么从 SKU 列取值**（提取 ⇒ 再比）；
   *   * `mode` 管**取出来的值怎么和图名比**（精确 / 模糊）。
   *  用户可以「先提取、再模糊」—— 那是绝大多数真实场景。
   */
  extractPattern: string;
  onExtractPatternChange: (next: string) => void;
  /** **原始文本预填**（用户口径 2026-10-13）：反推弹窗打开时用它填第一行。
   *  取当前名称列的**第一条**列值 —— 用户多半就是要从它里抠规格值。 */
  sampleSourceName: string;
  /** 进行匹配（父级用当前规则重算）。 */
  onRun: () => void;
  /** 逐行结果（命中 / 未命中都要显示 —— 漏配要能看出来）。 */
  rows: SkuMatchRow[];
  onOk: () => void;
  onCancel: () => void;
}) {
  const {
    open, token, zoneOptions, zones, onZonesChange, columnOptions, column, onColumnChange,
    extractPattern, onExtractPatternChange, sampleSourceName,
    onRun, rows, onOk, onCancel,
  } = props;
  //: 反推正则弹窗开关（用户口径 2026-10-13）—— SKU 列与图片名**各一个**
  //: （方案 B：两侧都由正则取值，所以两侧都需要反推入口）。

  const hit = rows.filter((r) => r.relPath).length;
  //: ⚠️ **未指定图片区域 ⇒ 不匹配**（用户口径 2026-10-13）：
  //: 「必须用户指定 sku 规格图的媒体资源区，后指定使用哪一些与图片名称去匹配」。
  //: 第一版无区域时回落到「全部 SKU 区域」⇒ 用户什么都没选就看到了匹配结果，
  //: 他无法判断这些图是从哪来的。**先选区域是硬前置**，不是可跳过的默认值。
  const zonePicked = zones.length > 0;
  const colPicked = column >= 0;

  return (
    <Modal
      open={open}
      centered
      //: ⚠️ **关掉即销毁**（用户口径 2026-10-14：「每次关闭发布商品弹窗，相关的
      //: **属性弹窗**中的参数需要清除干净」）—— 否则内部 state（反推弹窗开关等）
      //: 会活到下次打开。父级 `TablesPanel` 另有 `key={openSeq}` 兜底，
      //: 这里是同一诉求的第二道保险（两者不冲突）。
      destroyOnHidden
      title="SKU 图自动匹配"
      //: 用户口径 2026-10-13：「弹窗全屏的**宽高 50%**」→ 随后纠正为 **60%**。
      width="60vw"
      //: ⚠️ **高度也要 60%**（用户口径明确说「宽高」）。
      //: antd `Modal` 没有 `height` prop ⇒ 走 `styles.body`；
      //: 用 `60vh` 与宽度对称，内层保留滚动（列表可长）。
      styles={{ body: { height: "60vh", overflowY: "auto" } }}
      //: 用户口径 2026-10-14：「SKU 图底部的点击确认匹配结果**优化为应用匹配结果**」。
      //: ⚠️ 改名不是措辞问题：它现在**真的会把结果写回 SKU 表格**（见 `onOk`），
      //: 「确认」听起来只是关窗，用户不会预期表格被改。
      okText="应用匹配结果"
      cancelText="取消"
      onOk={onOk}
      onCancel={onCancel}
      okButtonProps={{ disabled: !zonePicked || !colPicked }}
    >
      {/* ⚠️ 这里**不再放顶部提示条**（用户口径 2026-10-13：「弹窗顶部的提示文案
          移除占用空间」）—— 原 Alert 占了约三行高度，而它说的两句话要么是界面
          自明的、要么可以挂在控件占位符上。信息保留在**占位符**里。

          ⚠️ 布局（用户口径 2026-10-14 **方案 B**）：
            第 1 行：「图片区域」（必选）+「SKU 名称列」（必选）并排，**各占 50%**
            第 2 行：「SKU 列取值正则」+「图片名称取值正则」并排，**各占 50%**
          （「匹配规则 精确/模糊」已整块删除 —— 匹配方式 = 两侧正则取值后**精确相等**。）

          ⚠️ **每个控件的标签单独包一层 `<div>`，不与控件混排** —— 这是
          「下拉图标掉到第二行」的真根因，详见下面第 1 行的注释。 */}
      {/* ⚠️ 第 1 行（用户口径 2026-10-13：「SKU 名称列的下拉框的下拉图标还是在
          第二行，正常默认应在第一行末位的，分析原因是否存在 css，移除对应样式」）。

          **根因（定位结论）**：不是 `Select` 自身的 CSS（全站 `global.css` 里
          **没有** `.ant-select` 规则，实测确认），是**标签与控件没分成两块** ——
          原写法把标签写成两个行内 `<Text>`，紧跟一个块级 `Select`：
            <div style={{ width: 200 }}>
              <Text strong>SKU 名称列</Text><Text>必选</Text>   ← 同一行内流
              <Select … />                                      ← 块级，另起一行
            </div>
          行内标签与块级控件混排时，`Select` 的可用宽度被标签行的残余空间影响，
          窄容器（50vw 下右栏固定 200）里就表现为**下拉箭头被挤到第二行**。
          ⇒ 修法：**标签单独包一层 `<div>`**（独占一行），`Select` 另起一行，
          控件的宽度来源变单一（就是那 200px）。 */}
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {/* 标签独占一行（不与 Select 混排）—— 见上面的根因说明。 */}
            <div style={{ lineHeight: "20px" }}>
              <Text strong>图片区域</Text>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                必选
              </Text>
            </div>
            <Select
              mode="multiple"
              allowClear
              style={{ width: "100%" }}
              value={zones}
              onChange={(next: string[]) => onZonesChange(next)}
              options={zoneOptions}
              placeholder="选择媒体区域"
              //: ⚠️ 候选 = **预检通过的全部图片区域**（用户口径 2026-10-13：
              //: 「预检通过的所有图片区域均支持被选中」）—— 不再限定 `kind=sku`。
              showSearch={false}
              maxTagCount="responsive"
            />
          </div>
          {/* 用户口径 2026-10-14：「图片区域和 sku 列名称也同上布局」⇒ **各占 50%**
              （此前是 `flex:1` + 固定 200）。 */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ lineHeight: "20px" }}>
              <Text strong>SKU 名称列</Text>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                必选
              </Text>
            </div>
            <Select
              style={{ width: "100%" }}
              value={column >= 0 ? column : undefined}
              onChange={onColumnChange}
              options={columnOptions}
              placeholder="选择名称列"
              showSearch
            />
          </div>
        </div>

        {/* ⚠️ 第 2 行（用户口径 2026-10-14 方案 B）：
            「SKU 列取值正则 + 图片名称取值正则」**各占 50%**，
            「匹配规则（精确/模糊）」**整块删除**（匹配方式 = 按正则取值后精确相等）。

            ⇒ 两侧都是**反推工具**产出的正则：代码不再猜数据形态
            （原先剥 `SKU01_` 前缀 / 剥「列名(值)」外壳那套已删）。 */}
        {/* ⚠️ 取值正则（用户口径 2026-10-14 定案）：
            **只有 SKU 列一条**。图片名侧**不再需要正则** ——
            用户口径「图片名称不是固定样式的，但一定包含 sku 名」
            ⇒ 图片名侧改判「文件名包含该 SKU 名」（见 `matchesSkuName`）。

            ⇒ 两侧不再对称；原先并排的「图片名称取值正则」整块删除。 */}
        <div style={{ lineHeight: "20px" }}>
        </div>

        <Space>
          <Button type="primary" onClick={onRun} disabled={!zonePicked || !colPicked}>
            执行匹配
          </Button>
          <Text type={hit > 0 ? "success" : "secondary"}>
            命中 {hit}/{rows.length} 行
          </Text>
          {!zonePicked && (
            <Text type="warning" style={{ fontSize: 12 }}>
              请先指定图片区域
            </Text>
          )}
        </Space>

        {rows.length > 0 && (
          <Table
            size="small"
            rowKey={(r: SkuMatchRow) => r.rowIndex}
            pagination={false}
            scroll={{ y: 260, x: "max-content" }}
            className="hermes-hide-scrollbar"
            dataSource={rows}
            columns={[
              {
                //: 用户口径 2026-10-14：「匹配到的 sku 图展示在**第一列**」。
                title: "匹配到的图",
                key: "img",
                width: 56,
                render: (_: unknown, r: SkuMatchRow) =>
                  r.relPath ? (
                    //: 用户口径 2026-10-13：「匹配到的图使用**小图**展示」。
                    //: ⚠️ `PrecheckThumb` 本身是 100% 填充（它吃父容器尺寸，
                    //: 没有 `size` prop）⇒ 小图靠**外层固定尺寸容器**实现，
                    //: 不给组件加 prop（它被多处复用，加尺寸参数会引出第二套行为）。
                    <div style={{ width: 36, height: 36, overflow: "hidden" }}>
                      <PrecheckThumb
                        token={token}
                        item={
                          {
                            group: r.relPath.split("/")[0] ?? "",
                            rel_path: r.relPath,
                            file_name: r.fileName,
                          } as PrecheckMediaItem
                        }
                      />
                    </div>
                  ) : (
                    <Tag color="error" style={{ fontSize: 11 }}>
                      未匹配
                    </Tag>
                  ),
              },
              {
                title: "规格（SKU 名称）",
                key: "name",
                width: 150,
                ellipsis: true,
                render: (_: unknown, r: SkuMatchRow) => (
                  <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: r.skuName }}>
                    {r.skuName || "-"}
                  </Text>
                ),
              },
              {
                //: 用户口径 2026-10-13：「触发与 sku 名称通过正则表达式提取文本
                //: 并在列表中展示提取后的文本」—— 展示正则**实际**取到的值。
                title: "提取后",
                key: "extracted",
                width: 150,
                ellipsis: true,
                render: (_: unknown, r: SkuMatchRow) =>
                  r.extracted ? (
                    <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: r.extracted }}>
                      {r.extracted}
                    </Text>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      -
                    </Text>
                  ),
              },
              {
                //: 用户口径 2026-10-13：**新增图片名称列，放第二列**。
                //: 它和「匹配到的图」是两件事：名称是**判据**（用户要拿它核对
                //: 为什么命中/没命中），缩略图是**结果**。
                title: "图片名称",
                key: "fileName",
                width: 150,
                ellipsis: true,
                render: (_: unknown, r: SkuMatchRow) =>
                  r.fileName ? (
                    <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: r.fileName }}>
                      {r.fileName}
                    </Text>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      -
                    </Text>
                  ),
              },
            ]}
          />
        )}
      </Space>
    </Modal>
  );
}

/** 单列的批量修改控件：**选列** + 模式 + 参数 + 应用（直接落，无二次确认）。 */
function BatchColumnControl(props: {
  table: PrecheckTable;
  column: number;
  columns: string[];
  onColumnChange: (column: number) => void;
  /** 作用行（undefined = 整列）。 */
  scope?: readonly number[];
  min?: number;
  max?: number;
  onApply: (column: number, rule: BatchEditRule, onlyRows?: readonly number[]) => void;
  /** 落改（无弹窗，附带一条轻提示）。 */
  applyDirectly: (text: string, run: () => void) => void;
}) {
  const { table, column, columns, onColumnChange, scope, min, max, onApply, applyDirectly } = props;
  const [mode, setMode] = useState<BatchEditMode>("delta");
  //: 加减模式：**先选符号**（用户口径 2026-10-09：加还是减要能选），再填数值。
  const [sign, setSign] = useState<"+" | "-">("+");
  const [operand, setOperand] = useState<number | null>(null);
  const [factor, setFactor] = useState<number | null>(1);

  const colName = columns[column] ?? `第 ${column + 1} 列`;

  /** 生成规则：**加减 与 倍数模式**的中间符号都并进操作数（减 ⇒ 取负），
   *  lib 只认带符号的数（用户口径 2026-10-09：倍数模式的中间也「可能加也可能减」）。 */
  const buildRule = (): BatchEditRule => {
    const raw = operand ?? 0;
    const signed = sign === "-" ? -Math.abs(raw) : raw;
    return { mode, operand: signed, factor: factor ?? 1, min, max };
  };

  /** 一条人能读懂的动作描述（轻提示里给用户看）。 */
  const describe = (): string => {
    const n = operand ?? 0;
    if (mode === "fixed") return `把「${colName}」整列改成 ${n}`;
    if (mode === "scale") {
      return `把「${colName}」按「原值 × ${factor ?? 1} ${sign} ${Math.abs(n)}」重算`;
    }
    return sign === "-"
      ? `把「${colName}」每条减 ${Math.abs(n)}`
      : `把「${colName}」每条加 ${Math.abs(n)}`;
  };

  return (
    <Space size={6} wrap>
      {/* 作用域提示（勾选行 ⇒ 只改选中行） */}
      <Text type="secondary" style={{ fontSize: 12 }}>
        {scope ? `仅对勾选的 ${scope.length} 行生效` : "未勾选行 ⇒ 整列生效"}
      </Text>
      {/* ① 选列（表里每列都可改，不写死价格 / 库存） */}
      <Select
        size="small"
        style={{ minWidth: 130 }}
        value={column}
        onChange={onColumnChange}
        options={columns.map((name, index) => ({ label: name, value: index }))}
      />
      {/* ② 模式 */}
      <Radio.Group
        size="small"
        value={mode}
        onChange={(e) => setMode(e.target.value as BatchEditMode)}
        options={[
          { label: "加减固定值", value: "delta" },
          { label: "倍数+固定值", value: "scale" },
          { label: "统一改固定值", value: "fixed" },
        ]}
      />
      {/* ③ 参数 */}
      {mode === "scale" && (
        <InputNumber
          size="small"
          style={{ width: 70 }}
          placeholder="倍数"
          value={factor}
          onChange={setFactor}
        />
      )}
      {/* 加减与倍数模式：中间符号**图标点击切换**（默认加 +；用户口径 2026-10-09） */}
      {(mode === "delta" || mode === "scale") && (
        <Tooltip title={sign === "+" ? "当前：加（点击切为减）" : "当前：减（点击切为加）"}>
          <Button
            size="small"
            icon={sign === "+" ? <PlusOutlined /> : <MinusOutlined />}
            onClick={() => setSign(sign === "+" ? "-" : "+")}
          >
            {sign === "+" ? "加" : "减"}
          </Button>
        </Tooltip>
      )}
      <InputNumber
        size="small"
        style={{ width: 90 }}
        placeholder={mode === "scale" ? "固定值" : "数值"}
        value={operand}
        onChange={setOperand}
      />
      <Button
        size="small"
        type="primary"
        //: 没填数值不改（`fixed` 的 0 是合法值，故只拦 `null`）。
        disabled={operand === null}
        onClick={() => {
          const rule = buildRule();
          //: 只数**作用域内**有值的行（勾了行就只数那些行）。
          const n = countFilledInColumn(table, column, scope);
          if (n === 0) return; // 没有有值的行，点了也没有变化
          //: 用户口径 2026-10-09：**移除二次弹窗提醒**，点「应用」直接落改。
          const where = scope ? `（勾选 ${scope.length} 行中 ${n} 行）` : `（${n} 行）`;
          applyDirectly(`${describe()} 已应用${where}`, () => onApply(column, rule, scope));
        }}
      >
        应用
      </Button>
    </Space>
  );
}

/** 表格（列数不固定：有什么展示什么）+ **价格 / 库存批量修改**（用户口径 2026-10-09）。 */
/**
 * **本地 SKU 图选择弹窗**（用户口径 2026-10-11）。
 *
 * > 「本地图列表如果未匹配到支持弹窗选择**媒体资源中加载到无违规词的 sku 图**，
 * >   弹窗参考第二步配置参数中的规格图弹窗实现」
 *
 * 与第二步的 `SkuPickModal` **同款形态**（60vw 居中 / 卡片网格 / 单选 /
 * 缩略图 + 「预览」按钮 + 大图左右切换 / 确认回填），差别只在两处：
 *   - 候选来源 = **预检已加载且无违规词**的 SKU 图（`has_forbidden !== true`
 *     且 `ocr_ok === true`）—— 用户口径明确要求「无违规词」；
 *   - 选中后回填的是**「这张 SKU 规格用哪张本地图」的映射**，供预检通过判定用。
 *
 * ⚠️ 候选**不含**违规图：`has_forbidden` 的图进了这里就等于把违规素材放行
 * （与第一步「命中违规词不允许勾选至灰」同一条红线）。
 */
function LocalSkuImagePickModal(props: {
  open: boolean;
  /** 候选 SKU 图（**已过滤掉违规图**）。 */
  items: PrecheckMediaItem[];
  token: string;
  /** 该行未匹配到的 SKU 名称（标题里说清给谁选）。 */
  skuName: string;
  onPick: (relPath: string, fileName: string) => void;
  onClose: () => void;
}) {
  const { open, items, token, skuName, onPick, onClose } = props;
  //: 用户口径同第二步：「sku 图**只能单选一张**」。
  const [picked, setPicked] = useState<string | null>(null);
  const [preview, setPreview] = useState<number | null>(null);

  useEffect(() => {
    if (open) {
      setPicked(null);
      setPreview(null);
    }
  }, [open]);

  const current = preview === null ? null : items[preview];
  const step = (delta: number) => {
    if (preview === null) return;
    const next = preview + delta;
    if (next < 0 || next >= items.length) return;
    setPreview(next);
  };

  return (
    <>
      <Modal
        open={open}
        centered
        onCancel={onClose}
        title={`选择本地图片 —— ${skuName || "未命名规格"}`}
        width="60vw"
        okText="确认"
        okButtonProps={{ disabled: picked === null }}
        onOk={() => {
          const hit = items.find((it) => it.rel_path === picked);
          if (hit) onPick(hit.rel_path, hit.file_name);
        }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="该规格没匹配到本地图，请手动选择一张（已排除违规图）"
        />
        {items.length === 0 ? (
          <Text type="secondary">
            数据包里没有可选的 SKU 图（请在第 1 步预检里加载并选中 SKU 图）
          </Text>
        ) : (
          <div className="sku-pick-grid">
            {items.map((it, i) => (
              <div
                key={it.rel_path}
                className={`sku-pick-card${picked === it.rel_path ? " picked" : ""}`}
                onClick={() => setPicked(it.rel_path)}
                title={it.file_name}
              >
                <div className="sku-pick-card-img">
                  <PrecheckThumb token={token} item={it} />
                </div>
                <div className="sku-pick-card-name">{it.file_name}</div>
                <Button
                  size="small"
                  type="link"
                  className="sku-pick-card-zoom"
                  onClick={(e) => {
                    e.stopPropagation();
                    setPreview(i);
                  }}
                >
                  预览
                </Button>
              </div>
            ))}
          </div>
        )}
      </Modal>

      {/* 大图预览（可左右切换）—— 与第二步同款形态。 */}
      <Modal
        open={preview !== null}
        centered
        onCancel={() => setPreview(null)}
        footer={null}
        width="70vw"
        styles={{ body: { textAlign: "center", padding: 8 } }}
      >
        {current && (
          <>
            <PrecheckThumb
              token={token}
              item={current}
            />
            <div style={{ marginTop: 8 }}>
              <Button size="small" disabled={preview === null || preview <= 0} onClick={() => step(-1)}>
                上一张
              </Button>
              <Text type="secondary" style={{ margin: "0 12px" }}>
                {(preview ?? 0) + 1} / {items.length}
              </Text>
              <Button
                size="small"
                disabled={preview === null || preview >= items.length - 1}
                onClick={() => step(1)}
              >
                下一张
              </Button>
            </div>
          </>
        )}
      </Modal>
    </>
  );
}

/** 零渲染：把选择快照在 **effect 里**上抛（不能在 render 里 setState 父组件）。 */

const fillStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  //: ⚠️ 尺寸**不在这里、也不在 `PrecheckThumb` 内部** —— 由**外壳**决定：
  //:   表格里 = `.sku-local-thumb`（CSS，72px，与线上图 `SkuImageThumb` 同值）；
  //:   预览弹窗里 = `.precheck-preview-body`（撑满弹窗）。
  //: 这样同一个 `PrecheckThumb` 能在两个场景复用而尺寸各自正确
  //: （曾在 fillStyle 里写死 72 ⇒ 预览弹窗里的图被压成 72px）。
  width: "100%",
  height: "100%",
  color: "#999",
};

/**
 * 预览主体：图片给 `<img>`，视频给**可播放的 `<video controls>`**。
 *
 * ⚠️ 用户口径 2026-10-08：「视频无法预览，应支持**多种视频格式**预览播放」。
 *
 * 分三态处理，每种都给出**明确**结果（不给空白框）：
 * 1. 浏览器原生可播（mp4/m4v/mov/webm/ogv）⇒ `<video controls>` 直接播；
 * 2. 浏览器放不了但有答案（avi/mkv/flv/wmv/ts…）⇒ 明确提示「当前浏览器不支持
 *    该格式」，并说明该文件**仍已做过抽帧 OCR 检测**（别让用户以为这文件漏检了）；
 * 3. 取流失败 ⇒ 说失败，不静默空白。
 */
function PreviewBody(props: { token: string; item: PrecheckMediaItem }) {
  const { token, item } = props;
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  //: ⚠️ 判「是不是视频」用**后缀**，不用 `group === "video"` ——
  //: `group` 现在是目录名（`主图视频` / `视频`），跟旧枚举值比恒不成立。
  const isVideo = isVideoFile(item.file_name || item.rel_path);

  useEffect(() => {
    let revoked: string | null = null;
    let alive = true;
    setFailed(false);
    setSrc(null);
    fetchPrecheckImage(token, item.rel_path)
      .then((url) => {
        if (!alive) {
          URL.revokeObjectURL(url);
          return;
        }
        revoked = url;
        setSrc(url);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [token, item.rel_path]);

  if (failed) return <Text style={{ color: "#fff" }}>加载失败</Text>;
  if (!src) return <Text style={{ color: "#fff" }}>加载中…</Text>;

  if (isVideo) {
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
        }}
      >
        <video
          src={src}
          controls
          autoPlay
          playsInline
          //: ⚠️ `onError` 是关键分支：`<video>` 对不认识的容器/编码**不会抛异常**，
          //: 只是黑屏不播（用户看到的就是「视频无法预览分析」）。必须显式告诉
          //: 用户「是这个格式浏览器放不了」，而不是留一块黑。
          onError={() => setFailed(true)}
          style={{ maxWidth: "100%", maxHeight: "100%", background: "#000" }}
        />
        {failed && <VideoUnsupportedHint item={item} />}
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={item.file_name}
      style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
    />
  );
}

function SelectionReporter(props: {
  blocked: number[];
  picked: number[];
  onChange: (next: { blocked: number[]; picked: number[] }) => void;
}) {
  const { blocked, picked, onChange } = props;
  //: ⚠️ 依赖用**序列化后的字符串**，数组每次渲染都是新引用 ⇒ 直接依赖数组
  //:   会让 effect 每帧都跑（无限循环）。
  const key = `${blocked.join(",")}|${picked.join(",")}`;
  useEffect(() => {
    onChange({ blocked, picked });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return null;
}

function TablesPanel(props: {
  /** 本面板显示的 SKU 表（父级完整数组的**子集**）。 */
  tables: PrecheckTable[];
  /** 上抛**本面板下标**对应的**新表**；父级负责映射回完整数组并落库。 */
  onTableChange: (tIndex: number, next: PrecheckTable) => void;
  /** 预检 token（本地图 / 线上图预览都要经 `/image` 取流）。 */
  token: string;
  /** 数据包里的 SKU 图媒体（按名称对齐到表格行 —— 需求 3 的本地图列）。 */
  skuMedia: PrecheckMediaItem[];
  /** 媒体资源的**勾选状态**（键 `group/rel_path`）。
   *
   *  ⚠️ 用户口径 2026-10-11：**取消勾选的 SKU 图要连带从 SKU 规格列表移除**
   *  （媒体区与规格表是同一份数据的两个视角，不能一处取消、另一处还在）。 */
  checked: Record<string, boolean>;
  /** 上报选择快照（需求 4）：`blocked` = 预选未通过的行、`picked` = 勾选的行。
   *
   *  ⚠️ 上抛而不是内部消化 —— 第二步要按同一份判据过滤价格库存表，而本组件的
   *  state 会随页签切换卸载而丢。 */
  onSelectionChange: (next: { blocked: number[]; picked: number[] }) => void;
  /** **上报 SKU 图匹配结果**（用户口径 2026-10-13）。
   *
   *  第二步「导入 SKU 图」上传的就是**这里匹配成功的**那些图
   *  —— 而不是「SKU 区域里的全部图」。
   *
   *  ⚠️ 上抛而不是内部消化：本组件只挂载在「商品sku表格」页签，
   *  切页签即卸载 ⇒ 匹配结果会丢（与 `onSelectionChange` 同一条理由）。 */
  onSkuMatchChange: (
    next: { specName: string; specValue: string; relPath: string; fileName: string }[],
  ) => void;
  /** 数据包里的区域名（SKU 图匹配弹窗的「图片区域」下拉候选；用户口径 2026-10-13）。 */
  /** **商品 SKU 映射的来源列**（= 第一步「规格分类」列去重）。
   *
   *  ⚠️ 删除某列时的二次确认要**点名**「该列已被 XX 使用」（用户口径 2026-10-15）——
   *  删除列发生在第一步，而「被映射使用」是第二步的事，只有拿到第二步的草稿才说得清。
   */
  specImportSources?: string[];
  /** **价格库存映射的来源列**（同上，用于删除列时的连带提示）。 */
  priceSources?: string[];
  /** **完整的价格库存映射**（SKU 名称列等取值要按它，用户口径 2026-10-15）。 */
  priceMapping?: readonly PriceMappingRow[];
  zoneNames: string[];
  /** 违规词检测是否**已通过**（用户口径 2026-10-13：SKU 图匹配的前置门禁）。
   *
   *  ⚠️ 复用上层的 `precheckPassed`（唯一判据 = 检测跑完 + 没有未通过的违规项），
   *  **不在本组件里另算一份** —— 两个判据迟早不一致。 */
  precheckPassed: boolean;
  /** **全部**预检媒体（不限于 `kind === "sku"`）。
   *
   *  ⚠️ 用户口径 2026-10-13：「预检通过的所有图片区域均支持被选中」
   *  ⇒ SKU 匹配弹窗的区域候选与候选图池都取自这里，而不是 `skuMedia`
   *  （那只有 `kind === "sku"`，用户选不到主图/详情图目录）。 */
  allMedia: PrecheckMediaItem[];
  /** 上报「用户已成功执行过匹配」（用户口径 2026-10-13：进第二步的关卡）。
   *
   *  ⚠️ 上抛而不是内部 state：本组件随页签切换卸载，而关卡判在向导。 */
  onSkuMatchRun: () => void;
}) {
  const {
    tables, onTableChange, token, skuMedia, checked, onSelectionChange,
    onSkuMatchChange, zoneNames, allMedia, precheckPassed, onSkuMatchRun,
  } = props;
  //: ⚠️ 门禁提示要文案（用户口径：给一句话而不是让按钮静默失效）。
  const { message } = App.useApp();
  /** 每张表**勾选的行下标**（用户口径 2026-10-09：勾选后可只改选中的行）。
   *  ⚠️ 用表下标做外层键（同名的多张表要能分开记）。
   *
   *  ⚠️ 需求 4/5（用户口径 2026-10-11）：
   *    - **默认全选**（「商品sku表格中的所有列默认全选」）；
   *    - 未选中的行 ⇒ 预选未通过（不进第二步的价格库存列表）；
   *    - 行内两图判定为 `different` / `unknown` ⇒ **置灰不可编辑、红框、预选未通过**，
   *      并且**自动取消勾选**（用户口径：预检不通过则默认置灰不勾选）。
   *  `undefined` = 尚未初始化（首次渲染按「全选」算，见 `effectivePicked`）。 */
  const [picked, setPicked] = useState<Record<number, boolean[]>>({});

  /** **已应用到表格的 SKU 匹配图**（`行下标 → rel_path`，用户口径 2026-10-14）。
   *
   *  > 「点击应用在商品 sku 表格 tab 下的列表中更新相关数据，
   *  >   第一列展示匹配到的图片预览图」
   *
   *  ⚠️ 与 `matchRows`（弹窗内的实时推导）是**两件事**：
   *  `matchRows` 每改一次规则就重算（预览用），而这里是**用户点「应用匹配结果」
   *  那一刻的确定结果** —— 只有它会被画进 SKU 表格第一列。
   *  弹窗里继续调规则不会动表格，直到用户再点一次应用（「应用」是个动作，
   *  不是「所见即所得」）。
   */
  const [appliedSkuImages, setAppliedSkuImages] = useState<Record<number, string>>({});

  /** **表格原始值快照**（用户口径 2026-10-13）—— 覆盖原列**可撤销**的依据。
   *
   *  ⚠️ 键是**「表 + 列」**（`snapshotKey` 复合键），值是**该列**的原值 ——
   *  用户口径 2026-10-15：「**恢复数据应只恢复当前列而不是全表**」。
   *  按表存（键只有表下标）时，任点一列的「还原原始值」会把整表所有列撤回，
   *  用户在别的列改的东西一并无损丢失。
   *
   *  ⚠️ 每列只在**首次覆盖前**存一次（之后不再覆盖快照），这样「还原」回到的是
   *  **最初读进来的原始值**，而不是「上一次改之前」——后者会让用户点了两次
   *  还原还在中间态里打转，越还原越糊涂。还原后该列的快照**删掉**，
   *  再改才重新存一份（否则第二次改完就撤不回来了）。
   *
   *  ⚠️ 纯内存、不落库、不写盘（用户数据包目录**只读**，这是硬约束）。 */
  const [snapshots, setSnapshots] = useState<Record<string, TableSnapshot>>({});

  /**
   * 本地图的**可匹配集合** —— 用户口径 2026-10-11 的**红线**：
   *
   * > 「预检的步骤是**先检查媒体资源是否存在违规词，然后再匹配 sku 图**，
   * >   违规的媒体资源**不允许被选中，也不允许被匹配到**」
   *
   * ⚠️ 所以模糊查询的输入**必须**是过滤后的集合（这一个 `useMemo` 是唯一闸门）：
   * 之前写成「查询用全量、只有弹窗过滤」⇒ 命中违规词的那张图**照样能被名称匹配上**
   * 并算作「预检通过」，等于让违规素材走免检通道（与第一步「命中违规词不允许勾选」
   * 同一条红线，却在这里破了个洞）。
   *
   * 两步顺序体现为两个条件同时成立：
   *   ① **先**跑过违规词检测（`ocr_ok === true`）—— 未检测（`null`）不算通过；
   *   ② **再**排除命中违规词的（`has_forbidden !== true`）。
   */
  const cleanSkuMedia = useMemo(
    //: 两步都要（用户口径 2026-10-11）：**红线**（已检测 + 非违规）→ **再**按勾选收窄。
    //: ⚠️ 两个函数都在 lib 里（单一实现）—— 写在组件里就没法被 vitest 覆盖。
    () => filterCheckedSkuMedia(selectableSkuMedia(skuMedia), checked),
    [skuMedia, checked],
  );

  /** **手动选择的本地图**（用户口径 2026-10-11：「未匹配到支持弹窗选择…」）。
   *  key = `tIndex:rowIndex`；有值 ⇒ **覆盖**模糊匹配结果（用户的选择优先）。
   *  ⚠️ 本 state 曾被一次误删（`specCategories` 整块清理时连带删掉），
   *  已恢复 —— 它是**商品 SKU 表格**读的权威覆盖，删了会导致「应用匹配结果」不生效。 */
  const [manualPicks, setManualPicks] = useState<
    Record<string, { relPath: string; fileName: string }>
  >({});

  /** **本地图匹配正则**（用户口径 2026-10-11：列表头「规格配置」按钮可改）。
   *
   *  `patternDraft` = 输入框里的**待应用**值；`pattern` = **已生效**的值。
   *  ⚠️ 分两个 state 是刻意的：用户要能改到一半不生效，点「重新匹配」才重跑
   *  （否则每敲一个字符就全表重算，26 行 × 每键一次 = 卡顿且没必要）。 */
  const [patternDraft, setPatternDraft] = useState(DEFAULT_SKU_IMAGE_PATTERN);
  const [pattern, setPattern] = useState(DEFAULT_SKU_IMAGE_PATTERN);

  //: 本地图**有序列表**（按数据包加载顺序）—— 用**当前生效的正则**抽取比对键。
  //: ⚠️ 输入是 `cleanSkuMedia`（**已排除违规图**）—— 见上面的红线。
  const localImages = useMemo(
    () => localSkuImageList(cleanSkuMedia, pattern),
    [cleanSkuMedia, pattern],
  );


  /** **表头设置图标点开的列**（用户口径 2026-10-14：正则取值入口从顶部移到每列表头）。
   *  `null` = 没开。弹窗是**同一个实例**（`ColumnPatternControl` 传 `withTrigger={false}`
   *  + 受控 `open`）—— 点哪列表头就以哪列为默认（`initialColumn`）。 */
  const [patternTarget, setPatternTarget] = useState<number | null>(null);
  //: 弹窗目标（哪一行在选图）。
  const [pickTarget, setPickTarget] = useState<{ tIndex: number; rowIndex: number; skuName: string } | null>(null);

  //: （弹窗候选复用上面的 `cleanSkuMedia` —— 同一份「无违规词」集合，不另起一份。）

  /** **SKU 图匹配**（用户口径 2026-10-13）：UI 状态。
   *
   *  规则（区域 / 名称列 / 匹配方式 / 正则）**住在弹窗里**，但结果上抛给向导
   *  —— 第二步「导入 SKU 图」吃的是这份结果。
   */
  const [matchOpen, setMatchOpen] = useState(false);
  const [matchZones, setMatchZones] = useState<string[]>([]);
  const [matchCol, setMatchCol] = useState<number>(-1);
  //: ⚠️ 用户口径 2026-10-14 方案 B：**图片名称取值正则**（与 SKU 列正则同构）。
  //: 原先的「匹配规则 精确/模糊」已删 —— 匹配方式固定为
  //: 「两侧各自跑正则取到完整 SKU 名称，再精确相等」。
  //: SKU 名称列的**取值正则**（可选，见 `SkuMatchModal` 的 props 注释）。
  const [matchExtract, setMatchExtract] = useState("");

  //: 「图片区域」候选 = **预检通过的全部图片区域**（用户口径 2026-10-13：
  //: 「预检通过的所有图片区域均支持被选中」）。
  //:
  //: ⚠️ **第一版有两个错，都要钉住**：
  //:  ① 数据源写死 `skuMedia`（= `kind === "sku"`），只列 SKU 目录 ⇒ 用户想拿
  //:     `主图/` 或 `详情图/` 里的图当规格图时**选不到**（真实场景常见：规格图
  //:     就放在主图目录里）。现在取**全部预检媒体**，按目录名去重。
  //:  ② 判「这个区有没有可选图」时，写法是在**逐项循环里**对**单张图**
  //:     `filterCheckedSkuMedia(selectableSkuMedia([m]))` —— 于是「该区的**第一张**
  //:     图恰好未勾选」就会让**整个区被跳过**（其余已勾选的图被连坐）。正确做法是
  //:     取**整区**算：`mediaOfZone(...)` 之后再过滤。这正是用户看到的
  //:     「图片区域不应现在 / 选不到」。
  const matchZoneOptions = useMemo(() => {
    const zonesSource = allMedia;
    const names: string[] = [];
    for (const m of zonesSource) {
      if (m.group && !names.includes(m.group)) names.push(m.group);
    }
    return names
      .map((name) => {
        //: ⚠️ 按**整区**判定可选数（不是逐张）—— 见上面 ②。
        const count = filterCheckedSkuMedia(
          selectableSkuMedia([...mediaOfZone(zonesSource, name)]),
          checked,
        ).length;
        return { value: name, label: `${name}（${count}）`, count };
      })
      //: 只列**确实有可选图**的区域（空区域选了对不上任何东西，只会让用户困惑）。
      .filter((o) => o.count > 0)
      .map(({ value, label }) => ({ value, label }));
  }, [allMedia, checked]);

  //: 「SKU 名称列」候选 = 所有列（用户自己选哪一个当名称列；不猜）。
  const matchColumnOptions = useMemo(
    () => (tables[0]?.columns ?? []).map((c, i) => ({ value: i, label: `${c}` })),
    [tables],
  );


  /** **匹配成功的 SKU 图**（用户口径 2026-10-13）→ 上抛给向导。
   *
   *  「第二步中的导入 sku 图上传的是第一步 sku 表中**已经匹配成功的**图」
   *  ⇒ 逐行取**最终生效**的本地图（手选优先于名称匹配），只保留有图的行。
   *
   *  ⚠️ 依赖 `tables` / `cleanSkuMedia` / `pattern` / `manualPicks`：
   *  用户改正则、取消勾选、手选换图 —— 三者任一变化都要重算（否则第二步
   *  拿到的还是旧匹配结果，会出现「第一步显示 A 图、第二步上传 B 图」）。
   */
  /** **匹配结果**（用户口径 2026-10-13）：用**弹窗里配的规则**算。
   *
   *  ⚠️ **硬前置：用户必须显式指定图片区域**（用户口径原话：「必须用户指定
   *  sku 规格图的媒体资源区，后指定使用哪一些与图片名称去匹配」）。
   *
   *  第一版在 `matchZones` 为空时**回落到「全部 SKU 区域」**，后果是用户一打开
   *  弹窗就看到「已经匹配到了数据」—— 他无法判断这些图来自哪个区域，也不知道
   *  自己还没做选择。**这不是「兜底」，是替用户做了一个他没做的决定。**
   *  现在：没选区域 ⇒ **返回空结果**（弹窗里 rows 为空、命中 0 行、确认键灰着）。
   *
   *  同理 `matchCol < 0`（还没选名称列）也不给结果 —— 两个都没有默认值。
   */
  /** 名称列的**第一条**列值 —— 反推弹窗的「原始文本」预填（用户口径 2026-10-13）。 */
  const sampleSourceName = useMemo(() => {
    const table = tables[0];
    if (!table || matchCol < 0) return "";
    return String(table.rows?.[0]?.[matchCol] ?? "").trim();
  }, [tables, matchCol]);


  const matchRows = useMemo<SkuMatchRow[]>(() => {
    const table = tables[0];
    if (!table) return [];
    //: ⚠️ 两道硬前置，都不给默认值（见上）。
    if (matchZones.length === 0) return [];
    if (matchCol < 0) return [];

    const nameCol = matchCol;
    if (nameCol < 0 || nameCol >= table.columns.length) return [];

    //: 候选图 = 用户选的区域里「已检过 + 非违规 + 已勾选」的。
    //:
    //: ⚠️ 数据源是**全部预检媒体**（不是 `cleanSkuMedia` = `kind === "sku"`）——
    //: 用户口径 2026-10-13：「预检通过的所有图片区域均支持被选中」。
    //: 红线不变：仍然过 `selectableSkuMedia`（已检测 + 非违规）+ 勾选过滤
    //: —— 换个数据源**不等于**放宽违规图门禁。
    const pool = filterCheckedSkuMedia(
      selectableSkuMedia([...allMedia]),
      checked,
    ).filter((m) => matchZones.includes(m.group));

    const rowsIn = table.rows ?? [];

    //: ① 先算每行「经正则取值后的 SKU 名」+ 手选优先。
    //: ⚠️ 提取失败**回落原值**（`applyExtract` 内部已这么做）而不是判未命中：
    //: 正则写错时全表落空，用户会以为「这些图就是配不上」，而真正的原因
    //: 是他的正则没匹配上任何一行。
    const keys = rowsIn.map((row, rowIndex) =>
      applyExtract(String(row[nameCol] ?? "").trim(), matchExtract),
    );
    const fullPool = selectableSkuMedia([...allMedia]);
    const picked: ({ relPath: string; fileName: string } | null)[] = rowsIn.map((_, rowIndex) => {
      const manualRaw = manualPicks[`0:${rowIndex}`];
      if (!manualRaw) return null;
      //: 手选校验用**全量可选池**（手选可以来自任意区域，不只当前选的）。
      return fullPool.some((m) => m.rel_path === manualRaw.relPath)
        ? { relPath: manualRaw.relPath, fileName: manualRaw.fileName }
        : null;
    });

    //: ② 自动分配：**只对没手选的行**做，且**排除已被手选占用的图**
    //: （一图一用 —— 手选的优先权高于自动）。
    const autoRows = rowsIn
      .map((_, rowIndex) => rowIndex)
      .filter((i) => !picked[i]);
    const auto = matchSkuRows(
      autoRows.map((i) => ({ skuName: keys[i] })),
      pool.map((m) => ({ fileName: m.file_name || m.rel_path, relPath: m.rel_path })),
      matchExtract,
      new Set(picked.filter(Boolean).map((p) => (p as { relPath: string }).relPath)),
    );
    //: 行号 → 分配结果（`auto` 的下标是 `autoRows` 的下标）。
    const autoByRow = new Map<number, { relPath: string | null; fileName: string }>();
    autoRows.forEach((rowIndex, k) => autoByRow.set(rowIndex, auto[k]));

    return rowsIn.map((row, rowIndex) => {
      const displayName = String(row[nameCol] ?? "").trim();
      const skuName = keys[rowIndex];
      const manual = picked[rowIndex];
      if (manual) {
        return {
          rowIndex,
          skuName: displayName,
          extracted: skuName,
          relPath: manual.relPath,
          fileName: manual.fileName,
          via: "manual" as const,
        };
      }
      const hit = autoByRow.get(rowIndex);
      if (hit?.relPath) {
        return {
          rowIndex,
          skuName: displayName,
          extracted: skuName,
          relPath: hit.relPath,
          fileName: hit.fileName,
          via: "regex" as const,
        };
      }
      return {
        rowIndex,
        skuName: displayName,
        extracted: skuName,
        relPath: null,
        fileName: "",
        via: "none" as const,
      };
    });
  }, [
    tables, matchCol, matchExtract, matchZones,
    allMedia, checked, manualPicks,
  ]);

  /** **上抛**给向导（第二步「导入 SKU 图」的数据源）—— 只带有图的行。 */
  const matchedSkuImages = useMemo(
    () =>
      matchRows
        .filter((r) => !!r.relPath)
        .map((r) => {
          const parsed = parseSkuName(r.skuName);
          return {
            specName: parsed.category,
            specValue: parsed.value || r.skuName,
            relPath: r.relPath as string,
            fileName: r.fileName,
          };
        }),
    [matchRows],
  );

  //: 上抛（用序列化键做依赖，避免每帧重跑 —— 数组每次渲染都是新引用）。
  const matchedKey = matchedSkuImages
    .map((it) => `${it.specName}\u0000${it.specValue}\u0000${it.relPath}`)
    .join("|");
  useEffect(() => {
    onSkuMatchChange(matchedSkuImages);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchedKey]);

  if (tables.length === 0) return <Empty description="数据包里没有可读取的表格" />;

  /** 把「本面板第 i 张表」映射到**父级完整数组**里对应的下标。 */
  const emit = (tIndex: number, next: PrecheckTable) => onTableChange(tIndex, next);

  /** **「SKU 图」这一列在表里的下标**（`{index, name}`；没这列 ⇒ `-1`）。
   *
   *  ⚠️ 用户口径 2026-10-15：「把第一列**放入 rows**，它是**固定列**」——
   *  表格第一列（`__matched_image`）的值就是 `rows[i][index]`，
   *  **不再**单独存在 `appliedSkuImages` 里（那是两处真相源）。
   */
  const skuImageColOf = (t: { columns: string[]; rows: (string | null)[][] }) => {
    const index = t.columns.findIndex((c) => String(c).trim() === SKU_IMAGE_COL);
    return { index, name: SKU_IMAGE_COL };
  };

  /** **整列删除**（用户口径 2026-10-15）。
   *
   *  ⚠️ 用 `dropTableColumn`（纯函数）而不是就地 splice ——
   *  纯函数能让调用方**先算「删了会影响什么」再决定**（二次确认的文案就靠它）。
   */
  const onDropColumn = (tIndex: number, colIndex: number) => {
    const table = props.tables[tIndex];
    if (!table) return;
    emit(tIndex, dropTableColumn(table, colIndex));
  };

  /** 删除某列时的**二次确认文案**（用户口径 2026-10-15）：
   *  > 「如果该列**在第二步已经被映射使用**，删除该列后二次确认提示会
   *  >   **同步删除已经映射使用的 XXX 配置参数**」
   *
   *  ⚠️ 必须**点名是哪几个配置**（说不清「XXX」等于没说）——
   *  用户要能判断「这个代价我认不认」。三类用法都在这里数一遍：
   *    ① 商品 SKU 映射的**来源**（规格分类列）；
   *    ② 价格库存映射的**来源列**；
   *    ③ 提取规则里被引用（那属于第一步自己的规则，一并提醒）。
   *  ⚠️ 口径来源：`props.priceMapping` 是**第二步的当前草稿**（没配就是空）
   *  —— 它反映的就是「现在删了会不会立刻少一条映射」。
   */
  const columnDropWarning = (name: string): React.ReactNode => {
    const used: string[] = [];
    //: ① 商品 SKU 映射（来源列 = 规格分类列）。
    if ((props.specImportSources ?? []).includes(name)) {
      used.push("商品 SKU 映射关系");
    }
    //: ② 价格库存映射（来源列）。
    if ((props.priceSources ?? []).includes(name.trim())) {
      used.push("价格库存映射关系表");
    }
    //: ③ 该列**自己**有没有跑过取值规则（列级快照）—— 删列会连快照一起没。
    if (used.length === 0) {
      return "该列的数据会一并删除，且不可恢复。";
    }
    return (
      <span>
        该列已被 {used.join("、")} 使用，删除后
        <Text strong>{`会同步删除已经映射使用的「${name}」配置参数`}</Text>
        ，且不可恢复。
      </span>
    );
  };

  /** **添加列**弹窗的开关与草稿（用户口径 2026-10-15）。 */
  const [addColTarget, setAddColTarget] = useState<number | null>(null);
  const [addColName, setAddColName] = useState("");
  const [addColKey, setAddColKey] = useState("");

  /** 提交「添加列」：中文名必填；英文名空 ⇒ 按顺序规则自动生成。 */
  const submitAddColumn = () => {
    if (addColTarget === null) return;
    const table = props.tables[addColTarget];
    if (!table) return;
    const name = addColName.trim();
    if (!name) {
      void message.warning("请填写表头中文名");
      return;
    }
    if (table.columns.some((c) => String(c).trim() === name)) {
      void message.warning(`已存在名为「${name}」的列`);
      return;
    }
    const key = addColKey.trim() || nextManualColumnKey(table.column_keys ?? []);
    if ((table.column_keys ?? []).includes(key)) {
      void message.warning(`英文名「${key}」已被占用`);
      return;
    }
    emit(addColTarget, addTableColumn(table, name, `${MANUAL_COLUMN_PREFIX}${key}`));
    setAddColTarget(null);
    setAddColName("");
    setAddColKey("");
  };

  const setCell = (tIndex: number, rowIndex: number, colIndex: number, value: string) => {
    const table = tables[tIndex];
    emit(tIndex, {
      ...table,
      rows: table.rows.map((r, ri) =>
        ri !== rowIndex ? r : r.map((c, ci) => (ci === colIndex ? value : c)),
      ),
    });
  };

  /** 对某表某一列套规则（**只改选中的行**；没勾选 = 整列）。
   *
   * ⚠️ 本面板只拿到 SKU 表的**子集**，改完必须把结果**交回父级按真实下标嵌回**
   * 完整数组 —— 直接上抛子集会把属性表丢掉，页签随之重建、面板状态被重置
   * （用户实测：第二次改库存点「应用」无响应）。
   */
  const batchColumn = (
    tIndex: number,
    column: number,
    rule: BatchEditRule,
    onlyRows?: readonly number[],
  ) => {
    emit(tIndex, applyBatchRuleToTable(tables[tIndex], column, rule, onlyRows));
  };

  /** **正则覆盖原列**（用户口径 2026-10-13）。
   *
   *  ① **首次覆盖前先存快照**（撤销用）—— 覆盖是破坏性的，原值没了就真没了；
   *     ⚠️ 快照**按列**存（表 + 列复合键），撤销也只撤这一列；
   *  ② 作用域交给 `applyPatternToColumn`（与预览**同一份实现**）——
   *     预览显示「命中 3 行」而实际改了 5 行是本轮最容易出的静默不一致。
   */
  const applyColumnPattern = (
    tIndex: number,
    column: number,
    pattern: string,
    onlyRows?: readonly number[],
  ) => {
    const sk = snapshotKey(tIndex, column);
    setSnapshots((prev) =>
      prev[sk] ? prev : { ...prev, [sk]: snapshotTable(tables[tIndex], tIndex, column) },
    );
    emit(tIndex, applyPatternToColumn(tables[tIndex], column, pattern, onlyRows));
  };

  /** **直接落值**（数据包规则执行用）—— 后端已算好逐行结果，这里只写。
   *
   *  ⚠️ 与 `applyColumnPattern` **按同一口径存快照**（都是「表 + 列」键）——
   *  两条路改的是同一列时共用一份快照，用户不会遇到「撤了一半」；
   *  改**不同列**时各存各的，「改 A 列、还原 B 列」不会把 A 列也撤掉。
   *  ⚠️ `after === null` ⇒ **留空**（该行提不出），**不写回原值**（硬约束 5）。 */
  const applyColumnValues = (
    tIndex: number,
    column: number,
    changed: readonly { rowIndex: number; after: string | null }[],
  ) => {
    const sk = snapshotKey(tIndex, column);
    setSnapshots((prev) =>
      prev[sk] ? prev : { ...prev, [sk]: snapshotTable(tables[tIndex], tIndex, column) },
    );
    emit(tIndex, applyValuesToColumn(tables[tIndex], column, changed));
  };

  /** **把 SKU 图自动匹配的结果写回 SKU 表格**（用户口径 2026-10-14）。
   *
   * > 「业务点：sku 图底部的点击确认匹配结果优化为应用匹配结果，
   * >   点击应用在商品 sku 表格 tab 下的列表中更新相关数据，
   * >   第一列展示匹配到的图片预览图」
   *
   * 语义：
   *  - 只处理**命中**的行（`relPath` 非空）—— 没匹配上的行保持原样，
   *    不写空值（那会把用户手填的内容抹掉）；
   *  - 写入**名称列**（`matchCol`）：把该行的规格值换成**匹配到的那张图**的标识，
   *    这样列表里「这一行用哪张图」是**数据**，不是渲染期的临时推导 ——
   *    否则关掉弹窗、切走再回来，对应关系就没了（用户看到的「没更新」）；
   *  - 同时记进 `appliedSkuImages`（`rowIndex → relPath`），供**第一列**渲染预览图。
   *
   * ⚠️ **不改 `tables` 的列结构**（不加列、不改表头）：用户口径是「第一列展示
   * 匹配到的图片预览图」—— 预览是**展示层**的东西，表头仍来自 xlsx 原值，
   * 与「读到什么就展示什么」一致。
   */
  const applySkuMatch = (rows: SkuMatchRow[]) => {
    const hits = rows.filter((r) => !!r.relPath);
    if (!hits.length) return;
    //: ③ ⚠️ **把匹配结果回填进「SKU 图」真列**（用户口径 2026-10-15：
    //: 「SKU 图应该给放回 rows 中」+「SKU 图与规格分类属于**后处理的固定表格列**」）。
    //:
    //: ⚠️ 为什么必须回填 `rows`：它是**表格的一列**，用户要用它做映射
    //: （`spec_image` 的来源列）。只存在 `appliedSkuImages`（渲染层）时，
    //: 取值要处处特判，而映射/规则/删除这些链路都拿不到它。
    //: ⚠️ 回填的是**同一份结果**（不是另算一遍）—— 两张表读同一份数据。
    //: ⚠️ 本函数在 `TablesPanel` 内 ⇒ 上抛用 `props.onTableChange`（按面板下标）。
    props.tables.forEach((t, ti) => {
      if (t.is_sku_table === false) return;
      const col = t.columns.findIndex((c) => String(c).trim() === SKU_IMAGE_COL);
      if (col < 0) return; //: 没插这列（非 SKU 表）⇒ 不动
      const byRow: Record<number, string> = {};
      for (const r of hits) byRow[r.rowIndex] = r.relPath as string;
      props.onTableChange(ti, {
        ...t,
        rows: t.rows.map((row, i) => {
          const next = [...row];
          //: ⚠️ 已有值就**不覆盖**（用户可能手选过那张图）。 
          if (!next[col]) next[col] = byRow[i] ?? null;
          return next;
        }),
      });
    });
    //: ① 第一列预览图（弹窗自己的表格用）。
    setAppliedSkuImages((prev) => {
      const next = { ...prev };
      for (const r of hits) next[r.rowIndex] = r.relPath as string;
      return next;
    });
    //: ② ⚠️ **同时写进 `manualPicks`** —— 这才是**商品 SKU 表格**读的真相源。
    //:
    //: 【真 bug 修复 2026-10-14】用户报：「点击应用匹配结果后，商品 sku 表格依旧
    //: 显示未匹配到本地数据，第一列已经展示了匹配到的 sku 列图」。
    //: 根因：SKU 表格的「本地图」判据走的是 `lookupLocalSkuImage(localImages, skuName)`
    //: （另一套模糊匹配），**完全不看 `appliedSkuImages`** ⇒
    //: 弹窗里匹配成功、表格那边根本不知道。
    //: 两个表读**同一份数据**（`cleanSkuMedia`），只是判据旧。
    //: `manualPicks` 正是表格的**权威覆盖**（「用户判断 > 自动匹配」）⇒
    //: 把弹窗结果写进去，两张表就一致了。
    setManualPicks((prev) => {
      const next = { ...prev };
      for (const r of hits) {
        next[`0:${r.rowIndex}`] = {
          relPath: r.relPath as string,
          fileName: r.fileName,
        };
      }
      return next;
    });
  };

  /** **还原原始值**（**只把这一个「表 + 列」还原到首次覆盖前的快照**）。
   *
   *  ⚠️ 作用域 = **当前列**（用户口径 2026-10-15：「恢复数据应只恢复当前列而不是
   *  全表」）—— 用户在另一列改的东西**必须留着**。
   */
  const restoreColumn = (tIndex: number, column: number) => {
    const sk = snapshotKey(tIndex, column);
    const snap = snapshots[sk];
    if (!snap) return;
    emit(tIndex, restoreRows(tables[tIndex], snap));
    setSnapshots((prev) => {
      const next = { ...prev };
      //: ⚠️ 删掉**该列**的快照（不是整表的）—— 再改一次还能重新存一份，
      //: 否则「还原 → 再改 → 再想还原」就没有可还原的快照了。
      delete next[sk];
      return next;
    });
  };

  return (
    <>
      {/* **「添加列」弹窗**（用户口径 2026-10-15）。
          ⚠️ 挂在 `tables.map` **之外**（只这一个实例）—— 挂进去会随表数量重复渲染，
          弹窗在切页签时会被卸载。目标表用 `addColTarget` 记录。 */}
      <Modal
        open={addColTarget !== null}
        title="添加列"
        //: ⚠️ antd 默认顶部对齐，本项目口径是**所有弹窗居中**（有守卫钉着）。
        centered
        destroyOnClose
        onOk={submitAddColumn}
        onCancel={() => setAddColTarget(null)}
        okText="添加"
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <div>
            <Text>表头中文名</Text>
            <Input
              value={addColName}
              placeholder="如 成本价"
              onChange={(e) => setAddColName(e.target.value)}
              style={{ marginTop: 4 }}
            />
          </div>
          <div>
            <Text>英文名</Text>
            <Input
              value={addColKey}
              //: ⚠️ **非必填**：留空则按顺序规则自动生成（`col_1`/`col_2`…）。
              placeholder={`非必填，留空自动生成（如 ${nextManualColumnKey(
                props.tables[addColTarget ?? 0]?.column_keys ?? [],
              )}）`}
              onChange={(e) => setAddColKey(e.target.value)}
              style={{ marginTop: 4 }}
            />
          </div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            手动新增的列可整列删除；原始列与固定列不可删除。
          </Text>
        </Space>
      </Modal>

      {tables.map((table, tIndex) => {
        //: ⚠️ **SKU 名称列走映射**（用户口径 2026-10-15：「预检阶段的派生判定
        //: （哪列是 SKU 名）**应走映射**」）—— 用户把「SKU 名称」映射到哪列就用哪列，
        //: 不再靠别名猜（数据包列名一换就猜空 ⇒ 整表读不出 SKU 名）。
        const nameCol = skuNameColumnIndex(table.columns, props.priceMapping ?? []);
        //: ⚠️ **线上图列已从第一步移除**（用户口径 2026-10-15）⇒ **不再有这一列**，
        //: 两图一致性判定随之失效（-1 ⇒ `onlineUrl` 恒空 ⇒ 不参与判定）。
        //: 这不是缺陷：线上图没了，就没有「线上 vs 本地」可判。
        const imageCol = -1;
        /** 每行的**派生判定**（需求 2/3/5）：规格分类 + 本地图 + 两图一致性。
         *  ⚠️ 纯函数派生（不写 state）—— 用户改单元格即时重算。 */
        const rowInfos = table.rows.map((row, rowIndex) => {
          const skuName = nameCol >= 0 ? String(row[nameCol] ?? "").trim() : "";
          const parsed = parseSkuName(skuName);
          const onlineUrl = imageCol >= 0 ? String(row[imageCol] ?? "").trim() : "";
          //: ① 用户在弹窗里**手动选过** ⇒ 直接用他的选择（用户判断优先于名称匹配）。
          //: ⚠️ 但若那张图**已在媒体区被取消勾选**（或违规/未检测）⇒ 手选也失效
          //: （用户口径 2026-10-11：取消勾选的图要连带移除，不能从后门留着）。
          const manualRaw = manualPicks[`${tIndex}:${rowIndex}`];
          const manual =
            manualRaw &&
            cleanSkuMedia.some((m) => m.rel_path === manualRaw.relPath)
              ? manualRaw
              : undefined;
          //: ② 否则按 SKU 名称做**前后通配符模糊查询，取第一个命中**。
          const localRel = manual?.relPath
            ?? (skuName ? lookupLocalSkuImage(localImages, skuName) : null);
          //: ⚠️ **预检判据（用户口径 2026-10-11，推翻上一版）**：
          //: 「将拆分后的 sku 名称去查本地 sku 图名进行对齐即为通过了预检」。
          //: ⇒ **不再做任何图片内容比对**（那套 MD5/HEAD 已整段移除：
          //:   实测 1688 图床不给哈希、小红书 CDN 又需签名，且画面级判据
          //:   在同系列图之间区分度不足 —— 判不了还不如不判）。
          //: 现在只有一件事：**本地 `SKU/` 文件夹里有没有与 SKU 名称对上的图**。
          const passed = !!localRel;
          return { rowIndex, skuName, parsed, onlineUrl, localRel, passed };
        });
        /** 预检**未通过**的行：本地没有对齐上图的 SKU ⇒ 置灰 + 红框 + 不可勾选。
         *
         * ⚠️ 这正是「预选未通过」的定义（用户口径）—— 不再有 `unknown` 这一态：
         * 对不上就是没通过，判据是确定的（是否存在该文件），不存在判不了的情况。 */
        const blockedRows = new Set(
          rowInfos.filter((r) => !r.passed).map((r) => r.rowIndex),
        );
        //: 需求 4/5：**默认全选**（用户口径「商品sku表格中的所有列默认全选」）；
        //: 预检未通过（本地无对齐图）的行**结构上**不许被勾选。
        //: 用户取消勾选的行同样不进第二步 —— 两者都由 `passedSkuSpecs` 收口。
        const pickedRows = (picked[tIndex] ?? table.rows.map(() => true))
          .map((on, i) => (on && !blockedRows.has(i) ? i : -1))
          .filter((i) => i >= 0);
        return (
        <div key={`${table.sheet_name}-${tIndex}`} style={{ marginBottom: 20 }}>
          {/* ⚠️ 需求 4：把本表的选择快照上抛给向导（第二步要用同一份判据）。
              单独一个零渲染子组件做上报 —— 在 map 里直接调 setState 会
              「渲染期更新父组件」，React 会警告且行为不确定。 */}
          <SelectionReporter
            blocked={[...blockedRows]}
            picked={pickedRows}
            onChange={onSelectionChange}
          />
          {/* 用户口径 2026-10-20（本轮）：
              ① **文件名整块移除**（红框里的「文件名称：店透-SKU预览-表格-xxx.xlsx」不再显示）；
              ② **两个按钮（添加列 / SKU 图自动匹配）挪到最左侧**；
              ③ 表格名 + 行列数 挪到**右侧**（原在左侧）。
              理由：用户扫视表格时先要「能做什么」（按钮），文件名属于来源信息、占位却无操作价值。 */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginBottom: 6,
            }}
          >
            {/* 左：操作按钮 */}
            <Space size={8} wrap={false}>
              {/* ⚠️ **手动添加列**（用户口径 2026-10-15）：
                  「商品sku表格**支持手动添加列**，弹窗用户设置表头**中文和英文名**
                    （**英文名非必填为空则按照一定的顺序规则自动生成**）」 */}
              {tIndex === 0 && (
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => {
                    setAddColTarget(tIndex);
                    setAddColName("");
                    setAddColKey("");
                  }}
                >
                  添加列
                </Button>
              )}
              {/* ⚠️ **仅第一张 SKU 表**（`tIndex === 0`）显示匹配按钮 ——
                  匹配结果是**全局一份**（上抛给向导），多张表各出一个按钮
                  会让人以为能分别配（实际后点覆盖先点）。 */}
              {tIndex === 0 && (
                <Button
                  size="small"
                  type="primary"
                  ghost
                  onClick={() => {
                    if (!precheckPassed) {
                      message.warning("请先进行违规词检测");
                      return;
                    }
                    setMatchOpen(true);
                  }}
                >
                  SKU 图自动匹配
                </Button>
              )}
              {tIndex === 0 && matchRows.length > 0 && (
                <Text
                  type={matchRows.some((r) => r.relPath) ? "success" : "warning"}
                  style={{ fontSize: 12 }}
                >
                  已匹配 {matchRows.filter((r) => r.relPath).length}/{matchRows.length} 行
                </Text>
              )}
            </Space>
            {/* 右：**来源文件名** + 表格名 + 行列数
                ⚠️ 用户口径 2026-10-20：「红框位置缺少读取的原始表格名称的展示」
                ⇒ 把后端下发的 `source_file`（xlsx 原文件名）显示在最左，
                它是「这批数据到底从哪个文件读来的」的唯一答案 ——
                没有它，用户换包后无法确认自己看的是不是新文件。 */}
            <Space size={8} wrap={false}>
              {table.source_file ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {table.source_file}
                </Text>
              ) : null}
              <Text strong>{table.sheet_name}</Text>
              <Tag>
                {table.columns.length} 列 × {table.rows.length} 行
              </Tag>
              {pickedRows.length > 0 && (
                <Tag color="blue">已选 {pickedRows.length} 行</Tag>
              )}
            </Space>
          </div>

          <Table
            size="small"
            //: ⚠️ rowKey 必须是**数字行下标**：勾选键（`selectedRowKeys`）会直接当作
            //: 批量修改的**行下标**用（`scope.has(rowIndex)`）。早先用 `${tIndex}-${index}`
            //: 字符串 ⇒ `scope` 里是 "0-5" 而 rowIndex 是 5 ⇒ **永不匹配** ⇒
            //: 选中行后「应用」点了没反应（用户实测 2026-10-09：应用未生效）。
            rowKey={(record: { __index: number }) => record.__index}
            pagination={false}
            //: 列数不固定 ⇒ 横向要能滚；滚动条外观藏掉（用户口径 2026-10-08）。
            className="hermes-hide-scrollbar"
            scroll={{ x: "max-content" }}
            dataSource={table.rows.map((row, index) => ({ __index: index, row }))}
            rowSelection={{
              //: 用户口径（2026-10-09）：规格列表加复选框，支持只对选中行批量改。
              //: ⚠️ 不提供「全选」按钮（用户口径：有上限的多选不要全选；这里行数大，
              //:  误点即成批改，故只留逐行勾选 + 表头全选）。
              selectedRowKeys: pickedRows,
              //: ⚠️ 需求 4/5：预选未通过的行**结构上不可勾选**（置灰）。
              getCheckboxProps: (record: { __index: number }) => ({
                disabled: blockedRows.has(record.__index),
              }),
              onChange: (keys) => {
                const next = table.rows.map((_, i) => keys.includes(i));
                setPicked((prev) => ({ ...prev, [tIndex]: next }));
              },
            }}
            //: ⚠️ 需求 5：预选未通过的行整行标红（红框 + 淡红底），一眼能找出来。
            rowClassName={(record: { __index: number }) =>
              blockedRows.has(record.__index) ? "publish-sku-row-blocked" : ""
            }
            columns={[
              //: **第一列：匹配到的 SKU 图预览**（用户口径 2026-10-14）。
              //:
              //: > 「点击应用在商品 sku 表格 tab 下的列表中更新相关数据，
              //: >   第一列展示匹配到的图片预览图」
              //:
              //: ⚠️ 数据源是 `appliedSkuImages`（**点过「应用匹配结果」才有的确定结果**），
              //: 不是 `matchRows`（弹窗内实时推导）。差别是刻意的：弹窗里改规则
              //: 不该悄悄改表格 —— 表格只在用户点「应用」时跟着动。
              //: ⚠️ 只画**已应用**的行；未匹配的行显示 `-`（不造占位图）。
              {
                title: "SKU 图",
                key: "__matched_image",
                width: SKU_THUMB_COL_WIDTH,
                render: (_: unknown, record: { __index: number }) => {
                  //: ⚠️ **值从 `rows` 取**（用户口径 2026-10-15：「把第一列**放入 rows**，
                  //: 它是**固定列**」）。
                  //: 旧实现读 `appliedSkuImages`（渲染层状态）—— 那样这列**不在表格数据里**，
                  //: 映射/规则/取值都拿不到它，只能靠特判。进 `rows` 后与「规格分类」
                  //: 同一条路：**一列数据，一处存放**。
                  //: ⚠️ 行内改过（手选切换）也写回 `rows`，所以这里就是唯一真相源。
                  const rel = skuImageColOf(table);
                  const relOfRow =
                    rel.index >= 0 ? (table.rows[record.__index]?.[rel.index] ?? null) : null;
                  //: 兜底：`rows` 还没回填时（匹配完才写）暂用内存态，避免整列闪空。
                  const relShown = relOfRow ?? appliedSkuImages[record.__index];
                  //: ⚠️ **点击切换已应用的 SKU 图**（用户口径 2026-10-14）：
                  //: > 「第一行的匹配 sku 图结果列支持切换 sku 图，弹窗选择
                  //: >   点击确认**替换历史 sku 图**」
                  //:
                  //: 「替换」是判据：选完必须**覆盖** `appliedSkuImages[行]`，
                  //: 不是新开一份记录（否则同一样式图会越攒越多、渲染取哪个不确定）。
                  //: 未匹配的行也**可点**（用户可直接手选，不必先跑自动匹配）。
                  const pick = () =>
                    setPickTarget({
                      tIndex,
                      rowIndex: record.__index,
                      skuName: "",
                    });
                  if (!relShown) {
                    return (
                      <Button
                        size="small"
                        type="link"
                        icon={<SwapOutlined />}
                        disabled={cleanSkuMedia.length === 0}
                        onClick={pick}
                      >
                        选择
                      </Button>
                    );
                  }
                  return (
                    <div className="sku-local-thumb">
                      <PrecheckThumb
                        token={token}
                        item={
                          {
                            group: "sku",
                            rel_path: relShown,
                            file_name: relShown.split("/").pop() ?? "",
                          } as PrecheckMediaItem
                        }
                      />
                      {/* 右上角「切换 SKU 图」图标（与规格列表同形）。 */}
                      <Tooltip title="切换 SKU 图">
                        <Button
                          size="small"
                          className="sku-local-thumb-switch"
                          icon={<SwapOutlined />}
                          disabled={cleanSkuMedia.length === 0}
                          onClick={pick}
                        />
                      </Tooltip>
                    </div>
                  );
                },
              },
              //: ⚠️ **用户口径 2026-10-14**（本轮最重要的一条）：
              //:
              //: > 「新增排队商品 sku 表格 tab **应读到什么就展示什么**，
              //: >   如果为图片则展示小图预览方式，**不应该还有线上图和本地图
              //: >   非表格中原始的列**」
              //:
              //: ⇒ **删掉两个自造列**（「线上图片」`__online_image` /
              //:   「本地图片」`__local_image`）。表格按 xlsx 原顺序展示。
              //:  （「规格分类」是**例外**：它是本轮新加的产品级列，见上。）
              //:
              //: ⇒ 图片列的识别下沉到**数据列统一渲染**（见下面 `table.columns`
              //:   的 `render`）：某列的值长得像图片 ⇒ 画**小图预览**；
              //:   否则照常给输入框。判据是**列里读到的东西**，不是列名猜测。
              //:
              //: ⚠️ 「读到什么就展示什么」的边界：**原始「图片」列要展示**。
              //: 此前它被专门排除（理由：URL 一长串是噪音）—— 那条口径**已被取代**：
              //: 现在它是图片列 ⇒ 渲染成小图预览，既不是噪音也不丢信息。

              //: ⚠️ **只跳过「SKU 图」一列**（用户口径 2026-10-15 两次纠正）：
              //:
              //: · **SKU 图** —— 由上面的**固定列**渲染（要缩略图 + 切换图标，
              //:   统一渲染做不到）；不跳过 ⇒ 屏幕上出现**两列同名**
              //:   （用户实测截图：两列「SKU 图」）。
              //: · **规格分类** —— **不跳过**，走**统一的数据列渲染**
              //:   （用户口径：「规则分类列恢复到原来历史 **默认为空支持表头的设置图标**」）。
              //:   统一渲染自带表头设置图标 ⇒ 与截图里的历史形态一致。
              //:   ⚠️ 它仍在 `columns` / `rows` 里（真列），所以统一渲染能画出来。
              ...table.columns
                .map((name, colIndex) => ({ name, colIndex }))
                .filter(({ name }) => String(name).trim() !== SKU_IMAGE_COL)
                .map(({ name, colIndex }) => ({
                //: **表头 + 设置图标**（用户口径 2026-10-14 再修订）：
                //: 「正则取值弹窗的位置应是**点击的对应设置图标**」「每一列的弹窗
                //:   均是**独立的**，每次打开**只展示当前列**的正则，**默认空**」。
                //:
                //: ⇒ 每列各挂**一个独立实例**（`ColumnPatternControl` 自带 Popover
                //:   触发器 = 这枚设置图标）—— 弹窗贴着各自图标弹、状态互不相干。
                //: ⚠️ 只有**文本列**才给图标（用户追加口径：「只展示在文本列，
                //:   数值列表头不展示设置图标」）。
                title: (
                  <Space size={2}>
                    <span>{name}</span>
                    {/* ⚠️ **整列删除**（用户口径 2026-10-15）：
                        「手动新增的列支持**整列删除**，**非手动新增和固定列不支持删除**」
                        ⇒ 判据 `canDropColumn(column_keys[colIndex])`（只有 `__manual_` 前缀给按钮）。
                        ⚠️ 若该列**已在第二步被映射使用**，二次确认要**点名会连带删掉的配置**——
                        用户口径：「会**同步删除已经映射使用的 XXX 配置参数**」。
                        那正是「删了之后第二步莫名少了个映射」的根因，必须明说。 */}
                    {canDropColumn((table.column_keys ?? [])[colIndex]) && (
                      <Popconfirm
                        title={`删除「${name}」整列？`}
                        description={columnDropWarning(name)}
                        okText="删除"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => onDropColumn(tIndex, colIndex)}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          className="column-drop-btn"
                          icon={<DeleteOutlined />}
                        />
                      </Popconfirm>
                    )}
                    {/* ⚠️ **所有列都有齿轮**（用户口径 2026-10-14：
                        「所有列均表头有设置图标，不再只局限于文本列」）。
                        正则区块**内部**仍只对文本列开放（`canApplyPattern`）。 */}
                    {hasColumnGear((table.column_kinds ?? [])[colIndex]) && (
                      <ColumnPatternControl
                        table={table}
                        tableIndex={tIndex}
                        pickedRows={pickedRows}
                        initialColumn={colIndex}
                        //: ⚠️ **只要图标**（用户口径 2026-10-14：「sku 名称列的设置图标
                        //: **很大且存在正则取值文本**，应统一**只需要设置图标**即可」）。
                        //: `withTrigger={false}` = 不画那个带文字的大按钮，只画小齿轮。
                        withTrigger={false}
                        //: 用户不再被允许在弹窗里换列（换了就与「贴着这枚图标」矛盾）。
                        onApply={(col, pattern, rows) => applyColumnPattern(tIndex, col, pattern, rows)}
                        onApplyValues={(col, changed) => applyColumnValues(tIndex, col, changed)}
                        onRestore={(col) => restoreColumn(tIndex, col)}
                        //: ⚠️ **按列**判有没有可还原的快照（用户口径 2026-10-15：
                        //: 「恢复数据应只恢复当前列而不是全表」）—— 该列没改过
                        //: 就不该出现还原按钮（点了也只会撤别的列，是「撤错了」）。
                        canRestore={!!snapshots[snapshotKey(tIndex, colIndex)]}
                      />
                    )}
                  </Space>
                ),
                dataIndex: ["row", colIndex],
                width: 160,
                render: (_: unknown, record: { __index: number; row: (string | null)[] }) => {
                  const raw = record.row[colIndex];
                  //: ⚠️ **读到什么就展示什么**（用户口径 2026-10-14）：
                  //: 某列的值看起来是图片 ⇒ 画**小图预览**，不再给输入框
                  //:（一长串 URL 填进输入框既看不清也没法改）。
                  //: 判据 = `isImageValue(值)`，**不是列名** —— 列名每个包都不一样。
                  //: ⚠️ 预览是**只读**的：值本身仍原样留在 `record.row` 里，
                  //: 表格编辑与发布链路读的还是原值。
                  if (colIndex !== nameCol && isImageValue(raw)) {
                    //: ⚠️ 用户口径 2026-10-14：「列表展示图片小图预览时**无需再展示地址**，
                    //: 使用 **tip** 展示地址链接」。
                    //: ⇒ 去掉旁边那行 URL 文本（一长串既占宽度又要横着读），
                    //: 地址收进 `Tooltip` —— 需要时可以复制/查看，不占版面。
                    return (
                      <Tooltip title={String(raw ?? "")}>
                        <SkuImageThumb token={token} onlineUrl={String(raw ?? "")} />
                      </Tooltip>
                    );
                  }
                  //: ⚠️ **用户口径 2026-10-14（**取代 2026-10-11 的剥前缀口径**）**：
                  //: 「展示原始值列，根据用户的操作再通过正则处理」「旧的 2026-10-11
                  //:  口径；『规格分类』列已单独展示前缀，不重复，旧口径和逻辑全部移除」。
                  //:
                  //: ⇒ 名称列**原样展示 xlsx 里的值**（如 `颜色分类:【中古风】实木三斗柜`），
                  //:   要剥前缀由用户**自己点表头设置图标跑正则** —— 不再由代码替他猜。
                  //: ⇒ `parseSkuName` 在本列彻底退场（展示与写回都不再用它）。
                  return (
                    <Input
                      size="small"
                      value={record.row[colIndex] ?? ""}
                      onChange={(e) => setCell(tIndex, record.__index, colIndex, e.target.value)}
                    />
                  );
                },
              })),
              //: 需求 5（用户口径 2026-10-11 最终版）：**本地图按 SKU 名称对齐**
              //: 即判预检通过；对不上 ⇒ 预选未通过（置灰 + 红框 + 不可勾选）。
              //: ⚠️ 判据是**确定**的（本地文件在不在），所以没有「判不了」的中间态。
              {
                title: "预检",
                key: "__verdict",
                width: 200,
                render: (_: unknown, record: { __index: number }) => {
                  const info = rowInfos.find((r) => r.rowIndex === record.__index);
                  if (!info) return null;
                  if (info.passed) {
                    return (
                      <Tag color="success" style={{ fontSize: 11 }}>
                        通过：本地图已对齐
                      </Tag>
                    );
                  }
                  //: ⚠️ **「选择本地图」按钮已移除**（用户口径 2026-10-15：
                  //: 「移除预检列中的选择本地图按钮功能移除，在**第一列**的
                  //:  已经有点击选择 sku 图的图标了」）。
                  //:
                  //: 两个入口做同一件事 = 用户得猜「该点哪个」，且两处状态要同步
                  //: （本项目踩过多次「两份真相源」）。第一列那枚图标是**唯一入口**。
                  //: ⚠️ 用户口径 2026-10-15：「（点第一列图标选择）文本移除」——
                  //: 引导语不放在这里（第一列那枚图标本身就有 Tooltip「切换 SKU 图」）。
                  //: 状态提示**保留**（它是有用的判据），只去掉后面那段括注。
                  return (
                    <Tag color="error" style={{ fontSize: 11 }}>
                      未匹配到本地图
                    </Tag>
                  );
                },
              },
            ]}
          />
        </div>
        );
      })}

      {/* **本地图选择弹窗**（用户口径 2026-10-11）：名称模糊查询未命中的行，
          支持从「已加载且无违规词」的 SKU 图里手动选一张。 */}
      <LocalSkuImagePickModal
        open={pickTarget !== null}
        items={cleanSkuMedia}
        token={token}
        skuName={pickTarget?.skuName ?? ""}
        onClose={() => setPickTarget(null)}
        onPick={(relPath, fileName) => {
          if (!pickTarget) return;
          const key = `${pickTarget.tIndex}:${pickTarget.rowIndex}`;
          setManualPicks((prev) => ({ ...prev, [key]: { relPath, fileName } }));
          //: ⚠️ **同时覆盖第一列已应用的 SKU 图**（用户口径 2026-10-14：
          //: 「弹窗选择点击确认**替换历史 sku 图**」）。
          //: 只写 `manualPicks` 不动 `appliedSkuImages` 的话，用户切完图
          //: 会看到第一列**还是旧图** —— 那是「替换没生效」，不是缓存问题。
          //: 覆盖而非追加：一行只有一张当前图。
          setAppliedSkuImages((prev) => ({ ...prev, [pickTarget.rowIndex]: relPath }));
          setPickTarget(null);
        }}
      />

      {/* **SKU 图自动匹配弹窗**（用户口径 2026-10-13）：规则化匹配（区域 / 名称列 / 方式）。 */}
      <SkuMatchModal
        open={matchOpen}
        token={token}
        zoneOptions={matchZoneOptions}
        zones={matchZones}
        onZonesChange={setMatchZones}
        columnOptions={matchColumnOptions}
        //: ⚠️ **传原值**（`-1` 表示没选）。第一版在这里回落成「猜一个名称列」，
        //: 于是弹窗一打开名称列就是填好的 —— 用户没选过任何东西，「必选」形同虚设。
        column={matchCol}
        onColumnChange={setMatchCol}
        extractPattern={matchExtract}
        sampleSourceName={sampleSourceName}
        onExtractPatternChange={setMatchExtract}
        //: ⚠️ 「执行匹配」= 用户**真的跑了规则** ⇒ 置 `skuMatchRan`（进第二步的关卡）。
        //: 弹窗内部每次改规则都会实时重算 `matchRows`，所以这里只需标记「跑过」。
        onRun={onSkuMatchRun}
        rows={matchRows}
        //: ⚠️ 用户口径 2026-10-14：「SKU 图底部的点击确认匹配结果**优化为应用匹配结果**，
        //: 点击应用在**商品 sku 表格 tab 下的列表中更新相关数据**，**第一列展示匹配到的图片预览图**」。
        //:
        //: ⇒ 这里**不再只关弹窗**：先把匹配结果写回 SKU 表（`onApplyMatch`），再关。
        //: 写回走父级的表格写入口（`setCell` 同一套 `emit`），保证列表真的更新，
        //: 而不是只在弹窗内部看着变了。
        //:
        //: ⚠️ **真 bug 修复（2026-10-14）**：用户报「sku 图已经自动匹配成功并应用了，
        //: 点击下一步依旧提示请先匹配 SKU 图」。根因 = `onSkuMatchRun`（置 `skuMatchRan`
        //: 的唯一入口）只挂在**「执行匹配」**按钮上，而用户的真实路径是
        //: **「应用匹配结果」** ⇒ 关卡判据永远 false。
        //: 「应用匹配结果」能点，前提就是**已经匹配出结果** ⇒ 它同样必须置真。
        onOk={() => {
          applySkuMatch(matchRows);
          onSkuMatchRun();
          setMatchOpen(false);
        }}
        onCancel={() => setMatchOpen(false)}
      />
    </>
  );
}

/* ------------------------------------------------------------------ *
 * 区域映射弹窗（用户口径 2026-10-13）
 *
 * > 「点击进入第二步时**弹窗提示指定**主图区域、详情图区域、商品视频、
 * >   映射到预检通过的媒体资源区域，选择后才可以进入第二步」
 * > 「均支持合并多个第一步中的图片区域」
 * > 「不拦截」（同一张图可同时进主图区和详情图区）
 * > 「映射弹窗中**不展示图**，使用多选下拉展示媒体区域名称即可」
 * ------------------------------------------------------------------ */

function ZoneMapModal(props: {
  open: boolean;
  /** 数据包里的媒体区域（保序）。 */
  zones: { name: string; kind: PrecheckKind }[];
  /** 各区域的媒体数（`name → count`），用于下拉项右侧显示。 */
  counts: Record<string, number>;
  /** 已选映射（受控）。 */
  value: ZoneMap;
  onChange: (next: ZoneMap) => void;
  onOk: () => void;
  onCancel: () => void;
}) {
  const { open, zones, counts, value, onChange, onOk, onCancel } = props;

  //: 每个区域在下拉里的选项（**只列区域名 + 数量，不展示图** —— 用户口径）。
  const options = zones.map((z) => ({
    value: z.name,
    label: `${z.name}（${counts[z.name] ?? 0}）`,
  }));

  const unused = unusedZones(zones, value);

  const slot = (key: PublishSlotKey) => (
    <div key={key} style={{ marginBottom: 14 }}>
      <Space size={6} style={{ marginBottom: 4 }}>
        <Text strong>{PUBLISH_SLOT_LABELS[key]}</Text>
        {PUBLISH_SLOT_REQUIRED[key] ? (
          <Text type="danger">*</Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            （可不选）
          </Text>
        )}
      </Space>
      <Select
        mode="multiple"
        allowClear
        //: ⚠️ 用户口径：「均支持**合并多个**第一步中的图片区域」⇒ 多选、
        //: 且模型值保序（antd `mode="multiple"` 的 value 数组即选择顺序）
        //: —— 顺序决定合并后的排列（主图顺序 = 封面顺序，不能乱）。
        value={value[key]}
        onChange={(next: string[]) => onChange({ ...value, [key]: next })}
        options={options}
        placeholder={
          PUBLISH_SLOT_REQUIRED[key] ? "请选择至少一个区域" : "不选则不发布视频"
        }
        style={{ width: "100%" }}
        //: 顺序由用户点选决定，禁掉搜索排序带来的意外（区域本来就不多）。
        showSearch={false}
      />
    </div>
  );

  return (
    <Modal
      open={open}
      centered
      title="指定媒体资源区域"
      width="50vw"
      okText="进入下一步"
      cancelText="取消"
      onOk={onOk}
      onCancel={onCancel}
      //: ⚠️ 用户口径「选择后才可以进入第二步」⇒ 不满足必填时确认键灰掉。
      okButtonProps={{ disabled: !isZoneMapReady(value) }}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="请为每个发布位指定来源区域"
        description="可多选合并，顺序即封面顺序；未指定的区域不会进入发布。"
      />
      {zones.length === 0 ? (
        <Empty description="数据包里没有媒体资源" />
      ) : (
        <>
          {(["main", "detail", "video"] as PublishSlotKey[]).map((k) => slot(k))}
          {/* ⚠️ 用户口径：漏配要能看出来 —— 列出**没被任何发布位使用**的区域。 */}
          {unused.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 4 }}
              message={`未被使用的区域：${unused
                .map((n) => `${n}（${counts[n] ?? 0}）`)
                .join("、")}`}
              description="这些区域不会进入发布；如需使用，请在上方发布位里选上它。"
            />
          )}
        </>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ *
 * 第三步：发布
 * ------------------------------------------------------------------ */

/** 只读预览的一个分区（与第二步的四分区同名同序）。 */
function PreviewSection(props: { title: string; children: React.ReactNode }) {
  return (
    <div className="publish-preview-section">
      <div className="publish-preview-title">{props.title}</div>
      <div className="publish-preview-body">{props.children}</div>
    </div>
  );
}

/** 只读预览的一组媒体（**只预览、不参与勾选**；顺序与第二步一致）。
 *  ⚠️ 用户口径（2026-10-10）：「媒体资源只预览顺序和第二步保持一致」。 */
function PreviewMedia(props: { label: string; items: ConfigMainImage[] }) {
  const { label, items } = props;
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);
  const previewRef = useRef<HTMLVideoElement | null>(null);
  const isVideo = (it: ConfigMainImage) =>
    /\.(mp4|mov|m4v|webm|avi)$/i.test(it.file_name || it.origin_name || "");
  const srcOf = (it: ConfigMainImage) => it.url || it.local_url;

  //: 关预览前显式暂停 + 清 src（视频卸载后音频仍播，用户口径 2026-10-09）。
  const closePreview = () => {
    const v = previewRef.current;
    if (v) {
      try {
        v.pause();
        v.currentTime = 0;
      } catch {
        /* 未加载时 pause 可能抛 */
      }
    }
    setPreviewIndex(null);
  };

  return (
    <div className="publish-preview-media">
      <span className="publish-preview-media-label">
        {label}：{items.length} 张
      </span>
      {items.length === 0 ? (
        <Text type="secondary">-</Text>
      ) : (
        <>
          {items.map((it, i) =>
            isVideo(it) ? (
              //: 视频缩略图首帧，点击开大图（可播放）。
              <video
                key={i}
                src={srcOf(it)}
                className="publish-preview-thumb"
                muted
                preload="metadata"
                onClick={() => setPreviewIndex(i)}
              />
            ) : (
              <img
                key={i}
                src={srcOf(it)}
                alt={`${label}${i + 1}`}
                className="publish-preview-thumb"
                onClick={() => setPreviewIndex(i)}
              />
            ),
          )}
          {/* 大图预览（只读；与第二步同款 Modal，可左右切换 + 视频可播放）。 */}
          <Modal
            open={previewIndex !== null}
            centered
            footer={null}
            width="70vw"
            onCancel={closePreview}
            styles={{ body: { textAlign: "center", padding: 8 } }}
          >
            {previewIndex !== null && (
              <>
                {isVideo(items[previewIndex]) ? (
                  <video
                    key={items[previewIndex].file_name || previewIndex}
                    ref={previewRef}
                    src={srcOf(items[previewIndex])}
                    controls
                    playsInline
                    preload="metadata"
                    style={{ maxWidth: "100%", maxHeight: "70vh", background: "#000" }}
                  />
                ) : (
                  <img
                    src={srcOf(items[previewIndex])}
                    alt={`${label}${previewIndex + 1}`}
                    style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain" }}
                  />
                )}
                <div style={{ marginTop: 8 }}>
                  <Button
                    size="small"
                    disabled={previewIndex <= 0}
                    onClick={() => setPreviewIndex((i) => Math.max(0, (i ?? 0) - 1))}
                  >
                    上一张
                  </Button>
                  <Text type="secondary" style={{ margin: "0 12px" }}>
                    {previewIndex + 1} / {items.length}
                  </Text>
                  <Button
                    size="small"
                    disabled={previewIndex >= items.length - 1}
                    onClick={() =>
                      setPreviewIndex((i) => Math.min(items.length - 1, (i ?? 0) + 1))
                    }
                  >
                    下一张
                  </Button>
                </div>
              </>
            )}
          </Modal>
        </>
      )}
    </div>
  );
}

function PublishStep(props: {
  shopName: string;
  title: string;
  categoryName: string;
  shortTitle: string;
  /** 第二步配置快照（完整参数，只读预览）。 */
  config: ConfigSnapshot | null;
  /** 商品主图（只读预览，顺序与第二步一致）。 */
  images: ConfigMainImage[];
  /** 商品视频（只读预览）。 */
  video: ConfigMainImage | null;
  /** 商品详情图（只读预览，顺序与第二步一致）。 */
  detailImages: ConfigMainImage[];
  /** **规格图原始对象**（`规格名 → 规格值 → ConfigMainImage`，与第二步同源，2026-10-10）。 */
  specImages: Record<string, Record<string, ConfigMainImage>>;
  priceRows: {
    spec?: string;
    price?: string | number | null;
    stock?: string | number | null;
    delivery_label?: string;
    ship_aging?: string;
  }[];
  skus: {
    _key?: string;
    name: string;
    price_yuan: number | null;
    stock: number | null;
    market_price_yuan?: number | null;
    image_path?: string | null;
    available?: boolean;
    barcode?: string;
    seller_code?: string;
    //: ⚠️ 发货时间文案（2026-10-15 补）：`按规格设置发货时间` 模式下每行各自一条。
    delivery_label?: string;
    ship_aging?: string;
    spec_image_url?: string | null;
  }[];
  result: ItemPublishResult | null;
}) {
  const {
    shopName, title, categoryName, shortTitle, config,
    images, video, detailImages, specImages, priceRows, skus,
    result,
  } = props;

  const invalid = skus.filter((s) => s.price_yuan === null || s.stock === null);

  //: ⚠️ 2026-10-10（用户口径「第二步/第三步统一用同一份数据源」）：第三步规格图
  //: **直接读 `specImages`**（第二步上抛的原对象），与第二步完全同源 —— 不再经
  //: `ConfigSnapshot.specImageUrls` 字符串副本（那是「第二步预览正常、第三步空」的根因）。
  //: 查表函数 `lookup_spec_image` 仍复用（唯一拆名逻辑），但查的是 `specImages` 原对象，
  //: 取 `ConfigMainImage.url || local_url`。
  const specImageAtRender = (skuName: string): ConfigMainImage | null =>
    lookup_spec_image(skuName, specImages);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {invalid.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${invalid.length} 条 SKU 缺价格或库存，发布可能被平台拒绝`}
        />
      )}
      {/* ⚠️ 用户口径（2026-10-10）：「错误信息应展示在**预览信息的顶部**」
          —— 失败原因公告上移到预览最前，用户点确认发布后不必往下翻才看到为什么失败。 */}
      {/* ⚠️ 用户口径（2026-10-10，**取代**「失败原因走弹窗」）：
          「发布失败原因**不使用弹窗**，使用第三步**顶部公告 error 类型**」
          —— 按 antd `Alert`（`type="error"` + `showIcon` + `closable`，可带 `description`）。 */}
      {result && (!result.published || (result.failed_images?.length ?? 0) > 0) && (
        <Alert
          type="error"
          showIcon
          closable
          message={
            result.published
              ? `${result.failed_images?.length ?? 0} 张图片上传失败`
              : `发布未通过（${result.fail_reasons?.length ?? 0} 条原因）`
          }
          description={
            <>
              {(result.fail_reasons?.length ?? 0) > 0 && (
                <Table
                  size="small"
                  rowKey={(row) => `${row.index}-${row.name}`}
                  pagination={false}
                  dataSource={result.fail_reasons ?? []}
                  columns={[
                    { title: "SKU", dataIndex: "name", ellipsis: true },
                    { title: "原因", dataIndex: "message" },
                  ]}
                />
              )}
              {(result.failed_images?.length ?? 0) > 0 && (
                <div className="publish-fail-images">
                  {(result.failed_images ?? []).map((f) => (
                    <div key={f.path}>
                      {f.path}：{f.reason}
                    </div>
                  ))}
                </div>
              )}
            </>
          }
        />
      )}
      {/* ⚠️ 用户口径 2026-10-08：移除「发布是真实上架操作」说明块与
          「我知道本次会真实发布到线上」确认勾选。
          红线本身**没变**（后端仍默认发布成功后立即下架），只是不再由前端
          摆这些文字与勾选。 */}
      {/* ⚠️ 用户口径（2026-10-10）：「第三步，发布确认并上架应预览第二步填写的
          **完整所有参数**；媒体资源只预览，顺序和第二步保持一致」。
          ⇒ 本页改为**只读全量预览**，区块顺序与第二步一一对应。 */}

      {/* ── 基础信息 ── */}
      <PreviewSection title="基础信息">
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="店铺">{shopName || "-"}</Descriptions.Item>
          <Descriptions.Item label="商品类目">{categoryName || "未选"}</Descriptions.Item>
          <Descriptions.Item label="商品标题">{title || "-"}</Descriptions.Item>
          <Descriptions.Item label="导购短标题">{shortTitle || "-"}</Descriptions.Item>
          <Descriptions.Item label="商品英文名" span={2}>
            {config?.englishName || "-"}
          </Descriptions.Item>
          {/* ⚠️ 用户口径（2026-10-10）：「第三步，**商品属性未展示预览**，
              选定的商品属性应展示预览」⇒ 展示第二步已填的属性（名 = 值）。
              属性是**按类目动态下发**的，不能写死名字 ⇒ 直接铺快照里的数组。 */}
          <Descriptions.Item label="商品属性" span={2}>
            {(config?.attributes ?? []).length === 0
              ? "-"
              : (config?.attributes ?? [])
                  .map((a) =>
                    a.attributeValueType === 2
                      ? `${a.attributeName}：${(a.attributeValueList ?? [])
                          .map((v) => v.attributeValue)
                          .join("、")}`
                      : `${a.attributeName}：${a.attributeValue}`,
                  )
                  .join("；")}
          </Descriptions.Item>
        </Descriptions>
      </PreviewSection>

      {/* ── 图文信息（主图 → 视频 → 详情图，顺序与第二步一致）── */}
      <PreviewSection title="图文信息">
        <PreviewMedia label="商品主图" items={images} />
        <PreviewMedia label="商品视频" items={video ? [video] : []} />
        <PreviewMedia label="商品详情图" items={detailImages} />
        <Descriptions size="small" bordered column={1} style={{ marginTop: 8 }}>
          <Descriptions.Item label="详情页描述">{config?.desc || "-"}</Descriptions.Item>
          <Descriptions.Item label="商详回看讲解">
            {config?.showPlayback ? "展示挂件" : "不展示"}
          </Descriptions.Item>
        </Descriptions>
      </PreviewSection>

      {/* ── 价格库存 ── */}
      <PreviewSection title="价格库存">
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="物流模板">
            {config?.logisticsPlanName || (config?.logisticsPlanId ? "已选" : "未选")}
          </Descriptions.Item>
          <Descriptions.Item label="发货模式">{config?.deliveryMode || "-"}</Descriptions.Item>
          {(config?.deliveryMode ?? "") === "现货模式" && (
            <Descriptions.Item label="现货发货时间">{config?.shipTime || "-"}</Descriptions.Item>
          )}
          {(config?.deliveryMode ?? "").includes("预售") && (
            <>
              <Descriptions.Item label="预售方式">{config?.presaleKind || "-"}</Descriptions.Item>
              <Descriptions.Item label="付款后发货">
                {config?.presaleDays === null || config?.presaleDays === undefined
                  ? "-"
                  : `${config.presaleDays} 天`}
              </Descriptions.Item>
            </>
          )}
          <Descriptions.Item label="商品规格" span={2}>
            {(config?.specTypes ?? []).length === 0
              ? "未启用"
              : (config?.specTypes ?? [])
                  .map((t) => {
                    const vals = (config?.specValues?.[t] ?? []).filter((v) => v.trim() !== "");
                    const imgs = config?.specImageCounts?.[t] ?? 0;
                    return `${t}（${vals.length} 个值${imgs ? ` · ${imgs} 张规格图` : ""}）`;
                  })
                  .join("；")}
          </Descriptions.Item>
          <Descriptions.Item label="SKU 行数" span={2}>
            {skus.length} 条
          </Descriptions.Item>
        </Descriptions>
        {/* **SKU 列表**（用户口径 2026-10-10：「SKU 行数下新增 sku 列表」，
            2026-10-10 追加：「高度调整为**最高 300px**，低于 300px 的**自适应**」）。
            ⇒ 行数少时容器自然收缩；超过 300px 才在**表格内部**滚动（`scroll.y` 在 CSS
            `max-height` 下不参与自适应，故去掉，改由容器 `.publish-preview-sku-list` 收口）。 */}
        <div className="publish-preview-sku-list">
          <Table
            size="small"
            rowKey={(row) => row._key ?? row.name}
            pagination={false}
            dataSource={skus}
            rowClassName={(row) => (row.available === false ? "price-row-disabled" : "")}
            columns={[
              {
                title: "规格图",
                key: "spec_image",
                width: 72,
                render: (_: unknown, row) => {
                  //: ⚠️ 2026-10-10：直接读 `specImages`（与第二步同源），取 `url || local_url`。
                  const img = specImageAtRender(row.name);
                  const url = img?.url || img?.local_url || row.spec_image_url;
                  return url ? (
                    //: 用户口径 2026-10-11：规格图小图**与主图/详情图小图一致**
                    //: （复用 `publish-preview-thumb`，56×56px，同一套样式）。
                    <img className="publish-preview-thumb" src={url} alt="规格图" />
                  ) : (
                    <Text type="secondary">-</Text>
                  );
                },
              },
              {
                title: "规格",
                dataIndex: "name",
                ellipsis: true,
                render: (name: string, row) => (
                  <Space size={6}>
                    <span>{name}</span>
                    {/*: 启用状态（用户口径 2026-10-10：第三步 SKU 列表要展示启用状态，与第二步表头一致）。 */}
                    <Text
                      type={row.available === false ? "secondary" : "success"}
                      style={{ fontSize: 12, whiteSpace: "nowrap" }}
                    >
                      {row.available === false ? "未启用" : "启用"}
                    </Text>
                  </Space>
                ),
              },
              { title: "售价", dataIndex: "price_yuan", width: 100 },
              { title: "库存", dataIndex: "stock", width: 90 },
              { title: "市场价", dataIndex: "market_price_yuan", width: 100 },
              { title: "条形码", dataIndex: "barcode", width: 120, ellipsis: true },
              { title: "商家编码", dataIndex: "seller_code", width: 120, ellipsis: true },
            ]}
          />
        </div>
        {/* ⚠️ 用户口径（2026-10-10）：「价格及库存**不能为列表**」⇒ 只给一行摘要，
            明细由用户在第二步的表里看（第三步只做确认前的总览）。 */}
        <div className="publish-preview-summary">
          价格及库存：已配置 {priceRows.length} 行；库存合计{" "}
          {priceRows.reduce((sum, r) => sum + (Number(r.stock ?? 0) || 0), 0)}
        </div>
      </PreviewSection>

      {/* ── 服务信息 ── */}
      <PreviewSection title="服务信息">
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="运费模板">
            {config?.freightTemplateName || (config?.freightTemplateId ? "已选" : "未选")}
          </Descriptions.Item>
          <Descriptions.Item label="开售时间">{config?.saleTime || "-"}</Descriptions.Item>
          {(config?.saleTime ?? "").includes("定时开售") && (
            <Descriptions.Item label="开售前价格保密">
              {config?.priceConfidential ? "保密" : "不保密"}
            </Descriptions.Item>
          )}
          <Descriptions.Item label="售后服务" span={2}>
            {(config?.afterSaleNames ?? []).length === 0
              ? "-"
              : (config?.afterSaleNames ?? []).join("、")}
          </Descriptions.Item>
        </Descriptions>
      </PreviewSection>

      {result?.published && (
        <Alert
          type={result.off_shelf ? "success" : "warning"}
          showIcon
          message={
            result.off_shelf
              ? "发布成功，已立即下架"
              : "发布成功，但下架失败 —— 请人工下架"
          }
        />
      )}

    </Space>
  );
}

/** 从 axios 错误里读后端给的结构化原因（与各页同一句读法）。 */
function readReason(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") {
    const text = (detail as { message?: unknown }).message;
    if (typeof text === "string" && text) return text;
  }
  const text = (error as { message?: unknown })?.message;
  return typeof text === "string" && text ? text : fallback;
}

/** 取后端错误 `detail.code`（如 `NO_DESKTOP`）；无则空串。 */
function readCode(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (detail && typeof detail === "object") {
    const code = (detail as { code?: unknown }).code;
    return typeof code === "string" ? code : "";
  }
  return "";
}