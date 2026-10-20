/**
 * 订单管理页（设计 16 §7，M6 · **一期只读**，2026-10-04）。
 *
 * ## 结构（用户口径：同一件事分块，不混在一起）
 *
 * 三个区块 + 一个抽屉：
 * 1. **监控卡** —— 订单数 / GMV / 客单价 / 退款率 / 平均发货时长 + 状态分布；
 * 2. **订单表** —— 状态 / 商品 / 金额 / 收件人（脱敏）/ 物流 / 时间；
 * 3. **订单详情抽屉** —— 商品、物流、售后、备注（本地）。
 *
 * ## ⚠️ 三条口径（都有守卫，见 `lib/order-format.ts`）
 *
 * 1. **一期只读**：页面**没有**发货 / 改价 / 售后审核按钮 —— 平台写接口未授权。
 *    唯一的写操作是**本地备注**，页面明确标注「仅本地」。
 * 2. **缺值 `-` 不补 0**（金额 / 退款率 / 平均发货时长）。
 * 3. **平台脱敏要如实说明**：真机实测千帆对部分订单**本身就只给脱敏值**
 *    （`高*` / `189***2857`）——`includeFull` 只是「不做二次脱敏」，
 *    **不等于一定有原值**。页面不许承诺「点开就有完整信息」。
 */

import { WaitMask } from "../../../components/ui/wait-mask";
import {
  DownloadOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  theme,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  exportOrders,
  fetchAccounts,
  fetchOrderDetail,
  fetchOrderMetrics,
  fetchOrders,
  handleAfterSale,
  previewOrderWrite,
  saveOrderRemark,
  shipOrder,
  shipOrdersBatch,
  syncOrders,
} from "../../../lib/api";
import type { OrderMetrics, OrderRow } from "../../../lib/api";
import {
  afterSaleConfirmText,
  batchShipBlockReason,
  canHandleAfterSale,
  canShipOrder,
  LOGISTICS_COMPANIES,
  shipConfirmText,
  shipOutcomeText,
} from "../../../lib/order-write";
import { accountNameMap } from "../../../lib/task-records";
import type { AccountNameMap } from "../../../lib/task-records";
import {
  EXPORT_WARNING,
  hoursText,
  maskingNotice,
  moneyText,
  ORDER_STATUSES,
  orderItemsSummary,
  orderStatusColor,
  orderStatusText,
  percentText,
  statusCounts,
  timeText,
} from "../../../lib/order-format";

const { Text, Paragraph } = Typography;

