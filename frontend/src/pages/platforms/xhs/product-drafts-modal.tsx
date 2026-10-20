/**
 * 「商品草稿箱」弹窗 —— 复刻千帆草稿箱（设计 24 §9.4 / 设计 23 §10）。
 *
 * ## 为什么是弹窗（2026-10-08 用户口径）
 *
 * 「**草稿箱弹窗的形式在商品页内展示，账号使用商品页选定的账号**」——
 * 草稿箱与商品列表是**同一件事的两个视图**（草稿 = 还没上架的商品），
 * 所以做成商品页里的一个弹窗，**账号直接跟随商品页顶部选定的店铺**，
 * 弹窗内**不再放账号下拉**（两处选账号必然出现「页面看 A、弹窗看 B」）。
 *
 * ## 数据不落库（用户口径）
 *
 * 草稿的唯一真相源在**平台**（用户在千帆页面里继续填、平台审核、平台限流
 * 都在那边）。本弹窗**只读平台、不落本地表**：本地存镜像必然漂移成
 * 「本地有、平台没有」，用户按哪份都对不上。
 *
 * ## 功能面（真机 DOM + bundle `8591.80981179.js` 取证）
 *
 * * 列表列：商品信息（名称 + 草稿 ID）/ 状态（待提交 / 已删除）/ 类型
 *   （单品 / 组合品）/ 创建时间 / 修改时间；
 * * 搜索：商品名称关键词；排序：创建 / 修改时间（升 / 降）；
 * * 行操作：**删除草稿**（`batch_delete_draft`，不可恢复）；
 * * 批量：勾选后批量删除（上限 100，**不设全选** —— 用户既有偏好）。
 *
 * ⚠️ 说明文案**禁止 `**`**（守卫覆盖整个 `frontend/src`）。
 * ⚠️ 弹窗尺寸按**视口百分比**（用户偏好），不用固定像素宽。
 */
import { WaitMask } from "../../../components/ui/wait-mask";
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";

import { deleteItemDrafts, fetchItemDrafts } from "../../../lib/api";
import type { ItemDraftRow } from "../../../types";

const { Text } = Typography;

/** 单次批量删除上限（与后端 `MAX(payload.draft_ids)=100` 同值，前端先拦）。 */
const MAX_BATCH_DELETE = 100;

type ProductDraftsModalProps = {
  open: boolean;
  /** 商品页选定的店铺账号（**弹窗不再自选**）。 */
  platformAccountId: number | null;
  /** 店铺名（标题里显示，便于确认在看哪个店）。 */
  shopName: string;
  onClose: () => void;
};

