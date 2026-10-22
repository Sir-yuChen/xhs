/**
 * 商品配置第二部分 —— 一比一复刻千帆发布页「商品主要参数配置」（设计 27）。
 *
 * 真机实测的四个分区（**合并为一个部分**，用户口径 2026-10-09）：
 *   1. 基础信息（4 项必填）—— 商品品牌 / 商品标题 / 导购短标题 / 商品属性
 *   2. 图文信息 —— 商品主图（含透明图）/ 主图视频 / 详情页图片 / 详情页描述 / 商详回看讲解
 *   3. 价格库存（1 项必填）—— 物流模板 / 发货模式 / 现货发货时间 / 商品规格 / 价格及库存表
 *   4. 服务信息 —— 物流类型 / 运费模板 / 开售时间 / 售后服务 / 商品英文名
 *
 * ## 布局（用户口径 2026-10-09）
 *
 * **左右两栏**：左「商品发布助手」（只读字段 + 错误原因），右「商品配置」四个分区。
 * 千帆后台的两部分参数（第一部分图/标题/类目在 `publish-config-step1`，
 * 本文件的参数部分）在**同一个第二步里合并为一部分**，不再内部再分步。
 *
 * ⚠️ 属性项、规格类型、尺码表都**按类目动态下发**，本组件按平台给的配置渲染，
 * 不写死任何属性名。
 * ⚠️ 商品标题 / 商品主图**由第一部分持有（唯一真相源）**，此处只读展示，不再放输入框/上传入口。
 * ⚠️ 缺值口径：取不到就留空，不造假（不填 0、不填「未知」）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Alert,
  App,
  AutoComplete,
  Button,
  Checkbox,
  DatePicker,
  Divider,
  Dropdown,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
//: ⚠️ 列类型单独 import（`ColumnsType<PriceRow>[number]` = antd 自己的列结构，
//: 手写等价类型会与 `onCell` 的 `GetComponentProps` 签名不兼容）。
import type { ColumnsType } from "antd/es/table";

import {
  DeleteOutlined,
  DownOutlined,
  ImportOutlined,
  InfoCircleOutlined,
  LeftOutlined,
  LoadingOutlined,
  MinusOutlined,
  PlusOutlined,
  RightOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import type { Dayjs } from "dayjs";

import { generateShortTitle, optimizeSkuNames, predictProperties } from "../../../lib/api";
import type { SkuNameOptimizeItem } from "../../../lib/api";
import { SKU_NAME_MAX_CHARS, displayWidth, exceedsLimit, isTooLong } from "../../../lib/sku-name-width";
import { SkuNameOptimizeModal } from "./sku-name-optimize-modal";
import type { AfterSaleRule } from "../../../lib/api";
import { buildImageUrls, lookupSpecFill, spec_combinations } from "../../../lib/publish-config";
import { withoutItemNo } from "../../../lib/publish-property-fields";
import { WaitMask } from "../../../components/ui/wait-mask";
import {
  applyBatchRule,
  buildSkuFillPlan,
  clampPriceMultiplier,
  DEFAULT_PRICE_MULTIPLIER,
  applyPriceMultiplier,
  defaultPriceMappingRows,
  DEFAULT_PRICE_MAPPING,
  DEFAULT_PRICE_FALLBACK_SOURCE,
  missingRequiredPriceTargets,
  isPriceMappingReady,
  PRICE_TARGET_LABELS,
  PRICE_TABLE_TARGETS,
  REQUIRED_PRICE_TARGETS,
  priceSourceOptions,
  type SkuFillPlan,
  validatePriceMapping,
  type PriceMappingRow,
  type PriceTargetKey,
  MAX_SPEC_MAPPING_ROWS,
  mappingFromRows,
  defaultSpecMappingRows,
  validateSpecMapping,
  isSpecMappingReady,
  type SpecMappingRow,
  PRICE_MULTIPLIER_MAX,
  PRICE_MULTIPLIER_MIN,
  type BatchEditMode,
  type BatchEditRule,
  type PassedSkuRow,
} from "../../../lib/precheck-publish";
import type { ConfigMainImage } from "./publish-config-step1";

import "./publish-config-step2.css";

const { Text } = Typography;

/** **规格图选择弹窗** —— 从预检通过且被选中的 SKU 图里选一张（用户口径 2026-10-09：
 *  「规格图前缀点击应支持选择第一步预检通过且选中的 sku 图，使用弹窗展示供用户选择，
 *   点击选中图确认后端上传该图并回显图」）。 */