export function OrdersPage() {
  const { message, modal } = App.useApp();
  const { token } = theme.useToken();

  const [accounts, setAccounts] = useState<AccountNameMap>({});
  const [accountId, setAccountId] = useState<number | null>(null);
  const [status, setStatus] = useState<string>("");
  const [keyword, setKeyword] = useState<string>("");
  const [includeFull, setIncludeFull] = useState(false);

  const [rows, setRows] = useState<OrderRow[]>([]);
  const [total, setTotal] = useState(0);
  /**
   * **所属店铺**（2026-10-04 用户口径「订单模块也需要是店铺维度」）。
   *
   * ⚠️ ADR-021 后店铺实体已退场，「账号 = 店铺」1:1 —— 店名由**后端下发**
   * （`list_orders` 的 `shop_name`），前端不自己拼（账号名 ≠ 店名，且口径要
   * 与商品页同一份实现）。空串 = 后端未给（账号被删 / 非法 id）⇒ 渲染 `-`。
   */
  const [shopName, setShopName] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [metrics, setMetrics] = useState<OrderMetrics | null>(null);

  const [detail, setDetail] = useState<OrderRow | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [remarkDraft, setRemarkDraft] = useState("");

  /**
   * M7 发货 / 售后（**真实平台写、不可撤回**）。
   *
   * ⚠️ 流程固定为：`previewOrderWrite`（拿 confirm_token）→ 用户确认 →
   * 原样回传 token 调写端点。**前端不自己算 token**（那是后端的口径）。
   */
  const [shipTarget, setShipTarget] = useState<OrderRow | null>(null);
  const [shipOpen, setShipOpen] = useState(false);
  const [shipCompany, setShipCompany] = useState(LOGISTICS_COMPANIES[0]?.code ?? "SF");
  const [shipTracking, setShipTracking] = useState("");
  const [shipSubmitting, setShipSubmitting] = useState(false);
  const [afterSaleTarget, setAfterSaleTarget] = useState<{
    orderNo: string;
    afterSaleId: string;
    amount?: number | null;
  } | null>(null);
  const [afterSaleOpen, setAfterSaleOpen] = useState(false);
  const [afterSaleReason, setAfterSaleReason] = useState("");
  const [afterSaleSubmitting, setAfterSaleSubmitting] = useState(false);

  // 账号清单（只放账号名，与账号下拉同口径；不随筛选重复拉）
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const list = await fetchAccounts();
        if (!alive) return;
        // ⚠️ 复用 lib 的整理函数（只放账号名 + `账号 #id` 兜底）——
        // 自己再遍历一次就是同一件事两份实现（DEF-044）
        setAccounts(accountNameMap(list));
        const first = list[0]?.id ?? null;
        setAccountId((prev) => prev ?? first);
      } catch {
        // 安静退：账号拉不到不影响订单表（后端会按 token 收口）
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(
    async (targetPage = page) => {
      if (!accountId) return;
      setLoading(true);
      try {
        const [list, m] = await Promise.all([
          fetchOrders({
            platformAccountId: accountId,
            status,
            keyword,
            page: targetPage,
            pageSize: 20,
            includeFull,
          }),
          fetchOrderMetrics(accountId),
        ]);
        setRows(list.items ?? []);
        setTotal(list.total ?? 0);
        setShopName(list.shop_name ?? "");
        setMetrics(m);
      } catch (error) {
        message.error(`订单列表加载失败：${(error as Error).message}`);
      } finally {
        setLoading(false);
      }
    },
    [accountId, includeFull, keyword, message, page, status],
  );

  useEffect(() => {
    void load(1);
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, status, includeFull]);

    /**
   * **发货**（M7，真实写、不可撤回）。
   *
   * ⚠️ 顺序：预览拿 token → 弹窗二次确认 → 原样回传 token 调写端点。
   * 前端不自己算 token（那是后端口径），也不吞平台原文（失败原因原样展示）。
   */
  const doShip = async () => {
    if (!accountId || !shipTarget) return;
    setShipSubmitting(true);
    try {
      const preview = await previewOrderWrite({
        platformAccountId: accountId,
        kind: "ship",
        orderNo: shipTarget.order_no,
        companyCode: shipCompany,
        trackingNo: shipTracking,
      });
      const result = await shipOrder({
        platformAccountId: accountId,
        orderNo: shipTarget.order_no,
        companyCode: shipCompany,
        trackingNo: shipTracking,
        confirmToken: preview.confirm_token,
      });
      message.success(`已发货：${shipTarget.order_no}`);
      setShipOpen(false);
      setShipTarget(null);
      setShipTracking("");
      void load(page);
      // 平台原文一并带出（可追溯）
      if (result?.platform_message) {
        message.info(String(result.platform_message));
      }
    } catch (error) {
      const text =
        (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
          ?.detail?.message ?? (error as Error).message;
      message.error(`发货失败：${text}`);
    } finally {
      setShipSubmitting(false);
    }
  };

  /** **售后审核**（M7，真实写、不可撤回）—— 同意 / 拒绝都走这里。 */
  const doHandleAfterSale = async (action: "agree" | "reject") => {
    if (!accountId || !afterSaleTarget) return;
    setAfterSaleSubmitting(true);
    try {
      const preview = await previewOrderWrite({
        platformAccountId: accountId,
        kind: "after_sale",
        afterSaleId: afterSaleTarget.afterSaleId,
        action,
        rejectReason: afterSaleReason,
      });
      await handleAfterSale({
        platformAccountId: accountId,
        afterSaleId: afterSaleTarget.afterSaleId,
        action,
        rejectReason: afterSaleReason,
        confirmToken: preview.confirm_token,
      });
      message.success(action === "reject" ? "已拒绝该售后" : "已同意该售后");
      setAfterSaleOpen(false);
      setAfterSaleTarget(null);
      setAfterSaleReason("");
      void load(page);
    } catch (error) {
      const text =
        (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
          ?.detail?.message ?? (error as Error).message;
      message.error(`处理失败：${text}`);
    } finally {
      setAfterSaleSubmitting(false);
    }
  };

const onSync = async () => {
    if (!accountId) return;
    setSyncing(true);
    try {
      await syncOrders(accountId);
      message.success("订单同步已入队，稍候刷新查看");
      // 同步在后台跑，立刻刷新拿到的是同步前的数据；不假装「已同步完成」
      setTimeout(() => void load(page), 3000);
    } catch (error) {
      message.error(`触发同步失败：${(error as Error).message}`);
    } finally {
      setSyncing(false);
    }
  };

  const onExport = () => {
    if (!accountId) return;
    // ⚠️ 导出**含完整收件信息** —— 必须先告知再执行（不是「点完才知道」）
    modal.confirm({
      centered: true,
      title: "导出订单",
      content: EXPORT_WARNING,
      okText: "继续导出",
      cancelText: "取消",
      onOk: async () => {
        try {
          const result = await exportOrders(accountId, { status, keyword });
          const blob = new Blob([result.csv], { type: "text/csv;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = result.filename;
          a.click();
          URL.revokeObjectURL(url);
          message.success(
            result.truncated
              ? `已导出 ${result.count} 条（达上限，请缩小筛选范围）`
              : `已导出 ${result.count} 条`,
          );
        } catch (error) {
          message.error(`导出失败：${(error as Error).message}`);
        }
      },
    });
  };

  const openDetail = async (row: OrderRow) => {
    if (!accountId) return;
    try {
      const data = await fetchOrderDetail(accountId, row.order_no, includeFull);
      setDetail(data);
      setRemarkDraft((data.remarks ?? [])[0]?.content ?? "");
      setDetailOpen(true);
    } catch (error) {
      message.error(`详情加载失败：${(error as Error).message}`);
    }
  };

  const saveRemark = async () => {
    if (!accountId || !detail) return;
    try {
      const result = await saveOrderRemark(accountId, detail.order_no, remarkDraft);
      message.success(result.deleted ? "备注已清除" : "备注已保存（仅本地）");
      void openDetail({ ...detail, order_no: detail.order_no });
    } catch (error) {
      message.error(`备注保存失败：${(error as Error).message}`);
    }
  };

  const counts = useMemo(() => statusCounts(metrics?.by_status), [metrics]);

  const columns: ColumnsType<OrderRow> = [
    {
      title: "订单号",
      dataIndex: "order_no",
      width: 190,
      render: (value: string, row) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => void openDetail(row)}>
          {value}
        </Button>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 92,
      render: (value: string) => (
        <Tag color={orderStatusColor(value)}>{orderStatusText(value)}</Tag>
      ),
    },
    {
      title: "商品",
      dataIndex: "items",
      ellipsis: true,
      render: (_: unknown, row) => orderItemsSummary(row.items as never),
    },
    {
      title: "买家",
      dataIndex: "buyer_nick",
      width: 120,
      ellipsis: true,
      render: (value: string) => value || "-",
    },
    {
      title: "收件人",
      dataIndex: "receiver_name",
      width: 110,
      render: (value: string) => value || "-",
    },
    {
      title: "金额",
      dataIndex: "pay_amount",
      width: 110,
      align: "right",
      render: (value: number | null) => moneyText(value),
    },
    {
      title: "物流",
      dataIndex: "tracking_no",
      width: 160,
      ellipsis: true,
      render: (_: unknown, row) =>
        row.tracking_no ? `${row.logistics_company ?? "-"} ${row.tracking_no}` : "-",
    },
    {
      title: "下单时间",
      dataIndex: "created_at",
      width: 120,
      render: (value: string) => timeText(value),
    },
    {
      // M7 写操作入口（设计 16 §7.4）：**仅目标状态才渲染按钮** ——
      // 非待发货 / 非待处理不给假入口（点了才知道不行是坏体验，也是坏设计）。
      // ⚠️ 前端不渲染 ≠ 后端放行：后端仍会再判一次（两处各自承担自己的职责）。
      title: "操作",
      key: "action",
      width: 150,
      render: (_: unknown, row) => {
        const pendingAfterSale = (row.after_sales ?? []).find(
          (item) => item.status === "待处理",
        );
        return (
          <Space size={4} wrap>
            {canShipOrder(row.status) ? (
              <Tooltip title="对这笔真实订单发货（不可撤回）">
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0 }}
                  onClick={() => {
                    setShipTarget(row);
                    setShipOpen(true);
                  }}
                >
                  发货
                </Button>
              </Tooltip>
            ) : null}
            {pendingAfterSale && canHandleAfterSale(pendingAfterSale.status) ? (
              <Tooltip title="处理这条售后（同意 / 拒绝，不可撤回）">
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0 }}
                  onClick={() => {
                    setAfterSaleTarget({
                      orderNo: row.order_no,
                      afterSaleId: String(pendingAfterSale.after_sale_id ?? ""),
                      amount: pendingAfterSale.amount ?? null,
                    });
                    setAfterSaleOpen(true);
                  }}
                >
                  售后
                </Button>
              </Tooltip>
            ) : null}
            {!canShipOrder(row.status) && !pendingAfterSale ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                -
              </Text>
            ) : null}
          </Space>
        );
      },
    },
  ];

  const notice = maskingNotice(detail?.receiver_name, detail?.receiver_phone);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「小红书运营 / 订单管理 /
          台账说明」三行整块移除；「同步订单 / 刷新 / 导出」收进下方「订单列表」卡右上角。 */}

      {/* ① 监控卡 */}
      <Card size="small" title="订单监控">
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Statistic title="订单数" value={metrics?.order_count ?? 0} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="GMV" value={moneyText(metrics?.gmv)} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="客单价" value={moneyText(metrics?.avg_order_value)} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="退款率"
              value={percentText(metrics?.refund_rate)}
              suffix={
                metrics?.refund_count ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {metrics.refund_count} 单
                  </Text>
                ) : null
              }
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="平均发货时长" value={hoursText(metrics?.avg_ship_hours)} />
          </Col>
          <Col xs={24} md={18}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              状态分布
            </Text>
            <div style={{ marginTop: 6 }}>
              <Space size={[6, 6]} wrap>
                {ORDER_STATUSES.map((s) => (
                  <Tag key={s} color={orderStatusColor(s)}>
                    {s} {counts[s] ?? 0}
                  </Tag>
                ))}
              </Space>
            </div>
          </Col>
        </Row>
      </Card>

      {/* ② 筛选 + 订单表 */}
      <Card
        size="small"
        title="订单列表"
        extra={
          <Space wrap>
            <Button size="small" icon={<SyncOutlined />} loading={syncing} onClick={() => void onSync()}>
              同步订单
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void load(page)}>
              刷新
            </Button>
            <Button size="small" icon={<DownloadOutlined />} onClick={onExport}>
              导出
            </Button>
            <Select
              style={{ width: 200 }}
              placeholder="店铺 / 账号"
              value={accountId ?? undefined}
              onChange={(value) => setAccountId(value)}
              options={Object.entries(accounts).map(([id, name]) => ({
                value: Number(id),
                // 店铺维度（2026-10-04 用户口径）：账号 = 店铺 1:1，店名即账号名
                // （ADR-021 后店名取自账号昵称）⇒ 下拉文案标注「店铺」，让用户
                // 一眼看出这个筛选是按店维度的。
                label: name,
              }))}
            />
            <Select
              style={{ width: 140 }}
              value={status}
              onChange={(value) => setStatus(value)}
              options={[
                { value: "", label: "全部状态" },
                ...ORDER_STATUSES.map((s) => ({ value: s, label: s })),
              ]}
            />
            <Input.Search
              style={{ width: 240 }}
              placeholder="订单号 / 买家 / 商品 / 快递单号"
              allowClear
              onSearch={(value) => {
                setKeyword(value);
                setTimeout(() => void load(1), 0);
              }}
            />
            <Tooltip title="完整收件信息仅在发货 / 打单场景使用">
              <Button
                type={includeFull ? "primary" : "default"}
                onClick={() => setIncludeFull((prev) => !prev)}
              >
                {includeFull ? "完整信息" : "脱敏显示"}
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {/* 店铺维度（2026-10-04 用户口径「订单模块也需要是店铺维度」）：
            与商品页同一口径 —— ADR-021 后店铺实体退场、账号 = 店铺 1:1，
            店名由后端下发（前端不自己拼）。空 ⇒ `-`（缺值口径）。 */}
        <Descriptions size="small" column={2} style={{ marginBottom: 8 }}>
          <Descriptions.Item label="店铺">{shopName || "-"}</Descriptions.Item>
          <Descriptions.Item label="订单来源">
            本地台账（同步订单任务落库 · 数据归属该店铺）
          </Descriptions.Item>
        </Descriptions>
        {/* 2026-10-20 统一：表格等待由公共 WaitMask 表达。 */}
        <WaitMask open={loading} mask={false} title="正在加载订单" />
        <Table<OrderRow>
          rowKey="id"
          size="small"
          //: ⚠️ 2026-10-20 统一：Table 自带的 loading 会渲染一层 antd Spin 覆盖，与公共 WaitMask 叠成两层 ⇒ 移除，等待交给 WaitMask。
          //: 等待提示由同状态驱动的 WaitMask 承担（见函数返回处）。
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1100 }}
          pagination={{
            current: page,
            pageSize: 20,
            total,
            showSizeChanger: false,
            onChange: (next) => {
              setPage(next);
              void load(next);
            },
          }}
        />
      </Card>

      {/* ③ 详情抽屉 */}
      <Drawer
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width="60vw"
        title={detail ? `订单 ${detail.order_no}` : "订单详情"}
        extra={
          detail ? (
            <Tag color={orderStatusColor(detail.status)}>{orderStatusText(detail.status)}</Tag>
          ) : null
        }
      >
        {detail ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {notice.includes("平台未提供完整") ? (
              <Text type="warning">
                <ExclamationCircleOutlined /> {notice}
              </Text>
            ) : (
              <Text type="secondary">{notice}</Text>
            )}

            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="买家">{detail.buyer_nick || "-"}</Descriptions.Item>
              <Descriptions.Item label="收件人">{detail.receiver_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="手机号">{detail.receiver_phone || "-"}</Descriptions.Item>
              <Descriptions.Item label="收货地址">
                {detail.receiver_address || "-"}
              </Descriptions.Item>
              <Descriptions.Item label="应付金额">{moneyText(detail.pay_amount)}</Descriptions.Item>
              <Descriptions.Item label="订单金额">
                {moneyText(detail.total_amount)}
              </Descriptions.Item>
              <Descriptions.Item label="运费">{moneyText(detail.freight)}</Descriptions.Item>
              <Descriptions.Item label="优惠">{moneyText(detail.discount)}</Descriptions.Item>
              <Descriptions.Item label="下单时间">{timeText(detail.created_at)}</Descriptions.Item>
              <Descriptions.Item label="付款时间">{timeText(detail.pay_time)}</Descriptions.Item>
              <Descriptions.Item label="发货时间">{timeText(detail.ship_time)}</Descriptions.Item>
              <Descriptions.Item label="完成时间">{timeText(detail.finish_time)}</Descriptions.Item>
              <Descriptions.Item label="平台状态码" span={2}>
                {detail.platform_status || "-"}
              </Descriptions.Item>
            </Descriptions>

            <Card size="small" title="商品">
              {(detail.items ?? []).length === 0 ? (
                <Text type="secondary">-</Text>
              ) : (
                (detail.items ?? []).map((item) => (
                  <div key={item.id} style={{ marginBottom: 4 }}>
                    {orderItemsSummary([item] as never)} · {moneyText(item.amount)}
                    {item.refund_status ? (
                      <Tag style={{ marginLeft: 8 }} color="red">
                        {item.refund_status}
                      </Tag>
                    ) : null}
                  </div>
                ))
              )}
            </Card>

            <Card size="small" title="物流">
              {(detail.shipments ?? []).length === 0 ? (
                <Text type="secondary">-</Text>
              ) : (
                (detail.shipments ?? []).map((ship) => (
                  <div key={ship.id}>
                    {ship.logistics_company || "-"} {ship.tracking_no || ""} · 发货{" "}
                    {timeText(ship.ship_time)}
                  </div>
                ))
              )}
            </Card>

            <Card size="small" title="售后">
              {(detail.after_sales ?? []).length === 0 ? (
                <Text type="secondary">-</Text>
              ) : (
                (detail.after_sales ?? []).map((row) => (
                  <div key={row.id}>
                    <Tag color="red">{row.status || "-"}</Tag>
                    {row.reason || "-"} · {moneyText(row.amount)}
                  </div>
                ))
              )}
            </Card>

            <Card
              size="small"
              title="备注"
              extra={<Text type="secondary">仅存本地，不同步到平台</Text>}
            >
              <Input.TextArea
                rows={3}
                value={remarkDraft}
                onChange={(event) => setRemarkDraft(event.target.value)}
                placeholder="例如：客户要求周六送（清空即删除备注）"
              />
              <div style={{ marginTop: 8 }}>
                <Button type="primary" onClick={() => void saveRemark()}>
                  保存备注
                </Button>
              </div>
            </Card>

            <Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}>
              发货与售后审核是真实平台写操作、不可撤回（M7，2026-10-04 门禁已解除）：
            仅对「待发货」/「待处理」的行给出入口，且必须经二次确认；改价等其余写操作仍未授权。
            </Paragraph>
          </div>
        ) : null}
      </Drawer>
      {/* M7：**发货二次确认**（真实写、不可撤回）—— 用户必须看到影响面再确认。 */}
      <Modal
        centered
        open={shipOpen}
        title={`发货确认 · ${shipTarget?.order_no ?? ""}`}
        okText="确认发货（不可撤回）"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: shipSubmitting }}
        onOk={() => void doShip()}
        onCancel={() => {
          setShipOpen(false);
          setShipTarget(null);
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="这是对真实订单的发货操作，提交后不可撤回。"
          description={shipConfirmText([
            { order_no: shipTarget?.order_no ?? "", tracking_no: shipTracking },
          ])}
        />
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>物流公司</Text>
          <Select
            style={{ width: "100%" }}
            value={shipCompany}
            onChange={(value) => setShipCompany(value)}
            options={LOGISTICS_COMPANIES.map((c) => ({ value: c.code, label: c.name }))}
          />
          <Text>运单号</Text>
          <Input
            value={shipTracking}
            onChange={(event) => setShipTracking(event.target.value)}
            placeholder="请输入运单号"
          />
        </Space>
      </Modal>

      {/* M7：**售后处理**（真实写、不可撤回）—— 同意 / 拒绝各自独立确认。 */}
      <Modal
        centered
        open={afterSaleOpen}
        title="售后处理确认"
        footer={null}
        onCancel={() => {
          setAfterSaleOpen(false);
          setAfterSaleTarget(null);
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="售后处理是对真实售后单的操作，提交后不可撤回。"
          description={afterSaleConfirmText(
            "agree",
            afterSaleTarget?.afterSaleId ?? "",
            afterSaleTarget?.amount ?? null,
          )}
        />
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>拒绝理由（选择拒绝时必填）</Text>
          <Input.TextArea
            value={afterSaleReason}
            onChange={(event) => setAfterSaleReason(event.target.value)}
            rows={2}
            placeholder="例如：商品已拆封，不符合退货条件"
          />
          <Space>
            <Button
              type="primary"
              loading={afterSaleSubmitting}
              onClick={() => void doHandleAfterSale("agree")}
            >
              同意售后（不可撤回）
            </Button>
            <Button
              danger
              loading={afterSaleSubmitting}
              disabled={!afterSaleReason.trim()}
              onClick={() => void doHandleAfterSale("reject")}
            >
              拒绝售后（不可撤回）
            </Button>
          </Space>
        </Space>
      </Modal>
    </div>
  );
}

export default OrdersPage;