export function ProductDraftsModal({
  open,
  platformAccountId,
  shopName,
  onClose,
}: ProductDraftsModalProps) {
  const { message } = App.useApp();
  const [keyword, setKeyword] = useState("");
  const [searchText, setSearchText] = useState("");
  const [sortField, setSortField] = useState("create_time");
  const [order, setOrder] = useState("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [rows, setRows] = useState<ItemDraftRow[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    if (!platformAccountId) {
      setRows([]);
      setTotal(0);
      return;
    }
    setLoading(true);
    try {
      const data = await fetchItemDrafts(
        platformAccountId,
        page,
        pageSize,
        keyword,
        sortField,
        order,
      );
      setRows(data.drafts);
      setTotal(data.total);
    } catch (error) {
      void message.error(readReason(error, "加载草稿箱失败"));
    } finally {
      setLoading(false);
    }
  }, [platformAccountId, keyword, order, page, pageSize, sortField, message]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  //: 关弹窗即复位筛选与勾选（下次打开是干净态 —— 不留上次的搜索词 / 页码）。
  const handleClose = useCallback(() => {
    setKeyword("");
    setSearchText("");
    setSelected([]);
    setPage(1);
    onClose();
  }, [onClose]);

  const handleDelete = useCallback(
    async (draftIds: string[]) => {
      if (!platformAccountId || draftIds.length === 0) return;
      setDeleting(true);
      try {
        const result = await deleteItemDrafts(platformAccountId, draftIds);
        void message.success(`已删除 ${result.deleted} 个草稿`);
        setSelected([]);
        await load();
      } catch (error) {
        void message.error(readReason(error, "删除草稿失败"));
      } finally {
        setDeleting(false);
      }
    },
    [platformAccountId, load, message],
  );

  return (
    <Modal
      centered
      open={open}
      onCancel={handleClose}
      onOk={handleClose}
      okText="关闭"
      cancelButtonProps={{ style: { display: "none" } }}
      width="70vw"
      style={{ maxWidth: 1200 }}
      styles={{ body: { maxHeight: "70vh", overflow: "hidden" } }}
      title={`商品草稿箱${shopName ? ` · ${shopName}` : ""}`}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space wrap>
          <Input.Search
            allowClear
            style={{ width: 260 }}
            placeholder="按商品名称搜索草稿"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            onSearch={(value) => {
              setKeyword(value.trim());
              setPage(1);
            }}
          />
          <Select
            style={{ width: 180 }}
            value={`${sortField}:${order}`}
            onChange={(value) => {
              const [field, dir] = value.split(":");
              setSortField(field);
              setOrder(dir);
              setPage(1);
            }}
            options={[
              { value: "create_time:desc", label: "创建时间（降序）" },
              { value: "create_time:asc", label: "创建时间（升序）" },
              { value: "update_time:desc", label: "修改时间（降序）" },
              { value: "update_time:asc", label: "修改时间（升序）" },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
          <Popconfirm
            title={`删除选中的 ${selected.length} 个草稿`}
            description="删除后不可恢复，确定继续？"
            okText="删除"
            okButtonProps={{ danger: true }}
            disabled={selected.length === 0}
            onConfirm={() => void handleDelete(selected)}
          >
            <Button
              danger
              icon={<DeleteOutlined />}
              loading={deleting}
              disabled={selected.length === 0}
            >
              批量删除{selected.length > 0 ? `（${selected.length}）` : ""}
            </Button>
          </Popconfirm>
        </Space>

        <Alert
          type="info"
          showIcon
          message="草稿箱数据实时来自千帆，本地不落库"
          description="账号跟随商品页选定的店铺。草稿只存商品骨架，其余字段需在千帆草稿箱补齐。"
        />

        {/* 2026-10-20 统一：表格等待由公共 WaitMask 表达。 */}

        <WaitMask open={loading} title="正在加载草稿" />

        <Table<ItemDraftRow>
          rowKey="draft_id"
          size="small"
          //: ⚠️ 2026-10-20 统一：Table 自带的 loading 会渲染一层 antd Spin 覆盖，与公共 WaitMask 叠成两层 ⇒ 移除，等待交给 WaitMask。
          //: 等待提示由同状态驱动的 WaitMask 承担（见函数返回处）。
          dataSource={rows}
          scroll={{ x: 900, y: "48vh" }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (count) => `共 ${count} 个草稿`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
          }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) =>
              setSelected(keys.map((key) => String(key)).slice(0, MAX_BATCH_DELETE)),
          }}
          locale={{ emptyText: <Empty description="暂无草稿" /> }}
          columns={[
            {
              title: "商品信息",
              dataIndex: "name",
              ellipsis: true,
              render: (_value: string, row: ItemDraftRow) => (
                <Space direction="vertical" size={0}>
                  <Tooltip title={row.name}>
                    <span>{row.name || "（无名称）"}</span>
                  </Tooltip>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    草稿 ID：{row.draft_id}
                  </Text>
                </Space>
              ),
            },
            {
              title: "状态",
              dataIndex: "status_label",
              width: 100,
              render: (value: string, row: ItemDraftRow) => (
                <Tag color={row.status === "-1" ? "red" : "blue"}>{value}</Tag>
              ),
            },
            {
              title: "类型",
              dataIndex: "is_union",
              width: 90,
              render: (value: boolean) =>
                value ? <Tag color="purple">组合品</Tag> : <Tag>单品</Tag>,
            },
            { title: "创建时间", dataIndex: "created_at", width: 170 },
            { title: "修改时间", dataIndex: "updated_at", width: 170 },
            {
              title: "操作",
              key: "actions",
              width: 90,
              fixed: "right",
              render: (_value: unknown, row: ItemDraftRow) => (
                <Popconfirm
                  title="删除该草稿"
                  description="删除后不可恢复，确定继续？"
                  okText="删除"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => void handleDelete([row.draft_id])}
                >
                  <Button type="link" danger size="small">
                    删除
                  </Button>
                </Popconfirm>
              ),
            },
          ]}
        />

        <Text type="secondary" style={{ fontSize: 12 }}>
          店铺：{shopName || "-"} · 本弹窗操作直接作用于千帆草稿箱，删除不可恢复。
        </Text>
      </Space>
    </Modal>
  );
}

/** 从 axios 错误里读后端给的结构化原因（与各页同一句读法）。 */
function readReason(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") {
    const text = (detail as { message?: unknown }).message;
    if (typeof text === "string" && text) return text;
  }
  const text = (error as { message?: unknown })?.message;
  return typeof text === "string" && text ? text : fallback;
}
