/**
 * 小红书笔记搜索 / 抓取的**共享筛选选项**（设计 21 切片 B，2026-10-04）。
 *
 * ## 为什么抽这个文件
 *
 * 「笔记发现」（`discovery-page.tsx`）与「数据抓取」（`crawler-page.tsx`）原先**逐字复制**
 * 了同一份 5 组筛选常量。两页合并为一页两 Tab 后，下拉候选必须**单一来源** ——
 * 否则一处加了新排序维度、另一处漏改，用户会发现「同一个筛选项在两个 Tab 里不一样」，
 * 而这类漂移没有任何断言会变红（DEF-044 同型：同一件事两份实现）。
 *
 * ## 口径
 *
 * 取值与 label 与平台侧 `XhsSearchOptions` 的语义一一对应，**不得就地改写**：
 * `sort_type_choice` / `note_type` / `note_time` / `note_range` / `pos_distance`
 * 都是传给后端 SDK 的原始枚举值（0 = 不限）。
 */

/** 排序维度：综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏。 */
export const SORT_OPTIONS = [
  { value: 0, label: "综合排序" },
  { value: 1, label: "最新" },
  { value: 2, label: "最多点赞" },
  { value: 3, label: "最多评论" },
  { value: 4, label: "最多收藏" },
];

/** 笔记类型：不限 / 视频笔记 / 普通笔记。 */
export const NOTE_TYPE_OPTIONS = [
  { value: 0, label: "不限类型" },
  { value: 1, label: "视频笔记" },
  { value: 2, label: "普通笔记" },
];

/** 发布时间范围：不限 / 一天内 / 一周内 / 半年内。 */
export const NOTE_TIME_OPTIONS = [
  { value: 0, label: "不限时间" },
  { value: 1, label: "一天内" },
  { value: 2, label: "一周内" },
  { value: 3, label: "半年内" },
];

/** 笔记范围：不限 / 已看过 / 未看过 / 已关注。 */
export const NOTE_RANGE_OPTIONS = [
  { value: 0, label: "不限范围" },
  { value: 1, label: "已看过" },
  { value: 2, label: "未看过" },
  { value: 3, label: "已关注" },
];

/** 距离维度：不限距离 / 同城 / 附近。 */
export const DISTANCE_OPTIONS = [
  { value: 0, label: "不限距离" },
  { value: 1, label: "同城" },
  { value: 2, label: "附近" },
];
