/**
 * 图片空间页（设计 22 §1，2026-10-06）—— 千帆云图片资产的浏览与操作。
 *
 * ## 三条硬约定（改本页前先读）
 *
 * 1. **本地零落库**：平台数据现取现渲染，本地不建表、不存副本（用户口径
 *    「所有数据不落库本地」）。「上传 / 下载」也是**平台侧**行为（文件进 / 出
 *    千帆云），不是本地库。
 * 2. **操作按钮功能全部要有**（2026-10-06 用户追加口径，推翻初版「只读」）：
 *    上传本地图片 / 新建文件夹 / 批量移动到 / 批量下载 / 批量删除 /
 *    行内（复制链接 · 移动到 · 更多→下载 · 删除）/ 回收站。
 *    写操作走「用户显式点击 + 本店归属 + 两层确认（删除）」。
 * 3. **与「素材库」不是一回事**：那个（笔记工坊 `?tab=materials`）是**本地磁盘**
 *    目录，这个是**千帆云** 15G 空间。
 *
 * ## 布局（一比一对照千帆 `pictureSpace`）
 *
 * 左栏（关键词搜索 + `我的图片` + 配额条）/ 右区（工具栏 + 表格 + 分页）。
 *
 * ## 缺值口径
 *
 * 尺寸缺 ⇒ `-`（不显示 `0 x 0`）；配额取不到 ⇒ **不渲染**配额条（不显示 `0 / 0`）。
 */
import {
  AppstoreOutlined,
  BorderOutlined,
  CheckSquareOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FileImageOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  MoreOutlined,
  ReloadOutlined,
  SearchOutlined,
  SwapOutlined,
  UndoOutlined,
  UnorderedListOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Dropdown,
  Empty,
  Input,
  Modal,
  Pagination,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useThemePalette } from "../../../app/providers";
import {
  createPictureSpaceFolder,
  deletePictureSpaceItems,
  downloadPictureSpaceItems,
  fetchAccountsPage,
  fetchPictureSpaceItems,
  fetchPictureSpaceResizeConditions,
  fetchPictureSpaceUsage,
  movePictureSpaceItems,
  permanentDeletePictureSpaceItems,
  previewPictureSpaceDelete,
  resizePictureSpace,
  restorePictureSpaceItems,
  uploadPictureSpaceFile,
} from "../../../lib/api";
import { formatShanghaiTime } from "../../../lib/time";
import type {
  PictureSpaceItem,
  PictureSpaceResizeCheck,
  PictureSpaceUsage,
  PlatformAccount,
} from "../../../types";

import { WaitMask } from "../../../components/ui/wait-mask";
const { Text } = Typography;

/** 排序字段（与后端 `MATERIAL_ORDER_FIELDS` 同值；后端表外值 400）。 */
const ORDER_FIELDS = [
  { value: "name", label: "文件名称" },
  { value: "createTime", label: "上传时间" },
  { value: "size", label: "文件大小" },
] as const;

/** 每页条数（与平台页面同口径：真机下拉 **100 / 200 / 500**）。 */
const PAGE_SIZES = [100, 200, 500];

/**
 * 写操作后刷新前的等待（毫秒）。
 *
 * ⚠️ 平台 `search_directory_manageable` 有**秒级缓存** —— 真机实测：删除成功后
 * 立刻重查列表**仍是旧数据**（隔 ~5 秒再查才准）。不留这一拍，用户会看到
 * 「删了但还在」并以为失败。
 */
const WRITE_SETTLE_MS = 1500;

/** 账号下拉每页大小（无限滚动；账号多时分页拉取，不一次全打）。 */
const ACCOUNT_PAGE_SIZE = 20;

type OrderValue = `${(typeof ORDER_FIELDS)[number]["value"]}_${"asc" | "desc"}`;

function splitOrder(value: OrderValue): { field: string; asc: boolean } {
  const [field, dir] = value.split("_");
  return { field, asc: dir === "asc" };
}

