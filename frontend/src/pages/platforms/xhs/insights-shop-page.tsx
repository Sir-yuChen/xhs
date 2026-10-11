/**
 * 商品洞察（设计 32 §6 / A6，2026-10-11 用户拍板 D2「拆」）。
 *
 * 与前身「数据洞察」的关系：那一页把**两条业务线**的指标塞在一个页面的两个 Tab 里；
 * 本页只承载**店铺线**（商品指标趋势 + 商品监控 + 订单指标入口）。
 *
 * 用户口径（市场共识）：**两条线的成绩先分开看**，组合视图只做汇总，
 * 不可把不可比的成绩揉成一个数 —— 笔记的「阅读/互动」与商品的「库存/销量/价格」
 * 本来就不是同一把尺子。
 *
 * ⚠️ 面板只传 `scope="shop"` —— 图表实现**一份**，两侧共用（不复制）。
 */
import { ShoppingCartOutlined } from "@ant-design/icons";
import { Button, Card, Space, Typography } from "antd";
import { Link } from "react-router-dom";

import { useThemePalette } from "../../../app/providers";
import { MetricsDashboardPanel } from "./metrics-dashboard-panel";

const { Text } = Typography;

export function InsightsShopPage() {
  const p = useThemePalette();

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* ---- 商品指标趋势（面板的商品侧） ---- */}
      <MetricsDashboardPanel scope="shop" />

      {/* ---- 订单指标入口 ---- */}
      <Card
        size="small"
        title={
          <Space>
            <ShoppingCartOutlined style={{ color: "#1668dc" }} />
            <span>订单指标</span>
          </Space>
        }
        extra={<Link to="/platforms/xhs/orders">进入订单管理</Link>}
        style={{ background: p.cardBg, borderColor: p.border }}
      >
        {/* ⚠️ 刻意**不**在这里再画一遍订单指标：订单页顶部已有完整指标卡
            （设计 16 §7 / M6），这里复制一份就是两份真相源 ——
            同一指标两处不一致时最难查。入口指过去即可。 */}
        <Text type="secondary">
          订单量 / 支付金额 / 售后等口径在「订单管理」页顶部实时计算（读本地台账，零平台请求）。
        </Text>
        <div style={{ marginTop: 12 }}>
          <Link to="/platforms/xhs/orders">
            <Button type="primary" size="small">查看订单指标</Button>
          </Link>
        </div>
      </Card>
    </Space>
  );
}
