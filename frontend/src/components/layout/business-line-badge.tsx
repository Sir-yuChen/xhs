/**
 * 业务线标识条（设计 32 §5.2 / §5.3；2026-10-11）。
 *
 * ## 它解决什么
 *
 * 侧栏把入口**分了组**（A3），但分组只在侧栏可见 —— 用户点进「商品列表」之后，
 * 界面上就没有任何东西说明「这一页要千帆（ark）令牌」。市场同类工具的共识是
 * **让上下文在选择时可见**（不要靠记忆）：
 *
 * > *「Make context visible … Show the selected brand, audience, offer, voice, and
 * > current campaign wherever someone is researching, drafting, reviewing, or publishing.」*
 *
 * ## 为什么不隐藏、而只标注
 *
 * `docs/02_设计文档/08` §4.5（用户口径）：
 * *「用户需要理解『为什么这家不能挂商品』，隐藏起来只会被当成 bug 报过来」*。
 * ⇒ 本组件**只标注**，不做门禁、不发请求、不改任何数据流。
 *
 * ⚠️ **零请求**：本组件不调任何接口。它的**存在**就是为了让用户知道该去哪补登，
 * 而不是替业务页面判一次可用性（前端重判必然与后端漂移，先例：设计 12 §17.8）。
 */
import { Tag } from "antd";
import { useLocation } from "react-router-dom";

import { BUSINESS_LINE_VIEWS, businessLineForPath } from "../../lib/business-lines";

/** 各业务线的 Tag 配色（唯一实现）。 */
const LINE_COLORS: Record<string, string> = {
  shop: "red",
  content: "blue",
  shared: "default",
};

export function BusinessLineBadge() {
  const location = useLocation();
  // 路由不在业务线映射里（如「总览」）⇒ 整条不渲染。
  // ⚠️ 不渲染 ≠ 漏登记：漏登记会让守卫红（`test_sidebar_...` 对 ROUTE_BUSINESS_LINES
  // 与侧栏分组做双向比对），所以这里的「不渲染」只服务真正的中性页。
  const line = businessLineForPath(location.pathname);
  if (line === null) return null;

  const view = BUSINESS_LINE_VIEWS[line];
  return (
    <div
      // 页面顶部一行：Tag + 说明。**高度固定 22px**，避免切页时内容区跳动。
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        height: 22,
        marginBottom: 8,
        fontSize: 12,
      }}
    >
      <Tag color={LINE_COLORS[line]} style={{ margin: 0, borderRadius: 4 }}>
        {view.caption}
      </Tag>
      {view.channel ? (
        <span style={{ opacity: 0.65 }}>
          本页读写的是本地库与{view.channelLabel}令牌
        </span>
      ) : (
        <span style={{ opacity: 0.65 }}>该能力服务于整个平台，不属于任一业务线</span>
      )}

    </div>
  );
}
