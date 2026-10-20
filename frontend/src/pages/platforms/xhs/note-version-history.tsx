/**
 * **生成记录（版本历史）** —— 「LLM 生成笔记」弹窗里的一个 Tab。
 *
 * 用户口径：
 * * 2026-10-17：「页面记录在 LLM 生成笔记中实现**只展示该商品对应生成的笔记**」
 * * 2026-10-17：「生成记录使用**分页列表**展示，使用**抽屉**展示详情」
 *
 * ## 判据
 *
 * 1. **只显示当前商品** —— 请求必带商品标识（版本表是全局的）。
 * 2. **分页列表**（antd `Table` + 分页）—— 横滑卡片看不到总数、也不能翻页；
 *    版本可能有几十条，列表 + 分页才是可用的读法。
 * 3. **详情走抽屉** —— 正文可能几百字，塞进表格行会把行高撑爆（列表要能扫读）；
 *    抽屉里给全文 + 话题 + 当时的文案要求 + 错误原因。
 * 4. **失败版本也列** —— 用户点过生成却没结果时，看不到失败记录会以为「点了没反应」。
 */
import {
  App,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import {
  adoptNoteVersion,
  fetchMaterialImageObjectUrl,
  fetchNoteVersion,
  fetchNoteVersionsByItem,
} from "../../../lib/api";
import type { NoteVersionItem } from "../../../types";

const { Text, Paragraph } = Typography;

/** 状态 → 中文名 + Tag 颜色（**表外取值原样显示**，不造名）。 */
function statusView(status: string): { label: string; color: string } {
  switch (status) {
    case "success":
      return { label: "成功", color: "green" };
    case "failed":
      return { label: "失败", color: "red" };
    case "running":
      return { label: "生成中", color: "blue" };
    case "queued":
      return { label: "排队中", color: "default" };
    default:
      return { label: status, color: "default" };
  }
}

/** 重生成范围 → 中文（`all` 不标：整篇新生成是默认，标出来反成噪音）。 */
function scopeLabel(scope: string): string {
  //: **按块**展示（用户口径 2026-10-18「后端支持每个文案区域单独重新生成」）——
  //: 旧值 `text` 保留显示「只改文案」（历史行里有它），新值各对应一块。
  const labels: Record<string, string> = {
    all: "整篇生成",
    text: "只改文案",
    title: "只改标题",
    body: "只改正文",
    topics: "只改话题",
    media: "只改图片",
  };
  return labels[scope] || "";
}

/** 媒体来源 → 中文（`product_image` 这种代码值不上屏）。 */
function mediaSourceLabel(source: string): string {
  const labels: Record<string, string> = {
    product_image: "商品原图",
    material: "本地素材",
    ai_generate: "AI 生成",
    reference_image: "参考图生成",
    video: "视频",
  };
  return labels[source] || (source ? source : "-");
}

/** 详情抽屉里的完整形态（列表接口只给摘要，正文与快照要单独取）。 */
type VersionDetail = NoteVersionItem & {
  body: string;
  copy_requirements: Record<string, unknown>;
  template_id: number | null;
  /** **这一版实际用了哪些图/视频**（顺序即图序，第 1 张是封面）。
   *  素材图是 `/api/...`（要鉴权）⇒ 渲染必须走 object URL。 */
  media: { url: string; local_path?: string; sort_order?: number; asset_type?: string }[];
};

const PAGE_SIZE = 10;

/** 列表缩略图边长（小尺寸即可，列表要能扫读）。 */
const COVER_SIZE = 44;

export function NoteVersionHistory({
  platformAccountId,
  itemId,
  /** 生成入队后由父组件递增，触发重新拉取（不轮询 —— 记录是事后查的）。 */
  refreshToken = 0,
  onRegen,
  onShowInResult,
  onUnadopt,
  adoptedRecordId,
}: {
  /** 平台账号主键（与 `itemId` 一起唯一定位商品）。 */
  platformAccountId: number;
  /** **平台商品 ID**（弹窗手里只有它，没有本地台账主键）。 */
  itemId: string;
  refreshToken?: number;
  /** 局部重生成（父组件负责提示与刷新）。不传 ⇒ 只读展示。
   *  ⚠️ 用户口径 2026-10-19：操作列**移除**两个旧的整块重生成按钮 ——
   *  逐块重生成已挪到「本次结果」面板（那里能按块点），列表里重复入口只会让人点错。 */
  onRegen?: (record: NoteVersionItem, scope: "text" | "media") => void;
  /** **点「采用」⇒ 把这一版回显到弹窗**（用户口径 2026-10-19）。 */
  onShowInResult?: (detail: VersionDetail) => void;
  /** **取消采用**：弹窗与这一版解绑（表单回默认，不动库）。 */
  onUnadopt?: (record: NoteVersionItem) => void;
  /** **当前采用的记录 id**（由父组件持有 —— 用户口径 2026-10-19 实测：
   *  这个状态原先只在本组件里，切页签/重挂载就丢，于是「取消采用」按钮又变回「采用」，
   *  用户点不到取消。真相源在父组件（`genResult.record_id`），这里只读它。 */
  adoptedRecordId?: number | null;
}) {
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<NoteVersionItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [localToken, setLocalToken] = useState(0);

  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [detailLoadingId, setDetailLoadingId] = useState<number | null>(null);
  const [adopting, setAdopting] = useState<number | null>(null);

  //: 拉数据：一次取「前 page*PAGE_SIZE 条」，前端按页切片
  //: （版本通常几条到几十条，一次取够比每翻一页再请求更顺；后端 `limit` 上限 200）。
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const result = await fetchNoteVersionsByItem(
          platformAccountId,
          itemId,
          PAGE_SIZE * page
        );
        if (cancelled) return;
        setItems(result.items || []);
        setTotal(result.total || 0);
      } catch (err) {
        if (!cancelled) {
          setItems([]);
          setTotal(0);
          //: 拉不到如实说（不静默显示「没有记录」—— 那会让用户以为历史丢了）
          setError(err instanceof Error ? err.message : "生成记录加载失败");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [platformAccountId, itemId, refreshToken, localToken, page]);

  //: 换商品回到第 1 页（否则停在旧商品的第 3 页会看到空列表）
  useEffect(() => {
    setPage(1);
  }, [platformAccountId, itemId]);

  const openDetail = useCallback(
    async (record: NoteVersionItem) => {
      setDetailLoadingId(record.id);
      try {
        const result = await fetchNoteVersion(record.id);
        setDetail(result.item);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "详情加载失败");
      } finally {
        setDetailLoadingId(null);
      }
    },
    [message]
  );

  /** 弹窗内「当前采用版」—— **以父组件为准**（`adoptedRecordId`）。
   *  ⚠️ 不再用本组件内部 state：切换页签会重新挂载，内部值丢失 ⇒ 按钮状态错乱。 */
  const [localAdoptedId, setLocalAdoptedId] = useState<number | null>(null);
  const adoptedId = adoptedRecordId !== undefined ? adoptedRecordId : localAdoptedId;
  const setAdoptedId = setLocalAdoptedId;

  async function onAdopt(record: NoteVersionItem) {
    setAdopting(record.id);
    try {
      //: ⚠️ **不再调 `adoptNoteVersion`**（用户口径 2026-10-19：
      //: *「采用回显只在弹窗中有效」*）—— 采用是「我现在以这一版为基线」的
      //: **临时选择**，不是要写进库的状态；写库会让别的入口也看到「已采用」，
      //: 与「只是在这个弹窗里接着改」的语义不符。
      setAdoptedId(record.id);
      const full = await fetchNoteVersion(record.id);
      onShowInResult?.(full.item);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "采用失败");
    } finally {
      setAdopting(null);
    }
  }

  /** **取消采用**：解绑当前版本，表单回默认（不删记录、不动库）。 */
  async function unadoptCurrent(record: NoteVersionItem) {
    setAdopting(record.id);
    try {
      setAdoptedId(null);
      onUnadopt?.(record);
    } finally {
      setAdopting(null);
    }
  }

  const pageItems = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const columns: ColumnsType<NoteVersionItem> = [
    {
      //: **封面列**（用户口径 2026-10-18：*「生成记录列表新增封面列，笔记图/视频数」*）
      //: —— 一眼看出这版长什么样，不用点进详情。
      //: ⚠️ 素材图是 `/api/...`（**要鉴权**），`<img src>` 带不了 token 会 401 破图
      //: ⇒ 统一走 `VersionCover`（内部转 object URL）。
      title: "封面",
      dataIndex: "cover_url",
      width: 72,
      render: (_: unknown, record) => <VersionCover record={record} />,
    },
    {
      //: **笔记类型**（用户口径 2026-10-19：*「生成记录中新增笔记类型列，图文或视频」*）——
      //: 值来自后端 `generation_records.note_form`（不是从媒体推断：
      //: 媒体全失败时也能正确显示这一版打算产出什么）。
      title: "类型",
      dataIndex: "note_form",
      width: 76,
      render: (value: string, record) => (
        <Tag
          color={value === "video" ? "purple" : "blue"}
          style={{ marginInlineEnd: 0 }}
        >
          {value === "video" ? "视频" : record.note_form ? "图文" : "-"}
        </Tag>
      ),
    },
    {
      title: "版本",
      dataIndex: "version_no",
      width: 96,
      render: (value: number, record) => (
        <Space size={4} wrap>
          <Text strong>第 {value} 版</Text>
          {/* 「采用中」只看**弹窗内**的临时选择（用户口径 2026-10-19：
              采用只在弹窗里有效），不读库里的 `is_adopted`。 */}
          {adoptedId === record.id && (
            <Tag color="processing" style={{ marginInlineEnd: 0 }}>
              采用中
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 106,
      render: (status: string, record) => (
        <Space size={4} wrap>
          <Tag color={statusView(status).color} style={{ marginInlineEnd: 0 }}>
            {statusView(status).label}
          </Tag>
          {scopeLabel(record.regen_scope) && (
            <Tag color="purple" style={{ marginInlineEnd: 0 }}>
              {scopeLabel(record.regen_scope)}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: "标题 / 摘要",
      dataIndex: "title",
      render: (_: unknown, record) =>
        record.status === "failed" ? (
          <Tooltip title={record.error_message || ""}>
            <Text type="danger" ellipsis style={{ display: "block", maxWidth: 340 }}>
              {record.error_message || "生成失败"}
            </Text>
          </Tooltip>
        ) : (
          <div style={{ maxWidth: 340 }}>
            <Text strong ellipsis style={{ display: "block" }}>
              {record.title || "（无标题）"}
            </Text>
            <Text type="secondary" ellipsis style={{ display: "block", fontSize: 12 }}>
              {record.body_excerpt}
              {record.body_length > 60 ? "…" : ""}
            </Text>
          </div>
        ),
    },
    {
      title: "字数",
      dataIndex: "body_length",
      width: 70,
      render: (value: number) => (value > 0 ? String(value) : "-"),
    },
    {
      //: **笔记图/视频数**（用户口径 2026-10-18：*「生成记录列表新增封面列，笔记图/视频数」*）
      title: "图 / 视频",
      dataIndex: "image_count",
      width: 92,
      render: (_: unknown, record) => {
        const imgs = record.image_count || 0;
        const vids = record.video_count || 0;
        if (!imgs && !vids) return "-";
        return (
          <Space size={4} wrap>
            {imgs > 0 && (
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                图 {imgs}
              </Tag>
            )}
            {vids > 0 && (
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>
                视频 {vids}
              </Tag>
            )}
          </Space>
        );
      },
    },
    {
      title: "话题",
      dataIndex: "topics",
      width: 66,
      render: (_: unknown, record) => (record.topics.length ? record.topics.length : "-"),
    },
    //: **「基线」列已移除**（用户口径 2026-10-19：*「基线列移除表数据删除」*）——
    //: 局部生成覆盖的是当前版本（不新增行、不挂父链），这一列恒为空，留着只会误导。
    {
      title: "时间",
      dataIndex: "created_at",
      width: 148,
      render: (value: string | null) => (value ? value.replace("T", " ").slice(0, 19) : "-"),
    },
    {
      title: "操作",
      key: "actions",
      width: 176,
      render: (_: unknown, record) => (
        <Space size={4} wrap>
          <Button
            size="small"
            type="link"
            style={{ padding: 0 }}
            loading={detailLoadingId === record.id}
            onClick={() => void openDetail(record)}
          >
            详情
          </Button>
          {/* ⚠️ 原来这里有两个整块重生成按钮，**已移除**
              （用户口径 2026-10-19）：逐块重生成在「本次结果」面板里能按块点，
              列表里再放一份会让人分不清「改的是哪一版」。 */}
          {/* **采用 / 取消采用**（用户口径 2026-10-19：*「采用回显只在弹窗中有效，
              应新增一个取消采用」*）—— 采用**不改库**，只切弹窗里的当前版本：
              填在弹窗里的表单会回显这一版，接着改 / 重生成都基于它。
              「取消采用」把弹窗与这一版解绑（表单回到默认），**不删任何记录**。 */}
          {record.status === "success" && (
            <Button
              size="small"
              type="link"
              style={{ padding: 0 }}
              loading={adopting === record.id}
              onClick={() =>
                void (adoptedId === record.id ? unadoptCurrent(record) : onAdopt(record))
              }
            >
              {adoptedId === record.id ? "取消采用" : "采用"}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  if (error) {
    return (
      <Text type="danger" style={{ fontSize: 12 }}>
        {error}
      </Text>
    );
  }

  return (
    <div>
      <Table<NoteVersionItem>
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={pageItems}
        locale={{
          emptyText: (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这个商品还没有生成记录" />
          ),
        }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          size: "small",
          showTotal: (count) => `共 ${count} 条`,
          onChange: (next) => setPage(next),
        }}
        scroll={{ x: 1080 }}
      />

      {/* 详情抽屉（用户口径 2026-10-17：*「使用抽屉展示详情」*） */}
      <Drawer
        open={detail !== null}
        onClose={() => setDetail(null)}
        width="min(720px, 92vw)"
        title={detail ? `第 ${detail.version_no} 版详情` : "版本详情"}
      >
        {detail && (
          /* 抽屉内容**按 Tab 自然分割**（用户口径 2026-10-18：*「把抽屉中所有内容使用
             tab 自然分割布局，另外不要使用 json 展示数据」*）。
             原实现是一条竖排长列表（概览 → 失败原因 → 标题 → 正文 → 话题 → 图 → JSON），
             要看图得先滚过几百字正文，而「文案要求」是 `JSON.stringify` 出来的原文 ——
             那是给开发者看的，不是给用户看的。
             分区依据 = 用户实际要回答的问题：
               概览：这一版是什么（状态/范围/模型/时间/失败原因）
               文案：写了什么（标题/正文/话题）
               配图：用了哪些图（缩略图 + 序号）
               要求：当时是怎么要求的（**逐项可读**，关键字原样保留） */
          <Tabs
            size="small"
            items={[
              {
                key: "overview",
                label: "概览",
                children: (
                  <Space direction="vertical" size={12} style={{ width: "100%" }}>
                    <Descriptions column={2} size="small" bordered>
                      <Descriptions.Item label="状态">
                        <Tag
                          color={statusView(detail.status).color}
                          style={{ marginInlineEnd: 0 }}
                        >
                          {statusView(detail.status).label}
                        </Tag>
                      </Descriptions.Item>
                      <Descriptions.Item label="重生成范围">
                        {scopeLabel(detail.regen_scope) || "整篇生成"}
                      </Descriptions.Item>
                      <Descriptions.Item label="采用">
                        {detail.is_adopted ? "是（当前采用版）" : "否"}
                      </Descriptions.Item>
                      <Descriptions.Item label="模型">
                        {detail.model_name || "-"}
                      </Descriptions.Item>
                      <Descriptions.Item label="媒体来源">
                        {mediaSourceLabel(detail.media_source)}
                      </Descriptions.Item>
                      <Descriptions.Item label="字数">{detail.body_length}</Descriptions.Item>
                      <Descriptions.Item label="话题数">{detail.topics.length}</Descriptions.Item>
                      <Descriptions.Item label="生成时间" span={2}>
                        {detail.created_at
                          ? detail.created_at.replace("T", " ").slice(0, 19)
                          : "-"}
                      </Descriptions.Item>
                    </Descriptions>
                    {detail.status === "failed" && (
                      <div>
                        <Text strong>失败原因</Text>
                        <Paragraph
                          type="danger"
                          style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}
                        >
                          {detail.error_message || "（未返回原因）"}
                        </Paragraph>
                      </div>
                    )}
                  </Space>
                ),
              },
              {
                key: "copy",
                //: **笔记完整内容**在一个 Tab 下（用户口径 2026-10-18：*「生成后的笔记
                //: 完整内容放一个 tab 下」*）—— 标题 + 正文 + 话题是「这篇笔记发出来长什么样」，
                //: 拆成三个 Tab 反而要来回点。空版注明「（空）」避免以为没渲染。
                label: `笔记内容${detail.title ? "" : "（空）"}`,
                children: (
                  <Space direction="vertical" size={12} style={{ width: "100%" }}>
                    <div>
                      <Text strong>标题</Text>
                      <Paragraph style={{ marginBottom: 0 }}>
                        {detail.title || "（无标题）"}
                      </Paragraph>
                    </div>
                    <div>
                      <Text strong>正文（{detail.body_length} 字）</Text>
                      <Paragraph
                        style={{
                          marginBottom: 0,
                          whiteSpace: "pre-wrap",
                          maxHeight: "48vh",
                          overflowY: "auto",
                          background: "var(--ant-color-fill-quaternary, #fafafa)",
                          padding: 10,
                          borderRadius: 6,
                        }}
                      >
                        {detail.body || "（空）"}
                      </Paragraph>
                    </div>
                    <div>
                      <Text strong>话题（{detail.topics.length} 个）</Text>
                      <div style={{ marginTop: 4 }}>
                        {detail.topics.length ? (
                          detail.topics.map((topic) => (
                            <Tag key={topic} style={{ marginBottom: 4 }}>
                              {topic}
                            </Tag>
                          ))
                        ) : (
                          <Text type="secondary">无</Text>
                        )}
                      </div>
                    </div>
                    <div>
                      {/* **图 / 视频也属于「笔记内容」**（用户口径 2026-10-18：
                          *「笔记内容保存本次生成的文本话题以及图或视频」*）——
                          它们和标题/正文/话题一样，是这一版产物的一部分；
                          单开一个「配图」Tab 会让用户以为内容只有文字。 */}
                      <Text strong>图 / 视频（{detail.media.length} 项）</Text>
                      <DetailMediaList media={detail.media} />
                    </div>
                  </Space>
                ),
              },
              {
                key: "requirements",
                label: "文案要求",
                children: <CopyRequirementsPanel value={detail.copy_requirements} />,
              },
            ]}
          />
        )}
      </Drawer>
    </div>
  );
}

/**
 * 版本封面（列表用）：有封面就显示缩略图，没有给一个中性占位。
 *
 * ⚠️ **素材图必须走 object URL**：`cover_url` 可能是
 * `/api/xhs/materials/files/6/file`（带鉴权），`<img src>` 带不了 Authorization
 * 头 ⇒ 恒 401 破图（本仓踩过一次）。公网 CDN（商品图）则原样用。
 */
function VersionCover({ record }: { record: NoteVersionItem }) {
  const raw = record.cover_url || "";
  const [src, setSrc] = useState("");
  useEffect(() => {
    let alive = true;
    let created = "";
    if (!raw) {
      setSrc("");
      return undefined;
    }
    if (!raw.startsWith("/api/")) {
      setSrc(raw);
      return undefined;
    }
    void (async () => {
      try {
        const objectUrl = await fetchMaterialImageObjectUrl(raw);
        if (!alive) {
          window.URL.revokeObjectURL(objectUrl);
          return;
        }
        created = objectUrl;
        setSrc(objectUrl);
      } catch {
        if (alive) setSrc("");
      }
    })();
    return () => {
      alive = false;
      if (created) window.URL.revokeObjectURL(created);
    };
  }, [raw]);

  if (!src) {
    return (
      <div
        style={{
          width: COVER_SIZE,
          height: COVER_SIZE,
          borderRadius: 4,
          border: "1px dashed var(--ant-color-border, #d9d9d9)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Text type="secondary" style={{ fontSize: 11 }}>
          {record.video_count ? "视频" : "无图"}
        </Text>
      </div>
    );
  }
  return (
    <img
      src={src}
      alt="封面"
      style={{
        width: COVER_SIZE,
        height: COVER_SIZE,
        objectFit: "cover",
        borderRadius: 4,
        border: "1px solid var(--ant-color-border, #d9d9d9)",
        display: "block",
      }}
    />
  );
}

/** 抽屉里的媒体清单缩略图边长（比列表大，要能看清内容）。 */
const DETAIL_THUMB_SIZE = 88;

/** 「文案要求」里各字段的中文名（**不把键名丢给用户看**）。 */
const COPY_FIELD_LABELS: Record<string, string> = {
  title_prompt: "标题方向",
  body_prompt: "正文方向",
  topic_prompt: "话题方向",
  title_min: "标题最少字数",
  title_max: "标题最多字数",
  body_min: "正文最少字数",
  body_max: "正文最多字数",
  topic_min: "话题最少个数",
  topic_max: "话题最多个数",
  emoji: "表情符号",
};

/**
 * **当时的文案要求**：逐项可读（用户口径 2026-10-18：*「不要使用 json 展示数据」*）。
 *
 * ⚠️ 原实现是 `JSON.stringify(value, null, 2)` —— 带引号、带花括号、字段名是
 * `title_prompt` / `body_max` 这类英文键，用户看不出「这一版当时到底要求了什么」。
 * 这里：三块提示词原样给全文（那是用户自己写的句子），数值/枚举给中文说明；
 * 未设置的值不显示（与生成时「没改就不下发」的口径一致 —— 没设置不是「空值」）。
 */
function CopyRequirementsPanel({ value }: { value: Record<string, unknown> }) {
  const raw = value || {};
  const promptKeys = ["title_prompt", "body_prompt", "topic_prompt"] as const;
  const prompts = promptKeys.filter((key) => String(raw[key] || "").trim());
  const ranges: { label: string; text: string }[] = [];
  const pair = (minKey: string, maxKey: string, unit: string, label: string) => {
    const min = raw[minKey];
    const max = raw[maxKey];
    if (min == null && max == null) return;
    ranges.push({ label, text: `${min ?? "?"} – ${max ?? "?"} ${unit}` });
  };
  pair("title_min", "title_max", "字", "标题字数");
  pair("body_min", "body_max", "字", "正文字数");
  pair("topic_min", "topic_max", "个", "话题数量");
  const emojiText =
    raw.emoji === "on" ? "带表情" : raw.emoji === "off" ? "不带表情" : "自动（按文案调性）";

  const configured = prompts.length > 0 || ranges.length > 0;
  if (!configured && !raw.emoji) {
    return <Text type="secondary">这一版没有自定义文案要求（完全按提示词模板）</Text>;
  }

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      {prompts.length > 0 && (
        <div>
          <Text strong>补充提示词</Text>
          {prompts.map((key) => (
            <div key={key} style={{ marginTop: 6 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {COPY_FIELD_LABELS[key]}
              </Text>
              <Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>
                {String(raw[key])}
              </Paragraph>
            </div>
          ))}
        </div>
      )}
      {ranges.length > 0 && (
        <div>
          <Text strong>数量区间</Text>
          <div style={{ marginTop: 4 }}>
            {ranges.map((item) => (
              <Tag key={item.label} style={{ marginBottom: 4 }}>
                {item.label}：{item.text}
              </Tag>
            ))}
          </div>
        </div>
      )}
      <div>
        <Text strong>表情符号</Text>
        <div style={{ marginTop: 4 }}>
          <Tag>{emojiText}</Tag>
        </div>
      </div>
    </Space>
  );
}

/**
 * 抽屉的媒体清单：**逐张显示图 / 视频**（用户口径 2026-10-18：
 * *「抽屉中没有图、视频」*）。
 *
 * ⚠️ 素材图 URL 是 `/api/xhs/materials/files/{id}/file`（**要鉴权**）——
 * `<img src>` 带不了 Authorization 头 ⇒ 401 破图。这里逐张转 object URL，
 * 卸载时统一 revoke（不 revoke 会泄漏内存）。公网 CDN（商品图）原样用。
 */
function DetailMediaList({
  media,
}: {
  media: { url: string; local_path?: string; sort_order?: number; asset_type?: string }[];
}) {
  const [resolved, setResolved] = useState<Record<string, string>>({});

  useEffect(() => {
    let alive = true;
    const created: string[] = [];
    void (async () => {
      const map: Record<string, string> = {};
      for (const entry of media) {
        const raw = entry.url || entry.local_path || "";
        if (!raw) continue;
        if (!raw.startsWith("/api/")) {
          map[raw] = raw;
          continue;
        }
        try {
          const objectUrl = await fetchMaterialImageObjectUrl(raw);
          if (!alive) {
            window.URL.revokeObjectURL(objectUrl);
            continue;
          }
          created.push(objectUrl);
          map[raw] = objectUrl;
        } catch {
          map[raw] = "";
        }
      }
      if (alive) setResolved(map);
    })();
    return () => {
      alive = false;
      created.forEach((url) => window.URL.revokeObjectURL(url));
    };
  }, [media]);

  if (!media.length) {
    return (
      <div style={{ marginTop: 4 }}>
        <Text type="secondary">这一版没有图 / 视频</Text>
      </div>
    );
  }

  return (
    <Space size={8} wrap style={{ marginTop: 4 }}>
      {media.map((entry, index) => {
        const raw = entry.url || entry.local_path || "";
        const src = resolved[raw] || "";
        const isVideo = (entry.asset_type || "image") === "video";
        return (
          <div key={`${raw}-${index}`} style={{ textAlign: "center" }}>
            {isVideo ? (
              <div
                style={{
                  width: DETAIL_THUMB_SIZE,
                  height: DETAIL_THUMB_SIZE,
                  borderRadius: 6,
                  border: "1px solid var(--ant-color-border, #d9d9d9)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  background: "var(--ant-color-fill-quaternary, #fafafa)",
                }}
              >
                <Text style={{ fontSize: 12 }}>视频</Text>
              </div>
            ) : src ? (
              <img
                src={src}
                alt={`第 ${index + 1} 张`}
                style={{
                  width: DETAIL_THUMB_SIZE,
                  height: DETAIL_THUMB_SIZE,
                  objectFit: "cover",
                  borderRadius: 6,
                  border: "1px solid var(--ant-color-border, #d9d9d9)",
                  display: "block",
                }}
                title={raw}
              />
            ) : (
              <div
                style={{
                  width: DETAIL_THUMB_SIZE,
                  height: DETAIL_THUMB_SIZE,
                  borderRadius: 6,
                  border: "1px dashed var(--ant-color-border, #d9d9d9)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Text type="secondary" style={{ fontSize: 11 }}>
                  加载中
                </Text>
              </div>
            )}
            <Text type="secondary" style={{ fontSize: 11 }}>
              {index === 0 ? "封面" : `第 ${index + 1} 张`}
            </Text>
          </div>
        );
      })}
    </Space>
  );
}

export default NoteVersionHistory;
