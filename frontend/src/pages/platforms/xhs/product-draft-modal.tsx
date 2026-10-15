/**
 * 「新增商品」弹窗 —— 商品数据包 → **草稿箱**（设计 23）。
 *
 * ## 红线：只存草稿，不上架
 *
 * 用户 2026-10-07 口径：「新增的商品**只能保存到草稿箱内，不允许直接发布到线上**」。
 * 落实在本组件里是**三层**：
 *
 * 1. 请求体（`ItemDraftCreatePayload`）里**没有**任何发布 / 上架字段 ——
 *    后端 `extra="forbid"` ⇒ 多传直接 422（契约层，不是文案承诺）；
 * 2. 提交按钮的文案是「**保存到草稿箱**」（不是「发布」「提交」「上架」）；
 * 3. 常驻红字说明「不会上架」—— 用户在看到「成功」时最容易脑补成「已发布」。
 *
 * ## 三个 Tab（用户偏好「分块不混在一起」）
 *
 * 1. **数据包** —— 目录路径 + 解析结果（含**逐条** `warnings[]`）；
 * 2. **商品信息** —— 标题 + 类目搜索（只从叶子类目里选）；
 * 3. **提交预览** —— SKU 表格 + 主图缩略图 + 红线说明。
 *
 * ⚠️ 弹窗宽度 **75vw**（视口百分比，不固定像素 —— 用户既有偏好）。
 * ⚠️ 有上限的列表**不设「全选」**（用户既有偏好）。
 * ⚠️ 说明文案里**禁止 `**`**（会原样显示，守卫覆盖整个 `frontend/src`）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { FolderOpenOutlined, SearchOutlined } from "@ant-design/icons";

import {
  createItemDraft,
  parseItemDraftPackage,
  searchItemDraftCategories,
} from "../../../lib/api";
import type {
  ItemDraftCategory,
  ItemDraftCreateResult,
  ItemDraftPackageParseResult,
} from "../../../types";

const { Text, Paragraph } = Typography;

export type ProductDraftModalProps = {
  open: boolean;
  platformAccountId: number | null;
  shopName: string;
  onClose: () => void;
  /** 建档成功（父页据此刷新草稿箱列表 / 提示）。 */
  onCreated?: (result: ItemDraftCreateResult) => void;
};

