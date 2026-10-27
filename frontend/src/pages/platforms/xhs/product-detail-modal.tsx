/**
 * 商品详情弹窗 —— **全部用 antd 原生组件**，不自研布局（2026-10-27 重做）。
 *
 * ## 组件选型
 *
 * | 区域 | antd 组件 |
 * |---|---|
 * | 弹窗外壳 | `Modal`（**宽高各占视口 70%**，用户口径 2026-10-27） |
 * | 整体布局 | `Space` 纵向一列（**无左右分栏、无 Tab**） |
 * | 商品卡 | `Card` + `Image` + `Statistic` + `Descriptions`（整宽置顶） |
 * | 右侧细节 | `Descriptions` + `Table` + `Card`（**单页流式，不用 Tab**） |
 * | 媒体分组 | `Card type="inner"` + `Image.PreviewGroup` + `Space` |
 *
 * ## ⚠️ 上一版为什么空白（别再犯）
 *
 * 上一版手写 flex，把 `height:100%` 逐层传进 `Tabs` 内部，并写了一段
 * `.ant-tabs-content-holder / .ant-tabs-content / .ant-tabs-tabpane` 的 CSS
 * 补高度链。结果「媒体」「全部字段」两个 Tab 的 `height:100%` 解析成 0，
 * 再叠 `overflow:auto` 就把内容整个裁掉 ⇒ **一片空白**。
 *
 * 本版**不参与高度分配**：`Modal` 只给 `styles.body.maxHeight`（超出才滚），
 * 内容按自然高度顺序展开。少一层高度传递就少一类踩坑。
 *
 * ## 2026-10-27 二次调整（用户口径）
 *
 * * 「左右展示布局合并为一个，不再使用 tab 展示」⇒ 去掉 `Tabs`，右栏内容
 *   顺序展开（商品数据 → 商品媒体），整个弹窗一列纵向滚动。
 * ## 2026-10-27 三次调整（用户口径）
 *
 * 「平台原始字段（82 个 · 45 个有值）区域移除」⇒ **整块删掉**，不只是标题。
 * 上一版只删了组件内重复标题，这次连调用方的标题 + 说明 + `PlatformRawDataTable`
 * 一起移除 —— 商品详情弹窗只展示**业务可读**的字段（基本信息 / 时间 / 属性 /
 * 规格 / 销量 / 质检），不再暴露平台原始 JSON。
 *
 * ⚠️ `platform-raw-data-table.tsx` 文件保留（`test_platform_raw_fields.py` 的后端契约
 * 与前端缺值口径守卫仍引用它），只是详情弹窗不再使用。
 */
