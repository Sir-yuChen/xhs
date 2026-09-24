/**
 * 发布门禁与发布态呈现（设计 13 §31「发布门禁」+ §32.3「发布状态唯一字段」）—— **纯逻辑，唯一实现**。
 *
 * 为什么抽出来：内容库卡片、详情弹窗、批量发布三处都要回答同一组问题
 * （这篇能不能发 / 失败态长什么样 / 批量要跳过几条）。三处各写一遍 =
 * 必现漂移（DEF-044/047/058 同型）；页面与测试调同一份函数，
 * 行为脚本（`tests/frontend/note_publish.cjs`）直接 import 它。
 *
 * 判据只有一条（与后端 `PUBLISHABLE_STATUSES` 对齐）：
 *
 *     note_type === "local_generated" && publish_status ∈ {pending, failed}
 *
 * —— 采集 / 同步笔记不走本链路（它们本来就是平台已存在或别人的内容），
 * 已发布的不能重复发。
 */

/** 可发布的状态（与后端常量同名同值；后端是权威，这里是前端的同一把尺子）。 */
export const PUBLISHABLE_STATUSES = ["pending", "failed"] as const;

/** 发布状态 → 展示标签（四态；空 = 不适用，不渲染）。 */
export const PUBLISH_STATUS_LABELS: Record<string, { text: string; color: string }> = {
  pending: { text: "待发布", color: "orange" },
  publishing: { text: "发布中", color: "blue" },
  published: { text: "已发布", color: "green" },
  failed: { text: "发布失败", color: "red" },
};

/**
 * 发布状态**筛选**四档（设计 13 §32.3.3，用户口径「筛选条件新增发布失败状态」）。
 *
 * 取值与后端 `/notes?publish_status=` 的查询参数**逐字同值**（`all` = 不传，不筛）；
 * 界面上的第 4 档「发布失败」就是本次新增的那一档。
 */
export type PublishStatusFilter = "all" | "pending" | "published" | "failed";

export const PUBLISH_STATUS_FILTER_OPTIONS: { value: PublishStatusFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "pending", label: "待发布" },
  { value: "published", label: "已发布" },
  { value: "failed", label: "发布失败" },
];

/**
 * 一张**唯一**的发布状态标签（设计 13 §32.3.4：卡片与详情只展示一个发布状态）。
 *
 * 为什么要有这个函数：改之前卡片上并排着两个状态（「已发布/待发布」开关 +
 * 「发布失败」tag），既有重复又可能互相矛盾。现在状态只有一个来源
 * （`publish_status`），本函数把它翻译成「文案 + 颜色 + 能不能点 + 是不是失败态」。
 *
 * @param noteType 落库链路决定的类型（`local_generated` 的状态由链路管理，不可手切）
 * @param publishStatus `publish_status`；空值（迁移前的历史形态）按「待发布」显示
 */
export type PublishStatusView = {
  text: string;
  color: string;
  /** 本地生成笔记 ⇒ `false`（状态由链路决定，手改会被后端 400 挡回，界面不给入口）。 */
  toggleable: boolean;
  /** 失败态：卡片上要挂 Tooltip 展示完整失败原因（长文案 `…` 收起，ADR-018）。 */
  failed: boolean;
};

export function publishStatusTag(
  noteType: string,
  publishStatus: string | undefined
): PublishStatusView {
  const status = (publishStatus ?? "").trim();
  const label = PUBLISH_STATUS_LABELS[status] ?? PUBLISH_STATUS_LABELS.pending;
  return {
    text: label.text,
    color: label.color,
    toggleable: noteType !== "local_generated",
    failed: status === "failed",
  };
}

/**
 * 手动切换的**目标**状态（开关语义：已发布 ⇄ 待发布）。
 *
 * 只在 `toggleable` 时调用；本地生成的笔记不给切换入口（见 `publishStatusTag`）。
 */
export function nextManualStatus(publishStatus: string | undefined): "pending" | "published" {
  return publishStatus === "published" ? "pending" : "published";
}

/**
 * 这篇笔记**能不能发布**（卡片 / 详情 / 批量共用）。
 *
 * @param noteType 落库链路决定的类型（`local_generated` 才可发布）
 * @param publishStatus 发布状态机（`""` = 不适用）
 */
export function canPublish(noteType: string, publishStatus: string | undefined): boolean {
  return noteType === "local_generated" && PUBLISHABLE_STATUSES.includes(publishStatus as never);
}

/**
 * 批量发布的**目标与跳过数**（确认框要用：跳过几条必须明示，不静默丢）。
 *
 * @param notes 当前列表（已选的会在里面）
 * @param selectedIds 勾选的笔记 id
 * @returns `{ targets, skipped }` —— targets 保持传入顺序（批量顺序处理）
 */
export function splitPublishTargets<T extends { id: number; note_type: string; publish_status?: string }>(
  notes: T[],
  selectedIds: number[]
): { targets: T[]; skipped: number } {
  const byId = new Map(notes.map((note) => [note.id, note]));
  const targets: T[] = [];
  let skipped = 0;
  for (const id of selectedIds) {
    const note = byId.get(id);
    if (note && canPublish(note.note_type, note.publish_status)) {
      targets.push(note);
    } else {
      skipped += 1;
    }
  }
  return { targets, skipped };
}
