/**
 * **SKU 名称显示宽度**（前后端唯一口径）。
 *
 * 用户口径 2026-10-20：「30 字 60 个字符」「以 60 字符为准」
 * ⇒ 计量方式是**显示宽度**：**汉字计 2、ASCII 计 1**。
 *
 * 为什么不能用 `str.length`：
 * `"中古色-实木三斗柜".length` 是 9（汉字按 1 算），而它在界面上占 9 个"半角位"
 * 之外还要再宽一倍 —— 用 `length` 判 60 个汉字（=120 宽）会被当成 60 而漏优化。
 *
 * ⚠️ 后端 `backend/app/services/sku_name_optimize_service.display_width` 必须与
 * 本函数**逐字同口径**：两边不一致就会出现「页面说超长、后端说没超」的静默错位。
 */
export function displayWidth(text: string): number {
  let width = 0;
  for (const ch of text || "") {
    const code = ch.codePointAt(0) ?? 0;
    const wide =
      (code >= 0x1100 && code <= 0x115f) || // 韩文字母
      (code >= 0x2e80 && code <= 0xa4cf) || // CJK 部首 / 假名 / 汉字
      (code >= 0xac00 && code <= 0xd7a3) || // 韩文音节
      (code >= 0xf900 && code <= 0xfaff) || // CJK 兼容汉字
      (code >= 0xfe30 && code <= 0xfe6f) || // CJK 兼容形式
      (code >= 0xff00 && code <= 0xff60) || // 全角标点
      (code >= 0xffe0 && code <= 0xffe6) || // 全角符号
      (code >= 0x20000 && code <= 0x3fffd); // CJK 扩展 B+
    width += wide ? 2 : 1;
  }
  return width;
}

/** 是否**超长**（严格大于）—— 弹窗**展示**与**阻断**的唯一判据。
 *
 *  ⚠️⚠️ 用户口径 2026-10-22：「**小于 60 字符就不应该展示**在 SKU 规格名称
 *  优化预览弹窗列表中」。
 *
 *  演化（原句保留，不静默删除）：
 *    · 2026-10-20 口径是「**达到**即触发」（`>=`）—— 那时弹窗是「提醒你可优化」；
 *    · 2026-10-22 起弹窗的语义变成「**这些必须处理**」⇒ 60 字符**合法**
 *      （平台原文「不能超过 30 个字（60 个字符）」= 60 本身没问题）
 *      ⇒ 必须用 **严格大于**，否则列表里会混进一堆 60 的合规项（用户截图为证）。
 *
 *  ⚠️ 与 `exceedsLimit` 现在是**同一判据**，但仍保留两个名字：
 *   `isTooLong` 用于「要不要展示 / 要不要优化」，`exceedsLimit` 用于「门禁」，
 *   语义侧重点不同；日后若再分叉（比如「达到就建议优化」回来），只改一处。
 */
export function isTooLong(name: string, maxChars: number): boolean {
  return displayWidth(name) > maxChars;
}

/** 是否**真的超限**（严格大于）—— 用于「超出则不允许进入下一步」的门禁。
 *
 *  ⚠️ 与 `isTooLong` 刻意分开：
 *    · `isTooLong`（≥）：**达到**上限就值得提示优化（60 字符时先给个建议）；
 *    · `exceedsLimit`（>）：**超过**才阻断（用户口径「长度不超过 60」⇒ 60 本身合法）。
 *  两者混用一个函数会在边界上打架（60 字符到底是合法还是要优化），必须分开。 */
export function exceedsLimit(name: string, maxChars: number): boolean {
  return displayWidth(name) > maxChars;
}

/** 规格名长度上限（**字符**，汉字计 2）—— 与后端配置项
 *  `xhs_sku_name_max_chars` 的默认值同口径（60）。
 *
 *  ⚠️ 平台原文（真机发布失败提示）：「规格名不能超过 **30 个字（60 个字符）**」。
 *  前端用同一常量做三处判据：超长弹窗的阈值、规格组输入框的实时提示、
 *  「下一步」的门禁 —— **一处定义，三处引用**（避免各自写死 60 再漂移）。 */
export const SKU_NAME_MAX_CHARS = 60;