import { useMemo } from "react";
import {
  Alert,
  Badge,
  Card,
  Col,
  Descriptions,
  Empty,
  Image,
  Modal,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";

import type { XhsShopItemDetail, XhsShopItemSku, XhsShopItemVideo } from "../../../types";

const { Text, Title, Paragraph } = Typography;

/** 分 → 元（后端契约进出都是分，这里是唯一换算点）。 */
function yuan(fen: number | null | undefined): string {
  if (fen == null) return "-";
  return `¥${(fen / 100).toFixed(2)}`;
}

/** 平台时间戳是**毫秒整数** ⇒ 人话。 */
function msToText(value: unknown): string {
  if (typeof value !== "number" || value <= 0) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** 平台布尔有三种写法：`true/false`、`1/0`、字符串。 */
function boolText(value: unknown): string {
  if (value === true || value === 1 || value === "1") return "是";
  if (value === false || value === 0 || value === "0") return "否";
  return "-";
}

const SKU_COLUMNS = [
  {
    //: 规格图（平台 `sku.spec_image`，2026-10-27 补 —— 接口一直返回，界面漏展示）。
    //: 用 `Image` 而不是 `<img>`：点击能放大预览，且与媒体 Tab 的交互一致。
    title: "规格图",
    dataIndex: "spec_image",
    width: 72,
    align: "center" as const,
    render: (v: string) =>
      v ? (
        <Image
          src={v}
          width={44}
          height={44}
          style={{ objectFit: "cover", borderRadius: 4, border: "1px solid #f0f0f0" }}
        />
      ) : (
        <Text type="secondary" style={{ fontSize: 11 }}>
          无图
        </Text>
      ),
  },
  {
    title: "SKU ID",
    dataIndex: "sku_id",
    width: 200,
    render: (v: string) => (
      <Text style={{ fontSize: 12 }} copyable={{ text: v }}>
        {v}
      </Text>
    ),
  },
  { title: "规格", dataIndex: "variant_name", ellipsis: true },
  {
    title: "价格",
    dataIndex: "price_fen",
    width: 100,
    align: "right" as const,
    render: (v: number | null) => yuan(v),
  },
  {
    title: "原价",
    dataIndex: "original_price_fen",
    width: 100,
    align: "right" as const,
    render: (v: number | null) => (v == null ? "-" : yuan(v)),
  },
  {
    title: "库存",
    dataIndex: "stock",
    width: 76,
    align: "right" as const,
    render: (v: number | null) => (v == null ? "-" : String(v)),
  },
  { title: "条码", dataIndex: "barcode", width: 150, ellipsis: true },
  {
    title: "可购买",
    dataIndex: "buyable",
    width: 80,
    render: (v: boolean) => (v ? <Tag color="green">是</Tag> : <Tag>否</Tag>),
  },
];

/** 图片网格（`Image.PreviewGroup` 点开可翻页预览）。 */
function MediaGrid({ images, empty }: { images: string[]; empty: string }) {
  if (images.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        {empty}
      </Text>
    );
  }
  return (
    <Image.PreviewGroup>
      <Space size={8} wrap>
        {images.map((link) => (
          <Image
            key={link}
            src={link}
            width={96}
            height={96}
            style={{ objectFit: "cover", borderRadius: 4, border: "1px solid #f0f0f0" }}
          />
        ))}
      </Space>
    </Image.PreviewGroup>
  );
}

export function ProductDetailModal({
  open,
  loading,
  error,
  detail,
  onClose,
  footer,
}: {
  open: boolean;
  loading: boolean;
  error: string | null;
  detail: XhsShopItemDetail | null;
  onClose: () => void;
  footer?: React.ReactNode;
}) {
  const skus = (detail?.skus ?? []) as XhsShopItemSku[];
  const videos = (detail?.videos ?? []) as XhsShopItemVideo[];
  const images = detail?.product_images ?? [];
  const detailImages = detail?.detail_images ?? [];

  const categoryPath = useMemo(() => {
    const rows = detail?.raw_fields?.categories;
    if (!Array.isArray(rows)) return detail?.item?.category || "-";
    const names = rows
      .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null)
      .sort((a, b) => Number(a.level ?? 0) - Number(b.level ?? 0))
      .map((r) => String(r.name ?? ""))
      .filter(Boolean);
    return names.length ? names.join(" / ") : "-";
  }, [detail]);

  const attributes = useMemo(() => {
    const rows = detail?.raw_fields?.attributes;
    if (!Array.isArray(rows)) return [] as { name: string; value: string }[];
    return rows
      .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null)
      .map((r) => ({
        name: String(r.attribute_name ?? ""),
        value: String(r.attribute_value ?? ""),
      }))
      .filter((r) => r.name);
  }, [detail]);

  const saleQty = useMemo(() => {
    const v = detail?.raw_fields?.sale_qty_info;
    return (typeof v === "object" && v !== null ? v : {}) as Record<string, unknown>;
  }, [detail]);

  const qualityInfo = useMemo(() => {
    const v = detail?.raw_fields?.product_quality_info;
    return (typeof v === "object" && v !== null ? v : {}) as Record<string, unknown>;
  }, [detail]);


  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={footer}
      title="商品详情"
      width="70vw"
      centered
      //: 高度按视口 70% 给死（用户口径 2026-10-27「全屏的 70%」），减去标题栏与
      //: footer 约占的 110px，body 自己滚。
      //: ⚠️ 仍然**不参与内部 flex 高度分配** —— 那一版硬绑 `height:100%` 逐层下传，
      //: 漏一层就让某些 Tab 空白（历史踩坑）；这里给的是固定像素高度，不是链。
      styles={{ body: { height: "calc(70vh - 110px)", overflowY: "auto", paddingTop: 8 } }}
    >
      {loading ? (
        <div style={{ padding: "80px 0", textAlign: "center" }}>
          <Spin tip="正在查询千帆实时数据…" />
        </div>
      ) : error ? (
        <Alert type="error" showIcon message="商品详情加载失败" description={error} />
      ) : !detail ? (
        <Empty description="没有详情数据" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {/* ===== 模块 ① 商品信息（商品卡 + 基本信息 + 时间合并） ===== */}
          <Card size="small" title="商品信息">
            {/* ⚠️ 间距不能用 `Row gutter`：antd 的 gutter 只对带 `span` 的 `Col` 生效，
                这里两列都是 `flex="..."` 写法 ⇒ gutter 被跳过 ⇒ 图与文字**贴在一起**
                （2026-10-27 用户截图报「间距不合理」就是这个）。改用显式 `marginRight`。 */}
            <Row wrap={false}>
              <Col flex="140px" style={{ marginRight: 20 }}>
                <Image
                  src={detail.item?.image || images[0] || ""}
                  style={{
                    width: 140,
                    height: 140,
                    objectFit: "contain",
                    background: "#fafafa",
                    borderRadius: 6,
                  }}
                />
              </Col>
              <Col flex="auto">
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space size={8} wrap align="center" style={{ marginBottom: 2 }}>
                    <Title level={5} style={{ margin: 0, fontSize: 14 }}>
                      {detail.item?.title || "（无标题）"}
                    </Title>
                    {detail.item?.status_label ? (
                      <Tag color={detail.item.status_key === "on_shelf" ? "green" : "default"}>
                        {detail.item.status_label}
                      </Tag>
                    ) : null}
                    {detail.item?.buyable ? <Tag color="blue">可购买</Tag> : <Tag>不可购买</Tag>}
                    {detail.item?.frozen ? <Tag color="red">已冻结</Tag> : null}
                    <Text strong style={{ fontSize: 17, color: "#cf1322" }}>
                      {yuan(detail.item?.price_min_fen)}
                    </Text>
                    {detail.item?.price_max_fen != null &&
                    detail.item?.price_max_fen !== detail.item?.price_min_fen ? (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        ~ {yuan(detail.item.price_max_fen)}
                      </Text>
                    ) : null}
                  </Space>
                  <Descriptions
                    size="small"
                    column={{ xs: 2, sm: 2, lg: 4 }}
                    colon={false}
                    styles={{ label: { fontSize: 12, color: "#8c8c8c" }, content: { fontSize: 12 } }}
                    style={{ marginTop: 4 }}
                  >
                    <Descriptions.Item label="商品 ID">
                      <Text style={{ fontSize: 11 }} copyable={{ text: detail.item_id }}>
                        {detail.item_id}
                      </Text>
                    </Descriptions.Item>
                    <Descriptions.Item label="货号">{detail.item?.article_no || "-"}</Descriptions.Item>
                    <Descriptions.Item label="分类">{categoryPath}</Descriptions.Item>
                    <Descriptions.Item label="挂链笔记">
                      {detail.item?.note_count ?? 0} 篇
                    </Descriptions.Item>
                    <Descriptions.Item label="总库存">{detail.item?.stock ?? "-"}</Descriptions.Item>
                    <Descriptions.Item label="在售规格">
                      {detail.item?.on_sale_sku_count ?? 0}/{detail.item?.sku_count ?? skus.length}
                    </Descriptions.Item>
                    <Descriptions.Item label="近 30 天销量">
                      {String(saleQty.sale_qty30 ?? detail.item?.sales_30d ?? 0)}
                    </Descriptions.Item>
                    <Descriptions.Item label="累计销量">
                      {String(saleQty.acc_sale_qty ?? detail.item?.sales_total ?? 0)}
                    </Descriptions.Item>
                    <Descriptions.Item label="创建时间">
                      {msToText(detail.raw_fields?.create_time) || detail.item?.create_time || "-"}
                    </Descriptions.Item>
                    <Descriptions.Item label="上架时间">
                      {msToText(detail.raw_fields?.on_shelf_time) || detail.item?.on_shelf_time || "-"}
                    </Descriptions.Item>
                    <Descriptions.Item label="更新时间">
                      {msToText(detail.raw_fields?.update_time) || detail.item?.update_time || "-"}
                    </Descriptions.Item>
                    <Descriptions.Item label="查询时刻">
                      {detail.queried_at || "-"}
                    </Descriptions.Item>
                  </Descriptions>
                </Space>
              </Col>
            </Row>
          </Card>

          {/* ===== 模块 ② 商品属性 ===== */}
          <Card size="small" title="商品属性">
            {attributes.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                平台没有返回商品属性
              </Text>
            ) : (
              <Descriptions size="small" column={{ xs: 2, sm: 3, lg: 4 }} bordered>
                {attributes.map((a) => (
                  <Descriptions.Item key={a.name} label={a.name}>
                    {a.value || "-"}
                  </Descriptions.Item>
                ))}
              </Descriptions>
            )}
          </Card>

          {/* ===== 模块 ③ 商品规格 ===== */}
          <Card size="small" title={`商品规格（${skus.length} 个）`}>
            <Table<XhsShopItemSku>
              size="small"
              rowKey={(r) => r.sku_id || r.sku_name}
              dataSource={skus}
              pagination={{ pageSize: 10, size: "small", showSizeChanger: false }}
              columns={SKU_COLUMNS}
              locale={{ emptyText: <Empty description="平台没有返回 SKU 明细" /> }}
            />
          </Card>

          {/* ===== 模块 ④ 质检 · 库存 · 服务 ===== */}
          <Card size="small" title="质检 · 库存 · 服务">
            <Descriptions size="small" column={{ xs: 2, sm: 3, lg: 4 }} bordered>
              <Descriptions.Item label="质检状态">
                {qualityInfo.status === 2 ? (
                  <Badge status="success" text="通过" />
                ) : (
                  String(qualityInfo.status ?? "-")
                )}
              </Descriptions.Item>
              <Descriptions.Item label="质量问题">
                {Array.isArray(qualityInfo.product_quality_issues) &&
                qualityInfo.product_quality_issues.length > 0 ? (
                  <Tag color="red">{qualityInfo.product_quality_issues.length} 项</Tag>
                ) : (
                  "无"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="库存告警规格">
                {(detail.item?.stock_warning_sku_count ?? 0) > 0 ? (
                  <Tag color="orange">{detail.item?.stock_warning_sku_count} 个</Tag>
                ) : (
                  "无"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="多包裹">
                {boolText(detail.raw_fields?.contains_multi_package)}
              </Descriptions.Item>
              <Descriptions.Item label="七天无理由">
                {boolText(detail.raw_fields?.is_free_return)}
              </Descriptions.Item>
              <Descriptions.Item label="正品保障">
                {boolText(detail.raw_fields?.is_genuine_guarantee)}
              </Descriptions.Item>
              <Descriptions.Item label="参加活动">
                {boolText(detail.raw_fields?.is_item_attend_activity)}
              </Descriptions.Item>
              <Descriptions.Item label="参加促销">
                {boolText(detail.raw_fields?.is_item_attend_promotion)}
              </Descriptions.Item>
              <Descriptions.Item label="渠道商品">
                {boolText(detail.raw_fields?.is_channel)}
              </Descriptions.Item>
              <Descriptions.Item label="精品标记">
                {boolText(detail.raw_fields?.is_boutique)}
              </Descriptions.Item>
              <Descriptions.Item label="销量更新时间">
                {msToText(saleQty.sale_qty_update_time) || "-"}
              </Descriptions.Item>
            </Descriptions>
          </Card>


          {/* ===== 模块 ⑤ 媒体资源 ===== */}
          <Card
            size="small"
            title={`媒体资源（主图 ${images.length} · 详情图 ${detailImages.length} · 视频 ${videos.length}）`}
          >
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
                      <Card type="inner" size="small" title={`商品主图（${images.length} 张）`}>
                        <MediaGrid images={images} empty="没有主图" />
                      </Card>
                      <Card type="inner" size="small" title={`商品详情图（${detailImages.length} 张）`}>
                        <MediaGrid images={detailImages} empty="没有详情图" />
                      </Card>
                      <Card type="inner" size="small" title={`商品视频（${videos.length} 个）`}>
                        {videos.length === 0 ? (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            该商品没有视频
                          </Text>
                        ) : (
                          <Row gutter={[16, 12]}>
                            {videos.map((video) => (
                              <Col key={video.path} xs={24} sm={12} md={8}>
                                <div>
                                  {/* 播放地址 = `store.xiaohongshu.com/{path}`（2026-10-27 抓包确认，免签）。
                                      平台 `videos[]` 只给 path，但拼该域名可直接播（实测 206 + ftypisom）。 */}
                                  <video
                                    src={video.url || ""}
                                    controls
                                    preload="metadata"
                                    poster=""
                                    style={{
                                      width: "100%",
                                      maxHeight: 260,
                                      background: "#000",
                                      borderRadius: 6,
                                    }}
                                  />
                                  <div style={{ marginTop: 4, fontSize: 12, color: "#8c8c8c" }}>
                                    {video.duration != null ? `${video.duration} 秒` : "时长未知"}
                                    {video.width && video.height
                                      ? ` · ${video.width}×${video.height}`
                                      : ""}
                                    {video.size != null
                                      ? ` · ${(video.size / 1024 / 1024).toFixed(2)} MB`
                                      : ""}
                                    {video.extension ? ` · ${video.extension}` : ""}
                                  </div>
                                </div>
                              </Col>
                            ))}
                          </Row>
                        )}
                      </Card>
            </Space>
          </Card>
        </Space>
      )}
    </Modal>
  );
}
