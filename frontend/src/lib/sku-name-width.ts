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

/** 是否触发优化（**达到**即触发，与后端 `is_too_long` 同口径）。 */
export function isTooLong(name: string, maxChars: number): boolean {
  return displayWidth(name) >= maxChars;
}