function SkuPickModal(props: {
  open: boolean;
  items: { rel_path: string; file_name: string }[];
  /** 取图 URL（鉴权流）。 */
  fetchThumb: (rel_path: string) => Promise<string>;
  onPick: (rel_path: string, file_name: string) => void;
  onClose: () => void;
  /** ⚠️ 用户口径（2026-10-10）：「选择规格图**被选中的规格图不能重复被选中**」
   *  ⇒ 已被其它规格值用掉的图在这里**禁用**（`rel_path` 口径）。 */
  usedNames?: string[];
  /** 打开时预选中的那张（= 该规格值当前已挂的图；它自己也视为「已用」但可再次选中）。 */
  initialPicked?: string | null;
  /** **确认后的上传中态**（用户口径 2026-10-10：「选择规格图顶级确认加等待蒙版」）。
   *  ⇒ 上传期间给「页面蒙版 + 耗时提示」，避免用户重复点确认 / 以为没反应。 */
  busy?: boolean;
}) {
  const { open, items, fetchThumb, onPick, onClose } = props;
  //: ⚠️ 用户口径（2026-10-10）：「sku 图**只能单选一张**」⇒ 单选键（不是数组）。
  const [picked, setPicked] = useState<string | null>(null);
  /** 大图预览的下标（用户口径 2026-10-10：「支持大图预览」）。 */
  const [preview, setPreview] = useState<number | null>(null);
  /** 每张图的 URL 缓存（预览与缩略图共用，避免重复取流）。 */
  const [urls, setUrls] = useState<Record<string, string>>({});

  //: 已被其它规格值占用的图（不可重复选）。
  const usedSet = useMemo(() => new Set(props.usedNames ?? []), [props.usedNames]);

  useEffect(() => {
    if (open) {
      setPicked(props.initialPicked ?? null);
      setPreview(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    let alive = true;
    for (const it of items) {
      if (urls[it.rel_path]) continue;
      fetchThumb(it.rel_path)
        .then((u) => alive && setUrls((prev) => ({ ...prev, [it.rel_path]: u })))
        .catch(() => undefined);
    }
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, fetchThumb]);

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
        title="选择规格图"
        width="60vw"
        okText="确认"
        //: ⚠️ 上传中禁用二次点击（配合蒙版，双保险）。
        okButtonProps={{ disabled: picked === null || props.busy }}
        confirmLoading={props.busy}
        onOk={() => {
          const hit = items.find((it) => it.rel_path === picked);
          if (hit) onPick(hit.rel_path, hit.file_name);
        }}
      >
        {/* 等待蒙版（用户口径 2026-10-10：确认后要盖住，避免误触/重复提交）。 */}
        {/* 等待蒙版（用户口径 2026-10-10：确认后要盖住，避免误触/重复提交）。
            ⚠️ 2026-10-20 换统一 WaitMask：老写法 Spin + tip 的 tip 会跟内容滚走，
            且全站样式不一。外层 position: relative 供蒙版铺满本弹窗体。 */}
        <div style={{ position: "relative" }}>
          <WaitMask open={!!props.busy} mask scope="panel" title="正在上传规格图" />
        {/* 用户口径（2026-10-10）：规则文案放弹窗**顶部提示**。 */}
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={SPEC_IMAGE_RULE} />
        {items.length === 0 ? (
          <Text type="secondary">数据包里没有可选的 SKU 图（请在第 1 步预检里选中 SKU 图）</Text>
        ) : (
          <div className="sku-pick-grid">
            {items.map((it, i) => {
              //: 已被**其它**规格值占用 ⇒ 禁用（不能重复选中）。
              const used = usedSet.has(it.rel_path);
              return (
                <div
                  key={it.rel_path}
                  className={`sku-pick-card${picked === it.rel_path ? " picked" : ""}${
                    used ? " used" : ""
                  }`}
                  onClick={() => {
                    if (used) return; //: 已用图不可选（点它无效）
                    setPicked(it.rel_path);
                  }}
                  title={used ? "该规格图已被其它规格值使用" : it.file_name}
                >
                  <div className="sku-pick-card-img">
                    {urls[it.rel_path] ? (
                      <img src={urls[it.rel_path]} alt="SKU 图" />
                    ) : (
                      <span className="sku-pick-thumb-placeholder">加载中…</span>
                    )}
                  </div>
                  <div className="sku-pick-card-name">{it.file_name}</div>
                  {used ? (
                    <span className="sku-pick-card-used">已使用</span>
                  ) : (
                    <Button
                      size="small"
                      type="link"
                      className="sku-pick-card-zoom"
                      onClick={(e) => {
                        e.stopPropagation(); //: 放大不改变选中态
                        setPreview(i);
                      }}
                    >
                      预览
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        )}
        </div>
      </Modal>

      {/* 大图预览（可左右切换）。 */}
      <Modal
        open={preview !== null}
        centered
        onCancel={() => setPreview(null)}
        footer={null}
        width="70vw"
        styles={{ body: { textAlign: "center", padding: 8 } }}
      >
        {current && (
          <div className="media-preview-body">
            <img
              src={urls[current.rel_path]}
              alt={current.file_name}
              style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain" }}
            />
            <div className="media-preview-bar">
              <Button
                size="small"
                icon={<LeftOutlined />}
                disabled={(preview ?? 0) <= 0}
                onClick={() => step(-1)}
              >
                上一张
              </Button>
              <Text type="secondary">
                {(preview ?? 0) + 1} / {items.length}
              </Text>
              <Button
                size="small"
                icon={<RightOutlined />}
                disabled={(preview ?? 0) >= items.length - 1}
                onClick={() => step(1)}
              >
                下一张
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}

/** **价格及库存表的「批量填写」**（用户口径 2026-10-10：与千帆一致）。
 *
 * ⚠️ 真机实测（浏览器 DOM，设计 24 §13）：千帆是**弹窗**（640px），标题
 * 「批量填写」+ 副标题「只应用于单包商品」，**无模式选择**（无加减/倍数）——
 * 就是逐字段一个输入框，填固定值应用到所有行。取代 2026-10-09 的「行内条 + 三模式」。
 * 字段：售价 / 库存 / 市场价 / 条形码 / 商家编码（规格图批量不在此做，规格图逻辑不变）。 */
function PriceRowBatchBar(props: {
  /** 完整行集（批量改动写回全部行）。 */
  allRows: PriceRow[];
  onChange: (next: PriceRow[]) => void;
  /** 打开「多件装打包」弹窗。 */
  onOpenPackage: () => void;
}) {
  const { allRows, onChange, onOpenPackage } = props;
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<{
    price: number | null;
    stock: number | null;
    market_price: number | null;
    barcode: string;
    seller_code: string;
  }>({ price: null, stock: null, market_price: null, barcode: "", seller_code: "" });

  //: ⚠️ 用户口径（2026-10-11）：「批量修改列，**该列必须为数值类**才可以支持在
  //: 下拉中展示」⇒ 下拉**只列数值列**（售价 / 库存 / 市场价）。
  //: 原清单里的「条形码」（`maxLen: 28`）「商家编码」（`maxLen: 64`）是**文本列** ——
  //: 加减一个数 / 乘一个倍数对文本没有意义（它们只走 `mode === "fixed"` 的整列赋值）。
  //: ⚠️ 与另一个批量修改弹窗（预检价格表那个）**同一口径**，两处必须一致。
  //: 原两项保留在下方注释，标记口径变更（不静默删除）：
  //:     { key: "barcode", label: "条形码", numeric: false, maxLen: 28 },
  //:     { key: "seller_code", label: "商家编码", numeric: false, maxLen: 64 },
  const fields: { key: keyof typeof values; label: string; numeric: boolean; maxLen?: number }[] = [
    { key: "price", label: "售价", numeric: true },
    { key: "stock", label: "库存", numeric: true },
    { key: "market_price", label: "市场价", numeric: true },
  ];

  const apply = () => {
    //: 全部留空 = 无操作
    const anyFilled = fields.some((f) => {
      const v = values[f.key];
      return v !== null && v !== "";
    });
    if (!anyFilled) {
      void message.warning("请至少填写一个字段");
      return;
    }
    let count = 0;
    onChange(
      allRows.map((r) => {
        //: ⚠️ 用户口径（2026-10-10）：不启用的行**跳过**（批量填写不动它）。
        if (r.available === false) return r;
        let next = r;
        for (const f of fields) {
          const v = values[f.key];
          if (v === null || v === "") continue;
          if (f.numeric) {
            next = { ...next, [f.key]: v as number };
          } else {
            next = { ...next, [f.key]: v as string };
          }
        }
        if (next !== r) count += 1;
        return next;
      }),
    );
    void message.success(`已批量填写 ${count} 行`);
    setOpen(false);
    setValues({ price: null, stock: null, market_price: null, barcode: "", seller_code: "" });
  };

  return (
    <>
      <Dropdown
        menu={{
          items: [
            { key: "batch-fill", label: "批量填写" },
            { key: "package", label: "多件装打包" },
            { key: "multi-warehouse", label: "多仓区域库存" },
          ],
          onClick: ({ key }) => {
            if (key === "batch-fill") setOpen(true);
            else if (key === "package") onOpenPackage();
            //: 多仓区域库存：暂未实现（占位，不弹窗）。
          },
        }}
      >
        <Button size="small">
          更多操作 <DownOutlined />
        </Button>
      </Dropdown>
      <Modal centered
        open={open}
        onCancel={() => setOpen(false)}
        onOk={apply}
        okText="确定"
        cancelText="取消"
        title={
          <Space size={8}>
            批量填写
            <Text type="secondary" style={{ fontSize: 12, fontWeight: "normal" }}>
              只应用于单包商品
            </Text>
          </Space>
        }
        width={640}
      >
        <div style={{ display: "grid", gridTemplateColumns: "84px 1fr", gap: "12px 16px", width: "100%" }}>
          {fields.map((f) => (
            <div key={f.key} style={{ display: "contents" }}>
              <span style={{ fontWeight: 600, alignSelf: "center" }}>{f.label}</span>
              {f.numeric ? (
                <InputNumber
                  style={{ width: "100%" }}
                  placeholder={f.key === "market_price" ? "不小于售价" : "请输入"}
                  min={0}
                  value={values[f.key] as number | null}
                  onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
                />
              ) : (
                <Input
                  maxLength={f.maxLen}
                  placeholder="请输入"
                  value={values[f.key] as string}
                  onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>
      </Modal>
    </>
  );
}

/** **批量修改列值**弹窗（用户口径 2026-10-10：与预检 SKU 表批量修改**逻辑一致**——
 *  三模式（加减/倍数+固定值/统一改固定值）+ 选列 + 符号图标切换；只是用**弹窗**展示，
 *  布局重新优化。与「批量填写」（纯固定值弹窗）**并存**，是两个独立入口。 */
function PriceRowBatchEditModal(props: {
  open: boolean;
  allRows: PriceRow[];
  onChange: (next: PriceRow[]) => void;
  onClose: () => void;
}) {
  const { open, allRows, onChange, onClose } = props;
  const { message } = App.useApp();
  //: ⚠️ 用户口径（2026-10-11）：「批量修改列，**该列必须为数值类**才可以支持在
  //: 下拉中展示」⇒ 下拉**只列数值列**（售价 / 库存 / 市场价）。
  //: 原清单里的「条形码」「商家编码」是**文本列**（`numeric: false`）—— 它们本就走
  //: `mode === "fixed"` 的整列赋值分支，一条「加减/倍数」规则对文本没有意义
  //: （加减一个数、乘一个倍数都不是合法操作）⇒ 不该出现在这个下拉里。
  //: ⚠️ 原两项**保留在下方注释**，标记口径变更（不静默删除）：
  //:     { key: "barcode", label: "条形码", numeric: false },
  //:     { key: "seller_code", label: "商家编码", numeric: false },
  const fields: { key: keyof PriceRow; label: string; numeric: boolean }[] = [
    { key: "price", label: "售价", numeric: true },
    { key: "stock", label: "库存", numeric: true },
    { key: "market_price", label: "市场价", numeric: true },
  ];
  const [field, setField] = useState<keyof PriceRow>("price");
  const [mode, setMode] = useState<BatchEditMode>("delta");
  const [sign, setSign] = useState<"+" | "-">("+");
  const [operand, setOperand] = useState<number | null>(null);
  const [factor, setFactor] = useState<number | null>(1);

  const target = fields.find((f) => f.key === field) ?? fields[0];

  const apply = () => {
    if (operand === null) {
      void message.warning("请填写数值");
      return;
    }
    const raw = sign === "-" ? -Math.abs(operand) : operand;
    const rule: BatchEditRule = { mode, operand: raw, factor: factor ?? 1, min: 0 };
    let count = 0;
    onChange(
      allRows.map((r) => {
        //: ⚠️ 用户口径（2026-10-10）：不启用的行**跳过**（批量修改列值不动它）。
        if (r.available === false) return r;
        const cur = (r as unknown as Record<string, unknown>)[field as string];
        if (!target.numeric) {
          if (mode !== "fixed") return r;
          count += 1;
          return { ...r, [field]: String(operand) };
        }
        const n = cur === null || cur === undefined || cur === "" ? null : Number(cur);
        const next = applyBatchRule(Number.isFinite(n as number) ? (n as number) : null, rule);
        if (next === null) return r;
        count += 1;
        return { ...r, [field]: next };
      }),
    );
    if (count === 0) {
      void message.warning("没有可修改的行");
      return;
    }
    if (mode === "fixed") void message.success(`「${target.label}」整列改成 ${operand}（${count} 行）`);
    else if (mode === "scale") void message.success(`「${target.label}」× ${factor ?? 1} ${sign} ${Math.abs(operand)}（${count} 行）`);
    else void message.success(`「${target.label}」每行 ${sign}${Math.abs(operand)}（${count} 行）`);
    //: ⚠️ 用户口径（2026-10-10）：点确定**不关弹窗**，只有用户点关闭（取消/X）才关。
  };

  return (
    <Modal centered
      open={open}
      onCancel={onClose}
      onOk={apply}
      okText="确定"
      cancelText="取消"
      title="批量修改列值"
      width={520}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 56, fontWeight: 600 }}>列</span>
          <Select
            style={{ minWidth: 130 }}
            value={field as string}
            onChange={(v) => setField(v as keyof PriceRow)}
            options={fields.map((f) => ({ label: f.label, value: f.key as string }))}
          />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 56, fontWeight: 600 }}>模式</span>
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value as BatchEditMode)}
            options={[
              { label: "加减固定值", value: "delta" },
              { label: "倍数+固定值", value: "scale" },
              { label: "统一改固定值", value: "fixed" },
            ]}
          />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 56, fontWeight: 600 }}>参数</span>
          {mode === "scale" && (
            <InputNumber
              style={{ width: 80 }}
              placeholder="倍数"
              value={factor}
              onChange={setFactor}
            />
          )}
          {(mode === "delta" || mode === "scale") && (
            <Tooltip title={sign === "+" ? "当前：加（点击切为减）" : "当前：减（点击切为加）"}>
              <Button
                icon={sign === "+" ? <PlusOutlined /> : <MinusOutlined />}
                onClick={() => setSign(sign === "+" ? "-" : "+")}
              >
                {sign === "+" ? "加" : "减"}
              </Button>
            </Tooltip>
          )}
          <InputNumber
            style={{ width: 110 }}
            placeholder={mode === "scale" ? "固定值" : "数值"}
            value={operand}
            onChange={setOperand}
          />
        </div>
        {!target.numeric && mode !== "fixed" && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            文本列（条形码/商家编码）仅支持「统一改固定值」
          </Text>
        )}
      </div>
    </Modal>
  );
}

/** 一个**打包规格**（多件装打包：N 件装 + 打包价）。 */
export type PackageBundle = {
  /** 打包数量（最少 2 件）。 */
  count: number;
  /** 打包价格（元）。 */
  price: number | null;
};

/** **多件装打包**弹窗（用户口径 2026-10-10，真机 DOM 设计 24 §14）：
 *  每个打包规格一行 = `数量`（min=2 默认 2）+ `价格`（请输入价格），
 *  「新增打包规格」追加行；取消/确定。 */
function PackageBundleModal(props: {
  open: boolean;
  bundles: PackageBundle[];
  onChange: (next: PackageBundle[]) => void;
  onClose: () => void;
}) {
  const { open, bundles, onChange, onClose } = props;
  const { message } = App.useApp();
  const [draft, setDraft] = useState<PackageBundle[]>(
    bundles.length ? bundles.map((b) => ({ ...b })) : [{ count: 2, price: null }],
  );

  //: 每次打开时同步外部值（props.bundles 变了 / 重开）
  useEffect(() => {
    if (open) {
      setDraft(bundles.length ? bundles.map((b) => ({ ...b })) : [{ count: 2, price: null }]);
    }
  }, [open, bundles]);

  const ok = () => {
    const valid = draft.filter((b) => b.count >= 2 && b.price !== null && b.price >= 0);
    if (valid.length === 0) {
      void message.warning("请至少配置一个数量≥2 且含价格的打包规格");
      return;
    }
    onChange(valid);
    onClose();
  };

  const addRow = () => setDraft((prev) => [...prev, { count: 2, price: null }]);

  return (
    <Modal centered
      open={open}
      onCancel={onClose}
      onOk={ok}
      okText="确定"
      cancelText="取消"
      title="多件装打包"
      width={640}
    >
      <Text style={{ display: "block", marginBottom: 16, fontWeight: 600 }}>
        选择要打包的数量
      </Text>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {draft.map((b, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <InputNumber
              style={{ width: 120 }}
              min={2}
              value={b.count}
              onChange={(v) =>
                setDraft((prev) => prev.map((x, xi) => (xi === i ? { ...x, count: v ?? 2 } : x)))
              }
            />
            <InputNumber
              style={{ width: 240 }}
              min={0}
              placeholder="请输入价格"
              value={b.price}
              onChange={(v) =>
                setDraft((prev) => prev.map((x, xi) => (xi === i ? { ...x, price: v } : x)))
              }
            />
            {draft.length > 1 && (
              <Button
                size="small"
                type="link"
                danger
                onClick={() => setDraft((prev) => prev.filter((_, xi) => xi !== i))}
              >
                删除
              </Button>
            )}
          </div>
        ))}
        <Button size="small" icon={<PlusOutlined />} onClick={addRow}>
          新增打包规格
        </Button>
      </div>
    </Modal>
  );
}

/** 图 / 视频缩略图组：**点击开大图预览（可左右切换）** + **悬停显删除**。
 *
 * ⚠️ 用户口径（2026-10-09）：
 *   - 「商品视频支持大图预览，删除按钮应与图片类似鼠标滑到视频区域内右上角展示删除图标」；
 *   - 「商品主图详情图均支持大图预览，左右切换」。
 * 故三种媒体（主图 / 视频 / 详情图）共用这一份实现（删图标不常驻、悬停才显）。
 */
/** **上传槽**（虚线框 + 加号 + 真·`<input type=file>`）—— 与「智能识图」区同一套上传组件。

 *  ⚠️ 用户口径（2026-10-10）：「请先在上方『智能识图』区上传商品主图 移除，
 *  应使用**图片上传组件**；详情图和视频均使用**上传组件**不要使用按钮」。
 *  ⇒ 商品主图 / 商品视频 / 商品详情图三处一律用**上传框**，不再用 Button。
 */
function UploadSlot(props: {
  /** 提示文案（Tooltip）。 */
  title: string;
  /** 是否允许多选（详情图可多选）。 */
  multiple?: boolean;
  /** `accept`（视频与图片不同）。 */
  accept: string;
  busy?: boolean;
  /** 选中的文件（已按 `multiple` 收好）。 */
  onFiles: (files: File[]) => void;
}) {
  const { title, multiple, accept, busy, onFiles } = props;
  return (
    <div className="unified-upload-slot">
      <Tooltip title={title}>
        <div className="unified-upload-inner">
          {busy ? <LoadingOutlined /> : <PlusOutlined />}
          <span>上传</span>
        </div>
      </Tooltip>
      {/* ⚠️ 真机 `<input type=file>`：点框即选文件。用隐藏 input 而不是 Button ——
          千帆就是点框选文件（`<select>`/Button 在语义与观感上都不是「上传」）。 */}
      <input
        className="unified-upload-picker"
        type="file"
        multiple={multiple}
        aria-label={title}
        accept={accept}
        onClick={(e) => {
          //: 每次点都重开选择器（同一文件重选也要有反应）。
          (e.target as HTMLInputElement).value = "";
        }}
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          if (files.length > 0) onFiles(multiple ? files : files.slice(0, 1));
        }}
      />
    </div>
  );
}

function MediaThumbs(props: {
  items: ConfigMainImage[];
  /** 是否可删除（主图只读展示时为 false）。 */
  removable?: boolean;
  onRemove?: (index: number) => void;
  /** **尾随插槽**（用户口径 2026-10-10）：渲染在缩略图流**末尾**，作为流里的一项
   *  （如上传框）—— 让上传框紧跟最后一张缩略图，不单独占一行。 */
  trailing?: React.ReactNode;
}) {
  const { items, removable = false, onRemove, trailing } = props;
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);

  const isVideo = (item: ConfigMainImage) =>
    /\.(mp4|mov|m4v|webm|avi)$/i.test(item.file_name || item.origin_name || "");

  const srcOf = (item: ConfigMainImage) => item.url || item.local_url;

  const current = previewIndex === null ? null : items[previewIndex];
  const step = (delta: number) => {
    if (previewIndex === null) return;
    const next = previewIndex + delta;
    if (next < 0 || next >= items.length) return;
    setPreviewIndex(next);
  };

  /** 关预览（用户口径 2026-10-09：「视频预览关闭弹窗后依旧在播放」）。
   *  ⚠️ 必须**显式暂停 + 清 src**：`<video>` 卸载/隐藏后音频仍在播。
   *  故关闭前先取到该元素、`pause()` 再清空，避免残留播放。 */
  const closePreview = () => {
    const v = previewRef.current;
    if (v) {
      try {
        v.pause();
        v.currentTime = 0;
      } catch {
        /* 忽略：未加载时 pause 可能抛 */
      }
    }
    setPreviewIndex(null);
  };
  const previewRef = useRef<HTMLVideoElement | null>(null);

  return (
    <>
      <Space wrap>
        {items.map((item, i) => (
          <span key={`${item.file_name}-${i}`} className="media-thumb-wrap">
            {isVideo(item) ? (
              //: 视频缩略图出**首帧**（比一块灰底写「视频」有用），点击开大图预览。
              <video src={srcOf(item)} className="media-thumb" muted preload="metadata" />
            ) : (
              <img src={srcOf(item)} alt={item.origin_name} className="media-thumb" />
            )}
            {/* 覆盖整块的点击层（点缩略图即开预览）。 */}
            <button
              type="button"
              className="media-thumb-open"
              aria-label={`预览${i + 1}`}
              onClick={() => setPreviewIndex(i)}
            />
            {removable && (
              <button
                type="button"
                className="media-thumb-remove"
                aria-label={`删除第${i + 1}个`}
                onClick={() => onRemove?.(i)}
              >
                <DeleteOutlined />
              </button>
            )}
          </span>
        ))}
        {trailing}
      </Space>

      {/* 大图预览（可左右切换）—— 主图 / 视频 / 详情图共用。
          ⚠️ 用户口径 2026-10-09：预览时**必须点播放才开始**（不自动播放）；
             关闭弹窗要**停止播放**（否则关了还在响）。 */}
      <Modal
        open={previewIndex !== null}
        centered
        onCancel={closePreview}
        footer={null}
        width="70vw"
        styles={{ body: { textAlign: "center", padding: 8 } }}
      >
        {current && (
          <div className="media-preview-body">
            {isVideo(current) ? (
              <video
                key={current.file_name || previewIndex}
                ref={previewRef}
                src={srcOf(current)}
                controls
                playsInline
                preload="metadata"
                style={{ maxWidth: "100%", maxHeight: "70vh", background: "#000" }}
              />
            ) : (
              <img
                src={srcOf(current)}
                alt={current.origin_name}
                style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain" }}
              />
            )}
            <div className="media-preview-bar">
              <Button
                size="small"
                icon={<LeftOutlined />}
                disabled={(previewIndex ?? 0) <= 0}
                onClick={() => step(-1)}
              >
                上一张
              </Button>
              <Text type="secondary">
                {(previewIndex ?? 0) + 1} / {items.length}
              </Text>
              <Button
                size="small"
                icon={<RightOutlined />}
                disabled={(previewIndex ?? 0) >= items.length - 1}
                onClick={() => step(1)}
              >
                下一张
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}

/** 导购短标题上限（真机 `18/24`）。 */
const SHORT_TITLE_MAX = 24;
/** 商品标题上限（真机 `56/60`）。 */
const TITLE_MAX = 60;
/** 详情页描述上限（真机 `0/500`）。 */
const DESC_MAX = 500;

/** 单个规格值的**数量上限**（真机文案：单个规格值的上限数量为 500 个）。 */
export const SPEC_VALUE_MAX = 500;

/** 单个规格值的**长度上限**（真机文案：规格值长度上限为 60 个字符）。 */
export const SPEC_VALUE_LEN_MAX = 60;

/** 真机「现货发货时间」三选一（推荐项在最后）。 */
export const SPOT_SHIP_TIME_OPTIONS = ["当日发货", "24小时", "48小时（推荐）"] as const;

/** 真机「全款预售发货时间」二选一。 */
export const PRESALE_KINDS = ["时段预售", "定时预售"] as const;

/* 规格值输入框宽度常量**已删除**（2026-10-11）：
   原口径「固定 200px」被用户实测推翻 ——「规格文本超出输入框外展示」的根因就是
   写死宽度（`flex:0 0 200px` 不长不缩，30+ 字的规格值必被裁）。
   现行 = CSS `width:auto` + `min-width` 保底 + `field-sizing: content` 按内容自适应，
   无需 JS 端常量。留此说明避免以后有人又加回一个固定宽度。 */

/** 现货+预售模式的**发货时效**候选（真机实测）。 */
export const SHIP_AGING_OPTIONS = [
  "3天",
  "5天",
  "7天",
  "15天",
  "20天",
  "25天",
  "30天",
  "45天",
] as const;

/** **发货模式说明**（真机实测：点击发货模式后页面新增这段说明）。
 *
 * ⚠️ 用户口径（2026-10-09）：「说明文案**拆分开**再对应模式」⇒ 从「6 行整块」
 * 改为**按发货模式键控**的映射，每个模式只挂自己那条，配说明图标（Tooltip）展示。
 */
export const DELIVERY_MODE_DOC: Record<string, string> = {
  现货模式: "可在不同的现货发货时间选项内进行选择。商家需要在设置的规定时间内完成发货，否则可能面临投诉或处罚。",
  全款预售模式:
    "消费者在规定时效内支付订单全款后，商家在约定时间内完成发货。如备货能力不足，建议使用全款预售发货模式，避免发货超时被处罚。",
  "现货+预售模式":
    "可在商品单个规格基础上配置不同发货时效；商品信息更新成功后，对应规格产生的订单需按照买家支付后的配置发货时效开始计算。",
  按规格设置发货时间:
    "可对商品单个规格（sku）维度进行预售，可设置规格指定时间发货。商家需要在设置的规定时间内完成发货，否则可能面临投诉或处罚。",
};

/** 发货模式说明的**总览**（真机原文第 1 条：四种模式的定义）。 */
export const DELIVERY_MODE_OVERVIEW =
  "发货模式目前分为现货模式、全款预售模式、现货+预售模式、按规格设置发货时间模式";

/** 真机 bundle 的**分区锚点全表**（6 个，逐字对过）。 */
export const SECTION_ANCHORS = [
  "类目信息",
  "基础信息",
  "图文信息",
  "尺码信息",
  "价格库存",
  "商品资质",
  "服务信息",
] as const;

/** 真机 bundle 原文：尺码表可选类型（组合展示用）。 */
export const SIZE_TABLE_KINDS = ["商品尺寸", "新尺码推荐", "基础尺码", "尺码推荐", "模特试穿"] as const;

/** 真机 bundle 原文：尺码表的两种配置方式（`sizeTableConfigType`）。 */
export const SIZE_CONFIG_TYPES = { form: "表格填写", image: "上传图片" } as const;

/** 真机 bundle 原文：三类尺码表的 state 骨架（逐字对过字段名）。 */
export const SIZE_TABLE_SHAPES = {
  baseSizeTable: {
    label: "基础尺码",
    fields: ["argList", "customArgList", "selectedArgIds", "sizeData"],
  },
  recommendSizeTable: {
    label: "尺码推荐",
    fields: ["argList", "customArgList", "selectedArgIds", "normalSizeData", "complexSizeData"],
  },
  modelSizeTable: {
    label: "模特试穿",
    fields: ["argList", "customArgList", "selectedArgIds", "sizeData"],
  },
} as const;

/** 真机 bundle 原文：尺码表建议文案。 */
export const SIZE_TABLE_SUGGEST = "经数据验证补充尺码表可有效提升商品转化，建议补充准确的尺码信息";

/** 真机 bundle 原文：商品规格说明。 */
export const SPEC_RULE =
  "根据类目不同，一个商品最多选择2个或3个规格类型，单个规格值的上限数量为500个；规格值长度上限为60个字符，名称不可重复";

/** 真机 bundle 原文：添加规格图规则。 */
export const SPEC_IMAGE_RULE = "图片大小应小于3MB；比例应为1:1或3:4；格式应为jpg、png、jpeg";

/** 物流类型候选（用户口径 2026-10-09：「物流类型应和类目有直接关系」）。
 *
 * ⚠️ **文案与数字值统一在 `lib/qianfan-enums.ts`**（与后端枚举一一对应）——
 * 本文件只做 re-export，避免同一份字面量在两处各写一遍后漂移
 * （`deliveryMode` 曾被前端当发货模式、后端当物流类型 ⇒ 平台报
 * 「商品不支持设置无物流发货」）。
 *
 * 真机 bundle（`8091.*.js`）`logisticsTypeOptions`：默认只给 **「物流配送」**；
 * 仅当该类目支持「无物流发货」时才追加 **「无物流发货或物流配送」**
 * （tip 文本见 `NOT_LOGISTICS_HINT`）。 */
import { LOGISTICS_TYPE_LABELS, NOT_LOGISTICS_HINT } from "../../../lib/qianfan-enums";

//: 转发给本文件的既有调用方（`import ... from` + `export` 才能在文件作用域里同时可用）。
export { LOGISTICS_TYPE_LABELS, NOT_LOGISTICS_HINT };

/** 真机 bundle 原文：价格表内置校验（逐列，逐字对过）。 */
export const PRICE_ROW_RULES = {
  priceEmpty: "请输入售价",
  pricePkg: "打包商品的单个售价不能高于该商品单独售卖价格",
  originalEmpty: "请输入市场价",
  stockEmpty: "请输入库存变更值",
  barcodeFormat: "条形码只支持字母大小写、数字、-、_/和*",
  barcodeEmpty: "请输入条形码",
  couponMin: "不能小于0.1元",
  couponDecimal: "小数点位数不能超过1位",
  couponLessThanOriginal: "注意，会员价需要小于原价，请重新调整价格",
  comboPriceEmpty: "请输入组合品售价",
} as const;

/** 真机 bundle：**只有第一组规格类型支持添加规格图**。 */

/** 真机 bundle 原文：详情页描述规则。 */
export const DESC_RULE = "最大长度为500个字，用以辅助描述商品售卖情况";

/** 真机 bundle 原文：详情页图片规则。 */
export const DESC_IMAGE_RULE =
  "最多100张；像素5000*15000px以内；总高度不超过50000px；仅支持jpg/jpeg/png格式；大小5M以内；建议上传至少3张与主图不同的详情页图片";

/** 真机 bundle 原文：开售时间选项（**新建 vs 编辑模式不同**）。 */
export const SHELF_TIME_OPTIONS_CREATE = [
  "审核通过后立即上架开售",
  "不设置，手工上架开售",
  "定时开售",
] as const;

export const SHELF_TIME_OPTIONS_EDIT = ["不设置定时开售", "定时开售"] as const;

/** 真机 bundle 的**字段校验文案**（字段名 → 规则，逐字对过）。 */
export const FIELD_VALIDATION = {
  deliveryMode: "请选择发货模式",
  existingValue: "请选择现货发货时间",
  relativeValue: "请输入发货时间",
  fixedValue: "请选择发货时间",
} as const;

/** 真机 bundle：物流模板未选时，发货时间选项的禁用提示。 */
export const SHIP_DISABLED_HINT = "请先选择物流模板";

/** 发货模式（真机四选一）。 */
export const DELIVERY_MODES = [
  "现货模式",
  "全款预售模式",
  "现货+预售模式",
  "按规格设置发货时间",
] as const;

/** 物流模板（千帆 `getSellerCarriageTemplate`）。 */
export type ShippingTemplate = {
  template_id: string;
  name: string;
};

/** 物流计划（发货仓）候选（千帆 `publish_render` 空白表单下发，与运费模板**两个数据源**）。 */
export type LogisticsTemplate = {
  plan_id: string;
  name: string;
  logistics_mode: string;
};

/** 一个属性项（平台按类目下发）。 */
export type PropertyField = {
  /** 属性 id（提交时用）。 */
  id: string;
  /** 属性名（如「材质」「风格」）。 */
  name: string;
  /** 输入形态：下拉 / 多选 / 文本 / 布尔。 */
  kind: "select" | "multi" | "text" | "bool";
  /** 候选项（select / multi 用）。 */
  options?: string[];
  /** 候选项的**平台值 id**（真机 `attributes[].attributeValueId` 要用；
   *  来自 `getCategoryProperty.descProperties[].values[].value_id`）。 */
  values?: { name: string; value_id: string }[];
  /** 是否关键属性（真机的「关键属性 3/5」）。 */
  key?: boolean;
  required?: boolean;
  /** 是否允许自定义输入新值（平台 `customizable`；false = 只能从候选项选）。 */
  customizable?: boolean;
  placeholder?: string;
  maxLength?: number;
};

/** 一个**规格维度**（平台 `saleProperties[]`，按类目下发）。 */
export type SpecAttribute = {
  /** 规格名（如「颜色分类」「尺寸」）。 */
  name: string;
  /** 平台规格维度 id（提交时 `variants[].attributeId` 用）。 */
  id: string;
  /** 输入形态：单选下拉 / 多选下拉 / 纯输入。 */
  kind: "select" | "multi" | "text";
  /** 是否允许自定义输入新值（false = 只能从候选项选，如「材质」）。 */
  customizable: boolean;
  /** 候选项（select / multi 用；`values` 带平台 valueId）。 */
  options?: string[];
  values?: { name: string; value_id: string }[];
};

/** 一个 SKU 行（价格及库存表）。 */
export type PriceRow = {
  key: string;
  /** 规格组合文本（如「颜色分类:白 / 尺寸:1.2m」）。 */
  spec: string;
  price: number | null;
  /** **改后库存**（本次提交要写的新值）—— 平台真实列名。 */
  stock: number | null;
  /** **当前库存**（线上现值，**只读回显**）。
   *
   *  ⚠️ 用户口径 2026-10-21（真机 CDP 抓包，见设计 24 §23）：
   *  平台价格库存表把库存拆成**两列** —— `当前库存` + `改后库存`；
   *  提交用的是 `改后库存`，`当前库存` 只是给人看的对账值。
   *  旧实现只有一列「库存」⇒ 用户看不出「改了之后比线上多了多少」。
   *  `null` = 新导入、线上还没有这个 SKU（不是 0，不造假）。 */
  current_stock?: number | null;
  market_price: number | null;
  barcode: string;
  seller_code: string;
  /** **规格ID**（用户口径 2026-10-21：千帆固定列必须有）。平台侧是生成物，
   *  我方只透传用户映射来的值；没配映射就是空串（不造假 ID）。 */
  spec_id?: string;
  /** **行级启用状态**（用户口径 2026-10-10，真机 `skuData[].available`）：
   *  默认启用；停用后该 SKU 不会被消费者看到。`undefined` = 启用。 */
  available?: boolean;
  /** ⚠️ **该行的发货时间文案**（只「按规格设置发货时间」模式用）。
   *
   *  真机（2026-10-15 抓包）：「按规格」时价格库存表多一列「发货时间」，
   *  每行各自选（下拉：现货〔当日发货/24小时/48小时〕| 预售〔时段预售/定时预售〕）
   *  ⇒ **每行一条 `skuData[].deliveryTime`**（这才是我方该提交的东西）。
   *  值形如 `「现货 / 48小时」`、`「预售 / 15天」`；后端 `build_delivery_time`
   *  把它解析成 `{type, time}`。 */
  delivery_label?: string;
  /** 现货+预售模式的那列「发货时效」（15天 / 20天 …）。 */
  ship_aging?: string;
};

/** **第二步配置快照**（用户口径 2026-10-10：「确认发布提交的参数应是第二步配置的完整参数」
 *  / 「第三步预览第二步填写的完整所有参数」）。
 *
 *  ⚠️ 为什么要上报：第二步的配置原本全是**组件内部 state**，而渲染是
 *  `{step === 1 && <PublishConfigStep2/>}` ⇒ 点「下一步」时组件**卸载、内部 state 全丢**
 *  ⇒ 提交体只带 title/category/图片/SKU，**配置根本没进提交**、第三步也无从预览。
 *  ⇒ 把「可提交 / 可预览的那部分」在变化时上报给向导（由向导持有到关闭为止）。 */
export type ConfigSnapshot = {
  deliveryMode: string;
  /** **物流类型**（`物流配送` / `无物流发货或物流配送`）。
   *  ⚠️ 与 `deliveryMode`（发货模式：现货/预售）**是两件事**：
   *  后端据此算提交体的 `deliveryMode` 数字值（物流配送=0）。
   *  缺它 ⇒ 平台报「商品不支持设置无物流发货」（用户实测 2026-10-21）。 */
  logisticsType?: string;
  shipTime: string;
  presaleKind: string;
  presaleDays: number | null;
  shipAging: string;
  saleTime: string;
  priceConfidential: boolean;
  specTypes: string[];
  specValues: Record<string, string[]>;
  /** 规格图数量（`规格名 → 已挂图数`；只报数量，产物是上传后的链接）。 */
  specImageCounts: Record<string, number>;
  /** **规格图远程 URL**（`规格名 → 规格值 → 远程地址`，第三步预览规格图用）。 */
  specImageUrls: Record<string, Record<string, string>>;
  /** **规格图完整发布体材料**（`规格名 → 规格值 → 已上传对象`，2026-10-10 补）：
   *  提交时 `skuData[].specImage` 直接传这个对象，不再传相对路径重新上传。 */
  specImageMaterials: Record<string, Record<string, ConfigMainImage>>;
  /** **规格图相对路径**：`规格名 → 规格值 → 预检包相对路径`（用户报「image_path 规格图也是空」，
   *  2026-10-10）。规格图**选择时**已上传预览，但**正式提交**时后端按此相对路径
   *  **统一重新上传**（与主图/详情图同链路、同去重）⇒ 向导据此填 SKU 的 `image_path`。
   *  ⚠️ 只报**第一组**（真机：仅第一组规格支持挂规格图）；手动本地上传的规格图无此项。 */
  specImagePaths: Record<string, Record<string, string>>;
  desc: string;
  showPlayback: boolean;
  englishName: string;
  supportedTypes: string[];
  /** **售后服务的最终展示名**（平台强制项 ∪ 用户勾选项 ⇒ 规则名）。
   *  ⚠️ 只报 `supportedTypes`（用户**主动**勾的）会漏掉平台强制项
   *  ⇒ 第三步「售后服务」整块显示空（用户实测 2026-10-10）。 */
  afterSaleNames: string[];
  logisticsPlanId: string | null;
  freightTemplateId: string | null;
  /** **物流模板名称**（用户口径 2026-10-10：第三步要展示**选择的模板名称**，不是「已选」）。 */
  logisticsPlanName: string;
  /** **运费模板名称**（同上）。 */
  freightTemplateName: string;
  /** **商品属性**（真机 `itemData[0].attributes` 形状；只带**已填**的项）。
   *  ⚠️ 缺它就是真机那条报错「attributes - 材质属性必须填写」——
   *  原先 `attributes` 直接抄**骨架**（骨架里是上一个商品的属性），用户填的从没进过提交体。 */
  attributes: PublishAttributeSnapshot[];
  /** **规格维度**（`规格名 → 平台 attributeId`）—— 规格值拆分要靠它，
   *  否则所有 SKU 行都落到「第一个规格 id」上 ⇒ 真机报「存在重复的规格」。
   *  ⚠️ 2026-10-10：补 `kind`（select/multi/text）与 `customizable`（是否可自定义输入），
   *  规格值输入框据此渲染成下拉 / 多选 / 纯输入。 */
  specAttributes: SpecAttribute[];
  /** **多件装打包**规格（用户口径 2026-10-10，真机「多件装打包」弹窗）。 */
  packageBundles: PackageBundle[];
};

export type PublishConfigStep2Props = {
  /** ⚠️ **清空信号**（2026-10-15）：换数据包时向导递增它 ⇒ 本组件清掉自己的
   *  SKU 导入产物（规格/规格值/规格图/开关/挂起值）。
   *  ⚠️ 用信号而**不是** `key` 重挂 —— 重挂会连带第一步一起重挂（实测自伤：
   *  第一步被打回「还没有加载数据包」空态，且「正在读取」文案永不消失）。 */
  resetToken?: number;
  /** 已选类目链路（展示用）。 */
  categoryPath: string[];
  /** 已选类目 id（拉属性 / 调平台接口用）。 */
  categoryId: string;
  /** 账号 id（自动生成类参数要调平台）。 */
  accountId: number;
  /** ⚠️ 商品标题**由第一部分持有** —— 这里只读展示，**不再放一个输入框**
   *  （用户口径 2026-10-08：「第二部分的基础参数第一部分有重复，应保持一致的值，
   *  且应只展示一次」）。 */
  title: string;
  onTitleChange: (next: string) => void;
  /** 已上传主图（同第一部分那份，不重复管理）。 */
  images: ConfigMainImage[];
  /** 商品主图变更（用户口径 2026-10-10：主图也要能用**上传组件**直接加图）。 */
  onImagesChange?: (next: ConfigMainImage[]) => void;
  /** **配置快照上报**（用户口径 2026-10-10：提交/预览要用**第二步配置的完整参数**）。
   *  ⚠️ 组件在点「下一步」时会卸载（内部 state 全丢）—— 必须由向导持有快照。 */
  onConfigChange?: (next: ConfigSnapshot) => void;
  /** **规格图原始对象上抛**（2026-10-10 用户口径「第二步/第三步统一用同一份数据源」）：
   *  把 `specImages`（`规格名 → 规格值 → ConfigMainImage`，含 url/local_url）**原样**上抛，
   *  第三步直接用它的 `url` 渲染，与第二步同源 —— 不再经 `ConfigSnapshot` 转字符串副本，
   *  避免「第二步预览正常、第三步空」的两份数据源不一致。 */
  onSpecImagesChange?: (next: Record<string, Record<string, ConfigMainImage>>) => void;
  /** **发布助手的异常提示数**（用户口径 2026-10-10：
   *  「点击下一步发布时校验助手中是否存在异常提示，存在则不允许进入下一步」）。 */
  onHintCountChange?: (n: number) => void;
  /** 关键属性 / 其他属性（平台下发；空数组 = 该类目没有）。 */
  keyProperties: PropertyField[];
  otherProperties: PropertyField[];
  /** 品牌候选（平台下发 + 「无品牌」）。 */
  brandOptions: string[];
  /** 是否为**编辑模式**（真机：开售时间选项在编辑模式少一项）。 */
  isEditMode?: boolean;
  /** 物流模板候选（平台下发）。 */
  shippingTemplates: ShippingTemplate[];
  /** 物流模板是否加载中。 */
  shippingTemplatesLoading?: boolean;
  /** 平台默认物流模板 id（未选时用它）。 */
  defaultTemplateId?: string | null;
  /** 物流计划（发货仓）候选（与运费模板 `shippingTemplates` 是**两个数据源**）。 */
  logisticsTemplates: LogisticsTemplate[];
  /** 物流计划是否加载中。 */
  logisticsTemplatesLoading?: boolean;
  /** 规格类型候选（真机：「套装明细/颜色分类/尺寸/款式/材质/是否带书架」）。 */
  specTypeOptions: string[];
  /** 价格行（由已选规格组合生成）。 */
  priceRows: PriceRow[];
  onPriceRowsChange: (next: PriceRow[]) => void;
  /** **预选通过**的 SKU 规格集合（用户口径 2026-10-11 需求 4/5）。
   *
   *  > 「未选中的不允许进入配置商品参数也就是预选未通过」
   *  > 「第二步配置参数时后续逻辑注意只是有预检通过的sku规格」
   *
   *  第一步 SKU 表里被取消勾选、或两图判定未通过的行**不进这里**。
   *  判据由第一步 `passedSkuSpecs()` 单一实现产出，本组件**只消费不重算**。
   */
  passedSpecs?: Set<string>;
  /** **预检通过且已勾选**的 SKU 整行（供「从预检 SKU 导入」建规格/价格/库存）。 */
  passedRows?: PassedSkuRow[];
  /** 弹窗左侧要列出的数据包规格分类名（保序去重；由向导算好传入）。 */
  specImportSources?: string[];
  /** **预检表格的全部列名**（价格库存映射的来源列候选 —— 用户口径「动态可调整」）。 */
  priceSourceColumns?: string[];
  /** **上报两份映射草稿给向导**（用户口径 2026-10-15）。
   *
   *  ⚠️ 为什么必须上报：第一步的 SKU 表**支持手动删列**，而删列前的二次确认要
   *  「点名该列已被 XX 映射使用」（用户原话：「会同步删除已经映射使用的 XXX 配置参数」）。
   *  那两份映射的**真相源在第二步**（本组件），第一步拿不到 ⇒ 必须上抛。
   *  ⚠️ 只上抛**最小形状**（`source` / `target` 字符串数组），不上抛整个 state ——
   *  第一步只需要「这列有没有被用到」，多传会让两边耦合。
   */
  onMappingDraftChange?: (draft: {
    specSources: string[];
    priceSources: string[];
    /** ⚠️ **完整映射行**（提交体要按它取列值，不能只给来源名单）。 */
    priceMapping: PriceMappingRow[];
    /** 售价回落列（`原价` 取不到时用它）。 */
    priceFallbackSource: string;
  }) => void;
  /** 按**用户配置的**列映射重新导出通过行（向导持有表格与勾选态，只有它能重算）。 */
  onRebuildRows?: (
    priceMapping: readonly PriceMappingRow[],
    priceFallbackSource: string,
    /** **商品 SKU 映射的列名**（用户口径 2026-10-21：SKU 名称不再单独配映射，
     *  由这几列决定 ⇒ 向导重算时必须把它一起传下去，否则 `passedSkuRows`
     *  找不到「一行一条 SKU」的定位列 ⇒ 恒空 ⇒ 报「没有『预检通过且已勾选』的 SKU 行」。 */
    specColumns: readonly string[],
  ) => PassedSkuRow[] | undefined;
  /** 上传失败重试（读向导的重试开关/次数）—— 规格图上传也认这份配置。 */
  withUploadRetry?: <T,>(run: () => Promise<T | null>, isOk?: (v: T) => boolean) => Promise<T | null>;
  /** 千帆第一部分（商品图片 / 标题 / 类目）—— 用户口径 2026-10-09：
   *  「第二步参数配置中的商品图片、商品标题、商品类目合并到下面各基础信息中」。
   *  由外层把 `PublishConfigStep1` 作为节点塞进「基础信息」区最前面。 */
  headSlot?: React.ReactNode;
  /** 「自动加载参数」按钮（用户口径 2026-10-09：按钮**独立于第一部分**，
   *  放在**基础信息外面的顶部**——整页参数配置区最上方、左右两栏之上）。 */
  autoLoadSlot?: React.ReactNode;
  /** 详情图（自动加载第 4 步产物 + 图文信息区共用）。 */
  detailImages?: ConfigMainImage[];
  onDetailImagesChange?: (next: ConfigMainImage[]) => void;
  /** 商品视频（自动加载第 5 步产物 + 图文信息区共用）。 */
  video?: ConfigMainImage | null;
  onVideoChange?: (next: ConfigMainImage | null) => void;
  /** 上传一个**本地视频文件**（用户口径 2026-10-10：「商品视频自定上传失败页面应支持
   *  手动上传，上传按钮不要置灰色」）。
   *
   *  ⚠️ 为什么必须有：视频走 `common_file` 直传，比图片更易失败（体积大 / 扩展名 /
   *  限流）。失败后若不给手动入口、按钮还 `disabled`，用户就**没有自救路径**
   *  （与 DEF-058「失败原因不落库 = 拿走用户自救路径」同型）。
   *  返回 `null` = 上传失败（不造假，前端提示原因）。 */
  onUploadVideoFile?: (file: File) => Promise<ConfigMainImage | null>;
  /** 上传一个**本地图片文件**到图片空间（规格图用；按内容 MD5 命名）。
   *  返回 `null` = 上传失败（走既有「不造假」口径，前端显示未上传）。 */
  onUploadImageFile?: (file: File) => Promise<ConfigMainImage | null>;
  /** **规格维度**（`规格名 → 平台 attributeId`，来自 `getCategoryProperty.saleProperties`）。
   *  ⚠️ 必需：真机发布要按规格**逐维度**给 `variants[].attributeId`。 */
  specAttributes?: SpecAttribute[];
  /** 预检**通过且被选中**的 SKU 图（用户口径 2026-10-09：规格图前缀点击后
   *  **从这些图里选**，选中即由后端上传并回显）。 */
  skuImages?: { rel_path: string; file_name: string }[];
  /** 把预检暂存的**一张 SKU 图**上传到图片空间（回到 `ConfigMainImage`）。 */
  onUploadSkuImage?: (rel_path: string, file_name: string) => Promise<ConfigMainImage | null>;
  /** **批量上传规格图**（用户报 2026-10-21「上传规格图耗时高」，实测 24 张 93 秒）。
   *
   *  逐张上传时每张都要单独取 permit + 单独 base64 ⇒ 前端并发再高也白搭
   *  （后端每次只收到一张）。这里接向导已有的 `uploadManyFromPackage`
   *  （一次 permit + 服务端并发 PUT，与主图/详情图同一条链路）。
   *
   *  ⚠️ 返回**与入参逐位对齐**（失败项也在，`image=null` + `reason`）；
   *  缺省时回落到逐张 `onUploadSkuImage`（保持向后兼容，老调用方不受影响）。 */
  onUploadSkuImagesBatch?: (
    items: { rel_path: string; file_name: string }[],
  ) => Promise<{ rel_path: string; ok: boolean; image: ConfigMainImage | null; reason: string }[]>;
  /** 取预检媒体图的 URL（鉴权流）；规格图选择弹窗的缩略图用。 */
  onFetchPrecheckImage?: (rel_path: string) => Promise<string>;
  /** 导购短标题（自动加载第 6 步产物 + 基础信息区共用；受控）。 */
  shortTitle?: string;
  onShortTitleChange?: (next: string) => void;
  /** ⚠️ 商品属性值 —— **受控于外层**（自动加载第 8 步要写它）。
   *  早期属性值存在本组件内部 ⇒ 外层自动加载填的值**到不了页面**（静默失效）。 */
  propertyValues?: Record<string, unknown>;
  onPropertyValuesChange?: React.Dispatch<React.SetStateAction<Record<string, unknown>>>;
  /** 售后服务规则（平台按类目下发；`must_support=true` ⇒ 勾选强制不可改）。
   *  ⚠️ 不写死「支持七天无理由退货」—— 规则集按类目变（服务券给「未核销随时退」等）。 */
  afterSaleRules?: AfterSaleRule[];
  /** 该类目是否支持「无物流发货」（物流类型与类目直接相关；`undefined` = 平台未下发 ⇒ 兜底）。 */
  supportNoLogistics?: boolean;
};

/** 分区外壳：标题 + 外框（与第一部分同一视觉口径）。 */
function Section(props: { title: string; required?: string; children: React.ReactNode }) {
  return (
    <section className="config-section">
      <div className="config-section-head">
        <span className="config-section-title">{props.title}</span>
        {props.required && <Tag color="red">{props.required}</Tag>}
      </div>
      <div className="config-section-body">{props.children}</div>
    </section>
  );
}

/** 一行：左标签 + 右控件（与第一部分同栅格）。 */
/** **下拉多选属性的选择上限**（用户口径 2026-10-10：「最多只能选三个，超过的置灰色」）。
 *
 * ⚠️ 唯一来源：选满后**未选项置灰**与 `onChange` 的**截断兜底**都用它，
 * 不在两处各写一个 3。 */
const MULTI_SELECT_MAX = 3;

/** 提交体里的一条**商品属性**（真机 `itemData[0].attributes[]`，2026-10-10 逐字逆出）。
 *
 * 单选 ⇒ `attributeValue` + `attributeValueId`（标量）；
 * 多选 ⇒ `attributeValueList`（数组，**最多 3 个**）。两种形状互斥、不允许同时带。 */
type PublishAttributeSnapshot =
  | {
      attributeId: string;
      attributeName: string;
      attributeValueType: 1;
      attributeValue: string;
      attributeValueId?: string;
    }
  | {
      attributeId: string;
      attributeName: string;
      attributeValueType: 2;
      attributeValueList: { attributeValue: string; attributeValueId?: string }[];
    };

/** 一条发布提示（真机：「商品发布助手」里的校验项）。 */
type PublishHint = {
  section: string;
  field: string;
  reason: string;
};

/** 「商品发布助手」面板。
 *
 * 用户口径（2026-10-10）：**分区结构与千帆后台一致** ——
 * 「发布提示 (N)」+ 各分区（类目信息 / 基础信息 / 图文信息 / 价格库存 / 服务信息），
 * 每个分区标题行带自己的「N 项必填」徽标（有未通过时）。
 * ⚠️「尺码信息」「商品资质」**不进助手**（用户口径 2026-10-10：类目相关、
 * 按条件才展示，不是常驻配置区）。
 */
const HELPER_SECTIONS = ["类目信息", "基础信息", "图文信息", "价格库存", "服务信息"] as const;

function PublishHelper(props: { hints: PublishHint[] }) {
  const [open, setOpen] = useState(true);
  const n = props.hints.length;
  return (
    <div className="publish-helper">
      <div className="publish-helper-bar">
        <span className="publish-helper-title">商品发布助手</span>
        <Button type="link" size="small" onClick={() => setOpen((v) => !v)}>
          发布提示 ({n})
        </Button>
      </div>
      {open && (
        <div className="publish-helper-body">
          {n === 0 ? (
            <Text type="secondary">没有待处理的提示</Text>
          ) : (
            HELPER_SECTIONS.map((section) => {
              const rows = props.hints.filter((h) => h.section === section);
              if (rows.length === 0) return null;
              return (
                <div key={section} className="publish-helper-section">
                  <div className="publish-helper-section-head">
                    <span className="publish-helper-section-title">{section}</span>
                    <span className="publish-helper-section-count">{rows.length} 项必填</span>
                  </div>
                  {rows.map((h) => (
                    <Tooltip
                      key={`${h.section}-${h.field}`}
                      title={`${h.field}：${h.reason}`}
                      placement="right"
                      mouseEnterDelay={0.3}
                    >
                      <div className="publish-helper-row">
                        <span className="publish-helper-field">{h.field}</span>
                        <Text type="danger" className="publish-helper-reason" ellipsis>
                          {h.reason}
                        </Text>
                      </div>
                    </Tooltip>
                  ))}
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

/** 一行：左标签 + 右控件（与第一部分同栅格）。
 *
 * ⚠️ 用户口径（2026-10-09）：**说明性文案一律收进「说明图标 + Tooltip」**，
 * 不再以正文行铺在控件下方（那会占布局、把表单撑高）。
 */
function Field(props: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="config-field"
      data-field={props.label}
    >
      <div className="config-field-label">
        {props.required && <span className="req-star">*</span>}
        {props.label}
        {props.hint && (
          <Tooltip title={props.hint}>
            <InfoCircleOutlined className="section-help-icon field-help-icon" />
          </Tooltip>
        )}
      </div>
      <div className="config-field-body">{props.children}</div>
    </div>
  );
}

/** 一组属性里**已填**的项数（真机「关键属性 3/4」的分子）。
 *
 * 判据与 `publishHints` 的 `missing` 同源：`undefined` / `null` / `""` 视为未填；
 * 多选属性传空数组同样算未填（避免 `[]` 被当成有值）。
 */
function filledCount(fields: PropertyField[], values?: Record<string, unknown>): number {
  return fields.filter((f) => {
    const v = values?.[f.id];
    if (v === undefined || v === null || v === "") return false;
    if (Array.isArray(v) && v.length === 0) return false;
    return true;
  }).length;
}

/** 按属性定义渲染一个控件（**不写死属性名**）。
 *
 * ⚠️ 宽度一律 `100%`（由 `.prop-cell` 的 flex 分配）—— 早期写死 `width: 320`
 * 而单元格只有 332px、左侧还有属性名，控件必然溢出并与**下一格的属性名重叠**
 * （用户报的「布局拥挤、输入框和标题重叠」）。
 */
function PropertyControl(props: { field: PropertyField; value: unknown; onChange: (v: unknown) => void }) {
  const { field } = props;
  if (field.kind === "bool") {
    return (
      <Radio.Group
        value={props.value as string}
        onChange={(e) => props.onChange(e.target.value)}
        options={[
          { label: "是", value: "是" },
          { label: "否", value: "否" },
        ]}
        optionType="button"
      />
    );
  }
  if (field.kind === "multi") {
    //: ⚠️ 用户口径（2026-10-10）：「下拉多选注意**最多只能选三个**，**超过的置灰色**」
    //: ⇒ 已选满 3 个时，**未选中的选项**在下拉里置灰（`option.disabled`），
    //: 已选中的仍可取消（否则用户没法换）。
    //: 仍保留 `slice(0, 3)` 作为**行为兜底**（防非交互路径/粘贴等绕过 UI 限制）。
    const selected = (props.value as string[]) ?? [];
    const full = selected.length >= MULTI_SELECT_MAX;
    return (
      <Select
        mode="multiple"
        className="prop-control prop-multi"
        placeholder={field.placeholder ?? "请选择"}
        value={selected}
        onChange={(next) => props.onChange((next as string[]).slice(0, MULTI_SELECT_MAX))}
        options={(field.options ?? []).map((o) => ({
          label: o,
          value: o,
          //: 已选满 ⇒ 未选项置灰；已选项保持可选（供取消）。
          disabled: full && !selected.includes(o),
        }))}
      />
    );
  }
  if (field.kind === "select") {
    return (
      <Select
        className="prop-control"
        placeholder={field.placeholder ?? "请选择"}
        value={(props.value as string) || undefined}
        onChange={props.onChange}
        allowClear
        options={(field.options ?? []).map((o) => ({ label: o, value: o }))}
      />
    );
  }
  return (
    <Input
      className="prop-control"
      placeholder={field.placeholder ?? "请输入"}
      maxLength={field.maxLength}
      value={(props.value as string) ?? ""}
      onChange={(e) => props.onChange(e.target.value)}
    />
  );
}

export default function PublishConfigStep2(props: PublishConfigStep2Props) {
  const { message } = App.useApp();
  //: ⚠️ 标题**不在这里存**（第一部分那份是唯一真相源）；短标题是第二部分独有的。
  //: 短标题现在**受控于外层**（自动加载第 6 步要写它），这里不再本地存。
  const shortTitle = props.shortTitle ?? "";
  const setShortTitle = (v: string) => props.onShortTitleChange?.(v);


  /** 物流计划（发货仓）选中值 —— 物流模板下拉（未选时用第一个计划）。 */
  const [logisticsPlanId, setLogisticsPlanId] = useState<string | null>(null);
  /** 运费模板选中值（未选时用平台默认）。 */
  const [freightTemplateId, setFreightTemplateId] = useState<string | null>(null);
  /** 预售方式（时段预售 / 定时预售）。 */
  const [presaleKind, setPresaleKind] = useState("时段预售");
  /** 时段预售：付款后 N 天。 */
  const [presaleDays, setPresaleDays] = useState<number | null>(null);
  /** 定时预售：发货日期。 */
  const [presaleDate, setPresaleDate] = useState<Dayjs | null>(null);

  /** 发货模式（真机四选一，默认现货模式）。 */
  const [deliveryMode, setDeliveryMode] = useState<string>(DELIVERY_MODES[0]);
  /** 现货模式：发货时间（默认取真机标「推荐」的那项）。
   *  ⚠️ 必须用**候选里的原值**（`"48小时（推荐）"`）—— 早期写 `"48小时"`
   *  与选项值对不上 ⇒ 单选**一项都不选中**（用户报「现货发货时间默认选中 48 小时」）。 */
  const [shipTime, setShipTime] = useState<string>(
    SPOT_SHIP_TIME_OPTIONS[SPOT_SHIP_TIME_OPTIONS.length - 1],
  );
  /** 已选规格类型（上限 2）。 */
  const [specTypes, setSpecTypes] = useState<string[]>([]);
  /** 每个规格类型的**规格值**（真机：输入框「请选择或输入规格值」）。 */
  const [specValues, setSpecValues] = useState<Record<string, string[]>>({});
  /** 每个规格类型的**候选项**（由平台 saleProperties 的 values 下发）。 */
  const [specValueOptions] = useState<Record<string, string[]>>({});
  /** 每个规格值的错误提示（超长/重复/超量）。 */
  const [specValueHints, setSpecValueHints] = useState<Record<string, string>>({});
  /** **规格图**：`规格名 → 规格值 → 图`（用户口径 2026-10-09：
   *  「每个规格应支持单独添加规格图，规格输入框前缀展示规格图，支持批量添加规格图」）。
   *  ⚠️ 只有**第一组规格类型**支持（真机：`MAIN_SPEC_ONLY_HINT`）。 */
  const [specImages, setSpecImages] = useState<
    Record<string, Record<string, ConfigMainImage>>
  >({});
  /** 规格图上传中计数（用于按钮 loading）。 */
  const [specImageUploading, setSpecImageUploading] = useState(0);
  //: ⚠️ 2026-10-10（用户口径「第二步/第三步统一用同一份数据源」）：把 `specImages`
  //: **原对象**上抛给向导，第三步直接读它的 `url` 渲染 —— 与第二步同源，不再经
  //: `ConfigSnapshot` 转字符串副本（那是「第二步预览正常、第三步空」的根因）。
  useEffect(() => {
    props.onSpecImagesChange?.(specImages);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specImages]);
  /** **规格图开关**（用户口径 2026-10-10 纠正：这不是「规格启用/停用」——
   *  规格添加即为启用，没有启用停用逻辑；开关只控制**该组是否配置规格图**。
   *  真机：第一组默认开、其余组默认关；关 ⇒ 该组的图前缀与「添加/批量上传」都不出现。 */
  const [specImageEnabled, setSpecImageEnabled] = useState<Record<string, boolean>>({});

  /* ---- 「从预检 SKU 导入」弹窗（用户口径 2026-10-11 最终版） ----
   *
   * 独立入口（**不在**自动加载里）：规格类型随类目变动，自动加载按顺序跑死，
   * 拿到的规格不一定是最终类目的 ⇒ 让用户在**商品规格区域**随时按当前类目重跑。
   *
   * 弹窗职责：让用户**明确确认**「第一步的规格分类 → 第二步的平台规格维度」映射
   * （默认**文本精确匹配**，可手改），确认后自动解析并加载规格值 / 价格 / 库存 / 规格图。
   */
  const [specImportOpen, setSpecImportOpen] = useState(false);
  /** **SKU 名称优化预览**（用户口径 2026-10-20）：点「预检通过SKU导入」时，
   *  若有规格名超长（默认 60 字符，汉字计 2）先弹这个，确认后再进导入弹窗。 */
  const [skuOptimizeOpen, setSkuOptimizeOpen] = useState(false);
  const [skuOptimizeItems, setSkuOptimizeItems] = useState<SkuNameOptimizeItem[]>([]);
  /** **手动输入**的规格值触发优化弹窗时，记住「优化完写回哪个规格的第几格」。
   *
   *  ⚠️ 与导入链路（`appliedSkuNames`）是**两条路**：导入是批量替换数据源，
   *  手动输入只是改一个输入框的值 ⇒ 必须单独记住落点，否则优化完写不回去。
   *  `null` = 本次弹窗不是从手动输入触发的（走导入链路）。 */
  const [pendingSpecValue, setPendingSpecValue] = useState<
    { spec: string; index: number } | null
  >(null);
  const [skuOptimizeMax, setSkuOptimizeMax] = useState(60);
  const [skuOptimizeBusy, setSkuOptimizeBusy] = useState(false);
  /** **生效的规格来源名**（用户确认优化后的名称用在这里）。
   *
   *  ⚠️ 用户口径 2026-10-20：「只影响本次导入结果」——
   *  不写回 SKU 表、不改 `props.specImportSources`，只在本组件内替换。
   *  `null` = 用原始来源（没做优化时）。 */
  const [appliedSkuNames, setAppliedSkuNames] = useState<string[] | null>(null);
  const effectiveImportSources = appliedSkuNames ?? props.specImportSources ?? [];
  /** **清掉本弹窗管辖的历史数据**（用户口径 2026-10-15）。
   *
   *  > 「点击选择重新加载资源后，**当前窗口内的所有历史数据均应被清除**，
   *  >   目前第二步的**商品规格和价格库存列均未清除**」
   *
   *  ⚠️ **单一实现**：点「选择」重开时清一次，导入时再清一次 —— 两处共用本函数，
   *  否则迟早漂移成「一处清、一处不清」（用户看到的正是这个现象）。
   *  ⚠️ 清的是**本弹窗管辖的两块**：商品规格（类型 + 值）与价格库存行。
   *  主图 / 详情图 / 属性**不属于本弹窗**，一律不动。
   *  ⚠️ **规格图也一起清**（它按「维度+值」存，维度清了图留着会变成孤儿条目，
   *  下次同名的维度会**复用到旧图**，用户看到「没传过却已有图」）。
   */
  const clearImportTargets = (opts?: { keepPending?: boolean }) => {
    setSpecTypes([]);
    setSpecValues({});
    setSpecImages({});
    props.onPriceRowsChange([]);
    //: 👆 挂起值：清（默认）—— 否则清完价格表后，旧挂起值会在下一轮重建里又落回去。
    //: ⚠️ **导入那一刻例外**（`keepPending: true`）：调用方刚设好**新的**挂起值，
    //: 清了就白设（那是「导入成功但一行没值」的老 bug 形态）。
    if (!opts?.keepPending) setPendingSpecFills([]);
  }

  /** ⚠️ **换数据包 ⇒ 清掉本组件的 SKU 导入产物**（用户报障 2026-10-15：
   *  「重新读取资源后，SKU 导入的数据未清空」）。
   *
   *  向导在换数据包成功后递增 `resetToken` ⇒ 这里响应式地调 `clearImportTargets()`。
   *  ⚠️ **不用 `key` 重挂**：实测重挂会把第一步 `PrecheckStep` 一起打回初始态
   *  （两行资源选择框 + 「正在读取本地数据包…」永不消失）。
   *  ⚠️ 首次挂载（`resetToken` 为 0 / undefined）**不执行** —— 否则弹窗一打开
   *  就把用户已配的东西清掉。
   */
  const lastResetTokenRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    const token = props.resetToken;
    if (token === undefined) return;
    if (lastResetTokenRef.current === undefined) {
      //: 首次见到 ⇒ 只记住，不清（打开弹窗不该清空）。
      lastResetTokenRef.current = token;
      return;
    }
    if (token === lastResetTokenRef.current) return;
    lastResetTokenRef.current = token;
    clearImportTargets();
    //: eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.resetToken]);;
  /** ⚠️ **打开弹窗时重置映射**为「预置 4 行 + 来源全空」（用户口径 2026-10-15）。
   *
   *  ⚠️ 不重置的后果：用户上次配了一半取消，这次打开看到的是半截配置，
   *  而他以为那是「默认状态」—— 会直接点确认导入。
   */
  const resetPriceMappingOnOpen = () => setPriceMapping(defaultPriceMappingRows());


  /** 真正打开「导入 sku 数据」弹窗（清历史 + 重置映射）。 */
  const openSpecImportModal = useCallback(() => {
    //: ⚠️ **不再自动带入默认映射**（用户口径 2026-10-14：
    //: 「用户第一步需**手动勾选**商品规格」）—— 打开时清空，
    //: 用户勾了哪个才建哪一行（不勾 = 不导入）。
    setImportMapping([]);
    //: ⚠️ **价格库存映射重置为「预置 5 行 + 来源全空」**（用户口径 2026-10-15）。
    resetPriceMappingOnOpen();
    //: ⚠️ **「点击选择重新加载资源后，当前窗口内的所有历史数据均应被清除」**
    //: （用户口径 2026-10-15）——清的是**本弹窗管辖的两块**：
    //: 商品规格（规格类型 + 规格值）与价格库存行。主图/详情图/属性不归它管。
    //: ⚠️ 与「导入时清历史」**同一份实现**（`clearImportTargets`）——
    //: 两处各写一份迟早漂移成「一处清一处不清」。
    clearImportTargets();
    setImportDetail("");
    setSpecImportOpen(true);
  }, [clearImportTargets, resetPriceMappingOnOpen]);

  /** 点「预检通过SKU导入」：**先查规格名是否超长**（用户口径 2026-10-20）。
   *
   * 有超长的 ⇒ 调大模型压缩 + 弹预览（用户比较后再决定）；
   * 没有超长的 ⇒ 直接进导入弹窗，**不白调一次模型**。
   * 调用失败也直接进导入（不能因为优化不了就堵住用户）。
   */
  /**
   * 点「导入 SKU 数据」入口。
   *
   * ⚠️⚠️ **用户口径 2026-10-22**：「调用大模型优化的逻辑**由用户触发**，
   * 不在后端默认调用」+「导入 sku 数据时**没有对规格名称添加 60 字符长度校验**，
   * 如果长度超出则**弹窗**展示旧规格名 / 新规格名 / 操作列」。
   *
   * ⇒ 这里是**纯本地判定**，一次网络都不打：
   *   · 没有超长的 ⇒ 直接开导入弹窗（零延迟，绝大多数包走这条）；
   *   · 有超长的   ⇒ 开「规格名称超长」弹窗，把超长的行逐条列出；
   *     用户自己决定哪一条点「优化」（那才调大模型）。
   * ⚠️ 旧实现进这里就先 `await optimizeSkuNames(...)` 批量调模型 —— 已删除：
   *   用户没点任何按钮就消耗 token，且失败时 `catch` 静默吞掉直接开导入
   *   （用户看到的正是「没有校验就进来了」）。
   */
  const openSpecImportWithNameCheck = useCallback(() => {
    //: ⚠️⚠️ **校验对象是「规格值」，不是「列名」**（用户口径 2026-10-22：
    //: 「导入 SKU 数据 / 导入 sku 映射数据时**没有弹出规格名称优化弹窗**」）。
    //:
    //: 真根因：`effectiveImportSources` 装的是 `mappingSources`，而它是
    //: `priceSourceOptions(columns)` = **表头列名列表**（「商品信息」「SKUID」
    //: 「颜色分类」…）。列名天生是短词，永远不可能超 60 ⇒ `longNames` 恒空
    //: ⇒ **每次都直接开导入弹窗，弹窗一次都没弹过**（用户看到的就是这个）。
    //: 真正会长的是**表格数据里的规格值**（如「奶油白色-【伸缩桌+六斗柜】…」）。
    //: ⇒ 从 `passedRows` 取 `specValue` 去重后判定。
    const specValuesForCheck = Array.from(
      new Set(
        (props.passedRows ?? [])
          .map((r) => String(r.specValue ?? "").trim())
          .filter(Boolean),
      ),
    );
    if (specValuesForCheck.length === 0) {
      openSpecImportModal();
      return;
    }
    //: ⚠️⚠️ **入口这里不再弹窗**（用户口径 2026-10-22：「所有 sku 数据导入完毕后，
    //: 最后弹出优化规格名称的弹窗」）——先让导入完整跑完、用户看到结果，
    //: 再在导入收尾处（`importFinished` 分支）弹「统一优化」。
    //: ⇒ 入口只负责打开导入弹窗，校验与优化统一放在收尾，避免「还没导入就先被打断」。
    void specValuesForCheck;
    openSpecImportModal();
  }, [props.passedRows, openSpecImportModal]);
  /** 映射草稿（**成对关系行**，最多 2 行；两列均必填且不许重复）。 */
  const [importMapping, setImportMapping] = useState<SpecMappingRow[]>([]);
  /** 导入执行态（跑的时候禁用「确认导入」并显示进度文案）。 */
  const [importRunning, setImportRunning] = useState(false);
  /** 导入**进度**文案（用户口径 2026-10-11：「统计耗时和执行进度情况展示」）。 */
  const [importProgress, setImportProgress] = useState<string>("");
  /** 导入**已用秒数**（单调钟起表，终态停）。 */
  const [importElapsed, setImportElapsed] = useState(0);
  /** **取消标志**（用户口径 2026-10-11：「导入中点击取消，则**中断未导入和未上传的
   *  数据，停止执行**」）。
   *
   *  ⚠️ 用 `useRef` 而不是 `useState`：上传循环是 `await` 里的**长任务**，
   *  它拿到的是**闭包快照** —— state 更新对它不可见（要等下一轮渲染才生效），
   *  循环会继续跑完。ref 是同一个可变对象，循环每次读都能看到最新值。
   *  （同类坑本项目已踩过：DEF 系列的「取消后仍继续执行」基本都是这个成因。） */
  const importCancelRef = useRef(false);
  /** **挂起待填值**：导入算出的 `spec → {price, stock, sellerCode}`。
   *
   *  ⚠️ 为什么不直接写 `priceRows`：`specTypes`/`specValues` 一变，下面那个
   *  「规格值 → 价格及库存表」effect 会用**笛卡尔积重建**整个表 ⇒ 直接写会被冲掉
   *  （用户会看到「导入了但价格还是空」）。所以先挂起，等重建完成后由 effect 落值。 */
  const [pendingSpecFills, setPendingSpecFills] = useState<
    SkuFillPlan["rows"]
  >([]);
  const [importDetail, setImportDetail] = useState<string>("");
  /** **售价倍率**（用户口径 2026-10-11：「商品售价规则是在价格与库存的原价列基础上
   *  默认 1.7 倍，参数可调整」）。默认 1.7；改动即时生效（下次导入用新值）。 */
  const [priceMultiplier, setPriceMultiplier] = useState<number>(DEFAULT_PRICE_MULTIPLIER);
  /** **价格库存列映射**（用户口径：动态可调整；**默认 = 现行那套逻辑**）。
   *  `售价 ← 原价`、`库存 ← 库存`、`商家编码 ← ID`。 */
  /** ⚠️ **价格库存映射关系表不设默认值**（用户口径 2026-10-15：
   *  「价格库存映射关系表不再设置默认值」）。
   *
   *  旧实现是 `DEFAULT_PRICE_MAPPING.map(...)` —— 打开弹窗就预填好两三行
   *  「售价 ← 原价」「库存 ← 库存」… 那会让用户**以为映射已经配好了**，
   *  而实际来源列是否选对、他有没有别的意图，只有他自己知道。
   *  ⇒ 初始为空数组，用户从「+」自己加（新增行两列都空，与口径一致）。
   *  ⚠️ `DEFAULT_PRICE_MAPPING` 常量**保留**（下方「按现行逻辑一键填充」按钮仍用它）——
   *  删常量等于把「快速回到默认配置」的路也一起删了。 */
  const [priceMapping, setPriceMapping] = useState<PriceMappingRow[]>(
    //: ⚠️ **预置 4 行**（SKU图/售价/市场价/库存，来源全空）—— 用户口径 2026-10-15：
    //: 「右侧的价格库存中，SKU图、售价、市场价、库存**所有必须配置**，
    //:  弹窗打开时就**默认展示**，它们对应的左侧下拉**默认为空**展示『请选择』文本」。
    //: ⚠️ 与「不设默认值」不矛盾：**不预填来源列**（那是猜），但**行要摆出来**
    //: （让用户知道这四项必须配）。
    defaultPriceMappingRows(),
  );

  /** ⚠️ **「还不能导入」的原因清单**（用户口径 2026-10-15：未配映射要**提示他去配**，
   *  不能只把按钮变灰让他猜）。
   *
   *  ⚠️ **单一实现**：按钮的 `disabled` 与弹窗里的提示都读它 ——
   *  两处各写一份判据迟早漂移成「按钮不能点、提示说没问题」。
   *  ⚠️ 只列**用户能自己解决**的项，并按他配的顺序（先规格映射、再价格库存映射）。
   */
  const importBlockers = useMemo(() => {
    const out: string[] = [];
    if (effectiveImportSources.length === 0) {
      out.push("第一步还没有预检通过且勾选的 SKU 行");
    } else if (!isSpecMappingReady(importMapping)) {
      out.push("请先配置「商品 SKU 映射关系」（左侧选规格分类、右侧选平台规格类型）");
    }
    if (!isPriceMappingReady(priceMapping)) {
      out.push("请先配置「价格库存映射关系表」（左侧选预检通过列、右侧选价格库存列）");
    } else {
      //: ⚠️ **四项必配**（用户口径 2026-10-15：「右侧的价格库存中，SKU图、售价、
      //: 市场价、库存所有**必须配置**」）。说清**是哪几项**没配 —— 「有必填项未填」
      //: 等于没说，用户还得自己一行行找。
      const missing = missingRequiredPriceTargets(priceMapping);
      if (missing.length > 0) {
        out.push(`「${missing.join("、")}」还没有选来源列`);
      }
    }
    return out;
  }, [effectiveImportSources, importMapping, priceMapping]);
  /** 售价的滞留来源列（原价取不到时用它，**不乘倍率**）。默认「计算价格」。 */
  const [priceFallbackSource, setPriceFallbackSource] = useState<string>(
    DEFAULT_PRICE_FALLBACK_SOURCE,
  );
  /** **多件装打包**规格 + 弹窗开关（用户口径 2026-10-10）。 */
  const [packageBundles, setPackageBundles] = useState<PackageBundle[]>([]);
  const [packageBundleOpen, setPackageBundleOpen] = useState(false);
  /** **批量修改列值**弹窗开关（用户口径 2026-10-10）。 */
  const [batchEditOpen, setBatchEditOpen] = useState(false);
  /** 商品主图 / 详情图手动上传中（用户口径 2026-10-10：用上传组件）。 */
  const [mainUploading, setMainUploading] = useState(false);
  const [detailUploading, setDetailUploading] = useState(false);
  /** 商品视频手动上传中（用户口径 2026-10-10：上传失败要能手动传、按钮不置灰）。 */
  const [videoUploading, setVideoUploading] = useState(false);

  /** 当前生效的物流计划 id（用户选定 or 第一个计划）。bundle 里是 `logisticsPlanId`。 */
  const selectedShippingTemplate =
    logisticsPlanId ??
    props.logisticsTemplates[0]?.plan_id ??
    null;

  /** **生效的运费模板 id**（显式选择 ?? 平台默认回落）—— 单一来源：
   *  发布助手判必填、第三步预览、提交都用它（用户实测「已选但展示未选」即漏了回落）。 */
  const effectiveFreightTemplateId =
    freightTemplateId ??
    props.shippingTemplates.find((t) => t.template_id === props.defaultTemplateId)?.template_id ??
    null;

  /** 真机：**一个输入框一个规格值** —— 末尾恒留 1 个空框等输入。 */
  const specValueInputs = useCallback(
    (spec: string): string[] => {
      const filled = (specValues[spec] ?? []).filter((v) => v.trim() !== "");
      return [...filled, ""];
    },
    [specValues],
  );

  /** 按规格名取该维度的**输入形态元数据**（`kind` + `customizable` + 候选值）。
   *  平台没下发（老数据 / 取不到）⇒ 回落纯文本输入，不造形态。 */
  const specMetaFor = useCallback(
    (spec: string): SpecAttribute | undefined =>
      (props.specAttributes ?? []).find((x) => x.name === spec),
    [props.specAttributes],
  );

  /** **把某个规格值改名 —— 唯一实现**（`setSpecValueAt` / `commitSpecValue` /
   *  「优化规格名称」弹窗确认 三处都走这里）。
   *
   *  ⚠️⚠️⚠️ 用户报 2026-10-22 两次，其实是**同一个根因**：
   *    ① 「编辑商品规格输入框后，**规格图丢失**，对应的价格及库存中的**所有列数据
   *       全部丢失**」；
   *    ② 「SKU 规格名称优化预览弹窗点击使用这些名称导入时**应只覆盖原来的规格名称**，
   *       目前覆盖后价格及库存列表中的**其它值也丢失了**」。
   *
   *  **根因（实测）：`PriceRow.key` 就是完整 `spec`**（`lib/publish-config.ts`
   *  `spec_combinations` 末尾 `const spec = combo.map(...).join(" / ")` ⇒
   *  `return { key: spec, spec }`，含「维度名:值」）。所以**值一改，key 就变**：
   *    · 价格库存那边：同步 effect 按新 key 重建，`byKey` 里只有旧 key ⇒
   *      `byKey.get(c.key)` 落空 ⇒ 走 `base` 分支把
   *      price/stock/market_price/current_stock/barcode/seller_code 全置 `null`；
   *    · 规格图那边：`specImages` / `specImageEnabled` 都是**按值做 key**，
   *      值改了旧键成了孤儿 ⇒ 图挂不上、开关读不到。
   *  未改名的行/值 key 不变，所以它们完好 —— 与用户截图完全吻合。
   *
   *  ⇒ 判据：**改名 = 三类键一起重排**，行的其它字段逐字段原样带过去。
   *  只在 `specValues` 里换字符串是不够的（那正是缺陷本身）。
   */
  const renameSpecValue = useCallback(
    (dim: string, before: string, after: string) => {
      const from = before.trim();
      const to = after.trim();
      if (!from || !to || from === to) return;
      //: ① 规格值列表：**按位置换**（同维度可能有重名，值本身不会重复，但位置最稳）。
      setSpecValues((prev) => {
        const list = prev[dim] ?? [];
        if (!list.includes(from)) return prev;
        return { ...prev, [dim]: list.map((v) => (v === from ? to : v)) };
      });
      //: ② 规格图：`规格名 → 值 → 图`，把值这一层的键换掉。
      setSpecImages((prev) => {
        const byValue = prev[dim];
        if (!byValue || !(from in byValue)) return prev;
        const renamed: Record<string, ConfigMainImage> = {};
        for (const [v, img] of Object.entries(byValue)) {
          renamed[v === from ? to : v] = img;
        }
        return { ...prev, [dim]: renamed };
      });
      //: ③ 规格图开关：**扁平** `{ "维度\\x00值": boolean }`（与 `specImages` 结构不同）。
      setSpecImageEnabled((prev) => {
        const oldKey = `${dim}\u0000${from}`;
        if (!(oldKey in prev)) return prev;
        const next = { ...prev };
        next[`${dim}\u0000${to}`] = next[oldKey];
        delete next[oldKey];
        return next;
      });
      //: ④ **价格库存表**：key 与 spec 里的这一段一起换，其它字段原样保留。
      //:    ⚠️ 必须在这里改 —— 只改 `specValues` 的话，下面那个同步 effect 会用
      //:    **新 key 重建行**，旧行匹配不上 ⇒ 价格/库存/编码全被 `null` 覆盖。
      props.onPriceRowsChange?.(
        props.priceRows.map((r) => {
          const parts = String(r.spec ?? "").split(" / ");
          let touched = false;
          const renamedParts = parts.map((part) => {
            const sep = part.indexOf(":");
            if (sep < 0) return part;
            if (part.slice(0, sep).trim() !== dim) return part;
            if (part.slice(sep + 1).trim() !== from) return part;
            touched = true;
            return `${part.slice(0, sep)}:${to}`;
          });
          if (!touched) return r;
          //: ⚠️ 用户口径 2026-10-22：「修改时只是名修改规格图，**库存表数据不应该变化清空**」
          //: ⇒ 这里**只换 `key` 与 `spec` 两段名字**，其余字段（price / stock /
          //:   current_stock / market_price / barcode / seller_code / spec_id /
          //:   available / 发货时间 …）全部靠 `...r` 原样带过去，一个都不碰。
          //:   任何 `delete`/重建行的写法都会把用户填过的数据弄丢。
          const spec = renamedParts.join(" / ");
          return { ...r, key: spec, spec };
        }),
      );
    },
    [props.priceRows, props.onPriceRowsChange],
  );

  /** 把某一格规格值换成 `next`（**走 `renameSpecValue`** 同步重排所有键）。 */
  const setSpecValueAt = useCallback(
    (spec: string, index: number, next: string) => {
      const list = (specValues[spec] ?? []).filter((v) => v.trim() !== "");
      const before = list[index] ?? "";
      //: ⚠️⚠️ **规格名不能重复**（用户口径 2026-10-22：「规格名不能重复添加校验」）。
      //:
      //: 原来只在 `commitSpecValue`（回车提交）拦 —— 但输入框是
      //: `onChange → setSpecValueAt` **逐字符写状态**，用户还没回车，重复值就已经
      //: 进了 `specValues` ⇒ 撞出两个同名值 ⇒ 笛卡尔积少一行 ⇒ 价格库存表行错位。
      //:
      //: ⇒ 在**写状态之前**拦：与同维度其它值重名就直接拒绝 + 红框提示。
      const typed = String(next ?? "").trim();
      if (typed && list.some((v, i) => i !== index && v.trim() === typed)) {
        setSpecValueHints((prev) => ({ ...prev, [spec]: "规格值名称不可重复" }));
        return;
      }
      renameSpecValue(spec, before, next);
      setSpecValueHints((prev) => ({ ...prev, [spec]: "" }));
    },
    [specValues, renameSpecValue],
  );

  /** 回车提交该规格值（校验：非空 / 不重复 / ≤60 字符 / ≤500 个）——真机口径。 */
  /** 当前「商品规格」里**已填的全部规格值**（去重；供顶部「优化规格名称」按钮判定）。 */
  const allSpecValues = useMemo(
    () =>
      Array.from(
        new Set(
          specTypes
            .flatMap((dim) => specValues[dim] ?? [])
            .map((v) => String(v ?? "").trim())
            .filter(Boolean),
        ),
      ),
    [specTypes, specValues],
  );

  /** 顶部「优化规格名称」按钮（用户口径 2026-10-22）：
   *  「点击也可以打开弹窗对商品 sku 规格名称进行优化」。
   *
   *  ⚠️ 与「导入后自动弹」的区别：这里**用户主动点**，所以不筛只超长的 ——
   *  用户可能就是想给某个名字换个更短的写法 ⇒ 有超长就优先列超长的，
   *  没有超长的也允许手动挑（这里为保证不误伤，空列表时按钮禁用）。 */
  const openOptimizeForCurrentSpecs = useCallback(() => {
    if (allSpecValues.length === 0) return;
    const tooLong = allSpecValues.filter((n) => isTooLong(n, SKU_NAME_MAX_CHARS));
    //: 有超长的 ⇒ 只列超长的（用户点这个按钮通常就是为了处理它们）；
    //: 一个都没超 ⇒ 列表全给（让用户自己挑要压哪个）。
    const picked = tooLong.length > 0 ? tooLong : allSpecValues;
    setSkuOptimizeItems(
      picked.map((n) => ({
        key: n,
        original: n,
        optimized: "",
        changed: false,
        truncated: false,
        reason: "",
      })),
    );
    setPendingSpecValue(null);
    setSkuOptimizeMax(SKU_NAME_MAX_CHARS);
    setSkuOptimizeOpen(true);
  }, [allSpecValues]);

  const commitSpecValue = useCallback(
    (spec: string, index: number) => {
      const cur = specValues[spec] ?? [];
      const raw = (cur[index] ?? "").trim();
      if (!raw) return;
      const list = cur.filter((v) => v.trim() !== "");

      const fail = (msg: string) => setSpecValueHints((prev) => ({ ...prev, [spec]: msg }));
      //: ⚠️⚠️ **口径必须与平台一致：按显示宽度（汉字计 2），不是码点数**
      //: （用户口径 2026-10-22：「商品规格组的输入框添加 **60 字符**长度校验，
      //:  超出则不允许进入下一步」；平台真机报错原文「规格名不能超过 30 个字
      //:  （60 个字符）」）。
      //: 旧写法 `Array.from(raw).length` 是**码点数** ⇒ 30 个汉字只算 30，
      //: 前端放行、发布时被平台拒（正是用户截图里那 4 条「variants」失败）。
      if (exceedsLimit(raw, SKU_NAME_MAX_CHARS)) {
        //: ⚠️⚠️ 用户口径 2026-10-22：「规格名称校验成功了，但是**没有优化规格名的弹窗**」
        //: + 「长度超出则**弹窗**展示旧规格名 / 新规格名 / 操作列」。
        //:
        //: ⇒ 超长时不是干报一句错了事，而是**把这一条直接抛进优化弹窗**：
        //:   用户在弹窗里点「优化」（调大模型）或手动改短，确认后用返回值替换该输入框。
        //: `pendingSpecValue` 记住「优化完要写回哪个规格的第几格」——
        //: 手动输入的规格值不经过导入链路，没有它写不回去。
        fail(
          `规格值长度上限为 ${SKU_NAME_MAX_CHARS} 个字符（汉字计 2），当前 ${displayWidth(raw)}`,
        );
        setPendingSpecValue({ spec, index });
        setSkuOptimizeItems([
          {
            key: raw,
            original: raw,
            optimized: "",
            changed: false,
            truncated: false,
            reason: "",
          },
        ]);
        setSkuOptimizeMax(SKU_NAME_MAX_CHARS);
        setSkuOptimizeOpen(true);
        return;
      }
      if (list.filter((v) => v !== raw).includes(raw)) {
        fail("规格值名称不可重复");
        return;
      }
      if (list.length >= SPEC_VALUE_MAX) {
        fail(`单个规格值的上限数量为 ${SPEC_VALUE_MAX} 个`);
        return;
      }
      //: 提交后再补一个新的空框（真机行为）
      setSpecValues((prev) => ({ ...prev, [spec]: [...list, ""] }));
      setSpecValueHints((prev) => ({ ...prev, [spec]: "" }));
    },
    [specValues],
  );

  /** 规格图选择弹窗的目标（用户口径：前缀点击 → 弹窗选**预检通过且选中的 SKU 图**）。 */
  const [skuPick, setSkuPick] = useState<{ spec: string; value: string } | null>(null);
  /** ⚠️ 用户口径（2026-10-10）：「被选中的规格图不能重复被选中」——
   *  汇总**其它**规格值（非当前编辑目标）已用掉的预检图 `rel_path`，在弹窗里禁用它们。
   *  同规格自身当前挂的那张**不**列入（允许再次选中 / 换成同图）。 */
  const precheckNamesInUse = useMemo(() => {
    const target = skuPick;
    const used = new Set<string>();
    for (const [spec, byValue] of Object.entries(specImages)) {
      for (const [value, img] of Object.entries(byValue)) {
        if (!img?.precheck_name) continue;
        if (target && spec === target.spec && value === target.value) continue;
        used.add(img.precheck_name);
      }
    }
    return [...used];
  }, [specImages, skuPick]);

  /** 只改第一组规格值的图（真机：仅第一组支持添加规格图）。 */
  /** **批量**写规格图 —— 一批只触发一次 setState。

   *  ⚠️ 用户口径 2026-10-22：「24 张图上传正常 30 秒以内肯定可以完成」。
   *  HAR 实测（127.0.0.1.har，92.5 秒全程）：每批 `upload-batch` 只有 **2.6 秒**，
   *  但批与批之间各空 **12~13 秒零网络请求** —— 空转不在网络上，在**渲染**：
   *  旧写法一批 8 张逐项调 `setSpecImage` ⇒ 8 次 `setSpecImages` ⇒
   *  8 × 456 行表格全量重渲染（3 批共 24 次）。改成下面这样一批一次，
   *  重渲染降到 3 次。**渲染次数才是批间空转的来源，不是上传。**
   */
  const setSpecImagesBatch = useCallback(
    (entries: Array<{ spec: string; value: string; img: ConfigMainImage | null }>) => {
      if (entries.length === 0) return;
      setSpecImages((prev) => {
        const next = { ...prev };
        //: ⚠️ 同 `setSpecImage`：规格名与值都 trim（查表侧拆名后也 trim）。
        for (const e of entries) {
          const sk = String(e.spec ?? "").trim();
          const group = { ...(next[sk] ?? {}) };
          const k = String(e.value ?? "").trim();
          if (e.img) group[k] = e.img;
          else delete group[k];
          next[sk] = group;
        }
        return next;
      });
    },
    [],
  );

  const setSpecImage = useCallback(
    (spec: string, value: string, img: ConfigMainImage | null) => {
      setSpecImages((prev) => {
        //: ⚠️ 2026-10-11：规格名 `spec` 也 trim —— 查表侧 `lookup_spec_image` 拆名后
        //: `spec.trim()`，写入侧不 trim 会在「规格名带空格」时 key 错位。
        const sk = String(spec ?? "").trim();
        const group = { ...(prev[sk] ?? {}) };
        const k = String(value ?? "").trim();
        if (img) group[k] = img;
        else delete group[k];
        return { ...prev, [sk]: group };
      });
    },
    [],
  );

  /** 点规格图前缀（或加号）→ **打开选择弹窗**（用户口径 2026-10-09）。
   *  ⚠️ 规则：**只从预检 SKU 图里选**，不弹本地文件框（用户明确「点击无响应」是因为
   *  先前没有 SKU 图时会走本地选择，交互看起来像没反应）。 */
  const pickSpecImage = useCallback(
    (spec: string, valueIndex: number) => {
      const filled = (specValues[spec] ?? []).filter((v) => v.trim() !== "");
      const value = filled[valueIndex];
      if (!value) {
        //: 没有规格值无从挂图 —— 提示而不是静默（否则就是「点击无响应」）。
        message.info("请先输入规格值，再选择规格图");
        return;
      }
      setSkuPick({ spec, value });
    },
    [specValues, message],
  );

  /** 手动上传**商品主图**（用户口径 2026-10-10：不再依赖「请先在上方『智能识图』区
   *  上传商品主图」那句提示，直接用图片上传组件）。 */
  const onPickMainImages = useCallback(
    async (files: File[]) => {
      if (!props.onImagesChange) return;
      setMainUploading(true);
      try {
        const added: ConfigMainImage[] = [];
        for (const file of files) {
          const img = await props.onUploadImageFile?.(file);
          if (img) added.push(img);
        }
        if (added.length === 0) {
          message.error("主图上传失败，请检查格式与大小后重试");
          return;
        }
        props.onImagesChange([...(props.images ?? []), ...added]);
        message.success(`已上传 ${added.length} 张商品主图`);
      } finally {
        setMainUploading(false);
      }
    },
    [props, message],
  );

  /** 手动上传**商品详情图**（用户口径 2026-10-10：用上传组件，不用按钮）。 */
  const onPickDetailImages = useCallback(
    async (files: File[]) => {
      if (!props.onDetailImagesChange) return;
      setDetailUploading(true);
      try {
        const added: ConfigMainImage[] = [];
        for (const file of files) {
          const img = await props.onUploadImageFile?.(file);
          if (img) added.push(img);
        }
        if (added.length === 0) {
          message.error("详情图上传失败，请检查格式与大小后重试");
          return;
        }
        props.onDetailImagesChange([...(props.detailImages ?? []), ...added]);
        message.success(`已上传 ${added.length} 张商品详情图`);
      } finally {
        setDetailUploading(false);
      }
    },
    [props, message],
  );

  /** 手动上传**商品视频**（用户口径 2026-10-10：用上传组件，不用按钮）。 */
  const onPickVideoFiles = useCallback(
    async (files: File[]) => {
      const file = files[0];
      if (!file) return;
      if (!props.onUploadVideoFile) {
        message.warning("当前环境不支持手动上传视频");
        return;
      }
      setVideoUploading(true);
      try {
        const img = await props.onUploadVideoFile(file);
        if (img) {
          props.onVideoChange?.(img);
          message.success(`视频已上传：${img.file_name}`);
        } else {
          message.error("视频上传失败，请检查格式与大小后重试");
        }
      } catch (error) {
        //: 失败**报原因**（不静默、不造假）—— 把后端给的中文原因带出来。
        const reason =
          error && typeof error === "object" && "message" in error
            ? String((error as { message?: unknown }).message ?? "")
            : "";
        message.error(reason || "视频上传失败");
      } finally {
        setVideoUploading(false);
      }
    },
    [props, message],
  );

  /** 从弹窗选中一张预检 SKU 图 → 上传 → 回显为该规格值的规格图。 */
  const chooseSkuImage = useCallback(
    async (rel_path: string, file_name: string) => {
      const target = skuPick;
      if (!target || !props.onUploadSkuImage) return;
      //: ⚠️ 二次防御（用户口径 2026-10-10：「被选中的规格图不能重复被选中」）：
      //: 弹窗已禁用已用图，但状态可能竞态 ⇒ 落值前再查一次，命中就拒绝并说明。
      const already = precheckNamesInUse.includes(rel_path);
      if (already) {
        message.warning("该规格图已被其它规格值使用，请另选一张");
        setSkuPick(null);
        return;
      }
      setSpecImageUploading((n) => n + 1);
      try {
        const img = await props.onUploadSkuImage(rel_path, file_name);
        //: 记下**源预检图**：`precheck_name` 作去重键（同一张图不能被两个规格值重复选中），
        //: `rel_path` 作提交时的相对路径（后端统一重新上传）。
        if (img) setSpecImage(target.spec, target.value, { ...img, precheck_name: rel_path, rel_path });
      } finally {
        setSpecImageUploading((n) => Math.max(0, n - 1));
        setSkuPick(null);
      }
    },
    [skuPick, setSpecImage, props, precheckNamesInUse, message],
  );

  /** **执行导入**：按用户确认的映射，把预检 SKU 行落成规格值 / 价格 / 库存 / 规格图。
   *
   *  用户口径（2026-10-11 最终版）：
   *    · 映射由用户确认（默认文本精确匹配），不猜；
   *    · 价格取第一步 `原价`（优先）→ 空则 `计算价格`；库存取 `库存`；`ID` ⇒ 商家编码；
   *    · 规格图上传**本地图列**那张，失败按既有重试配置重试；仍失败则**跳过上传**，
   *      但该行的规格名/值 + 价格/库存/商家编码**照常填**。
   *
   *  ⚠️ 实现要点：**先**把 `specTypes`/`specValues` 落好，价格表由既有的
   *  「规格值 → 价格及库存表」effect（笛卡尔积）**自动重建**；本函数再把
   *  价格/库存/商家编码按 `spec` 组合文本**对齐写回**（用 pendingFill 挂起，
   *  等价格表重建完成后由下面的 effect 落值）—— 直接写 priceRows 会被那次重建冲掉。
   */
  //: 导入耗时秒表（用户口径：确认后要「统计耗时」）。
  useEffect(() => {
    if (!importRunning) return;
    const t0 = performance.now();
    setImportElapsed(0);
    const id = window.setInterval(
      () => setImportElapsed(Math.round((performance.now() - t0) / 1000)),
      200,
    );
    return () => window.clearInterval(id);
  }, [importRunning]);

  //: 规格图上传的前端并发度。
  //: ⚠️ 真机 PUT 的并发由**后端**按 config 表 `xhs_image_upload_parallelism`（默认 5）
  //: 控制；这里只决定「同时提交几批」。两者取一致值最省事，故默认同为 5
  //: （前端拿不到运行时配置，写常量 + 后端仍是最终闸门，不会超发）。
  const SPEC_IMAGE_UPLOAD_CONCURRENCY = 5;

  const runSpecImport = useCallback(async () => {
    //: 每次开跑都**重置**中断标志（否则上一次的取消会毒到这一次）。
    importCancelRef.current = false;
    const rows = props.passedRows ?? [];
    const dims = (props.specAttributes ?? []).map((a) => ({
      name: a.name,
      id: a.id,
      customizable: a.customizable,
      values: a.values,
      options: a.options,
    }));
    //: 成对行 ⇒ 查表结构（`buildSkuFillPlan` 消费的是这个）。
    //: 价格库存的列映射由**用户配置**决定；行数据要按同一份映射重新导出
    //: （`props.passedRows` 是按默认映射算的，用户改了映射就必须重算）。
    //: ⚠️ **商品 SKU 映射的列** 必须一起传（2026-10-21）：SKU 名称不再单独配映射后，
    //: `passedSkuRows` 要靠它定位「一行一条 SKU」的列；不传 ⇒ 行集恒空
    //: ⇒ 用户实测的「没有『预检通过且已勾选』的 SKU 行，实际是存在的」。
    const specColumns = importMapping.map((r) => r.source).filter((x) => !!x.trim());
    const remapped =
      props.onRebuildRows?.(priceMapping, priceFallbackSource, specColumns) ?? rows;
    const plan = buildSkuFillPlan(
      remapped,
      dims,
      mappingFromRows(importMapping),
      priceMultiplier,
      //: ⚠️ 用户口径 2026-10-21：价格行的键 = **商品 SKU 映射那几列按行拼接**
      //: 出来的完整规格（「指定的列去重后就是对该组的 sku 规格」）。
      //: 不传它 ⇒ 退回「按维度值合并」⇒ 456 行只落 24 行价格（444 行丢失且不报错）。
      specColumns,
    );
    if (plan.dims.length === 0) {
      setImportDetail(
        `未导入：${plan.skipped.join("；") || "无可导入的规格"}`,
      );
      return;
    }
    setImportRunning(true);
    try {
      //: ⚠️ 用户口径：取消要**中断未导入的数据** —— 检查放在**一切写入之前**。
      //: 若放在清表之后，用户在开跑瞬间点取消会得到「历史被清空、新数据没写」
      //: 的最坏结果（数据丢失）。这里先看标志，**什么都不动**就退出。
      if (importCancelRef.current) {
        setImportDetail("已取消：未导入任何数据（历史数据保持不变）");
        return;
      }
      //: ⚠️ 用户口径 2026-10-11：「导入前存在的**历史相关数据需全部清除**」。
      //: 清的是「规格值 + 价格表」（都是上一次导入/手填的产物）；
      //: 主图/详情图/属性等**不属于本弹窗管辖**，一律不动。
      //:
      //: ⚠️ **顺序至关重要**：先挂起 fills，**再**清表 + 写规格。
      //: 三者都在同一次 React 批处理里 ⇒ 重建 effect 那一轮同时看到
      //: 「新规格（决定有哪些行）+ 挂起值（决定填什么）」⇒ 一次算对。
      //: 反序（先清后挂）会让重建先跑一轮、把挂起当空 ⇒ 行建好了但没值。
      //: ⚠️ **顺序至关重要**：先挂起 fills，**再**清表 + 写规格。
      //: 三者都在同一次 React 批处理里 ⇒ 重建 effect 那一轮同时看到
      //: 「新规格（决定有哪些行）+ 挂起值（决定填什么）」⇒ 一次算对。
      //: ⚠️ 清表走 `clearImportTargets`（**单一实现**，与「点选择重开」共用），
      //: 但传 `keepPending` —— 刚挂的新值不能被它清掉（那是「导入成功却一行没值」的成因）。
      setPendingSpecFills(plan.rows);
      clearImportTargets({ keepPending: true });
      //: ① 规格类型 + 规格值（合并去重，不覆盖用户已填的）。
      //: 逐维度落规格类型与值（上面已清空 ⇒ 这里是**纯写入**，不会再叠历史值）。
      const mergedTypes: string[] = [];
      const mergedValues: Record<string, string[]> = {};
      for (const dp of plan.dims) {
        mergedTypes.push(dp.dimName);
        mergedValues[dp.dimName] = dp.values.map((v) => v.value);
      }
      setSpecTypes(mergedTypes);
      setSpecValues(mergedValues);
      //: ③ 规格图：**本地图列**那张，**并发上传**（用户口径 2026-10-11：与自动加载
      //: 参数同一套并发能力，缩短导入耗时；并发数同样由 config 表
      //: `xhs_image_upload_parallelism` 在**后端**生效 —— 这里只负责并发提交，
      //: 真正打平台 PUT 的并发由 `upload-batch` 端点控制）。
      //:
      //: ⚠️ **取消语义必须保留**（用户口径「导入中点击取消，则中断未导入和未上传的
      //: 数据，停止执行」）：`importCancelRef` 仍是 `useRef`（不是 state —— 并发回调
      //: 里读 state 只能拿到闭包快照），并且**每个 worker 领活前先查标志** ⇒ 取消后
      //: 未领的项不再上传（已在途的几张让它跑完，不产生半截文件）。
      const total = plan.images.length;
      let ok = 0;
      let failed = 0;
      let done = 0;
      if (total === 0) setImportProgress("无规格图需要上传");
      const run = props.withUploadRetry;
      const upload = props.onUploadSkuImage;
      if (total > 0 && (!run || !upload)) {
        //: 没接上传能力时整批计入失败（与旧串行版同判据，不静默）。
        failed = total;
        done = total;
      } else if (total > 0 && props.onUploadSkuImagesBatch) {
        //: ✅ **批量：先拿齐所有凭证，再并发上传**（用户口径 2026-10-22：
        //: 「改为批量先获取所有图片的凭证，再上传」）。
        //:
        //: 根因：逐张走 `picture-space/upload` 时，**每张都要单独申请一次
        //: `upload_web_permit(count=1)`** ⇒ 24 张 = 24 次凭证 + 24 次 PUT = 48 次
        //: 串行网络往返 ⇒ 用户实测 **18/24 张耗时 307 秒**（约 17 秒/张）。
        //: 正常排队上传绝不可能这么慢 —— 慢在凭证申请这一跳，不在带宽。
        //:
        //: 批量端点（`picture-space/upload-batch`）的做法：
        //:   ① 先按 `_PERMIT_BATCH`（平台单次最多预分配 **14** 个 fileId）分批把凭证
        //:      **一次拿齐**，每个 fileId 记住自己那批的 addr/token；
        //:   ② 再按 `xhs_image_upload_parallelism`（默认 5）**服务端并发 PUT**。
        //: ⇒ 24 张只需 2 次凭证申请 + 5 路并发 PUT，而不是 48 次串行。
        //:
        //: ⚠️ 结果**与入参逐位对齐**（后端保证），逐项回填、失败不静默。
        //: ⚠️ 进度在整批返回后才更新（这是批量的固有代价）；为了不让用户以为
        //:   「卡死」，文案写清「正在上传」并由 `WaitMask` 每秒刷新已耗时。
        //: ⚠️ **分段批量**（用户口径 2026-10-22「改回批量上传」+ 2026-10-21
        //: 「进度不更新」两件事一起满足）：
        //:   · 一次 `await` 整批 ⇒ 期间零进度事件 ⇒ 用户看到「0/24 卡死」；
        //:   · 每段提交后立即落进度、图立即上屏 ⇒ 进度会动；
        //:   · 每段内部仍是**服务端并发 PUT**（`xhs_image_upload_parallelism` 默认 5）。
        //:
        //: 段大小取 8：实测单张 PUT 2.7~3.5 秒（真机 profile，见
        //: `.hermes/tmp/profile_publish2.py`），8 张 / 5 路并发 ≈ 5~6 秒一段
        //: ⇒ 24 张约 3 段、总耗时 ~20 秒（vs 顺序 64 秒）。
        //: ⚠️ 不要再退回「整批一次提交」：那是用户实测 307 秒卡死的形态。
        //: ⚠️⚠️ **与详情图完全一致**（用户口径 2026-10-22：「保持和详情图一致逻辑」）。
        //:
        //: 详情图那条路（`product-publish-wizard.tsx` 的 `uploadManyFromPackage`）
        //: 是**一次性把 19 张全交进去**：内部滑窗 12 路并发取字节 ⇒ 一次
        //: `upload-batch` 提交全部 ⇒ 服务端并发 PUT。HAR 实测详情图很快。
        //:
        //: 而 SKU 图这条被前端切成 3 批 × 8 张（`UPLOAD_CHUNK = 8`），每批
        //: 都要重新走一遍取字节 + 一次往返。HAR 实测（127.0.0.1.har，93 秒全程）：
        //:   · 每批 `upload-batch` 只有 **2.6 秒**；
        //:   · 但批与批之间各空 **12~13 秒零网络请求**；
        //:   · 开头还有 **50.7 秒**完全空档。
        //: ⇒ 分批本身就是要削掉的东西，不是「上传慢」。
        //: ⚠️ 不设上限：后端 `publish.py` 已放开 SKU 条数（用户口径「当前项目
        //: 后端不设置限制」），`upload-batch` 也支持一次多张（服务端自行分批 PUT）。
        const batchUpload = props.onUploadSkuImagesBatch;
        setImportProgress(`上传规格图 0/${total} 张（上传中…）`);
        const outcomes = await batchUpload(
          plan.images.map((it) => ({ rel_path: it.localRel, file_name: it.localFileName })),
        );
        //: 结果**与入参逐位对齐**（后端保证），失败不静默。
        //: ⚠️ **一次批量写**（不再逐项 `setSpecImage`）：逐项 setState 就是
        //: N × 456 行表格全量重渲染。
        const batchEntries: Array<{
          spec: string;
          value: string;
          img: ConfigMainImage | null;
        }> = [];
        outcomes.forEach((o, i) => {
          const item = plan.images[i];
          if (!item) return;
          if (o.ok && o.image) {
            ok += 1;
            batchEntries.push({ spec: item.dimName, value: item.value, img: o.image });
          } else {
            failed += 1;
          }
          done += 1;
        });
        setSpecImagesBatch(batchEntries);
        setImportProgress(`上传规格图 ${done}/${total} 张完成`);
      } else if (total > 0) {
        //: ⚠️⚠️ **顺序逐张上传**（用户口径 2026-10-21：「不要并发上传规格图了，
        //: 顺序上传」）——已删掉 `upload-batch` 分段并发那条路。
        //:
        //:  为什么顺序是对的：
        //:   · 批量端点一次 `await` 整批 ⇒ 期间**零进度事件**，用户看到
        //:     「0/24 卡死 80 秒」（用户截图为证）；
        //:   · 分段并发仍要按批等待，每批内部哪些成功、哪些失败用户看不见，
        //:     且失败时无法精确知道"卡在哪一张"；
        //:   · 逐张顺序 = 每张都有确定的前后顺序 ⇒ 进度**每张都动**、
        //:     失败立刻定位到具体规格值、取消后**下一张就不传**。
        //:
        //:  ⚠️ 用 `withUploadRetry`（带重试）+ `onUploadSkuImage`（单张）。
        //:  ⚠️ `importCancelRef` 每张前都查 ⇒ 取消后未传的项一张都不传
        //:    （用户口径「导入中点击取消，则中断未导入和未上传的数据」）。
        for (let i = 0; i < total; i += 1) {
          if (importCancelRef.current) break;
          const item = plan.images[i];
          setImportProgress(`上传规格图 ${done}/${total}（${item.value}）`);
          try {
            //: ⚠️ `withUploadRetry` 的签名是 `<T>(run: () => Promise<T|null>, isOk?)`
            //: —— 传的是**函数**，不是 (fn, args)。早先按 (fn, args) 写会直接
            //: tsc 报 `run is possibly undefined` 且运行期一次都传不出去。
            const image = await run!(() => upload!(item.localRel, item.localFileName));
            if (image) {
              ok += 1;
              setSpecImage(item.dimName, item.value, image);
            } else {
              failed += 1;
            }
          } catch {
            //: 单张失败不中断整批（其余规格图照传；失败数如实计入）。
            failed += 1;
          }
          done += 1;
          setImportProgress(`上传规格图 ${done}/${total}`);
        }
      }
      const cancelled = importCancelRef.current;
      const bits = [
        //: ⚠️ 用户报 2026-10-21：「展示的信息不准确」——
        //: 原文案只报 `dims[0]` 一个维度的值数（如「规格「规格/尺寸」19 个值」），
        //: 用户看不到**另一个维度**有多少值，也看不出分组顺序。
        //: ⇒ 所有维度逐个列出（顺序 = 用户映射顺序 = 价格表分组顺序）。
        `规格 ${plan.dims.map((d) => `${d.dimName} ${d.values.length} 个值`).join(" × ")}`,
        `${plan.rows.length} 行价格库存`,
        `规格图 ${ok} 张${failed > 0 ? `（失败 ${failed} 张，已跳过）` : ""}`,
        //: 用户口径：取消要**说清楚停在哪**（已传 N/M 张），否则用户不知道算不算完整。
        cancelled ? `已中断：规格图上传到 ${ok}/${total} 张` : "",
      ].filter(Boolean);
      //: ⚠️ 用户口径 ①「写明」：把映射关系与跳过原因一并显示（不静默）。
      setImportDetail([...bits, ...plan.notes, ...plan.skipped].join("；"));
      if (cancelled) {
        void message.warning("导入已被取消（已完成的规格与价格库存保留）");
      } else {
        //: ⚠️⚠️ **导入完成后不再自动弹优化窗**（用户口径 2026-10-22 最终版：
        //: 「导入数据后**不要在自动弹窗**优化规格名称，**只有用户手动触发**优化规格
        //: 名称按钮时才需要弹窗，**两部分逻辑拆分开**」）。
        //:
        //: 演化（原句保留，不静默删除）：
        //:   · 更早的版本在**入口**拦截弹窗 → 用户还没看到导入结果就被打断；
        //:   · 中间版本改成**导入收尾**自动弹 → 用户说「不要自动弹」；
        //:   · 现在：导入只负责导入，**超长提示走 message + 顶部按钮**，弹窗只由
        //:     用户主动点「优化规格名称」触发。
        //: ⚠️ 「超长」这件事仍要**说清楚**（不能因为不弹窗就静默）——
        //: 用一条 warning 文案点名条数，并指向那个按钮。
        const landed = Array.from(
          new Set(
            plan.dims
              .flatMap((d) => d.values.map((v) => String(v.value ?? "").trim()))
              .filter(Boolean),
          ),
        );
        const tooLong = landed.filter((n) => isTooLong(n, SKU_NAME_MAX_CHARS));
        void message.success(`已导入 ${plan.rows.length} 行规格数据`);
        if (tooLong.length > 0) {
          //: 只提示，不弹窗 —— 是否处理由用户点顶部「优化规格名称」决定。
          void message.warning(
            `有 ${tooLong.length} 个规格名超出 ${SKU_NAME_MAX_CHARS} 字符，点「优化规格名称」可批量压缩`,
          );
        }
      }
    } catch (err) {
      setImportDetail(`导入失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setImportRunning(false);
    }
  }, [props, importMapping, priceMapping, priceFallbackSource, priceMultiplier, setSpecImage]);

  /** ⚠️ **上报两份映射草稿**给向导（第一步删列时的二次确认要「点名」用）。
   *
   *  ⚠️ 依赖数组**必须带上 `props.onMappingDraftChange`** ——
   *  漏了的话回调里永远是首次渲染时那份（第一步拿到的是过期草稿，提示会说错人）。
   */
  useEffect(() => {
    props.onMappingDraftChange?.({
      specSources: importMapping.map((r) => r.source).filter((x) => !!x.trim()),
      priceSources: priceMapping.map((r) => r.source).filter((x) => !!x.trim()),
      //: ⚠️ 带上**完整映射行**（不是只有来源名单）—— 提交体要按 `target → source`
      //: 取列值。只给名单的话，取值那边仍只能猜列名（用户口径：全部使用映射关系）。
      priceMapping: priceMapping.map((r) => ({ ...r })),
      priceFallbackSource,
    });
  }, [importMapping, priceMapping, priceFallbackSource, props.onMappingDraftChange]);

  /** 规格值 → **价格及库存表**同步（用户口径 2026-10-09：「添加规格时价格及库存列表
   *  应同步添加」）。
   *
   * 按已填规格值做**笛卡尔积**生成行；已存在的行**保留已填的价格 / 库存**
   * （按 `key` 对齐，只增删不重置），这样「加一个规格值」只多一行、其余不动。 */
  useEffect(() => {
    const combos = spec_combinations(
      specTypes.map((name) => ({ name, values: specValues[name] ?? [] })),
    );
    const byKey = new Map(props.priceRows.map((r) => [r.key, r]));
    //: ⚠️ 需求 4（2026-10-11）：**只保留预选通过的 SKU 规格**。
    //: `c.spec` 就是 `规格名:值` 原样（与第一步 SKU 表的 `sku名称` 同形），
    //: 所以可以直接按集合比对。集合为空（没跑预检 / 表格没读到）时**不过滤** ——
    //: 「没数据」不等于「全不通过」，否则老链路（无预检产物）会一行规格都不剩。
    const allowed = props.passedSpecs;
    const visible = allowed && allowed.size > 0
      ? combos.filter((c) => allowed.has(String(c.spec ?? "").trim()))
      : combos;
    //: ⚠️ **挂起值在重建这一刻直接落上**（用户口径「导入后价格库存要有值」）。
    //: 旧实现把落值交给**另一个 effect**，而那两个 effect 的触发顺序/时机不由我们
    //: 控制（价格表要先回传外层再回传进来），实测会「导入成功但一行都没填上」。
    //: 现在重建与落值**在同一次计算里**完成 ⇒ 不存在时序窗口。
    //: ⚠️⚠️⚠️ 用户报 2026-10-21：「导入 sku 数据的价格及库存，映射关系配置了，
    //: 但**数据未填充**」（截图：价格/库存/市场价/商家编码 全空）。
    //:
    //: **真根因：两侧的 `spec` 格式不同，一条都匹配不上。**
    //:  · `pendingSpecFills`（来自 `buildSkuFillPlan`）的 spec 是
    //:    **行级拼接原文**（`skuSpecTextOf`）⇒ `奇遇童话01 / 尺寸/图案任意定制`
    //:  · `combos`（来自 `spec_combinations`）的 spec 是
    //:    **带维度名前缀** ⇒ `颜色分类:奇遇童话01 / 款式:尺寸/图案任意定制`
    //: ⇒ `pending.get(c.spec)` 恒 `undefined` ⇒ 每行都走 `return base`（全空），
    //:   而界面看起来「导入成功、规格都建好了」，只是值全是空的。
    //:
    //: ⇒ 判据改为**按值序列比对**（把维度前缀剥掉再比），两侧都能对上：
    //:   `颜色分类:X / 款式:Y` → `X / Y` === `X / Y`。
    //: ⚠️ 同时保留**原始 spec 精确命中**优先（万一将来两侧统一了格式，仍走快路径）。
    //: ⚠️ 匹配走 lib 的**单一实现** `lookupSpecFill`（可单测、可变异）。
    //: 页面内再写一份「剥前缀 + 两次 find」等于两份判据，迟早漂移。
    const pendingExact = new Map(pendingSpecFills.map((f) => [f.spec, f]));
    const next: PriceRow[] = visible.map((c) => {
      //: 先用原串精确命中；不行再按「剥掉维度前缀的值序列」命中。
      const fill = lookupSpecFill(pendingSpecFills, c.spec);
      const existing = byKey.get(c.key);
      const base: PriceRow = existing
        ? { ...existing, spec: c.spec }
        : {
            key: c.key,
            spec: c.spec,
            price: null,
            stock: null,
            market_price: null,
            barcode: "",
            seller_code: "",
          };
      if (!fill) return base;
      //: 导入是「以预检数据为准」的重建 ⇒ **覆盖**（不是只填空位）。
      return {
        ...base,
        price: fill.price,
        market_price: fill.marketPrice,
        //: **当前库存 / 改后库存**分开落（用户口径 2026-10-21：两列都要能映射）。
        current_stock: fill.currentStock ?? null,
        stock: fill.stock,
        seller_code: fill.sellerCode,
        //: **规格ID**（平台生成物，只透传用户映射来的值；没配就空串）。
        spec_id: fill.specId ?? "",
        //: ⚠️ 条形码也**按映射**写（用户口径 2026-10-15：下拉漏了它 ⇒ 配不出来）。
        barcode: fill.barcode,
      };
    });
    //: 落过值就把挂起清掉（否则每次外层回传都重写一遍，用户手改价会被反复覆盖）。
    if (pendingSpecFills.length > 0) {
      const appliedAll = visible.every((c) => !!lookupSpecFill(pendingSpecFills, c.spec) || true);
      if (appliedAll) setPendingSpecFills([]);
    }
    //: ⚠️ `next` 为空要**分两种状态**（用户报障 2026-10-15：「所有商品规格全部取消后
    //:   价格及库存列表关联的数据**未清空**」）：
    //:
    //:   · **`specTypes.length === 0`（用户把规格全取消了）⇒ 必须清空**：
    //:     没有规格 = 没有 SKU 组合 = 价格库存表**不该留任何行**。旧实现一律
    //:     `return` ⇒ 外层 `props.priceRows` 保留上一轮的 4 行 ⇒ 用户看到
    //:     「规格全没勾，价表还占着 4 行带值的行」（本次报障）。
    //:   · **`specTypes.length > 0` 但 `visible` 为空（规格勾了、值还没填，
    //:     或被 `passedSpecs` 过滤光）⇒ 不清**：此时是「过程态」，外层可能给了
    //:     一行裸价占位（`default`），清掉会让页面价表**闪空**。
    if (next.length === 0) {
      if (specTypes.length === 0 && props.priceRows.length > 0) {
        //: 清空（只在真有余行时上抛 —— 否则每次渲染都上抛空数组，自触发死循环）。
        props.onPriceRowsChange([]);
      }
      return;
    }
    //: 内容相同就不上抛（否则 effect 自触发死循环）。
    const same =
      next.length === props.priceRows.length &&
      next.every((r, i) => r.key === props.priceRows[i].key && r.spec === props.priceRows[i].spec);
    if (!same) props.onPriceRowsChange(next);
  }, [specTypes, specValues, props.priceRows, props.onPriceRowsChange, props.passedSpecs, pendingSpecFills]);

  /** 尺码表弹窗（真机：尺码信息 = 助手里的可点模块，点开才与后端交互）。 */
  const [sizeTableOpen, setSizeTableOpen] = useState(false);
  /** 商品资质弹窗（同上）。 */
  const [qualificationOpen, setQualificationOpen] = useState(false);
  /** 现货+预售模式的发货时效（默认 3天）。 */
  const [shipAging, setShipAging] = useState<string>(SHIP_AGING_OPTIONS[0]);
  /** 预售批次是否展开（现货+预售模式）。 */
  const [presaleBatchOpen, setPresaleBatchOpen] = useState(true); // 用户口径：默认展示
  /** 按规格模式下，价格表「发货时间」列的预售方式。 */
  const [presaleColumnKind, setPresaleColumnKind] = useState("时段预售");
  /** 按规格模式下，价格表「发货时间」列的天数。 */
  const [presaleColumnDays, setPresaleColumnDays] = useState<number | null>(null);

  /** 切换发货模式 —— 2026-10-13 用户口径「移除切换时温馨提示弹窗」：
   *  直接生效，且**不重置库存等 SKU 信息、属性参数不做关联**（只改 UI 列）。 */
  const requestDeliveryMode = useCallback(
    (next: string) => {
      if (next === deliveryMode) return;
      setDeliveryMode(next);
    },
    [deliveryMode],
  );

  /** 规格类型**上移 / 下移**（真机：≥2 个规格时行尾才有）。 */
  const moveSpecType = useCallback(
    (index: number, delta: number) => {
      const next = [...specTypes];
      const j = index + delta;
      if (j < 0 || j >= next.length) return;
      [next[index], next[j]] = [next[j], next[index]];
      setSpecTypes(next);
    },
    [specTypes],
  );

  /** 切换规格类型 —— 上限 2，超限不生效。
   *
   *  ⚠️ **取消勾选要连带清除该规格的「全部 SKU 数据」**（用户口径 2026-10-15）：
   *  > 「当商品规格**切换**时，被切换到的规格类型对应的数据**也应同步被清除**」
   *  > 「该规格下**全部 sku 数据**，包括商品规格与商品规格关联的**价格库存表中的关联数据**」
   *
   *  ⚠️ 旧实现只 `setSpecTypes(filter)` —— **只摘掉类型名**，而该规格的
   *  规格值 / 规格图 / 价格库存行**全留着** ⇒ 用户看到「已经取消勾选了，
   *  规格值还在、价格表还占着行」（数据残留 + 表里出现对不上的行）。
   *  ⚠️ 三块一起清，否则会留下**孤儿数据**：
   *    · `specValues[name]` —— 该规格的值；
   *    · `specImages[name]` —— 按「维度+值」存的图（不清 ⇒ 下次同名维度**复用到旧图**）；
   *    · 价格库存行 —— 由重建 effect 按新 `specTypes` 重算笛卡尔积，
   *      被切走的维度自然**不再参与组合** ⇒ 那批行消失（**不需要手工删行**，
   *      手工删反而会与重建 effect 打架）。
   */
  const toggleSpecType = useCallback(
    (name: string, checked: boolean) => {
      if (checked) {
        if (specTypes.length >= 2) return;
        setSpecTypes([...specTypes, name]);
        return;
      }
      setSpecTypes(specTypes.filter((x) => x !== name));
      //: ⚠️ **连带清该规格的 sku 数据**（规格值 + 规格图）。
      //: 价格库存表**不用手工清** —— 重建 effect 按新 `specTypes` 重算笛卡尔积，
      //: 被切走的维度不再参与组合 ⇒ 那批行自然消失（手工删会与 effect 打架）。
      setSpecValues((prev) => {
        if (!(name in prev)) return prev;
        const next = { ...prev };
        delete next[name];
        return next;
      });
      setSpecImages((prev) => {
        if (!(name in prev)) return prev;
        const next = { ...prev };
        delete next[name];
        return next;
      });
      //: ⚠️ **规格图开关**也要清（`specImageEnabled`）—— 不清的话下次勾回同名维度，
      //: 开关会**继承上次的状态**，用户看到「新勾的规格图开关是开的」（他以为默认关）。
      setSpecImageEnabled((prev) => {
        if (!(name in prev)) return prev;
        const next = { ...prev };
        delete next[name];
        return next;
      });
    },
    [specTypes],
  );

  const [shortTitleLoading, setShortTitleLoading] = useState(false);
  const [brand] = useState<string>("无品牌"); // 用户口径：品牌固定「无品牌」
  const [desc, setDesc] = useState("");
  const [showPlayback, setShowPlayback] = useState(false);
  const [logisticsType, setLogisticsType] = useState<string>(LOGISTICS_TYPE_LABELS.logistics);
  /** 该类目是否支持「无物流发货」（平台按类目下发；`undefined`=未下发 ⇒ 兜底展示）。 */
  const [supportNoLogistics, setSupportNoLogistics] = useState<boolean | undefined>(undefined);
  /** 定时开售的**开售前价格保密**（真机 `priceConfidential`，默认 false=不保密）。 */
  const [priceConfidential, setPriceConfidential] = useState(false);
  /** 价格及库存表：**规格名称搜索**（用户口径 2026-10-09）。 */
  const [specKeyword, setSpecKeyword] = useState("");
  /** 搜索过滤后的价格行（批量修改只作用于可见行）。 */
  const filteredPriceRows = useMemo(() => {
    const kw = specKeyword.trim().toLowerCase();
    if (!kw) return props.priceRows;
    return props.priceRows.filter((r) => (r.spec ?? "").toLowerCase().includes(kw));
  }, [props.priceRows, specKeyword]);
  /** 取某价格行的**规格图**（该行第一个规格值在第一组的规格图；没有 ⇒ null）。 */
  const specImageForRow = useCallback(
    (row: PriceRow): ConfigMainImage | null => {
      const first = specTypes[0];
      if (!first) return null;
      const firstValue = (row.spec ?? "").split(" / ")[0]?.split(":")[1]?.trim();
      if (!firstValue) return null;
      return specImages[first]?.[firstValue] ?? null;
    },
    [specTypes, specImages],
  );
  const [saleTime, setSaleTime] = useState<string>(SHELF_TIME_OPTIONS_CREATE[0]);
  /** 定时开售的**开售时间**（真机 `expectedPurchaseTime`；规则 min=now+1h / max=now+30d）。 */
  const [expectedPurchaseTime, setExpectedPurchaseTime] = useState<Dayjs | null>(null);
  /** 售后服务：用户**主动勾选**的规则 type 集合（平台强制项由 `must_support` 单独管）。 */
  const [supportedTypes, setSupportedTypes] = useState<string[]>([]);
  /** 售后服务规则（平台按类目下发；缺省空数组 ⇒ 页面提示先选类目）。 */
  const afterSaleRules = props.afterSaleRules ?? [];
  const [englishName, setEnglishName] = useState("");
  //: ⚠️ 属性值**受控于外层**（自动加载第 8 步要写它）；外层没传时退回本地 state。
  const [propValuesLocal, setPropValuesLocal] = useState<Record<string, unknown>>({});
  const propValues = props.propertyValues ?? propValuesLocal;
  const setPropValues = props.onPropertyValuesChange ?? setPropValuesLocal;

  /** **把可提交 / 可预览的配置上报给向导**（用户口径 2026-10-10：提交与第三步预览
   *  都要用「第二步配置的完整参数」）。
   *
   *  ⚠️ 本组件在点「下一步」时会卸载（`{step === 1 && <PublishConfigStep2/>}`）
   *  ⇒ 内部 state 全丢 —— 这是「提交体缺配置」与「第三步只显示未选」的共同根因。
   *  快照只在**变化时**上报（依赖数组里都是原始值 / 浅比较得住的引用）。 */
  useEffect(() => {
    props.onConfigChange?.({
      deliveryMode,
      //: ⚠️⚠️ **物流类型**（用户口径 2026-10-09「物流类型应和类目有直接关系」）——
      //: 此前只在组件内 `useState`，**从未上报** ⇒ 后端读不到 ⇒ 提交体沿用骨架
      //: 默认值 ⇒ 平台报「deliveryMode — 商品不支持设置无物流发货」。
      //: 后端按它算 `deliveryMode`（物流配送=0 / 无物流发货=1），与发货模式无关。
      logisticsType,
      shipTime,
      presaleKind,
      presaleDays,
      shipAging,
      saleTime,
      priceConfidential,
      specTypes,
      specValues,
      specImageCounts: Object.fromEntries(
        Object.entries(specImages).map(([k, v]) => [k, Object.keys(v).length]),
      ),
      //: **规格图远程 URL**（`规格名 → 规格值 → 上传后的远程地址`）—— 供第三步预览规格图。
      //: ⚠️ 用户口径（2026-10-10）：第三步 SKU 列表要展示规格图（与第二步表头一致），
      //: 预览必须用上传成功返回的远程地址（不是本地 object URL）。
      specImageUrls: Object.fromEntries(
        Object.entries(specImages).map(([spec, byValue]) => [
          spec,
          Object.fromEntries(
            Object.entries(byValue)
              .map(([value, img]) => [value, img?.url || img?.local_url || ""] as const)
              .filter(([, url]) => url !== ""),
          ),
        ]),
      ),
      //: **规格图完整发布体材料**（2026-10-10 补）：提交时 `skuData[].specImage` 直接用。
      specImageMaterials: Object.fromEntries(
        Object.entries(specImages).map(([spec, byValue]) => [
          spec,
          Object.fromEntries(
            Object.entries(byValue).filter(([, img]) => !!img),
          ),
        ]),
      ),
      //: **规格图相对路径**（`规格名 → 规格值 → 预检包相对路径`）—— 供向导填 SKU 的 `image_path`。
      //: ⚠️ 只带**有源预检路径**的（手动本地上传的规格图没有 `rel_path` ⇒ 跳过）。
      specImagePaths: Object.fromEntries(
        Object.entries(specImages).map(([spec, byValue]) => [
          spec,
          Object.fromEntries(
            Object.entries(byValue)
              .map(([value, img]) => [value, String(img?.rel_path ?? "")] as const)
              .filter(([, rel]) => rel !== ""),
          ),
        ]),
      ),
      desc,
      showPlayback,
      englishName,
      supportedTypes,
      afterSaleNames: afterSaleRules
        .filter((r) => r.must_support || supportedTypes.includes(r.type))
        .map((r) => r.name)
        .filter(Boolean),
      //: ⚠️ 报**生效值**（显式选择 ?? 平台默认回落）—— 用户实测「物流模板已选但展示未选」：
      //: 只报 `logisticsPlanId`（用户是否**点过**）会漏掉「默认回落」这条路。
      logisticsPlanId: selectedShippingTemplate,
      freightTemplateId: effectiveFreightTemplateId,
      logisticsPlanName:
        props.logisticsTemplates.find((t) => t.plan_id === selectedShippingTemplate)?.name ?? "",
      freightTemplateName:
        props.shippingTemplates.find((t) => t.template_id === effectiveFreightTemplateId)?.name ?? "",
      //: 商品属性：从**字段定义 + 已填值**构造（只带已填的；值名 → value_id 若平台给了）。
      //:
      //: ⚠️⚠️ **「货号」必须排除在外**（用户报障 2026-10-15：「相同商品信息和类目，
      //:   平台后台其他属性下存在货号，本地项目上传提示**无货号属性**」）。
      //:   · 「货号」是前端**自己合成**的 UI 项（平台 `desc_properties` **不下发**它，
      //:     见 `lib/publish-property-fields.ts` 的 `buildItemNoProperty`）；
      //:   · 它走**顶层字段** `body["articleNo"]`（真机 `itemData[0].articleNo`）；
      //:   · 塞进 `attributes[]` ⇒ 平台把它当成一个 **`attributeId` = `articleNo`
      //:     的类目属性**，而类目里**不存在**这个属性 ⇒ 报
      //:     「当前类目不存在[货号]属性，请刷新页面后再试」。
      //:   ⚠️ 判据用 `withoutItemNo`（id **或** 名）而不是只比 id：平台某些类目会把
      //:     属性归一化成非 `articleNo` 的 id、名字仍叫「货号」⇒ 只比 id 会漏。
      //: ⚠️ 真机形状（千帆 bundle `8091.31a2a571.js` 逐字逆出，2026-10-10）：
      //:   · **多选属性**（`attributeValueType === 2`）⇒ **一条记录**，值放
      //:     `attributeValueList: [{attributeValue, attributeValueId}]`（数组，**最多 3 个**）
      //:   · **单选属性** ⇒ 标量 `attributeValue` + `attributeValueId`
      //: 早期实现「**每个选中值各推一条**」⇒ 多选值被当成多条属性 ⇒ 真机报
      //: 「attributes - **多选属性最多设置3个属性值**」（用户截图 2026-10-10）。
      attributes: [...(props.keyProperties ?? []), ...withoutItemNo(props.otherProperties ?? [])].flatMap(
        (f): PublishAttributeSnapshot[] => {
          //: eslint-disable-next-line @typescript-eslint/no-unused-vars
        const raw = propValues[f.id];
        const list = (Array.isArray(raw)
          ? raw
          : raw === undefined || raw === null || raw === ""
            ? []
            : [raw]
        ).filter((one) => String(one).trim() !== "");
        if (list.length === 0) return [];
        //: 每值取平台 `valueId`（缺就不带，**不造假**）。
        const asPair = (one: unknown) => {
          const hit = (f.values ?? []).find((x) => x.name === String(one));
          return {
            attributeValue: String(one),
            ...(hit?.value_id ? { attributeValueId: hit.value_id } : {}),
          };
        };
        //: 多选：一条记录 + `attributeValueList`（上限 3，与下拉的 `MULTI_SELECT_MAX` 同源）。
        if (f.kind === "multi" || list.length > 1) {
          return [{
            attributeId: f.id,
            attributeName: f.name,
            //: ⚠️ 多选真机 `attributeValueType: 2`（`propertyInputType` 归一后的 MULTI）。
            attributeValueType: 2,
            attributeValueList: list.slice(0, MULTI_SELECT_MAX).map(asPair),
          }];
        }
        //: 单选：标量（真机 `attributeValueType: 1`）。
          return [{
            attributeId: f.id,
            attributeName: f.name,
            attributeValueType: 1,
            ...asPair(list[0]),
          }];
        },
      ),
      //: ⚠️ **严格按第二步勾选的规格**（用户口径 2026-10-10）：只带用户勾了的维度，
      //: 未勾选的就算 SKU 名里出现也不提（否则会带出用户没启用的规格）。
      specAttributes: (props.specAttributes ?? []).filter((x) => specTypes.includes(x.name)),
      //: **多件装打包**规格（用户口径 2026-10-10）。
      packageBundles,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    deliveryMode, shipTime, presaleKind, presaleDays, shipAging, saleTime,
    priceConfidential, specTypes, specValues, specImages, desc, showPlayback,
    englishName, supportedTypes, logisticsPlanId, freightTemplateId, packageBundles,
  ]);

  /** 价格表列（真机 bundle 列名映射：含发货模式相关的增删列）。
   *
   * ⚠️ 用户口径 2026-10-09：「价格及库存表格应支持编辑，**规格列不支持**，
   * 在此基础上添加**批量编辑**操作，参考预选 sku 表 tab 下的批量操作功能」。
   * ⇒ 除「规格」列外每格可编辑（写回 `props.priceRows`）；批量条按列套规则。 */
  /** **价格及库存表的列**（真机 CDP 抓包，设计 24 §23）。
   *
   *  ⚠️⚠️ 用户口径 2026-10-21：「价格及库存的表格列**不是固定的**，可能根据类目或
   *  商品规格变化」+「第一个商品规格为**主-总-分**的结构表格」。
   *
   *  真机表头（`6a55be4d…` 商品实读）：
   *    颜色分类 | 适用床尺寸 | 行号 | 规格图 | 售价* | 当前库存 | 改后库存 | 市场价 | 规格ID | 条形码 | 商家编码 | 操作
   *  ⇒ 前三段：**规格维度（1~10 个，名字由类目定）** → 行号 → 规格图 → 固定价格块。
   *
   *  旧实现只有一列「规格」（整串文本），用户看不出「哪几行属于同一个颜色」，
   *  也无法按维度核对。现在按维度拆列 + 第一维度 `rowSpan` 合并（= 主-总-分）。
   *
   *  ⚠️ 平台生成的列（行号 / 规格ID / 规格图 / 操作）**不可编辑**，只回显。
   */
  const priceColumns = useMemo(() => {
    const dims = specTypes.filter((d) => (specValues[d] ?? []).length > 0);
    //: ⚠️ 用 antd 自己的列类型（`ColumnsType<PriceRow>[number]`）而不是手写 `Col`：
    //: 手写类型会让 `onCell` 的签名与 antd 的 `GetComponentProps`（第二参 `index?`）
    //: 不兼容，tsc 报一长串不匹配。交给推断最稳。
    type Col = ColumnsType<PriceRow>[number];
    //: ① **规格维度列**（动态个数）。用 `spec` 里 `维度名:值 / 维度名:值` 反解取值 ——
    //:    `spec` 是本项目全链路唯一的组合标识（预检 / 导入 / 提交都靠它）。
    const dimOf = (row: PriceRow, dim: string): string => {
      for (const part of String(row.spec ?? "").split(" / ")) {
        const at = part.indexOf(":");
        if (at >= 0 && part.slice(0, at).trim() === dim) return part.slice(at + 1).trim();
      }
      return "";
    };
    const dimCols: Col[] = dims.map((dim, di) => ({
      title: dim,
      key: `dim-${dim}`,
      //: 真机列宽 135px（设计 24 §23 实测 colgroup）。
      width: 135,
      //: antd 原生对齐（不再写 CSS 覆盖 th/td 的 text-align）。
      align: "left" as const,
      //: 第一维度固定在最左（内容一致即可：横向滚动时仍能看出属于哪一组）。
      fixed: di === 0 ? ("left" as const) : undefined,
      render: (_: unknown, row: PriceRow) => {
        const v = dimOf(row, dim);
        const enabled = row.available !== false;
        return (
          <Space size={6}>
            <span>{v}</span>
            {/* 启用/停用入口只放在**最后一维**（每行一个决定），否则同组多个链接重复 */}
            {di === dims.length - 1 && (
              <Tag
                color={enabled ? "success" : "default"}
                style={{ marginInlineEnd: 0, fontSize: 12, lineHeight: "18px" }}
              >
                {enabled ? "已启用" : "不启用"}
              </Tag>
            )}
          </Space>
        );
      },
      //: **主-总-分**：第一维度按值分组，组首 `rowSpan = 组内行数`。
      //: ⚠️ 只看第一维（`di === 0`）：平台就是这么合并的（实测 rowspan=8）。
      onCell:
        di === 0
          ? (row: PriceRow, index?: number) => {
              const at = index ?? 0;
              //: 第 i 行是否是**同组的第一行**（值变了就是新组）。
              const isGroupStart =
                at === 0 ||
                dimOf(props.priceRows[at - 1], dim) !== dimOf(row, dim);
              if (!isGroupStart) return { rowSpan: 0 }; //: 0 = 被上面那格吃掉
              let span = 1;
              while (
                at + span < props.priceRows.length &&
                dimOf(props.priceRows[at + span], dim) === dimOf(row, dim)
              ) {
                span += 1;
              }
              return { rowSpan: span };
            }
          : undefined,
    }));

    /*: ⚠️ 用户口径 2026-10-21：「**价格及库存应表头全部居中**」。
     *
     *  `align` 会**同时**作用于表头 th 与单元格 td —— 用户只要求表头居中，
     *  单元格（价格/库存是右对齐的数值）保持原样。
     *  ⇒ 用 `onHeaderCell` 单独给 th 打 `style.textAlign`（antd 原生能力，
     *    不写 CSS 覆写），并统一补到**所有列**（含规格维度 / 行号 / 规格图 / 操作）。
     */
    const centerHeader = <T,>(col: T): T => ({
      ...col,
      onHeaderCell: () => ({ style: { textAlign: "center" as const } }),
    });

    const base: Col[] = [
      ...dimCols,
      //: ② 行号（平台生成，1..N 连续）。
      {
        title: "行号",
        key: "rowNo",
        width: 55,
        render: (_: unknown, __row: PriceRow, index: number) => (
          <span style={{ color: "rgba(0,0,0,.45)" }}>{index + 1}</span>
        ),
      },
      //: ③ 规格图：**只在第一维度组首行显示一张**（真机：一组一张，不是每行一张）。
      {
        title: "规格图",
        key: "specImage",
        width: 88,
        //: ⚠️⚠️ **规格图必须按第一维度合并多行**（用户口径 2026-10-21，截图为证：
        //: 千帆的规格图一格跨 6 行，我方每行各画一格 ⇒ 视觉上完全不是一张表）。
        //: 旧实现只在 `render` 里 `return null`（只清空内容）⇒ **单元格仍在**，
        //: 边框、斑马纹、行高都照旧 ⇒ 看起来就是「每行一个空格子」。
        //: ⇒ 合并必须由 `onCell` 返回 `rowSpan` 完成（组首行 = 组内行数，其余 = 0）。
        onCell: (_row: PriceRow, index?: number) => {
          const firstDim = dims[0];
          const at = index ?? 0;
          if (!firstDim || at < 0 || at >= props.priceRows.length) return { rowSpan: 1 };
          const cur = dimOf(props.priceRows[at], firstDim);
          const prevSame =
            at > 0 && dimOf(props.priceRows[at - 1], firstDim) === cur;
          if (prevSame) return { rowSpan: 0 };
          let span = 1;
          for (let i = at + 1; i < props.priceRows.length; i += 1) {
            if (dimOf(props.priceRows[i], firstDim) !== cur) break;
            span += 1;
          }
          return { rowSpan: span };
        },
        render: (_: unknown, row: PriceRow, index: number) => {
          //: 取该行第一维度的值去找规格图（`specImages[维度][值]`）。
          const firstDim = dims[0];
          const firstVal = firstDim ? dimOf(row, firstDim) : "";
          const img = firstDim && firstVal ? specImages[firstDim]?.[firstVal] : undefined;
          //: `rowSpan: 0` 的行 antd 不会渲染本单元格 ⇒ 这里返回什么都不会上屏；
          //: 保留判据是为了「万一 rowSpan 失效」时不出现重复图（防御，不是主逻辑）。
          const prevSame =
            index > 0 &&
            firstDim &&
            dimOf(props.priceRows[index - 1], firstDim) === firstVal;
          if (prevSame) return null;
          return img?.url ? (
            <img src={img.url} alt="" style={{ width: 40, height: 40, objectFit: "cover", borderRadius: 4 }} />
          ) : (
            <span style={{ color: "rgba(0,0,0,.3)", fontSize: 11 }}>无图</span>
          );
        },
      },
      //: ④ 固定价格块（与平台同序）。
      { title: "售价", dataIndex: "price", key: "price", align: "right" as const, width: 150 },
      //: `当前库存` = 线上现值，**只读**（null ⇒ 显示 `-`，不造 0）。
      //: **当前库存**（千帆固定列，宽 200）——用户口径 2026-10-21：
      //: 「当前库存…均必须配置」⇒ 它也能被映射；未映射时显示 `-`（不造假）。
      { title: "当前库存", dataIndex: "current_stock", key: "current_stock", align: "right" as const, width: 200 },
      { title: "改后库存", dataIndex: "stock", key: "stock", align: "right" as const, width: 130 },
      { title: "市场价", dataIndex: "market_price", key: "market_price", align: "right" as const, width: 150 },
      //: **规格ID**（千帆固定列 `规格ID`，宽 230）——用户口径 2026-10-21：
      //: 「缺少规格ID、条形码、商家编码…价格及库存固定列必须保持一致」。
      //: ⚠️ 平台侧它是**生成物**；我方只在用户把某列映射到它时才显示值，
      //: 否则显示 `-`（不造假、不生成假 ID）。
      { title: "规格ID", dataIndex: "spec_id", key: "spec_id", width: 230 },
      { title: "条形码", dataIndex: "barcode", key: "barcode", width: 200 },
      { title: "商家编码", dataIndex: "seller_code", key: "seller_code", width: 164 },
      //: **操作列**（真机是 `Switch` 开关 + 操作区，宽度 150）——用户口径
      //: 「价格及库存表应完全与千帆后台除动态列以外保持一致」。
      //: 开关语义 = `skuData[].available`（停用后消费者看不到该 SKU）。
      {
        title: "操作",
        key: "operate",
        width: 150,
        render: (_: unknown, row: PriceRow) => {
          const enabled = row.available !== false;
          return (
            <Space size={4}>
              <Switch
                size="small"
                checked={enabled}
                onChange={(next) =>
                  props.onPriceRowsChange(
                    props.priceRows.map((r) =>
                      r.key === row.key ? { ...r, available: next } : r,
                    ),
                  )
                }
              />
              <span style={{ fontSize: 12, color: "rgba(0,0,0,.65)" }}>
                {enabled ? "上架" : "下架"}
              </span>
            </Space>
          );
        },
      },
    ];
    //: 真机：现货+预售模式多一列「发货时效」；按规格模式多一列「发货时间」。
    if (deliveryMode === DELIVERY_MODES[2]) {
      base.push({ title: "发货时效 *", dataIndex: "shipAging", key: "shipAging", width: 120 });
    }
    if (deliveryMode === DELIVERY_MODES[3]) {
      base.push({ title: "发货时间 *", dataIndex: "delivery", key: "delivery", width: 160 });
    }
    const numeric = new Set(["price", "current_stock", "stock", "market_price"]);
    //: 只读列（平台生成 / 线上现值）—— 不渲染输入框。
    const readOnly = new Set(["rowNo", "specImage"]);
    //: ⚠️⚠️ 用户口径 2026-10-21：「**固定列预设需要用户配置映射关系**」——
    //: 即价格块的列**不是写死全显示**，而是「用户在导入弹窗里配了哪几个映射，
    //: 这里就出现哪几列」。没配的列不出现（配了才有意义，不配全是空列噪声）。
    //:
    //: 判据：`priceMapping` 里出现过的 `target` 才保留。
    //: ⚠️ `current_stock` / 行号 / 规格图 / 操作 是平台自身字段，不参与映射 ⇒ 恒显示。
    const mappedTargets = new Set(
      priceMapping.map((r) => String(r.target ?? "").trim()).filter(Boolean),
    );
    //: 映射里的 target 与列 `key` 同名（price / market_price / stock / barcode / seller_code）。
    //: ⚠️⚠️ 用户口径 2026-10-21（截图为证）：「**缺少列** … **与千帆后台保持一致**」——
    //: 千帆价格及库存表的**固定列恒在**，不因「用户还没配映射」就消失：
    //:   行号 / 规格图 / 售价 / 当前库存 / 改后库存 / 市场价 / 规格ID / 条形码 / 商家编码
    //: ⇒ 只有**售价 / 当前库存 / 改后库存 / 市场价**受映射控制（它们本来就是必配，恒有值）；
    //:   **规格ID / 条形码 / 商家编码 是可选映射，但列必须恒显示**，未配时该格显示 `-`
    //:   （缺值口径：不造假、也不让列凭空消失）。
    const mappedCols = ["price", "current_stock", "market_price", "stock"];
    //: ⚠️ **一个映射都没配时显示全部**：否则初次进来表格只剩行号/规格图，
    //: 用户会以为功能坏了（而且他还没机会配映射 —— 映射在弹窗里配，
    //: 弹窗的打开又依赖这张表先渲染出来）。
    const visible =
      mappedTargets.size === 0
        ? base
        : base.filter(
            (col) =>
              !mappedCols.includes(String(col.key)) || mappedTargets.has(String(col.key)),
          );
    return visible.map(centerHeader).map((col) => {
      //: ⚠️ `ColumnsType` 是「分组列 | 普通列」的联合 ⇒ `dataIndex` 不总存在。
      //: 我们这里全是普通列，用 `dataIndex` 作字段名（与 `key` 同名）。
      const field = "dataIndex" in col ? (col.dataIndex as string | undefined) : undefined;
      if (col.render || readOnly.has(col.key as string) || !field) return col;
      return {
        ...col,
        render: (_: unknown, row: PriceRow) => {
          //: 用户口径 2026-10-10：不启用的行**整行置灰不可编辑**。
          const disabled = row.available === false;
          return (
            <Input
              size="small"
              disabled={disabled}
              value={String((row as unknown as Record<string, unknown>)[field] ?? "")}
              onChange={(e) => {
                const raw = e.target.value;
                props.onPriceRowsChange(
                  props.priceRows.map((r) => {
                    if (r.key !== row.key) return r;
                    if (numeric.has(field)) {
                      const n = raw.trim() === "" ? null : Number(raw.replace(/[^0-9.-]/g, ""));
                      return { ...r, [field]: Number.isFinite(n as number) ? n : null };
                    }
                    return { ...r, [field]: raw };
                  }),
                );
              }}
            />
          );
        },
      };
    });
    //: ⚠️⚠️ 依赖里**不许放 `props` 对象**（用户口径 2026-10-22：「sku 数据导入后
    //: 运费模板的下拉切换时很慢」）。
    //:
    //: 根因：`props` 是父组件每次渲染新建的对象 ⇒ 依赖恒变 ⇒ 这个 useMemo
    //: （要对 **456 行 × 每列**做 `dimOf` 逐格解析 + `centerHeader` 包一层）
    //: **每次父组件重渲染都重算一遍**。导入 456 行后，点一下运费模板下拉
    //: （父组件 setState）就触发一次全表列重建 ⇒ 卡顿与行数成正比。
    //: ⇒ 只依赖**真正用到的两个字段**（`priceRows` / `onPriceRowsChange`），
    //:   并依赖 `specAttributes` 变化而不是整个 props。
  }, [
    deliveryMode,
    props.priceRows,
    props.onPriceRowsChange,
    specTypes,
    specValues,
    specImages,
    priceMapping,
  ]);


  /** 发布提示：按真机口径实时算**未通过的必填项**（分区 + 字段 + 原因）。 */
  const publishHints = useMemo<PublishHint[]>(() => {
    const out: PublishHint[] = [];
    const missing = (v: unknown) => v === undefined || v === null || v === "";
    //: 基础信息：关键属性必填项
    props.keyProperties.forEach((f) => {
      if (!f.required) return;
      if (missing(propValues[f.id])) {
        out.push({ section: "基础信息", field: f.name, reason: `请输入${f.name}` });
      }
    });
    //: 基础信息：**商品类目**（⚠️ 2026-10-10 补：门禁一直在读 `configCategoryId`，
    //: 但助手漏了这条 ⇒ 出现过「助手显示没有待处理提示，按钮却灰着」）
    if (!props.categoryId) {
      out.push({ section: "类目信息", field: "商品类目", reason: "请选择商品类目" });
    }
    //: 基础信息：商品标题 / 品牌
    if (!props.title.trim()) {
      out.push({ section: "基础信息", field: "商品标题", reason: "请输入商品标题" });
    }
    //: 价格库存：物流模板（物流计划）
    if (!selectedShippingTemplate) {
      out.push({ section: "价格库存", field: "物流模板", reason: "请选择物流模板" });
    }
    //: 价格库存：商品规格（用户口径 2026-10-10：「商品规格必填」）。
    //: ⚠️ 判据必须与门禁的 `skus.length` **同源**：勾了类型但**规格值填 0 个**
    //: ⇒ `priceRows` 仍是那行裸价（`spec === "-"`）⇒ 可提交行 0 ⇒ 当时助手恒 0 提示而按钮灰。
    if (specTypes.length === 0) {
      out.push({ section: "价格库存", field: "商品规格", reason: "请至少勾选一个商品规格" });
    } else {
      const noValues = specTypes.filter((name) => (specValues[name] ?? []).length === 0);
      if (noValues.length > 0) {
        out.push({
          section: "价格库存",
          field: "商品规格",
          reason: `${noValues.join("、")} 还没有规格值`,
        });
      }
    }
    //: 服务信息：运费模板（用户口径 2026-10-10：「运费模板必填」）
    if (!effectiveFreightTemplateId) {
      out.push({ section: "服务信息", field: "运费模板", reason: "请选择运费模板" });
    }
    //: 基础信息：商品主图 / 商品详情图（页面标了 required ⇒ 助手必须同列，
    //: 否则「红色项」与「实际能提交」不一致）
    if ((props.images ?? []).length === 0) {
      out.push({ section: "图文信息", field: "商品主图", reason: "请上传商品主图" });
    }
    if ((props.detailImages ?? []).length === 0) {
      out.push({ section: "图文信息", field: "商品详情图", reason: "请上传商品详情图" });
    }
    //: 价格库存：**价格及库存**（每行都要有价格与库存）
    const badRows = props.priceRows.filter((r) => r.price === null || r.stock === null);
    if (badRows.length > 0) {
      out.push({
        section: "价格库存",
        field: "价格及库存",
        reason: `${badRows.length} 行缺价格或库存`,
      });
    }
    //: ⚠️⚠️ 用户口径 2026-10-22：「**商品规格组的输入框添加 60 字符长度校验，
    //: 超出则不允许进入下一步**」。
    //:
    //: 判据与平台同口径 = **显示宽度**（汉字计 2）。旧代码只在 `commitSpecValue`
    //: （回车提交时）用码点数校验 ⇒ 两处漏洞：
    //:   ① 不按回车直接点「下一步」永远不校验；
    //:   ② 30 个汉字码点只算 30 ⇒ 前端放行、发布被平台拒（截图里那 4 条 variants）。
    //: ⇒ 放进 `publishHints`：它会经 `onHintCountChange` 上报给向导，
    //:   而向导的门禁判据就是「异常数 > 0 不许进下一步」—— 一处收口。
    const longSpecValues: string[] = [];
    for (const specName of specTypes) {
      for (const v of specValues[specName] ?? []) {
        if (v && exceedsLimit(v, SKU_NAME_MAX_CHARS)) longSpecValues.push(v);
      }
    }
    if (longSpecValues.length > 0) {
      out.push({
        section: "商品规格",
        field: "规格值",
        reason: `${longSpecValues.length} 个规格值超过 ${SKU_NAME_MAX_CHARS} 字符（汉字计 2）`,
      });
    }
    //: ⚠️ 用户口径（2026-10-10）：「第二步中**所有必须配置的参数均在助手中展示**」
    //: ⇒ 覆盖到每一个必配置项。下面两项**当前结构上恒有值**
    //: （`商品品牌` 固定「无品牌」/ `发货模式` 默认「现货模式」）⇒ 正常不会触发，
    //: 但**判据在**：日后若变成可空，助手会自动列出，不用再补。
    if (!brand) out.push({ section: "基础信息", field: "商品品牌", reason: "请选择商品品牌" });
    if (!deliveryMode) {
      out.push({ section: "价格库存", field: "发货模式", reason: "请选择发货模式" });
    }
    return out;
    //: ⚠️ 依赖必须包含**所有参与判定的值**（用户实测 2026-10-10：「商品规格已经勾选
    //: 助手中的红色异常提醒依旧存在」—— 漏了 `specTypes` / 运费模板 ⇒ 勾选后不重算）。
  }, [
    props.keyProperties, props.title, props.categoryId, propValues, selectedShippingTemplate,
    specTypes, specValues, effectiveFreightTemplateId, brand, deliveryMode, props.priceRows,
  ]);

  /** 把「助手异常提示数」上报给向导（门禁判据；用户口径 2026-10-10）。 */
  useEffect(() => {
    props.onHintCountChange?.(publishHints.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publishHints]);

  /** 跳到指定字段（发布提示里的项点击后定位；「去填写」按钮用户口径不展示）。 */
  const jumpToField = useCallback((field: string) => {
    const el = document.querySelector(`[data-field="${field}"]`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  /** 属性「智能生成」是否在跑。 */
  const [smartPropFilling, setSmartPropFilling] = useState(false);

  /** 属性**智能生成** —— 按商品信息自动填充属性值（走平台属性预测）。
   *
   * ⚠️ **必须带图**（`image_urls`）：真机复测（2026-10-09）无图平台恒回空数组，
   * 页面会误报「平台未给出可填充的属性值」。用主图 + 详情图一起喂。
   */
  const genProperties = useCallback(async () => {
    const fields = [...props.keyProperties, ...props.otherProperties];
    if (!fields.length) {
      message.warning("该类目没有可填充的属性");
      return;
    }
    if (!props.categoryId) {
      message.warning("请先选择商品类目");
      return;
    }
    setSmartPropFilling(true);
    try {
      const res = await predictProperties(props.accountId, {
        category_id: props.categoryId,
        title: props.title,
        image_urls: buildImageUrls([
          ...props.images,
          ...(props.detailImages ?? []),
        ]),
      });
      const predicted = res.properties ?? {};
      let n = 0;
      setPropValues((prev) => {
        const next = { ...prev };
        fields.forEach((f) => {
          const v = predicted[f.name];
          if (v !== undefined && v !== null && v !== "") {
            //: ⚠️ 多选属性的值必须是**数组**（`Select mode="multiple"` 只吃数组）。
            //: 平台可能给字符串（单值）也可能给数组（多值）—— 统一归一。
            next[f.id] =
              f.kind === "multi"
                ? Array.isArray(v)
                  ? v
                  : String(v).split(/[\s,，、/]+/).filter(Boolean)
                : v;
            n += 1;
          }
        });
        return next;
      });
      if (n === 0) message.info("平台未给出可填充的属性值");
      else message.success(`已自动填充 ${n} 项商品属性`);
    } catch {
      message.error("属性智能生成失败");
    } finally {
      setSmartPropFilling(false);
    }
  }, [
    props.accountId,
    props.categoryId,
    props.title,
    props.images,
    props.detailImages,
    props.keyProperties,
    props.otherProperties,
    message,
  ]);

  /** 智能生成导购短标题（走平台 `generateItemShortTitle`）。
   *
   * ⚠️ 入参是 `{title, categoryId}` —— 缺类目平台回空短标题。
   */
  const genShortTitle = useCallback(async () => {
    const base = (props.title || "").trim();
    if (!base) {
      message.warning("请先填写商品标题");
      return;
    }
    if (!props.categoryId) {
      message.warning("请先选择商品类目");
      return;
    }
    setShortTitleLoading(true);
    try {
      const res = await generateShortTitle(props.accountId, base, props.categoryId);
      const text = (res.short_title ?? "").trim();
      if (!text) {
        message.warning("平台没有返回短标题，可稍后重试");
        return;
      }
      setShortTitle(text.slice(0, SHORT_TITLE_MAX));
    } catch (err) {
      message.error(`生成失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setShortTitleLoading(false);
    }
  }, [props.accountId, props.categoryId, props.title, setShortTitle]);

  return (
    <div className="publish-config-step2">
      {/* 顶部提示（真机有「商品发布助手 / 发布提示(3)」） */}
      <Alert
        type="info"
        showIcon
        icon={<InfoCircleOutlined />}
        message="预填信息仅供参考，请检查后发布"
        style={{ marginBottom: 12 }}
      />

      {/* 「自动加载参数」按钮（用户口径 2026-10-09：放在**基础信息外面的顶部**，
          即整页参数配置区最上方、左右两栏之上）。 */}
      {props.autoLoadSlot}

      {/* 真机：**左右两栏** —— 左栏「商品发布助手/发布提示」224px，右栏参数配置 1176px。
          用户口径 2026-10-09：右栏**内部再分两步**（不再把「发布提示」与「参数配置」平铺）。 */}
      <div className="publish-two-col">
        <aside className="publish-col-left">
          <PublishHelper hints={publishHints} />
        </aside>
        <div className="publish-col-right">

      {/* 右栏：**千帆的两部分参数合并为一部分**（用户口径 2026-10-09），
          不再分「商品属性 / 其余参数」两个内部步骤 —— 同屏即可。
          左栏保留「商品发布助手」（发布提示 + 字段错误原因）。 */}

      {/* ── 1. 基础信息 ─────────────────────────────────── */}{/* 用户口径 2026-10-09：千帆第一部分的**商品图片 / 商品标题 / 商品类目**
            合并进「基础信息」区（由 `headSlot` 由外层塞入，内含「自动加载参数」按钮，
            逻辑不变）。 */}
      <Section title="基础信息" required="4 项必填">
        {props.headSlot}

        {/* 用户口径：**品牌固定为「无品牌」** —— 不做品牌选择/申请入口。 */}
        <Field label="商品品牌" required>
          <Text className="inherited-value">无品牌</Text>
        </Field>

        {/* 商品标题**由第一部分（headSlot）提供真实输入框**，此处不再重复展示
            （用户口径 2026-10-09：商品标题并入基础信息，只出现一次）。 */}

        <Field label="导购短标题" hint="在商品搜索、商品笔记、直播卡、频道活动等场景下优先展示。">
          <div className="title-line">
            <Input
              maxLength={SHORT_TITLE_MAX}
              showCount
              value={shortTitle}
              onChange={(e) => setShortTitle(e.target.value)}
              placeholder="请输入"
            />
            {/* ⚠️ 与商品标题同款口径：能自动生成的就走**平台**生成
                （`generateItemShortTitle`），不拿本地文本冒充。 */}
            <Button
              className="smart-gen-btn"
              loading={shortTitleLoading}
              onClick={() => void genShortTitle()}
            >
              智能生成
            </Button>
          </div>
        </Field>

        {/* 用户口径（2026-10-09）：**商品属性分「关键属性 / 其他属性」两部分**，
            每部分带计数（真机 `关键属性 3/4` / `其他属性 11/19`），
            说明文案改用**说明图标 + Tooltip**（不占布局）。
            ⚠️ 属性名/候选项一律来自平台，不写死。 */}
        <Divider plain>
          <Space>
            <span>商品属性</span>
            {/* 真机：属性区有「智能生成」——按商品信息自动填充属性 */}
            <Button
              size="small"
              loading={smartPropFilling}
              onClick={() => void genProperties()}
            >
              智能生成
            </Button>
            <Tooltip title="关键属性影响转化与搜索流量，建议填全。">
              <InfoCircleOutlined className="section-help-icon" />
            </Tooltip>
          </Space>
        </Divider>

        {/* ── 关键属性（平台按类目下发；真机判据 = 必填属性） ── */}
        <div className="prop-group-head">
          <span className="prop-group-title">
            关键属性 {filledCount(props.keyProperties, propValues)}/{props.keyProperties.length}
          </span>
          <Tooltip title="关键属性影响转化，建议填全">
            <InfoCircleOutlined className="section-help-icon" />
          </Tooltip>
        </div>
        {props.keyProperties.length > 0 ? (
          <div className="prop-grid">
            {props.keyProperties.map((f) => (
              <div className={`prop-cell${f.kind === "multi" ? " multi" : ""}`} key={f.id}>
                <span className="prop-label" title={f.name}>
                  {f.required && <span className="req-star">*</span>}
                  {f.name}
                </span>
                <PropertyControl
                  field={f}
                  value={propValues[f.id]}
                  onChange={(v) => setPropValues((prev) => ({ ...prev, [f.id]: v }))}
                />
              </div>
            ))}
          </div>
        ) : (
          <Text type="secondary" className="prop-empty">
            {props.categoryId ? "该类目暂无关键属性" : "请先在上一步选择商品类目"}
          </Text>
        )}

        {/* ── 其他属性（同页第二块） ── */}
        <div className="prop-group-head" style={{ marginTop: 12 }}>
          <span className="prop-group-title">
            其他属性 {filledCount(props.otherProperties, propValues)}/{props.otherProperties.length}
          </span>
          <Tooltip title="完整的商品属性利于提升搜索流量">
            <InfoCircleOutlined className="section-help-icon" />
          </Tooltip>
        </div>
        {props.otherProperties.length > 0 ? (
          <div className="prop-grid">
            {props.otherProperties.map((f) => (
              <div className={`prop-cell${f.kind === "multi" ? " multi" : ""}`} key={f.id}>
                <span className="prop-label" title={f.name}>
                  {f.name}
                </span>
                <PropertyControl
                  field={f}
                  value={propValues[f.id]}
                  onChange={(v) => setPropValues((prev) => ({ ...prev, [f.id]: v }))}
                />
              </div>
            ))}
          </div>
        ) : (
          <Text type="secondary" className="prop-empty">
            {props.categoryId ? "该类目暂无其他属性" : "请先在上一步选择商品类目"}
          </Text>
        )}
      </Section>

      {/* ── 2. 图文信息 ─────────────────────────────────── */}
      {/* 用户口径（2026-10-09）：图文信息区按 **商品主图 → 商品视频 → 商品详情图**
          顺序展示三类媒体资源（智能识图区是「喂大模型的图片来源」，与这里不冲突：
          它可传主图也可传详情图，这里展示的是最终发布的实际媒体）。 */}
      <Section title="图文信息" required="1 项必填">
        {/* ① 商品主图。
            ⚠️ 用户口径（2026-10-09）：商品主图**必填**（真机「图文信息 1 项必填」就是它）。
            ⚠️ 用户口径（2026-10-10）：**移除**「请先在上方『智能识图』区上传商品主图」
            那句提示 —— 改为直接用**图片上传组件**在本区上传。 */}
        <Field label="商品主图" required>
          <MediaThumbs
            items={props.images ?? []}
            removable
            onRemove={(i) =>
              props.onImagesChange?.((props.images ?? []).filter((_, j) => j !== i))
            }
            trailing={
              <UploadSlot
                title="上传本地商品主图（自动上传，不显示图片空间）"
                accept="image/png,image/jpeg,image/jpg"
                multiple
                busy={mainUploading}
                onFiles={(files) => void onPickMainImages(files)}
              />
            }
          />
        </Field>

        {/* ② 商品视频 —— **上传组件**（用户口径 2026-10-10：不要用按钮）。 */}
        <Field label="商品视频">
          <MediaThumbs
            items={props.video ? [props.video] : []}
            removable
            onRemove={() => props.onVideoChange?.(null)}
            trailing={
              <UploadSlot
                title="上传本地商品视频（上传前按内容 MD5 改名）"
                accept="video/*,.mp4,.mov,.m4v,.webm,.avi"
                busy={videoUploading}
                onFiles={(files) => void onPickVideoFiles(files)}
              />
            }
          />
        </Field>

        {/* ③ 商品详情图（同款缩略图 + 悬停删除 + 大图预览左右切换）。
            ⚠️ 用户口径（2026-10-09）：商品详情图**必填**。
            ⚠️ 用户口径（2026-10-10）：用**上传组件**（不用按钮），上传框紧跟缩略图流。 */}
        <Field label="商品详情图" required hint={DESC_IMAGE_RULE}>
          <MediaThumbs
            items={props.detailImages ?? []}
            removable
            onRemove={(i) =>
              props.onDetailImagesChange?.((props.detailImages ?? []).filter((_, j) => j !== i))
            }
            trailing={
              <UploadSlot
                title="上传本地商品详情图（可多选）"
                accept="image/png,image/jpeg,image/jpg"
                multiple
                busy={detailUploading}
                onFiles={(files) => void onPickDetailImages(files)}
              />
            }
          />
        </Field>

        <Field label="详情页描述" hint={DESC_RULE}>
          <Input.TextArea
            maxLength={DESC_MAX}
            showCount
            rows={4}
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="请输入"
          />
        </Field>

        <Field
          label="商详回看讲解"
          hint="展示回看讲解挂件，有利于买家更好地了解商品卖点"
        >
          <Switch
            checked={showPlayback}
            onChange={setShowPlayback}
            checkedChildren="展示挂件"
            unCheckedChildren="不展示"
          />
        </Field>
      </Section>

      {/* ── 3. 价格库存 ─────────────────────────────────── */}
      <Section title="价格库存" required="1 项必填">
        {/* 物流模板 = 单选「商品维度统一物流模板」+ 物流计划下拉（真机无「新建」入口，用户口径）。 */}
        <Field label="物流模板" required>
          <Radio.Group value="商品维度统一物流模板">
            <Radio value="商品维度统一物流模板">商品维度统一物流模板</Radio>
          </Radio.Group>
          {/* ⚠️ 用户口径（2026-10-09）：「物流模板没有使用默认值展示」——未选时
              **回落到第一个可选计划**，开箱即显示默认发货仓。与 `selectedShippingTemplate`
              （有效性判据）同一来源，不另造默认真相源。 */}
          <Select
            style={{ width: 320, marginTop: 8 }}
            placeholder={props.logisticsTemplates.length ? "请选择" : "模板加载中…"}
            loading={props.logisticsTemplatesLoading}
            value={selectedShippingTemplate ?? undefined}
            onChange={setLogisticsPlanId}
            options={props.logisticsTemplates.map((t) => ({
              label: `${t.name} | ${t.logistics_mode}`,
              value: t.plan_id,
            }))}
          />
        </Field>

        {/* 真机四模式（DELIVERY_MODES）：切换需二次确认，且各自的参数不同。
            ⚠️ 用户口径（2026-10-09）：「发货模式说明文案拆分开再对应模式，
            使用说明图片加 tip 展示」⇒ 每个模式各配一条说明 + 一个说明图标（Tooltip），
            不再整块铺 6 行正文。 */}
        <Field label="发货模式" required hint={selectedShippingTemplate ? "" : SHIP_DISABLED_HINT}>
          <Radio.Group
            value={deliveryMode}
            disabled={!selectedShippingTemplate}
            onChange={(e) => requestDeliveryMode(e.target.value)}
            optionType="button"
            options={DELIVERY_MODES.map((m) => ({
              label: (
                <span className="delivery-mode-option">
                  {m}
                  <Tooltip title={DELIVERY_MODE_DOC[m] ?? ""} overlayStyle={{ maxWidth: 420 }}>
                    <InfoCircleOutlined className="section-help-icon delivery-help-icon" />
                  </Tooltip>
                </span>
              ),
              value: m,
            }))}
          />
        </Field>

        {/* 切换发货模式后**变化的参数** —— ⚠️ 用户口径（2026-10-10）：
            「发货模式切换得到的不同参数属性页面 ui 缩进到与第二个发货模式起头
            使用**灰色背景**单独**虚线框**，页面布局也要看到**父子关系**」
            （取代 2026-10-09 的「去掉虚线框缩进」）。 */}
        <div className="child-block delivery-varying">
          <div className="child-block-title">
            <span>{deliveryMode} · 发货参数</span>
          </div>
        {/* ①/③ 现货发货时间 —— 真机在**现货模式**与**现货+预售模式**都出现。
            ⚠️ 用户口径（2026-10-09）：「现货发货时间中**单独的说明**应在**现货发货时间标题后**」
            ⇒ 把发货模式总览说明作为该字段 title 后的说明图标（Tooltip），不再单独成块。 */}
        {(deliveryMode === DELIVERY_MODES[0] || deliveryMode === DELIVERY_MODES[2]) && (
          //: ⚠️ 用户口径（2026-10-10）：「发货模式的 ui 布局不对…应参考开售时间的 ui 布局」
          //:  —— 子块内**不再套 `Field`**（Field 自带 126px 标签列，而子块已在「发货模式」
          //:  的 body 内 ⇒ **双重缩进**，标签被推进第二列）。改与开售时间同款的**行式**排布。
          <div className="child-param-row">
            <span className="child-param-label">现货发货时间</span>
            <Radio.Group
              value={shipTime}
              disabled={!selectedShippingTemplate}
              onChange={(e) => setShipTime(e.target.value)}
              options={SPOT_SHIP_TIME_OPTIONS.map((o) => ({ label: o, value: o }))}
            />
            <Tooltip title={selectedShippingTemplate ? DELIVERY_MODE_OVERVIEW : SHIP_DISABLED_HINT}>
              <InfoCircleOutlined className="section-help-icon" />
            </Tooltip>
          </div>
        )}

        {/* ② 全款预售模式 —— 全款预售发货时间（时段预售 / 定时预售）。 */}
        {deliveryMode === DELIVERY_MODES[1] && (
          <>
            <div className="child-param-row">
              <span className="child-param-label">全款预售发货时间</span>
              <Radio.Group
                value={presaleKind}
                onChange={(e) => setPresaleKind(e.target.value)}
                options={PRESALE_KINDS.map((o) => ({ label: o, value: o }))}
              />
            </div>
            {presaleKind === PRESALE_KINDS[0] ? (
              <div className="child-param-row">
                <span className="child-param-label">付款后发货</span>
                <Space>
                  <span>付款后</span>
                  <InputNumber min={3} max={180} value={presaleDays} onChange={setPresaleDays} />
                  <span>天内发货（请输入≥3且≤180的整数）</span>
                </Space>
              </div>
            ) : (
              <div className="child-param-row">
                <span className="child-param-label">定时预售</span>
                <Space>
                  <DatePicker value={presaleDate} onChange={setPresaleDate} placeholder="请选择日期" />
                  <span>该天24点前</span>
                </Space>
              </div>
            )}
          </>
        )}

        {/* ③ 现货+预售模式 —— 发货时效 + 展开预售批次；移除「多件装打包」「多仓区域库存」。 */}
        {deliveryMode === DELIVERY_MODES[2] && (
          <div className="child-param-row">
            <span className="child-param-label">发货时效</span>
            <Space direction="vertical" size={6}>
              <Space wrap size={8}>
                {SHIP_AGING_OPTIONS.map((o) => (
                  <Checkbox key={o} checked={shipAging === o} onChange={() => setShipAging(o)}>
                    {o}
                  </Checkbox>
                ))}
              </Space>
              <Button type="link" size="small" onClick={() => setPresaleBatchOpen((v) => !v)}>
                {presaleBatchOpen ? "收起预售批次" : "展开预售批次"}
              </Button>
              {presaleBatchOpen && (
                <Space wrap size={8}>
                  {SHIP_AGING_OPTIONS.slice(0, 4).map((o) => (
                    <span key={o} className="presale-batch-chip">{o}</span>
                  ))}
                </Space>
              )}
            </Space>
          </div>
        )}

        {/* ④ 按规格设置发货时间 —— 「去设置」入口。 */}
        {deliveryMode === DELIVERY_MODES[3] && (
          <div className="child-param-row">
            <span className="child-param-label">发货时间</span>
            <Button type="link">去设置</Button>
          </div>
        )}
        </div>

        {/* 商品规格：复选框一行展示（平台候选，上限 2）。 */}
        {/* ⚠️ 用户口径 2026-10-11（**最终**）：「商品规格可能是根据商品类目进行变动的，
            在**商品规格区域单独新增一个按钮弹窗**配置映射关系后自动解析加载规格数据」
            —— 因此**不在「自动加载参数」里做**（那条流水线按顺序跑死，第 9 步拿到的
            规格类型可能不是最终类目的）；改成本区域的独立入口，随时可重跑。 */}
        <Field label="商品规格" required hint={SPEC_RULE}>
          <Space wrap size={12}>
            {(props.specTypeOptions ?? []).map((name) => (
              <Checkbox
                key={name}
                checked={specTypes.includes(name)}
                onChange={(e) => toggleSpecType(name, e.target.checked)}
              >
                {name}
              </Checkbox>
            ))}
            {/* 灰色时给出原因（用户口径：灰着要能看懂为什么——静默禁用最难排查）。
                注意必须是 JSX 注释写法：行注释写在 JSX 子节点位置会被当文本渲染出来。 */}
            <Tooltip
              title={
                effectiveImportSources.length === 0
                  ? "没有可导入的 SKU：请先在第一步预检里读取数据包，并保证有「预检通过」的 SKU 行"
                  : "从预检通过的 SKU 导入规格 / 价格 / 库存"
              }
            >
            <Button
              size="small"
              type="primary"
              icon={<ImportOutlined />}
              disabled={effectiveImportSources.length === 0}
              onClick={() => openSpecImportWithNameCheck()}
            >
              预检通过SKU导入
            </Button>
            </Tooltip>
            {/* ⚠️ 用户口径 2026-10-22：「商品规格列表顶部新增一个**优化规格名称**按钮，
                点击也可以打开弹窗对商品 sku 规格名称进行优化」——
                手动改过 / 导入后留下的超长规格名，不必重跑导入，在这里直接优化。 */}
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              disabled={allSpecValues.length === 0}
              onClick={() => openOptimizeForCurrentSpecs()}
            >
              优化规格名称
            </Button>
          </Space>
        </Field>

        {/* 勾选规格后带出该规格的动态配置 —— ⚠️ 用户口径（2026-10-10）：
            「商品规格勾选后同理（灰底虚线框 + 父子关系），**不再添加最左侧的标题**」
            ⇒ 规格名从最左侧**移到块内标题行**（父块标题），左侧不再重复一个 {name}。 */}
        {/* **从预检 SKU 导入**弹窗（用户口径 2026-10-11 最终版）：
            先让用户确认「第一步规格分类 → 第二步平台规格」的映射，再自动解析加载。
            ⚠️ 映射**默认文本精确匹配**，不一致时由用户手选 —— 不选则该项不导入
            （宁可漏填，也不把值填错维度）。 */}
        {/* **SKU 名称优化预览**（用户口径 2026-10-20）：
            超长的规格名压到上限内，用户比较后确认；
            每行可「重新生成」覆盖该行。确认结果**只用于本次导入**。 */}
        <SkuNameOptimizeModal
          open={skuOptimizeOpen}
          maxChars={skuOptimizeMax}
          items={skuOptimizeItems}
          //: 用户口径 2026-10-21：「预览弹窗中展示**规格图**、原规格名、新规格名」。
          //: 规格图来自 `specImages`（维度 → 值 → 图），这里摊平成「值 → URL」：
          //: 规格名在第一个维度下才有图（与 `plan.images` 同口径），
          //: 跨维度重名时以先出现的为准（第一个维度优先）。
          imageByValue={Object.values(specImages).reduce<Record<string, string>>(
            (acc, byValue) => {
              for (const [value, img] of Object.entries(byValue ?? {})) {
                const url = (img as { url?: string } | undefined)?.url;
                if (url && !acc[value]) acc[value] = url;
              }
              return acc;
            },
            {},
          )}
          onClose={() => setSkuOptimizeOpen(false)}
          onConfirm={(applied) => {
            setSkuOptimizeOpen(false);
            //: ⚠️ 弹窗有**三个入口**，确认后必须回到各自的原处：
            //:   A. 手动输入框超长      → 写回那个输入框（`pendingSpecValue`）
            //:   B. 手动点击「导入 SKU 数据」 → 把结果写回 `specImportSources` 再开导入
            //:   C. **导入完成后的统一优化** → 用新名**覆盖商品规格里已有的旧值**
            //:      （用户口径 2026-10-22：「统一优化点击应用重新覆盖到商品
            //:      sku 旧规格名称值」）。判据 = `pendingSpecValue` 为空且
            //:      导入弹窗没开（说明是从导入收尾弹出来的）。
            if (pendingSpecValue) {
              const { spec, index } = pendingSpecValue;
              const picked = Object.values(applied)[0] ?? "";
              setSpecValueAt(spec, index, picked);
              setPendingSpecValue(null);
              return;
            }
            if (!specImportOpen) {
              //: 入口 C：覆盖已落库到「商品规格」里的旧值。
              //: ⚠️⚠️⚠️ 用户报 2026-10-22：「点击**使用这些名称导入**时应**只覆盖原来的
              //: 规格名称**，目前覆盖后价格及库存列表中的**其它值也丢失了**」。
              //:
              //: 真根因见 `renameSpecValue` 的注释：`PriceRow.key` 就是完整 `spec`
              //: ⇒ 只换 `specValues` 里的字符串，价格库存那边按新 key 重建时
              //: 匹配不上旧行，`base` 分支把价格/库存/编码全置 `null`。
              //:
              //: ⚠️⚠️ **必须一次性传整张改名表**，不能循环里逐条调。
              //:
              //: `renameSpecValue` 里改价格行用的是 `props.priceRows`（**闭包快照**）
              //: + `props.onPriceRowsChange(...)`（**异步回传外层**）。在同一个事件里
              //: 连续调用 N 次 ⇒ N 次都基于**同一份旧快照** map，后一次把前一次覆盖掉
              //: ⇒ 只有**最后一条**改名落到价格行上，其余行 key 还是旧值，
              //: 随后重建 effect 按新 key 找不到 ⇒ 整行价格/库存被清空
              //: （正是用户截图：改名那几行全空、未改名的行完好）。
              //:
              //: ⚠️⚠️⚠️ 用户口径 2026-10-22（第三条路，最终做法）：
              //: 「**先批量覆盖前先记录历史数据，覆盖后只规格名称变化，其它数据不变复原**」。
              //:
              //: 前两版都在「猜 key」：先按 `维度:值` 拼 key 重排，再猜归一化要不要 trim。
              //: 但 key 的构造散落在三处（`spec_combinations` / `specImages` / 扁平
              //: `specImageEnabled`），任何一处没跟上就丢数据 —— 用户已经连报三次。
              //:
              //: ⇒ 改成**快照式**，彻底不依赖「怎么拼 key」：
              //:   ① 覆盖前：把每行（按**值序列**索引，与 key 无关）的**全部字段**存一份；
              //:   ② 只改规格名（`specValues` / `specImages` / `specImageEnabled` 的键）；
              //:   ③ 覆盖后：价格库存行**用旧行做底**，只换 `key`/`spec` 两段名字，
              //:      其余字段**从快照逐字段复原** —— 名字变了也不影响价格/库存/编码。
              //:
              //: 与「按 key 找旧行」的区别：快照的索引是**规格值的序列**
              //: （`颜色值 / 款式值`，剥掉维度名），改名后这个序列**仍然可算**，
              //: 因为新名字就是旧名字的映射结果。所以永远找得回来。
              //: ⚠️⚠️⚠️ 用户口径 2026-10-22（最终做法，第三次修正）：
              //: 「先批量**覆盖前先记录历史数据**，覆盖后**只规格名称变化，其它数据不变复原**」
              //: + 「可以**按照行号复原**」。
              //:
              //: 前两版都在「按名字猜 key」：`PriceRow.key` 就是完整 `spec`（含维度名 +
              //: 值），还得考虑 trim、维度重名、`specImages` 的嵌套 vs 扁平……任何一处
              //: 没跟上就丢数据，用户连报三次。
              //:
              //: ⇒ **按行号**，这是唯一不依赖名字的索引：`priceRows` 的行序是规格值做
              //:   笛卡尔积的固定顺序，**改名只换标签、不改顺序** ⇒ 第 i 行改名前后是同
              //:   一个 SKU。所以：
              //:     ① 覆盖前：`snapshot[i] = { ...priceRows[i] }`（行号 → 整行）；
              //:     ② 只改名字：`specValues` / `specImages` / `specImageEnabled` 的键；
              //:     ③ 覆盖后：第 i 行 = `{ ...snapshot[i], key: 新spec, spec: 新spec }`
              //:        —— 其它字段**逐个来自快照**，价格/库存/编码一个都不会丢。
              const norm = (s: string) => String(s ?? "").trim();

              //: ① **快照（按行号）**：覆盖前的每一行原样留底。
              //:   ⚠️ 存 `{ ...r }` 而不是引用 —— 后续 effect 可能原地改 `r`。
              const snapshotByRow = props.priceRows.map((r) => ({ ...r }));

              //: ② 改名表（维度 + 旧值 → 新值），键两头归一化。
              const rk = (dim: string, value: string) => `${norm(dim)} ${norm(value)}`;
              const renameMap = new Map<string, string>();
              for (const [dim, list] of Object.entries(specValues)) {
                for (const v of list) {
                  const nextName = applied[norm(v)];
                  if (nextName && norm(nextName) !== norm(v)) renameMap.set(rk(dim, v), norm(nextName));
                }
              }
              //: ⚠️⚠️ **多规格的关键前提**：按行号复原要求「**改名前后行数不变、顺序不变**」。
              //:
              //:   `priceRows` 由 `spec_combinations` 对「每个维度的值」做笛卡尔积生成。
              //:   改名只是换标签 ⇒ 组合数不变 ⇒ 第 i 行还是同一个 SKU ⇒ 行号复原成立。
              //:
              //:   但有一个真实风险：**改名后同一维度里出现重名**
              //:   （如把「白色A」和「白色」都改成「白色」）。这时 `specValues` 里该维度
              //:   有两个相同值 ⇒ 组合数**变少** ⇒ 行号整体错位 ⇒ 按行号复原会把
              //:   别行的价格/库存搬到错的行上（比清空更糟：数据看起来有、但张冠李戴）。
              //:   ⇒ 覆盖前先拦：新名若与同维度**其它**值重名，直接拒绝整批并说明。
              const clashes: string[] = [];
              for (const [dim, list] of Object.entries(specValues)) {
                const after = list.map((v) => renameMap.get(rk(dim, v)) ?? v);
                const seen = new Set<string>();
                for (const v of after) {
                  const k = norm(v);
                  if (!k) continue;
                  if (seen.has(k)) clashes.push(`${dim}：${k}`);
                  seen.add(k);
                }
              }
              if (clashes.length > 0) {
                void message.error(
                  `优化后的名称在同规格下重复（${clashes.join("、")}），会导致 SKU 行错位，已取消本次覆盖。`,
                );
                return;
              }
              if (renameMap.size > 0) {
                //: ③ 规格值列表
                setSpecValues((prev) => {
                  const next: Record<string, string[]> = {};
                  for (const [dim, list] of Object.entries(prev)) {
                    next[dim] = list.map((v) => renameMap.get(rk(dim, v)) ?? v);
                  }
                  return next;
                });
                //: ④ 规格图（值这一层的键）
                setSpecImages((prev) => {
                  const next: Record<string, Record<string, ConfigMainImage>> = {};
                  for (const [dim, byValue] of Object.entries(prev)) {
                    const renamed: Record<string, ConfigMainImage> = {};
                    for (const [v, img] of Object.entries(byValue)) {
                      renamed[renameMap.get(rk(dim, v)) ?? v] = img;
                    }
                    next[dim] = renamed;
                  }
                  return next;
                });
                //: ⑤ 规格图开关（扁平 `维度 值`）
                setSpecImageEnabled((prev) => {
                  const next: Record<string, boolean> = {};
                  for (const [key, on] of Object.entries(prev)) {
                    const sep = key.indexOf(" ");
                    if (sep < 0) {
                      next[key] = on;
                      continue;
                    }
                    const dim = key.slice(0, sep);
                    const value = key.slice(sep + 1);
                    next[`${dim} ${renameMap.get(rk(dim, value)) ?? value}`] = on;
                  }
                  return next;
                });
                //: ⑥ 价格库存行：**按行号复原** —— 第 i 行只换 `key`/`spec`，
                //:   其余字段全部取自 `snapshotByRow[i]`。
                props.onPriceRowsChange?.(
                  props.priceRows.map((r, i) => {
                    const parts = String(r.spec ?? "").split(" / ");
                    let touched = false;
                    const renamedParts = parts.map((part) => {
                      const sep = part.indexOf(":");
                      if (sep < 0) return part;
                      const d = part.slice(0, sep).trim();
                      const v = part.slice(sep + 1).trim();
                      const hit = renameMap.get(rk(d, v));
                      if (!hit) return part;
                      touched = true;
                      return `${part.slice(0, sep)}:${hit}`;
                    });
                    if (!touched) return r;
                    const spec = renamedParts.join(" / ");
                    //: 用户口径：「**只改规格名称**，其它规格图以及对应的库存、价格等等列均不变」。
                    const snapshot = snapshotByRow[i];
                    if (!snapshot) return { ...r, key: spec, spec };
                    return { ...snapshot, key: spec, spec };
                  }),
                );
              }
              void message.success(`已把 ${Object.keys(applied).length} 个规格名更新为新名称`);
              return;
            }
            //: 入口 B：把优化后的名称按**原顺序**放回去（key 是原始名）。
            const next = (props.specImportSources ?? []).map((name) => applied[name] ?? name);
            setAppliedSkuNames(next);
            openSpecImportModal();
          }}
        />

        <Modal
          open={specImportOpen}
          title="导入sku数据"
          centered
          width={680}
          okText="确认导入"
          cancelText="取消"
          //: ⚠️ **不要用 `confirmLoading`**！它在跑的时候会把 OK 按钮变 loading，
          //: 而 antd 对 loading 态的 Modal footer 会**连带禁用同排的兄弟按钮**
          //: ⇒ 「取消」点不动（用户实测：「点击取消未中断也未关闭弹窗」）。
          //: 改用 `okButtonProps.loading` 只作用于 OK 自己，取消始终可点。
          okButtonProps={{
            loading: importRunning,
            //: 两列必填、不重复、最多两行（用户口径）—— 不满足则**不能**确认导入。
            //: ⚠️ 判据抽到 `importBlockers`（**单一实现**）—— 按钮的 disabled 与
            //: 上面那条「还差什么」提示必须同源，否则会出现「按钮说不能导、提示说能导」。
            disabled: importBlockers.length > 0,
          }}
          //: 用户口径 2026-10-11：「点击确认导入后**不允许关闭弹窗**，除非点击
          //: 取消按钮才可以关闭」。⇒ 跑的时候：蒙版不可点关、右上角 X 不可点；
          //: 只有「取消」按钮能关（用户明确要的逃生口）。
          maskClosable={!importRunning}
          closable={!importRunning}
          //: 取消按钮**任何状态下都可点**（它是唯一的逃生口）。
          cancelButtonProps={{ disabled: false, loading: false }}
          onCancel={() => {
            //: 走到这里只可能是「取消」按钮（mask/X 在跑时已禁用）。
            //: 用户口径 2026-10-11：「导入中点击取消，则**中断未导入和未上传的
            //: 数据，停止执行**」⇒ 先**置中断标志**（正在跑的循环会在下一张图前
            //: 看到它并退出），再关窗。已经传好的图**保留**（不回收已成功的上传）。
            importCancelRef.current = true;
            setSpecImportOpen(false);
          }}
          onOk={() => void runSpecImport()}
          destroyOnClose
        >
          {/* **蒙版 + 耗时 + 进度**（用户口径 2026-10-11：「点击确认应加蒙版覆盖
              统计耗时和执行进度情况展示」）—— 跑的时候整块盖住，
              防止中途改配置产出半成品。 */}
          {/* ⚠️ 2026-10-20：`tip` 已移除，改由统一 `<WaitMask>` 画在最上层
              （`Spin` 的 tip 跟内容滚走；`Spin` 只保留挡交互的职责）。 */}
          {/*: ⚠️ 2026-10-20 统一：`spinning` 恒 false —— 若为 true，`Spin` 会画自己的
               半透明底，与下方 `<WaitMask>` 叠成两层（用户实测）。这里只留结构职责。 */}
          <Spin
            spinning={false}
            indicator={<></>}
            style={{ width: "100%" }}
            wrapperClassName="import-spin-wrapper"
          >
          {/* ══════════ 区域一：手选商品规格 → 平台规格 ══════════
              **用户口径 2026-10-14**：
              「将**固定的规格分类移除**，相关逻辑也移除，第二步参数配置中，
               再 SKU 自动导入数据弹窗中**用户第一步需手动勾选商品规格**」

              ⚠️ 此前左侧是「预选通过规格类型」下拉（候选来自第一步那张**自造的
              『规格分类』列**）。那列已整条退场 ⇒ **来源不复存在**，改为
              **用户自己勾选**：勾哪些商品规格，就导入哪些（勾了才要映射）。
              ⚠️ 候选仍来自预检通过的行（`row.specName` = `sku名称` 冒号前的部分）。
              ⚠️ **名称里没有冒号时**（真实包 2026-10-15：`A款`）→ `specName` 退化为
              `SINGLE_SPEC_NAME`（`规格`）**一个维度**，值 = 整个名称 ——
              不再是空（空会让左侧候选整块消失、按钮恒灰）。 */}
          <div className="import-section">
            <div className="import-section-title">商品 SKU 映射关系</div>
            {/* ⚠️ **用户口径 2026-10-15**：
                「应拿第一步预检通过的**规格分类列去重**与平台类目生成的商品规格
                  下拉选择映射，且**不允许重复选择**，**最多两个映射关系**，
                  **布局参考同一个弹窗中的其它部分**」

                ⚠️ 与旧实现的差别（旧的是「勾选 + 下拉」紧跟在候选名后面）：
                  · 来源 = 第一步那张**「规格分类」列去重**（不再从 `sku名称` 拆冒号）；
                  · 布局 = **表格**（与下面「价格库存映射关系表」同款：两列 + 操作列）；
                  · 条数上限 = **2**（到 2 条不再给「+」）。
            */}
            <Table
              size="small"
              rowKey={(_, i2) => String(i2)}
              pagination={false}
              dataSource={importMapping}
              //: ⚠️ **空表时也要能加**（真 bug，用户实测 2026-10-15）：
              //: 旧写法把「+」只挂在**行的操作列**上 ⇒ 零行时**没有操作列**，
              //: 用户看到「还没有映射，请点操作列的 + 添加」却**根本找不到那个 +**
              //: ⇒ 功能等于不存在。⇒ 空态直接渲染一个**可点的添加按钮**。
              locale={{
                emptyText: (
                  <Button
                    size="small"
                    type="link"
                    icon={<PlusOutlined />}
                    onClick={() => setImportMapping([{ source: "", target: "" }])}
                  >
                    添加映射关系
                  </Button>
                ),
              }}
              columns={[
                {
                  //: ⚠️ 用户口径 2026-10-20：「左侧不再是规格分类而应该是
                  //: **与价格库存映射关系表一致，预检通过列的所有列**」
                  //: ⇒ 表头与下面「价格库存映射关系表」左列**同名**，不再叫「规格分类」。
                  title: "预检通过列",
                  key: "source",
                  render: (_: unknown, row: SpecMappingRow, i2: number) => {
                    const others = importMapping
                      .filter((r, j2) => j2 !== i2 && r.source.trim())
                      .map((r) => r.source.trim());
                    return (
                      <Select
                        style={{ width: "100%" }}
                        value={row.source || undefined}
                        placeholder="请选择"
                        status={!row.source.trim() ? "error" : undefined}
                        onChange={(v) =>
                          setImportMapping((prev) =>
                            prev.map((r, j2) => (j2 === i2 ? { ...r, source: v ?? "" } : r)),
                          )
                        }
                        options={effectiveImportSources.map((name) => ({
                          label: name,
                          value: name,
                          //: ⚠️ **不允许重复选择**（用户原话）。
                          disabled: others.includes(name),
                        }))}
                      />
                    );
                  },
                },
                {
                  title: "平台规格类型",
                  key: "target",
                  render: (_: unknown, row: SpecMappingRow, i2: number) => {
                    const others = importMapping.filter((r, j2) => j2 !== i2).map((r) => r.target);
                    return (
                      <Select
                        style={{ width: "100%" }}
                        value={row.target || undefined}
                        placeholder="请选择"
                        status={!row.target.trim() ? "error" : undefined}
                        onChange={(v) =>
                          setImportMapping((prev) =>
                            prev.map((r, j2) => (j2 === i2 ? { ...r, target: v ?? "" } : r)),
                          )
                        }
                        options={(props.specAttributes ?? []).map((d) => ({
                          label: d.name,
                          value: d.name,
                          //: 平台规格类型同样**不许重复**。
                          disabled: others.includes(d.name),
                        }))}
                      />
                    );
                  },
                },
                {
                  title: "操作",
                  key: "op",
                  width: 110,
                  render: (_: unknown, __: SpecMappingRow, i2: number) => (
                    <Space size={0}>
                      <Button
                        size="small"
                        type="link"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() =>
                          setImportMapping((prev) => prev.filter((_, j2) => j2 !== i2))
                        }
                      />
                      {i2 === importMapping.length - 1 && importMapping.length < MAX_SPEC_MAPPING_ROWS && (
                        <Tooltip title="添加一行映射">
                          <Button
                            size="small"
                            type="link"
                            icon={<PlusOutlined />}
                            //: 与价格库存一致：**新增行两列都空，不设默认值**。
                            onClick={() =>
                              setImportMapping((prev) => [...prev, { source: "", target: "" }])
                            }
                          />
                        </Tooltip>
                      )}
                    </Space>
                  ),
                },
              ]}
            />
            {importMapping.length >= MAX_SPEC_MAPPING_ROWS && (
              <div style={{ marginTop: 4 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {`最多 ${MAX_SPEC_MAPPING_ROWS} 个映射关系。`}
                </Text>
              </div>
            )}
            {validateSpecMapping(importMapping).map((msg) => (
              <div key={msg} style={{ marginTop: 4 }}>
                <Text type="danger" style={{ fontSize: 12 }}>
                  {msg}
                </Text>
              </div>
            ))}
          </div>

          {/* ══════════ 区域二：价格库存映射关系表 ══════════
              （用户口径 2026-10-11：「将价格库存列表与预检通过的列也配置成动态可调整的，
               当前映射逻辑配置的**为默认逻辑**」⇒ 默认值就是现行那套；
               目标列 / 来源列**都不支持重复映射**） */}
          <div className="import-section">
            <div className="import-section-title">价格库存映射关系表</div>
            <Table
              size="small"
              rowKey={(_, i) => String(i)}
              pagination={false}
              dataSource={priceMapping}
              //: ⚠️ 同「商品 SKU 映射关系」：空表时也要有可点的「+」——
              //: 去默认值后初始就是空表，只留一句话会让用户卡住（用户实测 2026-10-15）。
              locale={{
                emptyText: (
                  <Button
                    size="small"
                    type="link"
                    icon={<PlusOutlined />}
                    onClick={() => setPriceMapping([{ target: "", source: "" }])}
                  >
                    添加映射关系
                  </Button>
                ),
              }}
              columns={[
                {
                  //: ⚠️ 用户口径 2026-10-11：**两边均应与商品 SKU 映射关系一样自由选择**。
                  title: "预检通过列",
                  key: "source",
                  render: (_: unknown, row: PriceMappingRow, i: number) => {
                    return (
                      <Select
                        style={{ width: "100%" }}
                        value={row.source || undefined}
                        placeholder="请选择"
                        status={!row.source.trim() ? "error" : undefined}
                        onChange={(v) =>
                          setPriceMapping((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, source: v ?? "" } : r)),
                          )
                        }
                        //: ⚠️⚠️ 用户口径 2026-10-21：「**预检通过列取消一对一配，
                        //: 支持列被多次选择**」——
                        //: 原实现对来源列做了「已用即 disabled」的互斥限制，
                        //: 但同一列完全可能喂给多个目标：
                        //:   · 数据包「价格」列 → 售价，同时 → 市场价（划线价同源）；
                        //:   · 「库存」列 → 当前库存，同时 → 改后库存（把现库存原样带上）；
                        //:   · 「SKUID」列 → 商家编码，同时 → 规格ID。
                        //: ⇒ **取消互斥**，任何列都可被多次选（目标列仍不许重复，
                        //:   那是"一列落一个值"的硬约束，见右侧下拉的 `disabled`）。
                        options={priceSourceOptions(props.priceSourceColumns ?? []).map((name) => ({
                          label: name,
                          value: name,
                        }))}
                      />
                    );
                  },
                },
                {
                  title: "价格库存列",
                  key: "target",
                  render: (_: unknown, row: PriceMappingRow, i: number) => {
                    const others = priceMapping
                      .filter((r, j) => j !== i)
                      .map((r) => r.target);
                    return (
                      <Select
                        style={{ width: "100%" }}
                        value={row.target || undefined}
                        placeholder="请选择"
                        //: 必填（用户口径「不能为空」）⇒ 未选即红框。
                        status={!row.target ? "error" : undefined}
                        //: ⚠️⚠️ 用户口径 2026-10-21（截图为证）：「**红框中的值不允许修改切换**」
                        //: —— 即**四项必配**（售价 / 当前库存 / 改后库存 / 市场价）的
                        //: 目标列是**锁定**的：它们与千帆表头一一对应，改了就没有对应列可落值。
                        //: ⇒ `disabled` + 一个说清原因的 Tooltip（不让用户以为控件坏了）。
                        //: ⚠️ 其余行（条形码 / 商家编码 / 规格ID）仍可自由切换。
                        disabled={REQUIRED_PRICE_TARGETS.includes(row.target as PriceTargetKey)}
                        onChange={(v) =>
                          setPriceMapping((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, target: v as PriceTargetKey } : r)),
                          )
                        }
                        //: ⚠️ 用**价格库存表固定列**清单（唯一数据源 `PRICE_TABLE_TARGETS`）——
                        //: 全量会把「售价回落列」「规格分类」也列进来，而它们不是这张表的列
                        //: （用户报障 2026-10-15：「下拉框与价格及库存存在不一致的列，应保持一致」）。
                        options={PRICE_TABLE_TARGETS.map((t) => ({
                          label: PRICE_TARGET_LABELS[t],
                          value: t,
                          //: 目标列不许重复。
                          disabled: others.includes(t),
                        }))}
                      />
                    );
                  },
                },
                {
                  title: "操作",
                  key: "op",
                  width: 110,
                  render: (_: unknown, __: PriceMappingRow, i: number) => (
                    <Space size={0}>
                      <Button
                        size="small"
                        type="link"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => setPriceMapping((prev) => prev.filter((_, j) => j !== i))}
                      />
                      {i === priceMapping.length - 1 && (
                        <Tooltip title="添加一行映射">
                          <Button
                            size="small"
                            type="link"
                            icon={<PlusOutlined />}
                            //: 用户口径「**不限制条数**」⇒ 无上限，随时可加。
                            //: 用户口径：**添加的行应为空，不设置默认值**（两列都空）。
                            onClick={() =>
                              setPriceMapping((prev) => [...prev, { target: "", source: "" }])
                            }
                          />
                        </Tooltip>
                      )}
                    </Space>
                  ),
                },
              ]}
            />
            {/* 用户口径：多条问题「**放一行提示**」⇒ 单行、不换行堆叠。 */}
            {validatePriceMapping(priceMapping).map((msg) => (
              <div key={msg} style={{ marginTop: 4 }}>
                <Text type="danger" style={{ fontSize: 12 }}>
                  {msg}
                </Text>
              </div>
            ))}
          </div>

          {/* ⚠️ **「其它规则」整块已移除**（用户口径 2026-10-15：「其它规则的逻辑移除」）。
              原内容 = 「市场价计算规则：原价 × [倍率] + 恢复默认 + 取不到时回落列」。
              ⚠️ **不是删功能，是删入口**：倍率仍有默认值（`DEFAULT_PRICE_MULTIPLIER`
              = 1.7，见 `precheck-publish.ts`），导入时照常按它算市场价 ——
              只是用户不再在弹窗里配。要改倍率就改常量（或将来另开关口）。 */}

          {/* ⚠️ **未配映射时要说清「还差什么」**（用户口径 2026-10-15：
              「不预填，用户自己从头配（但没配之前导入按钮会恒灰，**需要提示他去配**）」）。
              ⚠️ 只把按钮变灰 = 用户对着一个点不动的按钮猜原因，这是本项目反复踩的坑
              （既有口径：门禁用「点击时提示」而不是 `disabled`；这里两种都给，
              因为导入是跑批操作，灰掉能防误点，提示负责说清原因）。 */}
          {importBlockers.length > 0 && (
            <Alert
              style={{ marginTop: 12 }}
              type="info"
              showIcon
              message={`还不能导入：${importBlockers.join("；")}`}
            />
          )}
          {importDetail && (
            <Alert
              style={{ marginTop: 12 }}
              type={importDetail.includes("失败") ? "warning" : "success"}
              showIcon
              message={importDetail}
            />
          )}
          </Spin>
          {/* 导入中需要挡交互（防止中途改配置）⇒ 这里显式 `mask`；
              上面的 `Spin` 已 `spinning={false}`，不会再叠第二层底。 */}
          <WaitMask
            open={skuOptimizeBusy}
            mask
            scope="panel"
            title="正在优化 SKU 规格名称"
          />
          <WaitMask
            open={importRunning}
            mask
            scope="panel"
            title={importProgress || "正在导入"}
            elapsed={importElapsed}
          />
        </Modal>

        {specTypes.map((name, gi) => (
          <div className="child-block spec-config" key={name}>
            <div className="child-block-title">
              <span>{name}</span>
              <Space size={4}>
                  {/* ⚠️ 用户口径（2026-10-10 纠正）：这是**规格图开关**，不是规格启用/停用 ——
                      规格添加即为启用；开关只控制该组是否配置规格图。
                      真机：第一组默认开、其余默认关；关闭 ⇒ 图前缀与按钮都不出现。 */}
                  <Switch
                    size="small"
                    checked={specImageEnabled[name] ?? gi === 0}
                    onChange={(on) =>
                      setSpecImageEnabled((prev) => ({ ...prev, [name]: on }))
                    }
                  />
                  {specImageEnabled[name] ?? gi === 0 ? (
                    <>{/* 2026-10-13 用户口径：移除「添加规格图」「批量上传规格图」按钮 ——
                         规格图只通过每个规格值输入框前缀的预览图点击切换。 */}</>
                  ) : null}
                  {/* 真机：**每个规格行都有**规格值排序 + 上移/下移（第 1 行上移禁用）。 */}
                  <span className="spec-sort-actions">
                    <span className="spec-sort-label">规格值排序</span>
                    <Button size="small" type="link" disabled={gi === 0} onClick={() => moveSpecType(gi, -1)}>上移</Button>
                    <Button size="small" type="link" disabled={gi === specTypes.length - 1} onClick={() => moveSpecType(gi, 1)}>下移</Button>
                  </span>
                </Space>
              </div>
              {/* 真机实测：**一个输入框一个规格值**（不用 tags 多值框）。
                  ⚠️ 用户口径 2026-10-09：**输入框前缀展示规格图**（只有第一组可加图）。 */}
              <div className="spec-config-values spec-values">
                {specValueInputs(name).map((v, vi) => {
                    //: ⚠️ 用户口径（2026-10-09）：「规格图应是**输入框的前缀**，不要单独分开；
                    //:  第一组规格**每个规格**均有规格图前缀」⇒ `canImage` 判的是**规格组**
                    //:  （`i === 0` = 第一组），**不是**规格值下标 —— 早先写成值下标 ⇒
                    //:  只有第一个规格值有前缀（用户实测 2026-10-10：「规格的第二个输入
                    //:  框没有规格图前缀，应是第一组的规格均应有规格图前缀」）。
                    //: ⚠️ 2026-10-10：key 已 trim（`setSpecImage`），此处读也要 trim 对齐。
                    const img = specImages[name]?.[String(v ?? "").trim()];
                    //: ⚠️ 用户口径（2026-10-10 纠正）：可用性由**规格图开关**决定（不再硬编码第一组）。
                    const canImage = specImageEnabled[name] ?? gi === 0;
                    //: ⚠️⚠️ **不再作为 `prefix` 传入**（用户实测 2026-10-11：「输入框文本过多
                    //: 向右滚会把规格图挤掉，回到最左也无法展示」）。根因：`ant-input-prefix`
                    //: 是**输入框内部**的 span，随文本一起横向滚动 ⇒ 图被挤走。
                    //: 现改为**输入框的兄弟节点**（`.spec-value-item` 里 thumb + input 并排）
                    //: ⇒ 图固定在框外左侧，与文本滚动完全无关。
                    const thumb =
                      canImage && img ? (
                      <img
                        className="spec-value-thumb"
                        src={img.url || img.local_url}
                        alt={`${v}规格图`}
                        title="点击更换规格图"
                        onClick={() => pickSpecImage(name, vi)}
                      />
                    ) : canImage ? (
                      <span
                        className="spec-value-img-btn"
                        role="button"
                        tabIndex={0}
                        aria-label="选择规格图"
                        title={SPEC_IMAGE_RULE}
                        onClick={() => pickSpecImage(name, vi)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") pickSpecImage(name, vi);
                        }}
                      >
                        <PlusOutlined />
                      </span>
                    ) : undefined;
                  const meta = specMetaFor(name);
                  //: 输入入口按维度元数据区分形态（用户口径 2026-10-10）：
                  //:  `kind=select` + customizable → AutoComplete（可输入可下拉选）；
                  //:  `kind=select` + 非 customizable → Select（只能选不能输）；
                  //:  其它（text / multi / 未下发）→ 保持纯 Input。
                  //: ⚠️ 规格图逻辑**不变**（前缀仍只挂第一组、一值一行）。
                  //: ⚠️⚠️ 三种形态（AutoComplete / Select / Input）统一用
                  //: **`.spec-value-item` 包裹**：`{规格图}{输入控件}` 并排，图在**框外**。
                  //: 用户实测（2026-10-11）：「规格输入框文本过多时向右走会将规格图挤掉，
                  //: 再回到最左侧时规格图也无法展示」—— 根因是图当时挂在 `prefix`（输入框
                  //: **内部**的 span），随文本一起横向滚动。抽成兄弟节点后与滚动无关。
                  if (meta?.kind === "select" && meta.values?.length) {
                    const options = (meta.options ?? []).map((o) => ({ value: o }));
                    return (
                      <span className="spec-value-item" key={vi}>
                        {thumb}
                        {meta.customizable ? (
                          <AutoComplete
                            className="spec-value-input"
                            value={v || undefined}
                            options={options}
                            placeholder="请选择或输入规格值"
                            filterOption={(input, opt) =>
                              (opt?.value ?? "").toLowerCase().includes(input.toLowerCase())
                            }
                            onChange={(val) => setSpecValueAt(name, vi, val)}
                            onSelect={(val) => setSpecValueAt(name, vi, val)}
                          >
                            <Input
                              value={v}
                              maxLength={SPEC_VALUE_LEN_MAX}
                              //: ⚠️ 用户口径 2026-10-22：超出 60 字符**红框展示**（显示宽度，汉字计 2）。
                              status={exceedsLimit(v, SKU_NAME_MAX_CHARS) ? "error" : undefined}
                              title={v || undefined}
                              onPressEnter={() => commitSpecValue(name, vi)}
                            />
                          </AutoComplete>
                        ) : (
                          <Select
                            className="spec-value-input spec-value-select"
                            value={v || undefined}
                            options={options}
                            placeholder="请选择规格值"
                            showSearch
                            optionFilterProp="label"
                            onChange={(val) => setSpecValueAt(name, vi, val)}
                          />
                        )}
                      </span>
                    );
                  }
                  return (
                    <span className="spec-value-item" key={vi}>
                      {thumb}
                      <Input
                        className="spec-value-input"
                        value={v}
                        maxLength={SPEC_VALUE_LEN_MAX}
                              //: ⚠️ 用户口径 2026-10-22：超出 60 字符**红框展示**（显示宽度，汉字计 2）。
                              status={exceedsLimit(v, SKU_NAME_MAX_CHARS) ? "error" : undefined}
                        placeholder="请选择或输入规格值"
                        //: ⚠️ 宽度固定 200px（用户口径）⇒ 长规格值显示不下。
                        //: 给 `title` 让用户**悬停看全文**（不靠加宽输入框解决）。
                        title={v || undefined}
                        onChange={(e) => setSpecValueAt(name, vi, e.target.value)}
                        onPressEnter={() => commitSpecValue(name, vi)}
                      />
                    </span>
                  );
                })}
              </div>
          </div>
        ))}

        {/* 规格图选择弹窗（用户口径：前缀点击 → 从预检通过且选中的 SKU 图里选）。 */}
        <SkuPickModal
          open={skuPick !== null}
          items={props.skuImages ?? []}
          fetchThumb={(rel_path) =>
            props.onFetchPrecheckImage
              ? props.onFetchPrecheckImage(rel_path)
              : Promise.reject(new Error("no fetcher"))
          }
          onPick={(rel_path, file_name) => void chooseSkuImage(rel_path, file_name)}
          onClose={() => setSkuPick(null)}
          //: ⚠️ 用户口径（2026-10-10）：「被选中的规格图不能重复被选中」——
          //:   传**其它**规格值已用掉的图（同规格自身当前那张可再次选中/更换）。
          usedNames={precheckNamesInUse}
          initialPicked={skuPick ? (specImages[skuPick.spec]?.[skuPick.value]?.precheck_name ?? null) : null}
          busy={specImageUploading > 0}
        />
        {/* 多件装打包弹窗（用户口径 2026-10-10）。 */}
        <PackageBundleModal
          open={packageBundleOpen}
          bundles={packageBundles}
          onChange={setPackageBundles}
          onClose={() => setPackageBundleOpen(false)}
        />
        {/* 批量修改列值弹窗（用户口径 2026-10-10）。 */}
        <PriceRowBatchEditModal
          open={batchEditOpen}
          allRows={props.priceRows}
          onChange={props.onPriceRowsChange}
          onClose={() => setBatchEditOpen(false)}
        />

        {/* 价格及库存表（用户口径 2026-10-09）：
            - **新增「规格图」列**（取该行第一个规格值在第一组的规格图）；
            - **移除操作列**（编辑价格 / 编辑库存两个行内入口撤掉）；
            - 表顶**规格名称搜索**；
            - **批量修改**与预检 sku 表同款（选列 + 加减/倍数/统一 + 符号图标切换 + 应用）。 */}
        <Field label="价格及库存">
          <div className="price-table-head">
            <Button size="small" onClick={() => setBatchEditOpen(true)}>
              批量修改列值
            </Button>
            <PriceRowBatchBar
              onChange={props.onPriceRowsChange}
              allRows={props.priceRows}
              onOpenPackage={() => setPackageBundleOpen(true)}
            />
            <Input.Search
              allowClear
              placeholder="搜索规格名称"
              style={{ width: 240 }}
              value={specKeyword}
              onChange={(e) => setSpecKeyword(e.target.value)}
            />
          </div>
          <Table<PriceRow>
            //: ⚠️ 用户口径 2026-10-21：「样式无需完全一样，**使用 antd 相关成熟的组件**，
            //: 内容一致即可」+「**减少使用 css**」——
            //: ⇒ 不再为对齐千帆写自定义 CSS，全部用 antd 原生能力：
            //:   · `bordered` 给网格线（平台是每格 1px 浅灰边）
            //:   · `size="middle"` 给接近平台的松弛行高（default 更接近 60px）
            //:   · 列上的 `align` / `fixed` 处理对齐与固定
            //:   · `rowSelection` 不启用（平台用操作列 Switch 控制上架，见列定义）
            bordered
            size="middle"
            rowKey="key"
            pagination={false}
            dataSource={filteredPriceRows}
            //: 停用行整体降透明度 —— 用 antd 的 `rowClassName` + 一行工具类，
            //: 不为此单独写样式块（见 css 里的 `.price-row-disabled`，只有 3 行）。
            rowClassName={(row: PriceRow) => (row.available === false ? "price-row-disabled" : "")}
            //: 用户口径 2026-10-11：「最高 400px，低于 400 自适应，高于 400 实现上下滚动」。
            //: ⚠️ 用 antd 原生 `scroll.y` 而不是外裹 div 的 max-height：
            //:   原生滚动会把**表头钉在顶部**（外裹 div 会把表头一起滚走，用户就看不到列名了）。
            //:
            //: ⚠️⚠️ **`scroll.x` 必须是数字，不能是 `"max-content"`**（用户口径 2026-10-22：
            //: 「UI 黑色主图需要调整优化」「运费模版下拉框切换页面反应很慢差不多需要 10 秒」）。
            //: 读 rc-table 源码定位的真机制：
            //:   `scrollX = scroll?.x` → `scrollWidth = typeof scrollX === "number" ? scrollX : null`
            //:   → `useWidthColumns()` 在 `scrollWidth === null` 时**整段跳过**
            //:   → `colWidths` 空 → `stickyOffsets.start` 全 0
            //:   → 固定列（第一维度 `fixed: "left"`）**全部叠在 left:0**、
            //:     且拿不到 `ant-table-cell-fix-left` 该有的实底 ⇒ 深色主题下露出
            //:     底下的黑，表现为「颜色分类整列黑成一片」。
            //:   ⇒ 同时它让每次渲染都要重新量宽，456 行 × 十余列在切换运费模板
            //:     这类整页重渲染时就是那 10 秒的来源。
            //: 取 2400：覆盖本表全部列宽之和（维度 135×N + 规格图 88 + 售价/库存
            //: 150~200 × 6 + 规格ID 230 + 条形码 200 + 商家编码 164 + 操作 150）。
            //:
            //: `virtual`：456 行非虚拟表格会把每行每列都渲染成真实 DOM（上万节点），
            //: 每次下拉切换都要重排一遍 —— 与第一步预检表同一口径（那里已启用）。
            virtual
            scroll={{ x: 2400, y: 400 }}
            columns={priceColumns}
          />
        </Field>
      </Section>

      <Section title="服务信息">
        {/* 物流类型（用户口径 2026-10-09：「物流类型应和类目有直接关系」）：
            - **默认「物流配送」**；仅当类目支持「无物流发货」时才出现第二个候选；
            - 说明走**图标 + Tooltip**（原话即 tip 文本）。 */}
        <Field
          label="物流类型"
          hint={supportNoLogistics === false ? "" : NOT_LOGISTICS_HINT}
        >
          <Radio.Group
            value={logisticsType}
            onChange={(e) => setLogisticsType(e.target.value)}
            options={(supportNoLogistics === false
              ? [LOGISTICS_TYPE_LABELS.logistics]
              : [LOGISTICS_TYPE_LABELS.logistics, LOGISTICS_TYPE_LABELS.notLogistics]
            ).map((o) => ({ label: o, value: o }))}
          />
          <Tooltip title={NOT_LOGISTICS_HINT} overlayStyle={{ maxWidth: 460 }}>
            <InfoCircleOutlined className="section-help-icon" style={{ marginLeft: 6 }} />
          </Tooltip>
        </Field>

        <Field label="运费模板" required>
          <Select
            style={{ width: 320 }}
            placeholder={props.shippingTemplates.length ? "请选择" : "模板加载中…"}
            loading={props.shippingTemplatesLoading}
            value={
              freightTemplateId ??
              props.shippingTemplates.find((t) => t.template_id === props.defaultTemplateId)
                ?.template_id ??
              undefined
            }
            onChange={(v) => setFreightTemplateId(v as string)}
            options={props.shippingTemplates.map((t) => ({
              label: t.name,
              value: t.template_id,
            }))}
          />
        </Field>

        {/* 开售时间（真机 bundle 逆出，2026-10-09 完整调研）：
            - 新建模式 3 项（`ON_SHELF=1` / `OFF_SHELF=2` / `SCHEDULE_SHELF=3`）；
            - 编辑模式 2 项（无「审核通过后立即上架开售」）；
            - 选「定时开售」⇒ 出现 `expectedPurchaseTime` 时间选择器，
              规则 `min = now + 1h`、`max = now + 30d`（bundle `expectedPurchaseTimeRule`）；
            - 校验：不在 [min,max] 内报「开售时间不能小于/大于 …」。 */}
        <Field label="开售时间">
          <Radio.Group
            value={saleTime}
            onChange={(e) => setSaleTime(e.target.value)}
            optionType="button"
            options={(props.isEditMode ? SHELF_TIME_OPTIONS_EDIT : SHELF_TIME_OPTIONS_CREATE).map(
              (o) => ({ label: o, value: o }),
            )}
          />
          {/* 选「定时开售」才出现时间选择器 + **开售前价格保密**（真机口径）。
              ⚠️ 用户口径（2026-10-10）：「**定时开售同理**」—— 与发货模式一致：
              灰底虚线框 + 父子关系（块内标题）。
              ⚠️ 用户口径 2026-10-09：「定时开售只展示了时间，还需要展示价格保密，默认不保密」。 */}
          {(saleTime === "定时开售" || saleTime === "定时开售（不设置）") && (
            <div className="child-block shelf-varying">
              <div className="child-block-title">
                <span>定时开售参数</span>
              </div>
              <div className="shelf-time-picker">
                <DatePicker
                  showTime={{ format: "HH:mm" }}
                  format="YYYY-MM-DD HH:mm"
                  value={expectedPurchaseTime}
                  onChange={setExpectedPurchaseTime}
                  disabledDate={(d) =>
                    //: min = now + 1h、max = now + 30d（bundle 规则）。
                    !!d && (d.valueOf() < Date.now() + 3600_000 || d.valueOf() > Date.now() + 30 * 86400_000)
                  }
                  placeholder="请选择开售时间"
                />
                <Tooltip title="开售时间需在 1 小时后至 30 天内">
                  <InfoCircleOutlined className="section-help-icon" />
                </Tooltip>
              </div>
              <div className="shelf-time-picker">
                <Checkbox
                  checked={priceConfidential}
                  onChange={(e) => setPriceConfidential(e.target.checked)}
                >
                  开售前价格保密
                </Checkbox>
                <Tooltip title="勾选后开售前不对外展示价格；默认不保密">
                  <InfoCircleOutlined className="section-help-icon" />
                </Tooltip>
              </div>
            </div>
          )}
        </Field>

        {/* 售后服务 —— ⚠️ 用户口径（2026-10-09）：「售后服务 7 天无理由是根据类目
            确认是否可编辑，这个问题你需要调研一下、真实测试抓取对应接口参数分析」。
            已实测 `getSupportAfterSale`：规则按类目下发，`mustSupport=true` ⇒ 平台强制
            （勾选不可取消），`false` ⇒ 可自选；规则**内容**也按类目变（服务券给
            「未核销随时退 + 超期自动退」）⇒ 按 props.afterSaleRules 渲染，不写死单条。 */}
        <Field label="售后服务">
          {afterSaleRules.length > 0 ? (
            <Space direction="vertical" size={4}>
              {afterSaleRules.map((r) => (
                <Checkbox
                  key={r.type || r.name}
                  checked={r.must_support || supportedTypes.includes(r.type)}
                  disabled={r.must_support}
                  onChange={(e) =>
                    setSupportedTypes((prev) =>
                      e.target.checked
                        ? [...new Set([...prev, r.type])]
                        : prev.filter((t) => t !== r.type),
                    )
                  }
                >
                  {r.name}
                  {r.must_support && (
                    <Tooltip title="该类目平台强制支持，不可取消">
                      <InfoCircleOutlined className="section-help-icon" />
                    </Tooltip>
                  )}
                </Checkbox>
              ))}
            </Space>
          ) : (
            <Text type="secondary">
              {props.categoryId ? "该类目无需配置售后规则" : "请先选择商品类目"}
            </Text>
          )}
        </Field>

        <Field label="商品英文名">
          <Input
            style={{ width: 320 }}
            value={englishName}
            onChange={(e) => setEnglishName(e.target.value)}
            placeholder="请输入"
          />
        </Field>
      </Section>

      {/* 尺码表弹窗（真机：尺码信息 = 助手里的可点模块；数据来自 `getSizeTableInfo`）。 */}
      <Modal
        open={sizeTableOpen}
        title="设置尺码表"
        width={720}
        centered
        okText="保存"
        cancelText="取消"
        onCancel={() => setSizeTableOpen(false)}
        onOk={() => setSizeTableOpen(false)}
      >
        <Field label="尺码表" hint={SIZE_TABLE_SUGGEST}>
          <Space wrap size={12}>
            {SIZE_TABLE_KINDS.map((k) => (
              <Checkbox key={k} defaultChecked={k === "基础尺码"}>
                {k}
              </Checkbox>
            ))}
          </Space>
        </Field>
        {SIZE_TABLE_KINDS.map((k) => {
          const shape = Object.values(SIZE_TABLE_SHAPES).find((v) => v.label === k);
          if (!shape) return null;
          return (
            <Field key={k} label={`${k}配置方式`} hint={`参数：${shape.fields.join(" / ")}`}>
              <Radio.Group
                optionType="button"
                defaultValue={SIZE_CONFIG_TYPES.form}
                options={[
                  { label: SIZE_CONFIG_TYPES.form, value: SIZE_CONFIG_TYPES.form },
                  { label: SIZE_CONFIG_TYPES.image, value: SIZE_CONFIG_TYPES.image },
                ]}
              />
            </Field>
          );
        })}
      </Modal>

      {/* 商品资质弹窗（真机：`qualification` 进提交 payload，按类目激活）。 */}
      <Modal
        open={qualificationOpen}
        title="配置商品资质"
        width={600}
        centered
        okText="保存"
        cancelText="取消"
        onCancel={() => setQualificationOpen(false)}
        onOk={() => setQualificationOpen(false)}
      >
        <Field label="资质文件" hint="按类目要求上传对应资质；无要求时留空">
          <Button size="small" type="dashed" icon={<PlusOutlined />}>
            上传资质
          </Button>
        </Field>
      </Modal>

      {/* 2026-10-13 用户口径：移除「切换发货模式温馨提示」弹窗 —— 切换直接生效，
          且不重置库存/SKU、属性参数不做关联。 */}
        </div>
      </div>
    </div>
  );
}
