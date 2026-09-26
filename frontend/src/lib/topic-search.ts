/**
 * 话题下拉的远程模糊搜索节流（设计 13 §34.4「话题筛选下拉的模糊搜索」）—— **纯逻辑，唯一实现**。
 *
 * 为什么抽出来：内容库筛选栏话题下拉走远程搜索（话题字典随同步链路增长，
 * 全量拉回再前端过滤只是把成本挪到进页面时）。用户口径「控制查询频率」⇒
 * 输入停下来才发请求：本文件管「停多久算停」，页面只管把请求函数交给它。
 * 行为脚本（`tests/frontend/library_topic_search.cjs`）直接 import 它。
 *
 * 防抖语义（与 antd Select 的 `onSearch` 逐次输入配合）：
 *
 *     search(a) → search(b) → [300ms 无新输入] → 只调 run(b)
 *
 * —— 连续击键只发**最后一次**，中途的输入不会打到后端（每一次都是一次
 * `GET /topics?keyword=` 请求，频率必须受控）。
 */

/** 防抖窗口（毫秒）：输入停止这么久才认为「用户想好了」。 */
export const TOPIC_SEARCH_DEBOUNCE_MS = 300;

/** `createTopicSearchDebouncer` 的返回：`search` 供 onSearch 用，`cancel` 供卸载/关闭用。 */
export interface TopicSearchDebouncer {
  /** 输入变化时调（可高频）；内部按 `TOPIC_SEARCH_DEBOUNCE_MS` 合并。 */
  search: (keyword: string) => void;
  /** 取消尚未触发的那一次（组件卸载 / 下拉关闭时调，防泄漏与过期回填）。 */
  cancel: () => void;
}

/**
 * 造一个防抖搜索器。`run` 是真正的查询函数（页面传 `fetchTopics` 包装）。
 *
 * ⚠️ 用 `setTimeout` 而非 `requestIdleCallback`：测试与行为脚本需要**确定性的
 * 时间语义**（`waitFor(条件, {timeout})` 等它触发），idle 回调的触发时机由
 * 浏览器调度，不可断言。
 */
export function createTopicSearchDebouncer(
  run: (keyword: string) => void,
  delayMs: number = TOPIC_SEARCH_DEBOUNCE_MS
): TopicSearchDebouncer {
  let timer: ReturnType<typeof setTimeout> | null = null;

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function search(keyword: string) {
    cancel(); // 新输入作废上一次未触发的（防抖 = 只认最后一次）
    timer = setTimeout(() => {
      timer = null;
      run(keyword);
    }, delayMs);
  }

  return { search, cancel };
}
