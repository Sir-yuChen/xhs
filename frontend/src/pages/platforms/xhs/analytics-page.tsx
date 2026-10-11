/**
 * 「数据洞察（旧）」—— **只剩重定向**（设计 32 §6 / A6，2026-10-11 用户拍板 D2「拆」）。
 *
 * ## 为什么保留这个文件而不是删掉
 *
 * 原 `/platforms/xhs/analytics` 一页两 Tab（内容洞察 / 指标监控）。
 * 现在拆成两条业务线各自的页面：
 *  * `/platforms/xhs/insights/content` —— 内容洞察（笔记侧）
 *  * `/platforms/xhs/insights/shop`    —— 商品洞察（店铺侧）
 *
 * 保留本文件做**重定向**，理由与设计 21 / 22 里那几处一致：**深链与书签不死**。
 * 用户的收藏夹 / 浏览器历史 / 别人发过的链接里都是 `/analytics`，
 * 直接删文件会让它们 404 —— 「页面搬家」不该以用户失去入口为代价。
 *
 * ⚠️ 旧的 `?tab=metrics` 一律回落到**内容洞察**（笔记侧）：
 * 拆页后「指标监控」不再是一个页签，而它的**主用途**是看笔记趋势
 * （商品指标在拆出的「商品洞察」里有更完整的呈现）。这是刻意的**保底**，
 * 不是「猜用户想要什么」——它保证链接一定有落地页。
 */
import { Navigate, useSearchParams } from "react-router-dom";

export function XhsAnalyticsPage() {
  const [searchParams] = useSearchParams();
  const tab = searchParams.get("tab");

  //: 只有「商品侧」的旧链接才落到商品洞察；其余（含无参数、`insight`、`metrics`）
  //: 一律落内容洞察。判据集中在这里一处，不要在别处再判一次。
  const target = tab === "shop" ? "/platforms/xhs/insights/shop" : "/platforms/xhs/insights/content";

  return <Navigate to={target} replace />;
}
