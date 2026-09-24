/**
 * 内容库的三项「枚举筛选」口径 —— **纯逻辑，唯一实现**（2026-09-23）。
 *
 * 为什么抽出来：账号 / 笔记类型 / 发布状态三组下拉的取值、文案与「不筛」表达
 * 必须与后端查询参数**逐字对齐**，而且要在页面（antd Select）与行为脚本
 * （`tests/frontend/note_filters.cjs`，不需要 antd 桩）**两份消费者**之间共用。
 * 各写一遍 = 必现漂移（DEF-044/047/058 同型）：界面能选的值后端不认、
 * 或后端 400 的取值界面没有出口。
 *
 * 与后端同一把尺子：
 * - 账号：`GET /notes?platform_account_id=`（**不传 = 不筛**，无「全部」魔法值）；
 * - 笔记类型：`GET /notes?note_type=`（合法值见后端 `NOTE_TYPE_FILTER_VALUES`，
 *   未知值 400 `NOTE_TYPE_UNSUPPORTED`）；
 * - 发布状态：`GET /notes?publish_status=`（四档，`all` = 不传，见 `note-publish-gates`）。
 */

/**
 * 笔记类型筛选值（设计 13 §25）。
 *
 * `""` = **不筛**（界面上的「全部类型」）—— 它**不是**一个类型，所以不进
 * `NOTE_TYPE_FILTER_VALUES`（避免把「不筛」混进枚举比较里）。
 */
export type NoteTypeFilter = "" | "synced" | "local_generated" | "local";

/** 后端认的笔记类型取值（**不含**「不筛」；顺序即下拉顺序：同步 → 生成 → 本地）。 */
export const NOTE_TYPE_FILTER_VALUES: readonly string[] = [
  "synced",
  "local_generated",
  "local",
];

/** 笔记类型下拉（首项是「全部类型」= 不筛）。 */
export const NOTE_TYPE_FILTER_OPTIONS: { value: NoteTypeFilter; label: string }[] = [
  { value: "", label: "全部类型" },
  { value: "synced", label: "三方同步" },
  { value: "local_generated", label: "本地生成" },
  { value: "local", label: "本地采集" },
];

/**
 * 单条笔记的类型文案（列表 / 详情 / 导出共用一份）。
 *
 * 与 `NOTE_TYPE_FILTER_OPTIONS` 同源：下拉里能选的类型，列表里必须能对上号，
 * 否则用户会以为「筛了但没筛到」。未知值（老响应 / 未来新增类型）按缺值口径
 * 返回**原始值**，不编一个中文名糊上（硬约束⑤）。
 */
export function noteTypeLabel(noteType: string | undefined): string {
  const key = (noteType ?? "").trim();
  const hit = NOTE_TYPE_FILTER_OPTIONS.find((option) => option.value === key && key !== "");
  return hit ? hit.label : key;
}

/**
 * 「这条笔记该不该有同步按钮」—— **唯一判定**（2026-09-23 用户口径）。
 *
 * > 需求原文：「已经发布过的笔记一定存在同步按钮功能」。
 *
 * 两个条件任一成立就渲染：
 * ① `note_type === "synced"` —— 三方同步笔记，本来就有平台最新内容可拉；
 * ② `publish_status === "published"` —— **发布过**（含本地生成发布成功的）。
 *
 * ⚠️ ② 是这次新加的，理由是「发布过」意味着这条笔记**在平台上真实存在**，
 * 于是它就有了「拉最新内容」的对象 —— 这与它是怎么来的无关。
 * 漏掉它的实际表现（真实库已复现）：本地生成笔记发布成功后仍是
 * `local_generated`，旧判定只看类型 ⇒ **卡片上本来就没有同步按钮，发布成功后
 * 更不会长出来**，用户看到的就是「明明发出去了，却没有同步」。
 *
 * 为什么按 `publish_status` 而不是 `published` 投影（设计 13 §32.3.2）：
 * `publish_status` 是**权威字段**，`published` 只是给 CSV / 既有消费者的投影；
 * 判定用户能做什么事，必须看权威那一列。
 *
 * ⚠️ **前后端同口径**（2026-09-23 起）：后端 `POST /notes/{id}/sync` 的闸门已同步
 * 放宽到同一个判据（`backend/app/models/note.py::note_supports_sync()`，判据是
 * `synced` 或 `publish_status == "published"`），不满足时给结构化 400
 * **`NOTE_NOT_SYNCABLE`**（原 `NOTE_NOT_SYNCED` 已随口径改名）。
 * ⇒ 按钮显示与接口放行**一致**，不会再出现「按钮在、点下去 400」。
 * ⚠️ 改这条判据时**必须同时改后端那个函数** —— 两份判据各改一半就是
 * 「按钮显示却 400」或「按钮没有却还能调」。
 */
