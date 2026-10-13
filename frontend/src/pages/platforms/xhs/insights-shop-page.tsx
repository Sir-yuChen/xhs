/**
 * 商品洞察（设计 32 §6 / A6，2026-10-11 用户拍板 D2「拆」；2026-10-13 R3 重排）。
 *
 * 与前身「数据洞察」的关系：那一页把**两条业务线**的指标塞在一个页面的两个 Tab 里；
 * 本页只承载**店铺线**（商品库存/销量 + 店铺快捷入口 + 订单口径入口）。
 *
 * 用户口径（市场共识）：**两条线的成绩先分开看**，组合视图只做汇总，
 * 不可把不可比的成绩揉成一个数 —— 笔记的「阅读/互动」与商品的「库存/销量/价格」
 * 本来就不是同一把尺子。
 *
 * ## 2026-10-13 用户口径：指标（快照/趋势）相关内容整块移除
 *
 * 用户口径「把商品洞察和内容洞察中的指标相关内容逻辑移除，包括店铺商品指标快照
 * 采集、笔记指标快照采集定时任务移除，后续作为专项单独排期」。
 *
 * ⇒ 原先的「商品指标趋势面板」（`MetricsDashboardPanel scope="shop"`）与其底层
 * 快照采集（`shop-metrics/collect|trend` 端点 + 定时任务）**整块移除**。
 * 本页现在承载：① **店铺快捷入口**（把「看数」接到「干活」）；
 * ② **订单口径说明**（不复制订单指标卡：订单页顶部已有完整指标，复制即两份真相源）。
 * 商品维度的库存/销量/价格在「商品管理」列表页有实时值（那是台账，不是快照趋势）。
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

const { Text } = Typography;

/** 店铺线快捷入口（把「看数」接到「干活」——用户看完商品指标常要去改商品/发订单）。 */
const SHOP_ENTRIES = [
  {
    key: "items",
    to: "/platforms/xhs/shop-items",
    icon: <ShoppingOutlined />,
    title: "商品管理",
    desc: "库存 / 销量 / 价格实时台账，批量上架下架",
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
      {/* ---- ① 店铺快捷入口 ---- */}
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

      {/* ---- ② 订单口径说明 ---- */}
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
