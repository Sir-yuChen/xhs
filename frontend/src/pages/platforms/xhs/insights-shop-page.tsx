/**
 * 商品洞察（设计 32 §6 / A6，2026-10-11 用户拍板 D2「拆」；2026-10-13 R3 重排）。
 *
 * 与前身「数据洞察」的关系：那一页把**两条业务线**的指标塞在一个页面的两个 Tab 里；
 * 本页只承载**店铺线**（商品指标趋势 + 商品监控 + 订单指标入口）。
 *
 * 用户口径（市场共识）：**两条线的成绩先分开看**，组合视图只做汇总，
 * 不可把不可比的成绩揉成一个数 —— 笔记的「阅读/互动」与商品的「库存/销量/价格」
 * 本来就不是同一把尺子。
 *
 * ## 2026-10-13 重排（R3）
 *
 * 用户口径「重新布局的商品洞察页面和内容洞察页面，要求 UI 更合理美观」。原页只有
 * 一个面板 + 一张入口卡（56 行，观感偏空）。本页补齐**经营三块**：
 * ① **商品指标趋势**（复用面板，`scope="shop"` —— 图表实现一份，两侧共用）；
 * ② **店铺快捷入口**（商品列表 / 订单管理 —— 把「看数」直接接到「干活」）；
 * ③ **订单口径说明**（不复制订单指标卡：订单页顶部已有完整指标，复制即两份真相源）。
 *
 * ⚠️ 面板只传 `scope="shop"` —— 图表实现**一份**，两侧共用（不复制）。
 */
import {
  BarChartOutlined,
  RightOutlined,
  ShoppingCartOutlined,
  ShoppingOutlined,
} from "@ant-design/icons";
import { Card, Col, Row, Space, Statistic, Typography } from "antd";
import { Link } from "react-router-dom";

import { useThemePalette } from "../../../app/providers";
import { MetricsDashboardPanel } from "./metrics-dashboard-panel";

const { Text } = Typography;

/** 店铺线快捷入口（把「看数」接到「干活」——用户看完商品指标常要去改商品/发订单）。 */
const SHOP_ENTRIES = [
  {
    key: "items",
    to: "/platforms/xhs/shop-items",
    icon: <ShoppingOutlined />,
    title: "商品管理",
    desc: "查看 / 编辑商品，批量上架下架",
  },
  {
    key: "orders",
    to: "/platforms/xhs/orders",
    icon: <ShoppingCartOutlined />,
    title: "订单管理",
    desc: "订单量 / 支付金额 / 售后指标",
  },
] as const;

export function InsightsShopPage() {
  const p = useThemePalette();

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* ---- ① 商品指标趋势 + 商品监控（面板的店铺侧） ---- */}
      <MetricsDashboardPanel scope="shop" />

      {/* ---- ② 店铺快捷入口 ---- */}
      <Row gutter={[16, 16]}>
        {SHOP_ENTRIES.map((entry) => (
          <Col xs={24} md={12} key={entry.key}>
            <Link to={entry.to} style={{ display: "block" }}>
              <Card
                size="small"
                hoverable
                style={{ background: p.cardBg, borderColor: p.border }}
              >
                <Space align="center" size={16} style={{ width: "100%", justifyContent: "space-between" }}>
                  <Space align="center" size={12}>
                    <span style={{ color: "#1668dc", fontSize: 22 }}>{entry.icon}</span>
                    <span>
                      <Text strong style={{ display: "block", color: p.text }}>
                        {entry.title}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {entry.desc}
                      </Text>
                    </span>
                  </Space>
                  <RightOutlined style={{ color: p.textTertiary }} />
                </Space>
              </Card>
            </Link>
          </Col>
        ))}
      </Row>

      {/* ---- ③ 订单口径说明 ---- */}
      <Card
        size="small"
        title={
          <Space>
            <BarChartOutlined style={{ color: "#1668dc" }} />
            <span>订单指标</span>
          </Space>
        }
        extra={<Link to="/platforms/xhs/orders">进入订单管理</Link>}
        style={{ background: p.cardBg, borderColor: p.border }}
      >
        {/* ⚠️ 刻意**不**在这里再画一遍订单指标：订单页顶部已有完整指标卡
            （设计 16 §7 / M6），这里复制一份就是两份真相源 ——
            同一指标两处不一致时最难查。入口指过去即可。 */}
        <Space size={24} wrap>
          <Statistic
            title={<span style={{ color: p.textTertiary, fontSize: 13 }}>订单量 / 支付金额 / 售后</span>}
            value="在「订单管理」页顶部实时计算"
            valueStyle={{ fontSize: 14, color: p.textSecondary, fontWeight: 400 }}
          />
        </Space>
      </Card>
    </Space>
  );
}