/** 数据包解析 + 建档的等待状态（父页蒙版与本弹窗共用同一套语义）。 */
export function ProductDraftModal({
  open,
  platformAccountId,
  shopName,
  onClose,
  onCreated,
}: ProductDraftModalProps) {
  //: ⚠️ 必须走 `App.useApp()` 而不是静态 `message` —— 静态导入拿不到
  //: ConfigProvider 主题（守卫 `test_static_antd_message_import_only_survives_in_api_ts`）。
  const { message } = App.useApp();
  const [tab, setTab] = useState("package");
  const [packagePath, setPackagePath] = useState("");
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState<ItemDraftPackageParseResult | null>(null);

  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<ItemDraftCategory | null>(null);
  const [keyword, setKeyword] = useState("");
  const [searchingCategory, setSearchingCategory] = useState(false);
  const [categories, setCategories] = useState<ItemDraftCategory[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ItemDraftCreateResult | null>(null);

  // 每次打开清空上一轮的结果 —— 留着上一单的「成功」是误导。
  useEffect(() => {
    if (!open) return;
    setTab("package");
    setParsed(null);
    setResult(null);
    setCategory(null);
    setCategories([]);
    setKeyword("");
  }, [open]);

  const limits = parsed?.limits ?? { max_images: 100, max_skus: 200 };

  /** 提交时要用的图片清单：主图 + 详情图，按解析结果原样（顺序 = 封面顺序）。 */
  const mainImages = useMemo(() => parsed?.main_images ?? [], [parsed]);
  const detailImages = useMemo(() => parsed?.detail_images ?? [], [parsed]);
  const totalImages = mainImages.length + detailImages.length + (parsed?.sku_rows.length ?? 0);

  const handleParse = useCallback(async () => {
    if (!platformAccountId) return;
    const path = packagePath.trim();
    if (!path) {
      void message.warning("请先填写商品数据包目录");
      return;
    }
    setParsing(true);
    setResult(null);
    try {
      const data = await parseItemDraftPackage(platformAccountId, path);
      setParsed(data);
      setTitle(data.suggested_title || "");
      void message.success(
        `解析到 ${data.sku_rows.length} 条 SKU、主图 ${data.main_images.length} 张、` +
          `详情图 ${data.detail_images.length} 张`,
      );
    } catch (error) {
      void message.error(readReason(error, "解析数据包失败"));
    } finally {
      setParsing(false);
    }
  }, [packagePath, platformAccountId]);

  const handleSearchCategory = useCallback(
    async (value: string) => {
      if (!platformAccountId) return;
      const text = value.trim();
      if (!text) {
        void message.warning("请先输入类目关键词");
        return;
      }
      setSearchingCategory(true);
      try {
        const list = await searchItemDraftCategories(platformAccountId, text);
        setCategories(list);
        if (list.length === 0) {
          void message.info("没有搜到可用的叶子类目，换个关键词试试");
        }
      } catch (error) {
        void message.error(readReason(error, "搜索类目失败"));
      } finally {
        setSearchingCategory(false);
      }
    },
    [platformAccountId],
  );

  const handleSubmit = useCallback(async () => {
    if (!platformAccountId || !parsed) return;
    if (!category) {
      void message.warning("请先选择商品类目");
      return;
    }
    if (!title.trim()) {
      void message.warning("请填写商品标题");
      return;
    }
    setSubmitting(true);
    try {
      const data = await createItemDraft({
        platform_account_id: platformAccountId,
        category_id: category.id,
        name: title.trim(),
        main_images: mainImages,
        detail_images: detailImages,
        skus: parsed.sku_rows.map((row) => ({
          name: row.name,
          price_yuan: row.price_yuan,
          stock: row.stock,
          //: ⚠️ 本地路径**不上传给平台** —— 后端按路径读字节再上传；
          //: 这里传的是「用哪张图」，不是「图在哪」（避免把盘上路径写进平台数据）。
          image_path: row.image_path ?? null,
        })),
      });
      setResult(data);
      onCreated?.(data);
      void message.success("已保存到草稿箱（未上架）");
    } catch (error) {
      void message.error(readReason(error, "保存草稿失败"));
    } finally {
      setSubmitting(false);
    }
  }, [category, detailImages, mainImages, onCreated, parsed, platformAccountId, title]);

  const overLimit = totalImages > limits.max_images;

  const packageTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space.Compact style={{ width: "100%" }}>
        <Input
          prefix={<FolderOpenOutlined />}
          placeholder="商品数据包目录的绝对路径，例如 C:/Users/xxx/Desktop/商品ID_935947169189"
          value={packagePath}
          onChange={(event) => setPackagePath(event.target.value)}
          onPressEnter={() => void handleParse()}
          disabled={parsing}
        />
        <Button type="primary" loading={parsing} onClick={() => void handleParse()}>
          解析数据包
        </Button>
      </Space.Compact>

      {!parsed ? (
        <Empty
          description={
            <Text type="secondary">
              填写数据包目录后点「解析数据包」；解析只读本地磁盘，不会访问平台。
            </Text>
          }
        />
      ) : (
        <>
          <Descriptions size="small" bordered column={4}>
            <Descriptions.Item label="SKU">{parsed.sku_rows.length} 条</Descriptions.Item>
            <Descriptions.Item label="主图">{parsed.main_images.length} 张</Descriptions.Item>
            <Descriptions.Item label="详情图">{parsed.detail_images.length} 张</Descriptions.Item>
            <Descriptions.Item label="视频">{parsed.videos.length} 个</Descriptions.Item>
          </Descriptions>

          {overLimit && (
            <Alert
              type="warning"
              showIcon
              message={`图片共 ${totalImages} 张，超过单次 ${limits.max_images} 张上限`}
              description="超出的图片不会被上传，可在千帆商品编辑页补充。"
            />
          )}

          {parsed.warnings.length > 0 && (
            <Alert
              type="info"
              showIcon
              message={`解析提示（${parsed.warnings.length} 条）`}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {parsed.warnings.map((text, index) => (
                    <li key={`${index}-${text.slice(0, 12)}`}>
                      <Text style={{ fontSize: 12 }}>{text}</Text>
                    </li>
                  ))}
                </ul>
              }
            />
          )}

          <Button type="primary" onClick={() => setTab("info")}>
            下一步：填写商品信息
          </Button>
        </>
      )}
    </Space>
  );

  const infoTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Form layout="vertical">
        <Form.Item
          label="商品标题"
          required
          help="已按数据包第一条 SKU 名预填，可修改；平台商品标题上限 60 字。"
        >
          <Input
            value={title}
            maxLength={60}
            showCount
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：【中古风】实木三斗柜"
          />
        </Form.Item>
      </Form>

      <Space.Compact style={{ width: "100%" }}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="输入类目关键词，例如 斗柜 / 梳妆台 / 收纳柜"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          onPressEnter={() => void handleSearchCategory(keyword)}
        />
        <Button
          type="primary"
          loading={searchingCategory}
          onClick={() => void handleSearchCategory(keyword)}
        >
          搜索类目
        </Button>
      </Space.Compact>

      {categories.length > 0 && (
        <List
          size="small"
          bordered
          dataSource={categories}
          style={{ maxHeight: 240, overflowY: "auto" }}
          renderItem={(item) => (
            <List.Item
              actions={[
                <Button
                  key="pick"
                  type={category?.id === item.id ? "primary" : "default"}
                  size="small"
                  onClick={() => setCategory(item)}
                >
                  {category?.id === item.id ? "已选择" : "选择"}
                </Button>,
              ]}
            >
              <Space size={8}>
                <Text>{item.name}</Text>
                {item.need_brand && <Tag color="orange">需品牌资质</Tag>}
              </Space>
            </List.Item>
          )}
        />
      )}

      {category && (
        <Alert
          type="success"
          showIcon
          message={`已选类目：${category.name}`}
          description={<Text type="secondary">类目 ID：{category.id}</Text>}
        />
      )}

      <Button type="primary" disabled={!category} onClick={() => setTab("preview")}>
        下一步：确认提交
      </Button>
    </Space>
  );

  const previewTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert
        type="warning"
        showIcon
        message="保存后只进草稿箱，不会上架"
        description="商品不会对买家可见；上架需在千帆草稿箱补齐必填项后提交。"
      />

      <Descriptions size="small" bordered column={2}>
        <Descriptions.Item label="商品标题">{title || "-"}</Descriptions.Item>
        <Descriptions.Item label="商品类目">{category?.name ?? "-"}</Descriptions.Item>
        <Descriptions.Item label="主图">{mainImages.length} 张</Descriptions.Item>
        <Descriptions.Item label="详情图">{detailImages.length} 张</Descriptions.Item>
        <Descriptions.Item label="SKU">{parsed?.sku_rows.length ?? 0} 条</Descriptions.Item>
        <Descriptions.Item label="店铺">{shopName || "-"}</Descriptions.Item>
      </Descriptions>

      <Table
        size="small"
        rowKey={(row) => row.name}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        dataSource={parsed?.sku_rows ?? []}
        columns={[
          {
            title: "SKU 名称",
            dataIndex: "name",
            ellipsis: true,
            render: (value: string) => (
              <Tooltip title={value}>
                <span>{value}</span>
              </Tooltip>
            ),
          },
          {
            title: "价格（元）",
            dataIndex: "price_yuan",
            width: 120,
            render: (value: number | null) =>
              value === null || value === undefined ? <Text type="secondary">待补</Text> : value,
          },
          {
            title: "库存",
            dataIndex: "stock",
            width: 100,
            render: (value: number | null) =>
              value === null || value === undefined ? <Text type="secondary">0</Text> : value,
          },
          {
            title: "规格图",
            dataIndex: "image_path",
            width: 90,
            render: (value: string | null) =>
              value ? <Tag color="blue">已有</Tag> : <Tag>无</Tag>,
          },
        ]}
      />

      {result && (
        <Alert
          type="success"
          showIcon
          message={`已保存到草稿箱：${result.draft_id}`}
          description={
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Text style={{ fontSize: 12 }}>{result.note}</Text>
              <Text style={{ fontSize: 12 }}>
                上传图片 {result.uploaded_images} 张 · SKU {result.sku_count} 条
                {result.failed_images.length > 0
                  ? ` · 失败 ${result.failed_images.length} 张`
                  : ""}
              </Text>
              {result.warnings.length > 0 && (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {result.warnings.map((text, index) => (
                    <li key={`${index}-${text.slice(0, 12)}`}>
                      <Text style={{ fontSize: 12 }}>{text}</Text>
                    </li>
                  ))}
                </ul>
              )}
            </Space>
          }
        />
      )}

      <Button
        type="primary"
        loading={submitting}
        disabled={!category || !!result}
        onClick={() => void handleSubmit()}
      >
        保存到草稿箱
      </Button>
    </Space>
  );

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width="75vw"
      //: ⚠️ `centered` 是必须的（守卫 `test_every_modal_is_vertically_centered`）：
      //: antd 默认顶部对齐，用户口径「所有弹窗均应修复成居中」。
      centered
      title="新增商品（保存到草稿箱）"
      destroyOnClose
      footer={
        <Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            保存后仅进草稿箱，不会上架
          </Text>
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "package", label: "数据包", children: packageTab },
          {
            key: "info",
            label: "商品信息",
            disabled: !parsed,
            children: infoTab,
          },
          {
            key: "preview",
            label: "提交预览",
            disabled: !parsed || !category,
            children: previewTab,
          },
        ]}
      />
      <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
        说明：草稿保存的是商品骨架（类目 / 标题 / 主图 / 详情图 / SKU）。
        商品属性、商品描述、视频需在千帆商品编辑页补充 —— 平台草稿接口不接受这几项。
      </Paragraph>
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
