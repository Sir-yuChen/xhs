/**
 * 店铺违规管理 · 类型（设计 43，2026-10-18）
 *
 * 契约来自后端 `/api/xhs/qianfan/violation/*`，后端契约来自千帆真机抓包
 * （`docs/02_设计文档/_调研_千帆店铺违规管理_2026-10-18.md`）。
 *
 * ⚠️ 这里的字段名与后端 `violation_service.normalize_violation_row()` **逐字对应**；
 * 改一侧必须改另一侧（守卫 `test_violation_manage.py` 与 `violation-manage.test.ts` 各钉一半）。
 */

/** 下拉选项（处罚状态 / 违规对象，后端下发，前端不硬编码）。 */
export type ViolationEnumOption = {
  value: number;
  label: string;
};

/** 违规原因 tag 组（`tag_name` **可能带尾空格**，前端按字符串精确匹配，不 trim）。 */
export type ViolationReasonTag = {
  tag_name: string;
  tag_ids: string[];
};

/** 枚举集合（`POST /violation/meta`）。 */
export type ViolationMeta = {
  violation_status: ViolationEnumOption[];
  violation_entity: ViolationEnumOption[];
  reason_tags: ViolationReasonTag[];
  spot_check_reason_tags: ViolationReasonTag[];
};

/** 列表一行（列表与详情共用同一份 dto 形状）。 */
/** 删除关联内容的一条回执状态（设计 44，**先平台后本地**）。 */
export type ViolationDeleteStatus =
  | "deleted"        // 平台删成功 + 本地有 ⇒ 本地也删了
  | "platform_only"  // 平台删成功 + 本地没有 ⇒ 跳过本地（不是失败）
  | "failed"         // 平台删除失败 ⇒ 本地未改动
  | "skipped"        // 店铺 / 账号 / 直播间：没有可删实体
  | "absent";        // 平台已无此数据且本地也没有 ⇒ 已在别处删过，不重复删

/** 删除关联内容的一条回执。 */
export type ViolationDeleteResult = {
  entity_type: number;
  entity_id: string;
  ok: boolean;
  status: ViolationDeleteStatus;
  reason?: string;
};

/** 删除关联内容的响应（单条与批量同形；四态分别计数）。 */
export type ViolationDeleteResponse = {
  /** 请求条数。 */
  requested: number;
  /** 平台删成功且本地也删掉的条数。 */
  deleted: number;
  /** 平台删成功但本地没有（跳过）的条数。 */
  platform_only: number;
  /** 平台删除失败的条数。 */
  failed: number;
  /** 该对象类型没有可删实体的条数。 */
  skipped: number;
  /** 平台已无此数据且本地也没有（已在别处删过，不重复删）的条数。 */
  absent?: number;
  results: ViolationDeleteResult[];
};

/** 一条删除目标：违规行的对象类型 + 对象 ID。 */
export type ViolationDeleteTarget = {
  entity_type: number;
  entity_id: string;
};

export type ViolationRow = {
  violation_id: string | null;
  violation_reason: string | null;
  violation_desc: string | null;
  violation_time: number | null;
  tenant: string | null;
  entity_type: number | null;
  entity_type_name: string | null;
  entity_id: string | null;
  entity_name: string | null;
  entity_icon: string | null;
  entity_ext: Record<string, unknown> | null;
  level: number | null;
  level_name: string | null;
  status: number | null;
  status_name: string | null;
  appeal_status: number | null;
  appeal_status_name: string | null;
  actions: string[];
  action_codes: string[];
  can_appeal: boolean | null;
  can_punished: boolean | null;
  correctable: boolean | null;
  severe: boolean | null;
  read: number | null;
  exam_status: string | null;
  tag_id: string | null;
  warn_flag: number | null;
  modify_advice: string | null;
  modify_advice_link: string | null;
  ext_info: string | null;
  period_end_time: number | null;
  /** 平台原始 dto 的其余字段（详情弹窗会用，如 `richProcessId`）。 */
  [key: string]: unknown;
};

/** 详情里每条处罚的执行时间与状态。 */
export type ViolationActionDetail = {
  name: string | null;
  code: string | null;
  time_info: string | null;
  status: number | null;
};

export type ViolationDetail = ViolationRow & {
  action_details: ViolationActionDetail[];
};

export type ViolationListResponse = {
  total: number;
  items: ViolationRow[];
  scene: "violation" | "warning";
  query: Record<string, unknown>;
  account?: { id: number | null; nickname: string | null };
};

export type ViolationCountsResponse = {
  quick: string;
  count: number;
};

export type ViolationListQuery = {
  platform_account_id: number;
  scene?: "violation" | "warning";
  page_no?: number;
  page_size?: number;
  keyword?: string;
  entity_types?: number[];
  reason_name?: string;
  statuses?: number[];
  severe_only?: boolean;
  allow_appeal?: boolean | null;
  correctable?: boolean | null;
  unread_only?: boolean;
  exam_only?: boolean;
  pending_only?: boolean;
  recent_days?: boolean;
  date_start?: string;
  date_end?: string;
  quick?: string;
};

export type ViolationDetailQuery = {
  platform_account_id: number;
  violation_id: string;
  tenant?: string;
  tag_id?: string;
};

export type ViolationEvidenceQuery = {
  platform_account_id: number;
  violation_id: string;
  entity_id?: string;
  entity_type?: number | null;
  tag_id?: string;
  tenant?: string;
};

export type ViolationMetaQuery = {
  platform_account_id: number;
  include_spot_check?: boolean;
};

export type CheckupRisksQuery = {
  platform_account_id: number;
  page_no?: number;
  page_size?: number;
};

export type NegativeFeedbackQuery = {
  platform_account_id: number;
  kind: "quality" | "logistics";
  page_no?: number;
  page_size?: number;
  view?: "list" | "summary";
  time_window_days?: number;
};

export type PagedSellerQuery = {
  platform_account_id: number;
  page_no?: number;
  page_size?: number;
  keyword?: string;
};

export type SpotCheckQuery = {
  platform_account_id: number;
  page_no?: number;
  page_size?: number;
  keyword?: string;
  reason_name?: string;
  allow_appeal?: boolean | null;
  punish_status?: string;
};

export type ExemptionItemsQuery = {
  platform_account_id: number;
  page_no?: number;
  page_size?: number;
  date_start?: string;
  date_end?: string;
};

/** 通用「列表 + 总数」响应（赔付 / 仲裁 / 负反馈 / 抽检 / 免罚明细共用）。 */
export type ViolationPagedResponse<T = Record<string, unknown>> = {
  total: number;
  items: T[];
  kind?: string;
  view?: string;
  query?: Record<string, unknown>;
};

export type CheckupAdviceResponse = {
  items: Record<string, unknown>[];
  high_price: {
    tag?: number;
    tagDesc?: string;
    highPriceItemNum?: number;
    yesHighPriceItemNum?: number;
    highPriceItemDiffNum?: number;
    highPriceEffectRate?: number;
  } | null;
};
