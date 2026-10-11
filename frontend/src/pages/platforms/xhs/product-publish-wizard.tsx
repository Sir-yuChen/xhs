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
 * ⚠️ 红线（用户口径，本文件**不负责**实现，只管不让用户误以为安全）：
 * 发布是真实写操作，成功即产生在售商品；后端默认「发布成功后立即下架」，
 * 前端**不得**传 `off_shelf_after: false`。
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
  Progress,
  Radio,
  Select,
  Space,
  Spin,
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
  LeftOutlined,
  LoadingOutlined,
  MinusOutlined,
  PlusOutlined,
  RightOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";


import {
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
} from "../../../lib/precheck-publish";
import { buildImageUrls, lookup_spec_image, md5_file_name, search_keyword } from "../../../lib/publish-config";
import PublishConfigStep1, { type ConfigMainImage } from "./publish-config-step1";
import PublishConfigStep2, {
  type ConfigSnapshot,
  type PriceRow,
  type PropertyField,
  type SpecAttribute,
} from "./publish-config-step2";
import type { CategoryPropertyItem, ItemPublishResult } from "../../../types";

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

/** 四个媒体区（顺序固定 = 用户口径的「加载模版」）。 */
const ZONES: { key: PrecheckGroup; label: string }[] = [
  { key: "main", label: "主图" },
  { key: "video", label: "商品视频" },
  { key: "sku", label: "SKU 图" },
  { key: "detail", label: "详情图" },
];

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
        setOtherProperties(
          (data.desc_properties ?? []).filter((p) => !p.key).map((p) => ({ ...p })),
        );
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
  }, [platformAccountId, configCategoryId]);

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
    }[]
  >([]);

  // ---- 第三步：发布 ----
  const [submitting, setSubmitting] = useState(false);
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
    if (open) reset();
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
    setTables(data.tables.map((t) => ({ ...t, rows: t.rows.map((r) => [...r]) })));
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

  //: 第二步「商品图片」的候选 = **预检通过且被选中**的主图。
  //:
  //: ⚠️ 两个条件都要（用户口径 2026-10-08：「应是预检通过且选中的媒体资源」）：
  //:   1. 预检通过（`ocr_ok === true` 且未命中违规词）；
  //:   2. 第一步勾选了它。
  //: 曾经的写法只按 `group === "main"` 过滤 ⇒ 用户取消勾选的「商品属性图」
  //: 照样被上传，勾选形同虚设。
  //:
  //: ⚠️ 用 `{rel_path, file_name}` 而不是绝对路径 —— 取图走既有的
  //: `token + rel_path` 端点（不把本地磁盘结构透给前端）。
  const mainImagePaths = useMemo(
    () =>
      selectedMedia
        .filter((m) => m.group === "main")
        .map((m) => ({ rel_path: m.rel_path, file_name: m.file_name })),
    [selectedMedia],
  );

  //: 详情图候选（自动加载第 4 步用）：预检通过且被选中的 `detail` 组。
  const detailImagePaths = useMemo(
    () =>
      selectedMedia
        .filter((m) => m.group === "detail")
        .map((m) => ({ rel_path: m.rel_path, file_name: m.file_name })),
    [selectedMedia],
  );

  //: 商品视频候选（自动加载第 5 步用）：预检通过且被选中的 `video` 组。
  const videoPaths = useMemo(
    () =>
      selectedMedia
        .filter((m) => m.group === "video")
        .map((m) => ({ rel_path: m.rel_path, file_name: m.file_name })),
    [selectedMedia],
  );

  //: SKU 图候选（规格图选择弹窗用）：预检通过且被选中的 `sku` 组。
  const skuImagePaths = useMemo(
    () =>
      selectedMedia
        .filter((m) => m.group === "sku")
        .map((m) => ({ rel_path: m.rel_path, file_name: m.file_name })),
    [selectedMedia],
  );

  const payload = useMemo(
    () =>
      buildPublishPayloadFromPrecheck(
        token && precheck ? { token, media: selectedMedia, tables } : null,
      ),
    [token, precheck, selectedMedia, tables],
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
          })),
      );
  //: ⚠️ 2026-10-10：依赖加 `specImages`（`specImageRefFor`/`specImageUrlFor` 现查 `specImages`
  //: 原对象而非 `configSnapshot` 副本），否则选规格图后 `skus` 不重映射、image_path 仍空。
  }, [priceRows, configSnapshot, specImages]);

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
      const uploaded: ConfigMainImage[] = [];
      for (const p of mainImagePaths) {
        //: 用户口径 2026-10-11：上传失败按弹窗里配的「是否重试 + 重试次数」重跑。
        const one = await withUploadRetry(() => uploadOneFromPackage(p.rel_path, p.file_name));
        if (one) uploaded.push(one);
      }
      if (uploaded.length === 0) {
        setStage(0, { status: "failed", detail: "主图全部上传失败，已停止后续阶段" });
        void message.warning("主图全部上传失败，已跳过后续步骤");
        return;
      }
      setConfigImages(uploaded);
      setStage(0, { status: "done", detail: `已上传 ${uploaded.length} 张主图` });

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
        const details: ConfigMainImage[] = [];
        for (const p of detailImagePaths) {
          const one = await withUploadRetry(() => uploadOneFromPackage(p.rel_path, p.file_name));
          if (one) details.push(one);
        }
        setConfigDetailImages(details);
        setStage(3, details.length > 0
          ? { status: "done", detail: `已上传 ${details.length} 张详情图` }
          : { status: "failed", detail: "详情图全部上传失败" });
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
          setOtherProperties(others);
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

  const submit = useCallback(async () => {
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
            //: 规格图已上传对象（2026-10-10 补）：后端直接用，跳过重新上传。
            spec_image_material: specMat
              ? {
                  materialId: specMat.materialId ?? "",
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
        //: ⚠️ 不传 off_shelf_after —— 保持后端默认 true（红线）。
      });
      setResult(data);
      onPublished?.(data);
      if (data.published) {
        //: 用户口径 2026-10-11：成功提示**放顶部**（message 默认顶部），
        //: 且发布成功后**关闭弹窗**（商品已下架，无需留在向导里）。
        void message.warning({
          content: data.off_shelf ? "发布成功，已立即下架" : "发布成功但下架失败，请人工下架",
          duration: 3,
        });
        if (data.off_shelf) {
          //: 成功后关闭弹窗（稍等让顶部提示可见，再关）。发布已下架，预检 token
          //: 无需再 discard（数据已用完），直接 onClose 关弹窗。
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
  }, [configCategoryId, configSnapshot, message, onClose, onPublished, payload, platformAccountId, skus, title]);

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
    setStep(1);
  }, [message, precheck]);

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
              //: ⚠️ **预检通过才放行**（用户口径 2026-10-08）：
              //: 没读过数据包、没跑过预检、或还有检测失败的条目都进不去。
              disabled={!precheckPassed || loading || picking}
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
              <Button
                onClick={() => void saveDraft()}
                disabled={submitting || draftSaving}
                loading={draftSaving}
              >
                保存草稿
              </Button>
              <Button type="primary" danger onClick={() => void submit()} loading={submitting}>
                确认发布
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

      {step === 0 && (
        <PrecheckStep
          packagePath={packagePath}
          onPick={() => void pickAndLoad()}
          picking={picking}
          loading={loading}
          onLoad={() => void loadWithPath(packagePath)}
          onPrecheck={() => void runPrecheck()}
          inspected={inspected}
          precheckPassed={precheckPassed}
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
        />
      )}

      {/* 用户口径（2026-10-10）：「来回步骤切换时，所有已填写的参数均应保持不清空，
          只有弹窗关闭或发布成功后清空」。
          ⚠️ 根因：条件渲染 `{step === 1 && <PublishConfigStep2/>}` 在切步时**卸载组件**
          ⇒ 内部 state（发货模式/规格/开售时间/售后/描述…）全丢。
          修法：**保持挂载、只隐藏**（`display: none`）—— state 天然保留；
          清空只发生在 `reset()`（关弹窗）与发布成功回写时。 */}
      <div style={{ display: step === 1 ? undefined : "none" }}>
      {(
        /* 用户口径 2026-10-09：「第二步参数配置中的商品图片、商品标题、商品类目
           合并到下面各基础信息中」—— 即千帆第一部分（图/标题/类目）不再单独成块，
           而是作为 `headSlot` 并入第二部分的「基础信息」区最前面。
           「自动加载参数」按钮逻辑不变（仍在第一部分组件里）。 */
        <PublishConfigStep2
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
          detailImages={configDetailImages}
          onDetailImagesChange={setConfigDetailImages}
          video={configVideo}
          onVideoChange={setConfigVideo}
          onUploadVideoFile={uploadVideoFile}
          onUploadImageFile={uploadImageFile}
          specAttributes={specAttributes}
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
                {s.status === "done" && <CheckCircleFilled style={{ color: "#52c41a" }} />}
                {s.status === "failed" && <CloseCircleFilled style={{ color: "#ff4d4f" }} />}
                {s.status === "skipped" && (
                  <span className="auto-stage-skip-dot" aria-label="已跳过" />
                )}
              </span>
              <span className="auto-stage-title">{s.title}</span>
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
        <div className="publish-wait-mask" role="status" aria-live="polite">
          <div className="publish-wait-card">
            <Spin size="large" />
            <div className="publish-wait-text">
              正在发布并立即下架… 已耗时 {Math.floor(submitElapsedSec)}s
            </div>
            <div className="publish-wait-sub">
              发布成功后会立即下架（红线）；请勿关闭本窗口或重复点击。
            </div>
          </div>
        </div>
      )}
      {/* **保存草稿蒙版 + 耗时**（用户口径 2026-10-10：保存应与发布一样加蒙版统计耗时）。 */}
      {draftSaving && (
        <div className="publish-wait-mask" role="status" aria-live="polite">
          <div className="publish-wait-card">
            <Spin size="large" />
            <div className="publish-wait-text">
              正在保存草稿… 已耗时 {Math.floor(draftElapsedSec)}s
            </div>
            <div className="publish-wait-sub">
              保存到草稿箱（未上架）；请勿关闭本窗口或重复点击。
            </div>
          </div>
        </div>
      )}
      {/* 等待遮罩：`position: fixed` 铺满视口 + flex 居中 ⇒
          文案**永远在窗口正中**，与弹窗内滚动位置无关
          （antd `Spin` 的 tip 跟着内容流滚，长内容时会滚出视野）。 */}
      {waitLabel !== null && (
        <div className="publish-wait-mask" role="status" aria-live="polite">
          <div className="publish-wait-card">
            <Spin size="large" />
            <div className="publish-wait-text">
              {/* **执行进度**（用户口径 2026-10-09）：有 done/total 就显示进度条 +
                  已处理/总数，让用户看见「在动、还有多少」；否则退回耗时秒数。 */}
              {progress && progress.total > 0
                ? `${progress.phase ?? waitLabel}…（${progress.done}/${progress.total}）`
                : `${waitLabel}… 已耗时 ${waitElapsedSec}s`}
            </div>
            {progress && progress.total > 0 && (
              <Progress percent={progress.percent} style={{ width: 280 }} />
            )}
          </div>
        </div>
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
  onLoad: () => void;
  onPrecheck: () => void;
  /** 是否已跑过违规词检测。 */
  inspected: boolean;
  /** 预检是否通过（决定能否进第二步）。 */
  precheckPassed: boolean;
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
}) {
  const {
    packagePath, onPick, picking, loading, onLoad,
    onPrecheck, inspected, precheckPassed, onPreview,
    precheckGroups, onOpenGroupPicker, groupOptions,
    token, precheck, checked, onCheckedChange, tables, onTablesChange,
  } = props;

  /** 属性图表在 `tables` 里的下标（**存在的商品属性图**才 >= 0）。
   *
   *  用户口径 2026-10-09：数据包里有商品属性图 ⇒ 识别成 KV ⇒ 出一个「商品属性」页签。
   *  判据是后端给该表填了 `kv`（属性图专属字段），不是按标题猜。 */
  const attrTableIndex = tables.findIndex((t) => Array.isArray(t.kv));

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

  return (
    <div>
      <Space.Compact style={{ width: "100%", marginBottom: 12 }}>
        {/* ⚠️ 用户口径 2026-10-08：「地址栏**不允许输入**，只能预览」——
            路径唯一来源是「选择文件夹」（后端弹系统选择器）。手输无效路径
            只会让人对着「目录不存在」反复试，不如直接不给输。
            用 `readOnly` 而非 `disabled`：内容仍可选中复制（预览要能看全）。 */}
        <Input
          prefix={<FolderOpenOutlined />}
          placeholder="请点击右侧「选择文件夹」"
          value={packagePath}
          readOnly
          //: readOnly 下仍允许聚焦选中，便于用户复制路径核对；但不接受修改。
          onKeyDown={(e) => e.preventDefault()}
          style={{ cursor: "default" }}
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
          {inspected ? "重新预检" : "预检"}
        </Button>
      </Space.Compact>

      {/* 已选分组的**摘要**：弹窗是临时视图，选完得能一眼看见当前生效的范围。 */}
      <div style={{ marginTop: 6, marginBottom: 0 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          违规词检测范围：
          {precheckGroups.length === 0
            ? "全部分组"
            : precheckGroups.map((g) => g || UNGROUPED_LABEL).join("、")}
        </Text>
      </div>

      {precheck && (
        <Alert
          style={{ marginBottom: 12 }}
          type={precheckPassed ? "success" : inspected ? "warning" : "info"}
          showIcon
          message={
            !inspected
              ? "已读取，尚未预检 —— 点击「预检」做违规词检测，通过后可进入下一步"
              : precheckPassed
                ? "预检通过，可以进入下一步配置商品参数"
                : "预检未通过：有文件未能完成检测，请查看原因后重新预检"
          }
        />
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

      {precheck && precheck.warnings.length > 0 && (
        <Alert
          type="warning"
          style={{ marginBottom: 12 }}
          message={precheck.warnings.map((w) => (
            <div key={w}>· {w}</div>
          ))}
        />
      )}

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
                  {ZONES.map((zone) => {
                    const items = precheck.media.filter((m) => m.group === zone.key);
                    if (items.length === 0) return null;
                    const keys = items.map((m) => `${m.group}/${m.rel_path}`);
                    //: ⚠️ 命中违规词的项**禁选**（用户口径 2026-10-08：「命中的不允许
                    //: 勾选至灰」）—— 三个批量按钮也必须认这份名单，否则「全选」就能绕过。
                    const locked = new Set(
                      items.filter((m) => m.has_forbidden).map((m) => `${m.group}/${m.rel_path}`),
                    );
                    return (
                      <div key={zone.key} style={{ marginBottom: 16 }}>
                        <Space style={{ marginBottom: 8 }}>
                          <Text strong>
                            {zone.label}（{items.filter((m) => checked[`${m.group}/${m.rel_path}`]).length}
                            /{items.length}）
                          </Text>
                          {locked.size > 0 && (
                            <Tag color="error">{locked.size} 个命中违规词，不可选</Tag>
                          )}
                          <Button
                            size="small"
                            onClick={() => setZone(keys, true, locked)}
                            //: 该区全被锁 ⇒ 全选无意义，置灰（避免用户点了没反应以为坏了）。
                            disabled={items.every((m) => m.has_forbidden)}
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
                                !items.every((m) => checked[`${m.group}/${m.rel_path}`]),
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
              //: 属性表（有 `kv`）**不算表格** —— 它有自己的「商品属性」页签，
              //: 两处都出现等于同一份数据展示两次（用户口径 2026-10-09）。
              //: ⚠️ 用户口径（2026-10-09）：页签名从「表格」改为**「商品sku表格」**
              //: （该页签装的就是 SKU / 规格表）。
              label: `商品sku表格（${tables.filter((t) => !Array.isArray(t.kv)).length}）`,
              children: (
                <TablesPanel
                  tables={tables.filter((t) => !Array.isArray(t.kv))}
                  //: ⚠️ 面板只显示 SKU 表（`tables` 的子集），改完必须**按真实下标**
                  //: 嵌回**完整数组**再落库 —— 直接上抛子集会把属性表（kv）丢掉，
                  //: 页签随之重建、面板状态被重置（用户实测：第二次改库存无响应）。
                  onTableChange={(panelIndex, next) =>
                    onTablesChange(mergePanelTable(tables, panelIndex, next))
                  }
                />
              ),
            },
            //: 用户口径 2026-10-09：数据包里**存在商品属性图** ⇒ 识别成 KV 后
            //: 新增一个「商品属性」页签展示（没有属性图就不出现该页签）。
            ...(attrTableIndex >= 0
              ? [
                  {
                    key: "attributes",
                    label: "商品属性",
                    children: (
                      <AttributeKvPanel
                        table={tables[attrTableIndex]}
                        onChange={(next) =>
                          onTablesChange(tables.map((t, i) => (i === attrTableIndex ? next : t)))
                        }
                      />
                    ),
                  },
                ]
              : []),
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
        const key = `${item.group}/${item.rel_path}`;
        const isVideo = item.group === "video";
        const isLocked = locked.has(key) || !!item.has_forbidden;
        return (
          <div
            key={key}
            style={{
              border: item.has_forbidden ? "2px solid #ff4d4f" : "1px solid #f0f0f0",
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
  const isVideo = item.group === "video";

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

/** 单张缩略图（鉴权取流 → blob URL）。 */
function PrecheckThumb(props: { token: string; item: PrecheckMediaItem }) {
  const { token, item } = props;
  const [src, setSrc] = useState<string | null>(null);
  const isVideo = item.group === "video";

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

/** 浏览器不支持预览时的说明用不上抽帧细节（用户口径 2026-10-09：页面不展示 OCR）。 */

const fillStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  height: "100%",
  color: "#999",
};

/* ------------------------------------------------------------------ *
 * 商品属性（属性图 OCR 出的 KV —— 用户口径 2026-10-09）
 * ------------------------------------------------------------------ */

/** 属性图的 **KV 展示**：一行「属性名 → 值」，值可编辑。
 *
 *  为什么不是表格：属性图是「参数名：参数值」的成对信息，4 列网格读起来
 *  要左右跳（列1→列2 是一对、列3→列4 是另一对）；KV 一行一对最直观。
 *  ⚠️ OCR 出的错字**原样展示**（不猜不纠正）—— 用户可自己改。
 */
function AttributeKvPanel(props: {
  table: PrecheckTable;
  onChange: (next: PrecheckTable) => void;
}) {
  const { table, onChange } = props;
  const kv = table.kv ?? [];

  const setValue = (index: number, value: string) => {
    onChange({ ...table, kv: kv.map((item, i) => (i === index ? { ...item, value } : item)) });
  };
  const setKey = (index: number, key: string) => {
    onChange({ ...table, kv: kv.map((item, i) => (i === index ? { ...item, key } : item)) });
  };

  if (kv.length === 0) {
    return <Empty description="没有从属性图里识别出属性（请自行核对原图）" />;
  }

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
        <Text strong>{table.title}</Text>
        <Tag>{kv.length} 项属性</Tag>
        <Tooltip title={table.source_file ?? ""}>
          <Text
            type="secondary"
            style={{
              fontSize: 12,
              maxWidth: 360,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            文件名称：{table.source_file || "-"}
          </Text>
        </Tooltip>
      </div>
      <div className="attr-kv-list">
        {kv.map((item, index) => (
          <div className="attr-kv-row" key={`${index}-${item.key}`}>
            <Input
              size="small"
              className="attr-kv-key"
              value={item.key}
              placeholder="属性名"
              onChange={(e) => setKey(index, e.target.value)}
            />
            <span className="attr-kv-colon">：</span>
            <Input
              size="small"
              className="attr-kv-value"
              value={item.value}
              placeholder="属性值"
              onChange={(e) => setValue(index, e.target.value)}
            />
          </div>
        ))}
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ *
 * 表格（列数不固定：有什么展示什么）
 * ------------------------------------------------------------------ */

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
  onApply: (column: number, rule: BatchEditRule, onlyRows?: readonly number[]) => void;
}) {
  const { table, pickedRows, onApply } = props;
  const { message } = App.useApp();
  //: 列**可任选**（用户口径 2026-10-09：表里除了「原价」「库存」还有「计算价格」
  //:  等更多列，不该写死两列）；默认落在能认出的价格 / 库存列上，认不出就用第一列。
  const priceCol = priceColumnIndex(table.columns);
  const stockCol = stockColumnIndex(table.columns);
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
function TablesPanel(props: {
  /** 本面板显示的 SKU 表（父级完整数组的**子集**）。 */
  tables: PrecheckTable[];
  /** 上抛**本面板下标**对应的**新表**；父级负责映射回完整数组并落库。 */
  onTableChange: (tIndex: number, next: PrecheckTable) => void;
}) {
  const { tables, onTableChange } = props;
  /** 每张表**勾选的行下标**（用户口径 2026-10-09：勾选后可只改选中的行）。
   *  ⚠️ 用表下标做外层键（同名的多张表要能分开记）。 */
  const [picked, setPicked] = useState<Record<number, number[]>>({});

  if (tables.length === 0) return <Empty description="数据包里没有可读取的表格" />;

  /** 把「本面板第 i 张表」映射到**父级完整数组**里对应的下标。 */
  const emit = (tIndex: number, next: PrecheckTable) => onTableChange(tIndex, next);

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

  return (
    <>
      {tables.map((table, tIndex) => {
        const pickedRows = picked[tIndex] ?? [];
        return (
        <div key={`${table.sheet_name}-${tIndex}`} style={{ marginBottom: 20 }}>
          {/* 用户口径 2026-10-08：表名 / 列×行 在左、**文件名在右上角**，同一行。 */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginBottom: 6,
            }}
          >
            <Space wrap={false}>
              <Text strong>{table.sheet_name}</Text>
              <Tag>
                {table.columns.length} 列 × {table.rows.length} 行
              </Tag>
              {pickedRows.length > 0 && (
                <Tag color="blue">已选 {pickedRows.length} 行</Tag>
              )}
            </Space>
            <Tooltip title={table.source_file ?? ""}>
              <Text
                type="secondary"
                style={{
                  fontSize: 12,
                  maxWidth: 360,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                文件名称：{table.source_file || "-"}
              </Text>
            </Tooltip>
          </div>

          {/* 价格 / 库存批量修改（用户口径 2026-10-09）——
              原参数基础上加减固定值 / 原参数 × 倍数 + 固定值 / 统一改成固定值。
              勾选行后只改选中行；未勾选则整列改。 */}
          <BatchColumnBar
            table={table}
            pickedRows={pickedRows}
            onApply={(col, rule, rows) => batchColumn(tIndex, col, rule, rows)}
          />

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
              onChange: (keys) =>
                setPicked((prev) => ({ ...prev, [tIndex]: keys as number[] })),
            }}
            columns={table.columns.map((name, colIndex) => ({
              title: name,
              dataIndex: ["row", colIndex],
              width: 160,
              render: (_: unknown, record: { __index: number; row: (string | null)[] }) => (
                <Input
                  size="small"
                  value={record.row[colIndex] ?? ""}
                  onChange={(e) => setCell(tIndex, record.__index, colIndex, e.target.value)}
                />
              ),
            }))}
          />
        </div>
        );
      })}
    </>
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
  priceRows: { spec?: string; price?: string | number | null; stock?: string | number | null }[];
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