export function canSyncNote(note: { note_type?: string; publish_status?: string }): boolean {
  if ((note?.note_type ?? "").trim() === "synced") return true;
  return (note?.publish_status ?? "").trim() === "published";
}

/**
 * 账号下拉选项的文案：**昵称（取不到就明说）**，与内容库「来源账号」列同一事实。
 *
 * 内容库不展示店铺 / 类型后缀（那是商品台账的口径）—— 这里只有一个账号昵称，
 * 昵称为空时写「未命名账号」，不留空白让用户猜是哪一行。
 */
export function accountFilterLabel(account: { nickname?: string }): string {
  return (account?.nickname || "").trim() || "未命名账号";
}

/**
 * 笔记类型 Tag 的配色（2026-09-23 用户口径：**类型改用 Tag 放在封面右下角**）。
 *
 * 与详情弹窗**同一份映射**（原先详情里是内联三元表达式，卡片再写一遍就是
 * 两份实现 —— DEF-061 的同型教训）：同一字段的呈现只允许一个出处。
 *
 * * `synced` → 蓝（`CloudSyncOutlined` 同色系，与筛选下拉的「三方同步」呼应）；
 * * `local_generated` → 紫（详情弹窗原本就是这个色，保持一致）；
 * * `local` → 默认灰（采集来的别人笔记，视觉重量最轻）；
 * * 未知 / 缺值 → 默认灰，**不编颜色**（硬约束⑤）。
 */
export function noteTypeTagColor(noteType: string | undefined): string {
  switch ((noteType ?? "").trim()) {
    case "synced":
      return "blue";
    case "local_generated":
      return "purple";
    default:
      return "default";
  }
}

/**
 * 卡片级 Tooltip 的**去向说明**（2026-09-23 用户口径，两轮演变）：
 *
 * ① 第一轮「卡片上不展示了，鼠标放到卡片上显示标签即可」⇒ 类型只在悬停提示里，
 *    当时这里是一段「类型名 + 一句它意味着什么」的长文案。
 * ② 第二轮「笔记卡片上应展示本地生成或三方同步」⇒ 类型改成**正文文本**。
 * ③ 第三轮「三方同步/本地生成使用 tag 放图片右下角」⇒ 类型改成**封面右下角 Tag**，
 *    已经是**直接可见**的；同时用户要求把
 *    「三方同步笔记：账号同步链路落库，可拉取平台最新内容」与
 *    「本地生成笔记：由商品智能生成，待发布 / 发布失败可重发」**两句长文案移除**。
 *
 * ⇒ 类型既然直接可见，悬停提示就成了「复读机」（提示文字和Tag一字不差），
 * **`noteTypeTooltip` 与卡片级 Tooltip 整条移除**，不留一个只会重复的提示框。
 * 若以后要给类型补说明，应该是**新的、短的**文案，并且要先问「用户在这里
 * 真正要判断什么」—— 长解释历来是噪声（ADR-018：长文案收起而不是铺开）。
 */

