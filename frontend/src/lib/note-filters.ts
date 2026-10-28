/**
 * 内容库筛选器的**共用文案**（纯逻辑，无 antd 依赖）。
 *
 * 为什么单独成文件：页面（antd Select）与行为脚本（`tests/frontend/*.cjs`，不桩 antd）
 * 是两份消费者，各写一遍文案必然漂移 —— 界面能选的值与用户看到的名字对不上。
 *
 * ⚠️ 笔记类型（`local_generated` / `collected`）的标签、配色与筛选下拉**已整条移除**
 * （2026-10-27）：内容库只收录这两类，类型恒为已知值，「按类型筛选」没有意义。
 */

/**
 * 账号下拉选项的文案：**昵称（取不到就明说）**，与内容库「来源账号」列同一事实。
 *
 * 内容库不展示店铺 / 类型后缀（那是商品台账的口径）—— 这里只有一个账号昵称，
 * 昵称为空时写「未命名账号」，不留空白让用户猜是哪一行。
 */
export function accountFilterLabel(account: { nickname?: string }): string {
  return (account?.nickname || "").trim() || "未命名账号";
}