/** 字节 → 人类可读（与平台一致：K / M / G，保留两位）。 */
function formatSize(bytes: number): string {
  if (!bytes) return "0K";
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}K`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)}M`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)}G`;
}

/** 尺寸 → `800 x 800`；缺任一边 ⇒ `-`（**不补 0**）。 */
function formatDimensions(width: number | null, height: number | null): string {
  if (width == null || height == null) return "-";
  return `${width} x ${height}`;
}

/** epoch ms → `YYYY-MM-DD HH:MM`（走全站同日口径）。 */
function formatUploaded(ms: number | null): string {
  if (!ms) return "-";
  return formatShanghaiTime(new Date(ms).toISOString());
}

export function PictureSpacePage() {
  const p = useThemePalette();
  const { message, modal } = App.useApp();

  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountTotal, setAccountTotal] = useState(0);
  const accountsLoadingRef = useRef(false);
  const [accountId, setAccountId] = useState<number | undefined>(undefined);
  const [items, setItems] = useState<PictureSpaceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [usage, setUsage] = useState<PictureSpaceUsage | null>(null);
  const [keyword, setKeyword] = useState("");
  const [appliedKeyword, setAppliedKeyword] = useState("");
  const [orderValue, setOrderValue] = useState<OrderValue>("name_asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(100);
  const [view, setView] = useState<"list" | "grid">("list");
  const [isLoading, setIsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderName, setFolderName] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  //: 视图：`main` = 我的图片（正常）；`recycle` = 回收站（真机 `/pictureSpace/recycle`）。
  const [scene, setScene] = useState<"main" | "recycle">("main");
  //: 移动弹窗：目标目录 id（`"0"` = 根）。
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveTarget, setMoveTarget] = useState("0");
  // 空间扩容（2026-10-07）：弹窗打开时拉条件，逐条展示「是否支持扩容申请」。
  const [resizeOpen, setResizeOpen] = useState(false);
  const [resizeCheck, setResizeCheck] = useState<PictureSpaceResizeCheck | null>(null);
  const [resizeLoading, setResizeLoading] = useState(false);

  // 列**全部**小红书账号（不分 sub_type）—— 图片空间对任一账号都成立
  // （用户 2026-10-06 口径：「应支持当前所有小红书账号，目前只有开心睡大觉的店」）。
  // 账号下拉走**无限滚动**（`onPopupScroll` + 分页拉取），账号多时不卡。
  useEffect(() => {
    void (async () => {
      try {
        const res = await fetchAccountsPage("xhs", 1, ACCOUNT_PAGE_SIZE);
        setAccounts(res.items);
        setAccountTotal(res.total);
        setAccountId((current) => current ?? res.items[0]?.id);
      } catch {
        setAccounts([]);
        setAccountTotal(0);
      }
    })();
  }, []);

  /** 账号下拉滚动到底 → 追加下一页（无限下拉）。 */
  const loadMoreAccounts = useCallback(async () => {
    if (accountsLoadingRef.current || accounts.length >= accountTotal) return;
    accountsLoadingRef.current = true;
    try {
      const nextPage = Math.floor(accounts.length / ACCOUNT_PAGE_SIZE) + 1;
      const res = await fetchAccountsPage("xhs", nextPage, ACCOUNT_PAGE_SIZE);
      setAccounts((current) => {
        const seen = new Set(current.map((a) => a.id));
        return [...current, ...res.items.filter((a) => !seen.has(a.id))];
      });
      setAccountTotal(res.total);
    } catch {
      /* 静默：滚动加载失败不打断页面 */
    } finally {
      accountsLoadingRef.current = false;
    }
  }, [accounts.length, accountTotal]);

  const load = useCallback(async () => {
    if (!accountId) {
      setItems([]);
      setTotal(0);
      return;
    }
    setIsLoading(true);
    setError(null);
    const { field, asc } = splitOrder(orderValue);
    try {
      const [res, quota] = await Promise.all([
        fetchPictureSpaceItems({
          platform_account_id: accountId,
          keyword: appliedKeyword,
          order_field: scene === "recycle" ? "updateTime" : field,
          order_asc: scene === "recycle" ? false : asc,
          page,
          page_size: pageSize,
          ...(scene === "recycle" ? { recycle: true } : {}),
        }),
        fetchPictureSpaceUsage(accountId).catch(() => null),
      ]);
      setItems(res.items);
      setTotal(res.total);
      setUsage(quota);
      setSelectedIds([]);
    } catch {
      setError(scene === "recycle" ? "回收站加载失败，请稍后重试。" : "图片空间加载失败，请稍后重试。");
      setItems([]);
      setTotal(0);
    } finally {
      setIsLoading(false);
    }
  }, [accountId, appliedKeyword, orderValue, page, pageSize, scene]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 写操作统一包装：忙碌态 + 结构化错误原因 + 成功后刷新。
   *
   * ⚠️ 刷新前**留一拍**（`WRITE_SETTLE_MS`）：平台 `search_directory_manageable`
   * 有秒级缓存 —— 真机实测「删除成功后立刻重查，列表仍是旧数据」（隔 ~5 秒再查才准）。
   * 不留这一拍，用户会看到「删了但还在」并以为失败（实测误判过一次）。
   */
  async function runWrite(fn: () => Promise<unknown>, okText: string) {
    setBusy(true);
    try {
      await fn();
      message.success(okText);
      await new Promise((resolve) => setTimeout(resolve, WRITE_SETTLE_MS));
      await load();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: { message?: string } } } })?.response
        ?.data?.detail;
      message.error(detail?.message || "操作失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  const selectedItems = useMemo(
    () => items.filter((it) => selectedIds.includes(it.id)),
    [items, selectedIds]
  );

  /** 切换单条选中（卡片点击 / 卡片复选框共用；与列表 `selectedIds` 同一份）。 */
  function toggleSelect(id: string) {
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id]
    );
  }

  async function copyLink(item: PictureSpaceItem) {
    if (!item.link) {
      message.warning("该图片没有可复制的链接。");
      return;
    }
    try {
      await navigator.clipboard.writeText(item.link);
      message.success("链接已复制。");
    } catch {
      message.error("复制失败，请手动复制。");
    }
  }

  function createFolder() {
    const name = folderName.trim();
    if (!name) {
      message.warning("请填写文件夹名。");
      return;
    }
    if (name.length > 20) {
      message.warning("文件夹名最多 20 个字。");
      return;
    }
    void runWrite(async () => {
      await createPictureSpaceFolder(accountId!, name);
      setFolderOpen(false);
      setFolderName("");
    }, `文件夹「${name}」已创建。`);
  }

  /** 上传本地图片（三步写链路：申请凭证 → PUT → 登记）。 */
  function uploadFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    void runWrite(async () => {
      for (const file of list) {
        await uploadPictureSpaceFile(accountId!, file);
      }
    }, `已上传 ${list.length} 个文件。`);
  }

  /** 打开扩容弹窗（拉条件：是否支持扩容申请）。 */
  function openResize() {
    if (!accountId) return;
    setResizeOpen(true);
    setResizeCheck(null);
    setResizeLoading(true);
    fetchPictureSpaceResizeConditions(accountId)
      .then((res) => setResizeCheck(res))
      .catch(() => setResizeCheck({ conditions: [], can_apply: false, white_seller: false, remaining: 0 }))
      .finally(() => setResizeLoading(false));
  }

  /** 申请扩容（成功后重取配额，与平台一致）。 */
  function applyResize() {
    void runWrite(async () => {
      await resizePictureSpace(accountId!);
      setResizeOpen(false);
      await load();
    }, "扩容成功。");
  }

  /** 移动到目录（**先弹窗选目标目录**，再执行；平台侧可逆，不需要两层确认）。 */
  function moveItems(target: PictureSpaceItem[]) {
    if (target.length === 0) {
      message.warning("请先勾选要移动的文件。");
      return;
    }
    setMoveTarget("0");
    setMoveOpen(true);
  }

  /** 确认移动（弹窗「确定」调用）。 */
  function confirmMove() {
    const target = selectedItems;
    if (target.length === 0) {
      setMoveOpen(false);
      return;
    }
    void runWrite(async () => {
      await movePictureSpaceItems(accountId!, target, moveTarget || "0");
      setMoveOpen(false);
    }, `已移动 ${target.length} 个文件。`);
  }

  /** **从回收站还原**（低风险可逆，不需要两层确认）。 */
  function restoreItems(target: PictureSpaceItem[]) {
    if (target.length === 0) {
      message.warning("请先勾选要恢复的图片。");
      return;
    }
    void runWrite(
      () => restorePictureSpaceItems(accountId!, target),
      `已恢复 ${target.length} 个图片。`
    );
  }

  /** **彻底删除**（回收站，**不可还原** ⇒ 两层确认）。 */
  function permanentDeleteItems(target: PictureSpaceItem[]) {
    if (target.length === 0) {
      message.warning("请先勾选要彻底删除的图片。");
      return;
    }
    void (async () => {
      setBusy(true);
      try {
        const preview = await previewPictureSpaceDelete(accountId!, target, "permanent-delete");
        modal.confirm({
          title: `确认彻底删除选中的 ${preview.count} 个图片？`,
          content: (
            <div>
              <div style={{ marginBottom: 8, color: "#cf1322" }}>
                彻底删除后无法恢复（平台回收站也不保留）。
              </div>
              <div style={{ fontSize: 12, color: p.textTertiary }}>
                {preview.sample_names.join("、")}
                {preview.count > preview.sample_names.length ? ` 等 ${preview.count} 个图片` : ""}
              </div>
            </div>
          ),
          okText: "彻底删除",
          okButtonProps: { danger: true },
          cancelText: "取消",
          onOk: async () => {
            try {
              await permanentDeletePictureSpaceItems(accountId!, target, preview.confirm_token);
              message.success(`已彻底删除 ${preview.count} 个图片。`);
              await new Promise((resolve) => setTimeout(resolve, WRITE_SETTLE_MS));
              await load();
            } catch (err) {
              const detail = (err as { response?: { data?: { detail?: { message?: string } } } })
                ?.response?.data?.detail;
              message.error(detail?.message || "彻底删除失败，请稍后重试。");
            }
          },
        });
      } catch {
        message.error("预览失败，请稍后重试。");
      } finally {
        setBusy(false);
      }
    })();
  }

  function downloadItems(target: PictureSpaceItem[]) {
    if (target.length === 0) {
      message.warning("请先勾选要下载的文件。");
      return;
    }
    void runWrite(async () => {
      const res = await downloadPictureSpaceItems(accountId!, target);
      if (res.task_id) {
        message.info("下载任务已提交，可在平台下载中心查看。");
      }
    }, `已提交 ${target.length} 个文件的下载任务。`);
  }

  /**
   * 删除（**两层确认**，与商品批量删除同一把尺子）。
   *
   * 先调 `preview` 拿 `confirm_token` 与「将删除哪些」，弹确认框列文件名；
   * 用户点「确定」才带 token 发真删请求。平台侧是**进回收站**（可还原）。
   */
  function deleteItems(target: PictureSpaceItem[]) {
    if (target.length === 0) {
      message.warning("请先勾选要删除的文件。");
      return;
    }
    void (async () => {
      setBusy(true);
      try {
        const preview = await previewPictureSpaceDelete(accountId!, target);
        modal.confirm({
          title: `确认删除选中的 ${preview.count} 个文件？`,
          content: (
            <div>
              <div style={{ marginBottom: 8 }}>删除后可在「回收站」还原。</div>
              <div style={{ fontSize: 12, color: p.textTertiary }}>
                {preview.sample_names.join("、")}
                {preview.count > preview.sample_names.length ? ` 等 ${preview.count} 个文件` : ""}
              </div>
            </div>
          ),
          okText: "删除",
          okButtonProps: { danger: true },
          cancelText: "取消",
          onOk: async () => {
            try {
              await deletePictureSpaceItems(accountId!, target, preview.confirm_token);
              message.success(`已删除 ${preview.count} 个文件（可在回收站还原）。`);
              // 同 `runWrite`：平台列表有秒级缓存，留一拍再刷新。
              await new Promise((resolve) => setTimeout(resolve, WRITE_SETTLE_MS));
              await load();
            } catch (err) {
              const detail = (err as { response?: { data?: { detail?: { message?: string } } } })
                ?.response?.data?.detail;
              message.error(detail?.message || "删除失败，请稍后重试。");
            }
          },
        });
      } catch {
        message.error("删除预览失败，请稍后重试。");
      } finally {
        setBusy(false);
      }
    })();
  }

  const usedRatio = useMemo(() => {
    if (!usage?.used_space || !usage?.total_space) return null;
    return Math.min(100, Math.round((usage.used_space / usage.total_space) * 100));
  }, [usage]);

  /** 行内「更多」下拉项（主视图：下载 / 删除；回收站视图：恢复 / 彻底删除）。 */
  function rowMoreMenu(item: PictureSpaceItem) {
    if (scene === "recycle") {
      return {
        items: [
          { key: "restore", icon: <UndoOutlined />, label: "恢复" },
          { key: "permadelete", icon: <DeleteOutlined />, label: "彻底删除", danger: true },
        ],
        onClick: ({ key }: { key: string }) => {
          if (key === "restore") restoreItems([item]);
          if (key === "permadelete") permanentDeleteItems([item]);
        },
      };
    }
    return {
      items: [
        { key: "download", icon: <DownloadOutlined />, label: "下载" },
        { key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true },
      ],
      onClick: ({ key }: { key: string }) => {
        if (key === "download") downloadItems([item]);
        if (key === "delete") deleteItems([item]);
      },
    };
  }

  const columns: ColumnsType<PictureSpaceItem> = [
    {
      title: "图片",
      key: "thumb",
      width: 84,
      render: (_: unknown, record) =>
        record.type === 2 && record.link ? (
          <img
            src={record.link}
            alt={record.display_name}
            loading="lazy"
            style={{ width: 48, height: 48, objectFit: "cover", borderRadius: 4, border: `1px solid ${p.border}` }}
          />
        ) : (
          <FileImageOutlined style={{ fontSize: 22, color: p.textTertiary }} />
        ),
    },
    {
      title: "名称",
      dataIndex: "display_name",
      key: "name",
      // ⚠️ 固定 **200px**（用户 2026-10-07 口径「名称列宽度固定200px，使用tip展示完整名称」）。
      // 超长 UUID 截断成 `…`，**完整名走 Tooltip**（`ellipsis` 的 title 与自定义 render
      // 不共存，故在 render 里显式包 Tooltip —— 与商品列表「商品信息」列的兜法同源）。
      width: 200,
      ellipsis: true,
      render: (text: string) => (
        <Tooltip title={text || "-"}>
          <Text ellipsis style={{ fontSize: 12 }}>
            {text || "-"}
          </Text>
        </Tooltip>
      ),
    },
    { title: "大小", key: "size", width: 90, render: (_: unknown, r) => formatSize(r.size) },
    { title: "尺寸", key: "dims", width: 110, render: (_: unknown, r) => formatDimensions(r.width, r.height) },
    // 主视图「上传时间」/ 回收站「删除时间」（真机回收站列头就是「删除时间」）。
    scene === "recycle"
      ? { title: "删除时间", key: "time", width: 150, render: (_: unknown, r) => formatUploaded(r.update_time) }
      : { title: "上传时间", key: "time", width: 150, render: (_: unknown, r) => formatUploaded(r.create_time) },
    {
      title: "是否引用",
      key: "referenced",
      width: 100,
      render: (_: unknown, r) =>
        r.referenced ? <Tag color="blue">已引用</Tag> : <Text type="secondary">未引用</Text>,
    },
    {
      title: "操作",
      key: "ops",
      // ⚠️ **三个按钮一行展示**（用户 2026-10-07「操作列的三个按钮一行展示」，取代上一版
      // 「一行两个」）+ **钉最右**（`fixed:"right"`）：
      // 名称列固定 200 后全表宽 1006 > 容器（1366 视口 ~872）⇒ 表**内部**横向滚动，
      // 操作列钉右后常驻可见（不钉右会被滚出视口 ⇒ 用户抱怨的「操作列超出页面」）。
      fixed: "right",
      width: scene === "recycle" ? 190 : 240,
      render: (_: unknown, r) =>
        scene === "recycle" ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, alignItems: "center" }}>
            <Button size="small" type="link" icon={<UndoOutlined />} onClick={() => restoreItems([r])}>
              恢复
            </Button>
            <Button
              size="small"
              type="link"
              danger
              icon={<DeleteOutlined />}
              onClick={() => permanentDeleteItems([r])}
            >
              彻底删除
            </Button>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, alignItems: "center" }}>
            <Button size="small" type="link" icon={<CopyOutlined />} onClick={() => void copyLink(r)}>
              复制链接
            </Button>
            <Button size="small" type="link" icon={<FolderOpenOutlined />} onClick={() => moveItems([r])}>
              移动到
            </Button>
            <Dropdown menu={rowMoreMenu(r)} trigger={["click"]}>
              <Button size="small" type="link" icon={<MoreOutlined />}>
                更多
              </Button>
            </Dropdown>
          </div>
        ),
    },
  ];

  const emptyState = (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        accounts.length === 0
          ? "还没有千帆账号。先在「账号矩阵」导入千帆登录态。"
          : appliedKeyword
            ? `没有匹配「${appliedKeyword}」的图片。`
            : "这个图片空间里还没有图片。"
      }
    />
  );

  return (
    // ⚠️ 页面根必须**显式收口宽度**（2026-10-07 用户「图片列表超出页面 / 回收站未展示入口」）：
    // 根是 flex（左栏 220px + 右区 flex:1），而右区里表格 `scroll.x=1050` 的最小宽会
    // **把整个 flex 容器撑得比视口宽** ⇒ Content(`overflow:auto`) 出现**页面级横向滚动**，
    // 左栏（含「回收站」入口）被顶出视口左侧 = 用户声称的「回收站入口不见了」。
    // 收口三件套：① 根 `width:100%` + `maxWidth:100%`；② `overflowX:hidden`（横向只在表内滚，
    // 不出现在页面上）；③ 右区已 `minWidth:0`（flex 子项默认 `min-width:auto` 才是真凶）。
    <div
      style={{
        display: "flex",
        gap: 15,
        alignItems: "flex-start",
        width: "100%",
        maxWidth: "100%",
        overflowX: "hidden",
      }}
    >
      {/* ── 左栏：搜索 + 我的图片 + 配额（一比一对照千帆页面） ── */}
      <div style={{ width: 220, flexShrink: 0 }}>
        <Card size="small" styles={{ body: { padding: 12 } }} style={{ background: p.cardBg, borderColor: p.border }}>
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: p.textTertiary }} />}
            placeholder="输入关键词进行搜索"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onPressEnter={() => {
              setAppliedKeyword(keyword.trim());
              setPage(1);
            }}
          />
          <div style={{ marginTop: 12 }}>
            <div
              onClick={() => {
                setScene("main");
                setPage(1);
              }}
              style={{
                padding: "6px 8px",
                borderRadius: 4,
                cursor: "pointer",
                background: scene === "main" ? p.fillWeak : "transparent",
                color: p.text,
                fontSize: 13,
              }}
            >
              <FileImageOutlined style={{ marginRight: 8 }} />
              我的图片
            </div>
            {/* 回收站入口（真机 `/pictureSpace/recycle`）：切视图，不跳路由。 */}
            <div
              onClick={() => {
                setScene("recycle");
                setPage(1);
              }}
              style={{
                padding: "6px 8px",
                borderRadius: 4,
                marginTop: 4,
                cursor: "pointer",
                background: scene === "recycle" ? p.fillWeak : "transparent",
                color: p.text,
                fontSize: 13,
              }}
            >
              <DeleteOutlined style={{ marginRight: 8 }} />
              回收站
            </div>
          </div>
        </Card>

        {/* 配额条：取不到 ⇒ **不渲染**（不显示 `0 / 0`）。 */}
        {usage && usage.total_space ? (
          <Card size="small" styles={{ body: { padding: 12 } }} style={{ marginTop: 12, background: p.cardBg, borderColor: p.border }}>
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              {/* 用量 + 「空间扩容 >」**同一行**（一比一对照千帆：额度右侧是扩容入口）。
                  白名单卖家（`white_seller`）平台不给扩容入口 ⇒ 不渲染。
                  ⚠️ 用户 2026-10-07「149.92M /15.00G 一行展示扩容申请入口」：左栏窄，
                  不加 nowrap 会折行 ⇒ 用量 `nowrap` + 按钮 `flexShrink:0` 保住一行。 */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  flexWrap: "nowrap",
                  gap: 4,
                }}
              >
                <Text style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {formatSize(usage.used_space ?? 0)} <Text type="secondary">/{formatSize(usage.total_space)}</Text>
                </Text>
                {!usage.white_seller ? (
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0, height: "auto", fontWeight: 600, flexShrink: 0, whiteSpace: "nowrap" }}
                    onClick={openResize}
                  >
                    空间扩容 &gt;
                  </Button>
                ) : null}
              </div>
              <div style={{ height: 6, borderRadius: 3, background: p.borderSoft, overflow: "hidden" }}>
                <div style={{ width: `${usedRatio ?? 0}%`, height: "100%", background: "#1668dc" }} />
              </div>
            </Space>
          </Card>
        ) : null}
      </div>

      {/* ── 右区：工具栏 + 数据（**只有数据区滚动**，见下） ── */}
      {/*
        ⚠️ 用户 2026-10-07「顶部和底部固定不动，只有数据行可滚动；卡片列与列表一致」：
        右区做成**限定高度的纵向 flex** ——
        ① 工具栏卡（顶部）`flexShrink:0` 固定；
        ② 数据区（表格体 / 卡片墙）`flex:1; minHeight:0; overflow:auto` 自己滚；
        ③ 分页（底部，**两视图共用同一个**）`flexShrink:0` 固定。
        高度 = 视口 - 顶栏48 - 上下内边距(15+24) ≈ `calc(100vh - 90px)`。
      */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          height: "calc(100vh - 90px)",
        }}
      >
        <Card
          style={{ flexShrink: 0, background: p.cardBg, borderColor: p.border }}
          styles={{ body: { padding: 12 } }}
        >
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", justifyContent: "space-between" }}>
            <Space size={8} wrap>
              <Select
                size="small"
                value={accountId}
                style={{ minWidth: 170 }}
                placeholder="选择小红书账号"
                onChange={(value) => {
                  setAccountId(value);
                  setPage(1);
                }}
                // 无限下拉：滚动到底自动加载下一页（账号多时不卡）。
                onPopupScroll={(e) => {
                  const el = e.target as HTMLElement;
                  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 8) {
                    void loadMoreAccounts();
                  }
                }}
                options={accounts.map((a) => ({ value: a.id, label: a.nickname || `账号 #${a.id}` }))}
              />
              {/* 操作按钮（用户口径「操作按钮功能全部要做」）。回收站视图只留恢复/彻底删。 */}
              {scene === "main" ? (
                <>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/*"
                    multiple
                    style={{ display: "none" }}
                    onChange={(e) => {
                      uploadFiles(e.target.files);
                      e.target.value = "";
                    }}
                  />
                  <Button size="small" icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()}>
                    上传本地图片
                  </Button>
                  <Button size="small" icon={<FolderAddOutlined />} onClick={() => setFolderOpen(true)}>
                    新建文件夹
                  </Button>
                  <Dropdown
                    trigger={["click"]}
                    menu={{
                      items: [
                        { key: "move", label: "批量移动到", disabled: selectedIds.length === 0 },
                        { key: "download", label: "批量下载", disabled: selectedIds.length === 0 },
                        { key: "delete", label: "批量删除", danger: true, disabled: selectedIds.length === 0 },
                      ],
                      onClick: ({ key }) => {
                        if (key === "move") moveItems(selectedItems);
                        if (key === "download") downloadItems(selectedItems);
                        if (key === "delete") deleteItems(selectedItems);
                      },
                    }}
                  >
                    <Button size="small" icon={<MoreOutlined />}>
                      更多操作{selectedIds.length > 0 ? `（已选中 ${selectedIds.length} 条）` : ""}
                    </Button>
                  </Dropdown>
                </>
              ) : (
                <>
                  <Button
                    size="small"
                    icon={<UndoOutlined />}
                    disabled={selectedIds.length === 0}
                    onClick={() => restoreItems(selectedItems)}
                  >
                    批量恢复{selectedIds.length > 0 ? `（${selectedIds.length}）` : ""}
                  </Button>
                  <Button
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={selectedIds.length === 0}
                    onClick={() => permanentDeleteItems(selectedItems)}
                  >
                    批量彻底删除{selectedIds.length > 0 ? `（${selectedIds.length}）` : ""}
                  </Button>
                </>
              )}
              {scene === "main" ? (
                <Select
                  size="small"
                  value={orderValue}
                  style={{ minWidth: 150 }}
                  onChange={(value) => {
                    setOrderValue(value);
                    setPage(1);
                  }}
                  options={ORDER_FIELDS.flatMap((f) => [
                    { value: `${f.value}_asc`, label: `按${f.label}升序` },
                    { value: `${f.value}_desc`, label: `按${f.label}降序` },
                  ])}
                />
              ) : null}
              <Input
                size="small"
                allowClear
                style={{ width: 170 }}
                prefix={<SearchOutlined style={{ color: p.textTertiary }} />}
                placeholder="搜索文件名"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                onPressEnter={() => {
                  setAppliedKeyword(keyword.trim());
                  setPage(1);
                }}
              />
              <Button
                size="small"
                icon={<SearchOutlined />}
                onClick={() => {
                  setAppliedKeyword(keyword.trim());
                  setPage(1);
                }}
              >
                搜索
              </Button>
            </Space>
            <Space size={8}>
              {/* 选择操作（用户 2026-10-07）：全选 / 取消全选 / 反选 / 全选未引用。
                  **图标 + Tooltip**（用户口径「全部使用图标替代文本，文本使用tip展示」）。
                  作用域 = 当前页 `items`（跨页全选需另加服务端选择，不在本轮）。 */}
              <Tooltip title="全选（当前页）">
                <Button
                  size="small"
                  icon={<CheckSquareOutlined />}
                  onClick={() => setSelectedIds(items.map((it) => it.id))}
                  disabled={items.length === 0}
                />
              </Tooltip>
              <Tooltip title="取消全选">
                <Button
                  size="small"
                  icon={<BorderOutlined />}
                  onClick={() => setSelectedIds([])}
                  disabled={selectedIds.length === 0}
                />
              </Tooltip>
              <Tooltip title="反选">
                <Button
                  size="small"
                  icon={<SwapOutlined />}
                  onClick={() =>
                    setSelectedIds((current) => {
                      const set = new Set(current);
                      return items.filter((it) => !set.has(it.id)).map((it) => it.id);
                    })
                  }
                  disabled={items.length === 0}
                />
              </Tooltip>
              {/* 全选**未引用**的图（用户 2026-10-07「新增全选未被引用的图」）——
                  只选 `referenced === false` 的当前页条目。 */}
              <Tooltip title="全选未引用的图">
                <Button
                  size="small"
                  icon={<FileImageOutlined />}
                  onClick={() => setSelectedIds(items.filter((it) => !it.referenced).map((it) => it.id))}
                  disabled={items.length === 0}
                />
              </Tooltip>
              <Segmented
                size="small"
                value={view}
                onChange={(value) => setView(value as "list" | "grid")}
                options={[
                  { value: "list", icon: <UnorderedListOutlined /> },
                  { value: "grid", icon: <AppstoreOutlined /> },
                ]}
              />
              <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} loading={isLoading}>
                刷新
              </Button>
            </Space>
          </div>
        </Card>

        {error && (
          <Text type="danger" style={{ display: "block", margin: "12px 0" }}>
            {error}
          </Text>
        )}

        {/* 数据区：**只有这里滚动**（工具栏卡与下方分页固定不动）。
            列表 / 卡片两视图共用同一份 `items` 与同一个分页（卡片只是展示样式变化）。 */}
        <Spin
          spinning={isLoading || busy}
          className="ps-scroll-host"
          wrapperClassName="ps-scroll-host"
        >
          <div className="ps-scroll-hidden" style={{ flex: "1 1 auto", minHeight: 0, overflow: "auto", paddingTop: 12 }}>
            {view === "grid" ? (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                {items.length === 0 && !isLoading ? (
                  emptyState
                ) : (
                  items.map((item) => (
                    <Card
                      key={item.id}
                      size="small"
                      hoverable
                      // 点卡片 = 选中/取消（用户 2026-10-07「点击卡片即为选中或取消选中」）。
                      onClick={() => toggleSelect(item.id)}
                      styles={{ body: { padding: 8 } }}
                      style={{
                        width: 168,
                        cursor: "pointer",
                        background: p.cardBg,
                        // 选中态描边（与列表勾选同一份 `selectedIds`）。
                        borderColor: selectedIds.includes(item.id) ? "#1668dc" : p.border,
                      }}
                    >
                      {/* 卡片复选框（用户 2026-10-07「卡片页支持复选框」）。
                          与列表勾选**共用** `selectedIds` —— 切视图选择不丢。 */}
                      <div style={{ position: "relative" }}>
                        <Checkbox
                          checked={selectedIds.includes(item.id)}
                          onChange={() => toggleSelect(item.id)}
                          // 拦掉冒泡，避免「点复选框」被卡片 onClick 再切一次（抵消）。
                          onClick={(e) => e.stopPropagation()}
                          style={{ position: "absolute", top: 4, left: 4, zIndex: 2 }}
                        />
                        {/* 右上角：是否被引用（用户 2026-10-07「卡片右上角展示是否被引用」）。 */}
                        <span style={{ position: "absolute", top: 4, right: 4, zIndex: 2 }}>
                          {item.referenced ? (
                            <Tag color="blue" style={{ marginInlineEnd: 0 }}>已引用</Tag>
                          ) : (
                            <Tag style={{ marginInlineEnd: 0 }}>未引用</Tag>
                          )}
                        </span>
                        {item.type === 2 && item.link ? (
                          <img
                            src={item.link}
                            alt={item.display_name}
                            loading="lazy"
                            style={{ width: "100%", height: 120, objectFit: "cover", borderRadius: 4 }}
                          />
                        ) : (
                          <div style={{ height: 120, display: "flex", alignItems: "center", justifyContent: "center" }}>
                            <FileImageOutlined style={{ fontSize: 28, color: p.textTertiary }} />
                          </div>
                        )}
                      </div>
                      <Tooltip title={item.display_name}>
                        <Text ellipsis style={{ display: "block", fontSize: 11, marginTop: 6 }}>
                          {item.display_name || "-"}
                        </Text>
                      </Tooltip>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {formatSize(item.size)} · {formatDimensions(item.width, item.height)}
                      </Text>
                    </Card>
                  ))
                )}
              </div>
            ) : (
              <Table<PictureSpaceItem>
                rowKey="id"
                size="small"
                // ⚠️ `scroll.x` 必须有 —— 「操作」列设了 `fixed:"right"`（钉最右），
                // antd 的固定列**依赖 scroll 容器**，没有 scroll.x 固定不生效。
                // 名称列固定 200 ⇒ 全表 1006 > 容器（1366 视口 ~872）⇒ 表**内部**横向滚动，
                // 中间列可滚、操作列常驻可见。取 1006 = 各列宽之和（32+84+200+90+110+150+100+240）。
                scroll={{ x: 1006 }}
                columns={columns}
                dataSource={items}
                loading={isLoading}
                locale={{ emptyText: emptyState }}
                // 固定表头：外层数据区滚动时表头不动（`sticky` 配 `scroll` 容器）。
                sticky
                rowSelection={{
                  selectedRowKeys: selectedIds,
                  onChange: (keys) => setSelectedIds(keys.map(String)),
                }}
                // ⚠️ 表格**自带分页关掉** —— 分页统一由下方**固定分页条**渲染
                // （用户 2026-10-07「卡片列与列表一致」：两视图必须共用同一个分页）。
                pagination={false}
              />
            )}
          </div>
        </Spin>
        <WaitMask open={isLoading || busy} title="正在加载图片空间" />

        {/* ── 底部区：说明 + **公共分页**（两视图共用）**同一行**，整体**固定不滚动**。 ── */}
        <div
          style={{
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            paddingTop: 8,
            marginTop: 6,
            borderTop: `1px solid ${p.borderSoft}`,
          }}
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            {scene === "recycle" ? (
              <>回收站仅保存 7 天内删除的图片，超时系统自动清除；回收站内文件不占存储空间。</>
            ) : (
              <>
                本页数据来自<Text strong>千帆云</Text>，本地不落库；删除的文件可在平台「回收站」还原。
              </>
            )}
          </Text>
          <Pagination
            size="small"
            current={page}
            pageSize={pageSize}
            total={total}
            showSizeChanger
            pageSizeOptions={PAGE_SIZES.map(String)}
            showTotal={(n) => `共 ${n} 条`}
            showQuickJumper
            onChange={(nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            }}
          />
        </div>
      </div>

      {/* 新建文件夹弹窗（平台输入框 maxlength=20）。 */}
      <Modal
        open={folderOpen}
        title="新建文件夹"
        okText="确定"
        cancelText="取消"
        centered
        confirmLoading={busy}
        onOk={createFolder}
        onCancel={() => {
          setFolderOpen(false);
          setFolderName("");
        }}
        destroyOnClose
      >
        <Input
          value={folderName}
          maxLength={20}
          placeholder="请输入文件夹名"
          onChange={(e) => setFolderName(e.target.value)}
          onPressEnter={createFolder}
          suffix={<Text type="secondary" style={{ fontSize: 12 }}>{folderName.length}/20</Text>}
        />
      </Modal>

      {/* 移动弹窗：选目标目录（`0` = 根）。 */}
      <Modal
        open={moveOpen}
        title={`移动 ${selectedIds.length} 个文件`}
        okText="确定"
        cancelText="取消"
        centered
        confirmLoading={busy}
        onOk={confirmMove}
        onCancel={() => setMoveOpen(false)}
      >
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            目标目录：「我的图片」为根目录；后续需求可在此选择子文件夹。
          </Text>
          <Select
            style={{ width: "100%" }}
            value={moveTarget}
            onChange={setMoveTarget}
            options={[{ value: "0", label: "我的图片（根目录）" }]}
          />
        </Space>
      </Modal>

      {/* 图片空间扩容弹窗（2026-10-07 用户「弹窗展示是否支持扩容申请」+「与千帆保持一致」）。
          ⚠️ 1:1 对照真机弹窗（`d-modal check-resize-storage-modal`）结构：
          标题「图片空间扩容」→ 小节标题「扩容条件」→ 逐条 banner（通过=绿、未通过=红，
          `desc` 为平台原文）→ 页脚左侧状态文案「尚未满足条件，无法扩容，剩余扩容次数：N次」
          / 右侧「取消」「提交」（全通过才可提交）。 */}
      <Modal
        open={resizeOpen}
        title="图片空间扩容"
        centered
        width={520}
        onCancel={() => setResizeOpen(false)}
        footer={
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {resizeCheck
                ? resizeCheck.can_apply
                  ? `已满足条件，可提交扩容申请，剩余扩容次数：${resizeCheck.remaining}次`
                  : `尚未满足条件，无法扩容，剩余扩容次数：${resizeCheck.remaining}次`
                : "正在查询扩容条件…"}
            </Text>
            <Space size={8}>
              <Button onClick={() => setResizeOpen(false)}>取消</Button>
              <Button
                type="primary"
                loading={busy}
                disabled={resizeLoading || !resizeCheck?.can_apply}
                onClick={applyResize}
              >
                提交
              </Button>
            </Space>
          </div>
        }
      >
        <Spin spinning={resizeLoading}>
          <Space direction="vertical" size={8} style={{ width: "100%", minHeight: 60 }}>
            <Text strong>扩容条件</Text>
            {resizeCheck && resizeCheck.conditions.length > 0 ? (
              resizeCheck.conditions.map((c) => (
                <Alert
                  key={`${c.type}-${c.desc}`}
                  type={c.pass ? "success" : "error"}
                  showIcon
                  message={c.desc || "扩容条件"}
                />
              ))
            ) : (
              <Alert
                type="info"
                showIcon
                message={resizeLoading ? "正在查询扩容条件…" : "暂未查询到扩容条件"}
              />
            )}
          </Space>
        </Spin>
<WaitMask open={resizeLoading} title="正在处理图片" />
      </Modal>
    </div>
  );
}
