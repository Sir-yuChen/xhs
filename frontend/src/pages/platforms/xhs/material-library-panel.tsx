/**
 * 素材库面板（T9 / 设计 17 §7.3、§13.1）。
 *
 * 挂进笔记工坊的第 4 个页签（`?tab=materials`）。三块：
 *   ① 文件夹列表（新建 / 重命名 / 重扫 / 删除）—— 删除**只删索引行，不物理删磁盘文件**
 *      （只读边界：用户的素材目录不是我们的存储）；
 *   ② 文件网格（缩略图 + 搜索 + 按类型筛选 + 分页）；
 *   ③ 选中态回传给父级（供「生成配置弹窗」的 `media_source=material` 勾选用）。
 *
 * ⚠️ 三条呈现纪律（与全站一致）：
 *   - **缺值是缺值**：`last_scan_at` 为 null 渲染「未扫描」（不是空串、不是「刚刚」），
 *     视频无缩略图渲染占位块（设计即如此，不是缺陷）；
 *   - **扫描是后台线程**：点「重扫」后轮询 `last_scan_at` 变化，不做前端乐观刷新；
 *   - **消息统一 antd message**（`App.useApp()`），不用 inline Alert 占版面。
 */
import {
  App,
  Button,
  Card,
  Empty,
  Image,
  Input,
  Modal,
  Pagination,
  Popconfirm,
  Segmented,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  FolderAddOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createMaterialFolder,
  deleteMaterialFolder,
  fetchMaterialFiles,
  fetchMaterialFolders,
  materialFileUrl,
  scanMaterialFolder,
  updateMaterialFolder,
} from "../../../lib/api";
import type { MaterialFile, MaterialFolder } from "../../../types";

const { Text } = Typography;

/** 扫描轮询节奏（5s，与全站队列轮询同常量口径）。 */
const SCAN_POLL_INTERVAL_MS = 5000;
/** 扫描最多等这么多轮就停（避免无限轮询；超时给一行说明，不假装还在扫）。 */
const SCAN_MAX_ROUNDS = 24;
const FILE_PAGE_SIZE = 24;

type TypeFilter = "all" | "image" | "video";

function formatSize(bytes: number): string {
  if (!bytes || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function MaterialLibraryPanel({
  onSelectionChange,
}: {
  /** 勾选变化回传给宿主（生成配置弹窗的素材来源用它）。 */
  onSelectionChange?: (fileIds: number[]) => void;
}) {
  const { message } = App.useApp();
  const [folders, setFolders] = useState<MaterialFolder[]>([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [activeFolderId, setActiveFolderId] = useState<number | null>(null);

  const [files, setFiles] = useState<MaterialFile[]>([]);
  const [filesTotal, setFilesTotal] = useState(0);
  const [filesLoading, setFilesLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newPath, setNewPath] = useState("");
  const [renaming, setRenaming] = useState<MaterialFolder | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // 扫描轮询：记住「点重扫那一刻」的 last_scan_at，变了即停（不靠猜时长）。
  const scanBaselineRef = useRef<Record<number, string | null>>({});
  const scanRoundsRef = useRef(0);
  const scanPollRef = useRef<number | null>(null);

  const loadFolders = useCallback(async (): Promise<MaterialFolder[]> => {
    const list = await fetchMaterialFolders();
    setFolders(list);
    // 首次加载自动选中第一个文件夹（不为空却显示空网格）
    setActiveFolderId((current) => {
      if (current !== null && list.some((f) => f.id === current)) return current;
      return list.length ? list[0].id : null;
    });
    return list;
  }, []);

  const loadFiles = useCallback(
    async (folderId: number | null, nextPage = page) => {
      setFilesLoading(true);
      try {
        const result = await fetchMaterialFiles({
          ...(folderId ? { folderId } : {}),
          ...(keyword.trim() ? { keyword: keyword.trim() } : {}),
          ...(typeFilter !== "all" ? { fileType: typeFilter } : {}),
          page: nextPage,
          pageSize: FILE_PAGE_SIZE,
        });
        setFiles(result.items);
        setFilesTotal(result.total);
      } catch (error) {
        setFiles([]);
        setFilesTotal(0);
        // 取不到 ⇒ 一行安静说明（不是红块：这是列表查询的正常失败面）
        message.error(error instanceof Error ? error.message : "素材文件加载失败");
      } finally {
        setFilesLoading(false);
      }
    },
    [keyword, typeFilter, page, message]
  );

  useEffect(() => {
    void (async () => {
      setFoldersLoading(true);
      try {
        await loadFolders();
      } catch (error) {
        message.error(error instanceof Error ? error.message : "素材文件夹加载失败");
      } finally {
        setFoldersLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadFiles(activeFolderId, page);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeFolderId, page, typeFilter]);

  // 组件卸载时收掉扫描轮询（不留后台定时器）。
  useEffect(
    () => () => {
      if (scanPollRef.current !== null) window.clearInterval(scanPollRef.current);
    },
    []
  );

  useEffect(() => {
    onSelectionChange?.(selectedIds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds]);

  const activeFolder = useMemo(
    () => folders.find((f) => f.id === activeFolderId) || null,
    [folders, activeFolderId]
  );

  /** 点重扫：记基线 → 请求 → 轮询该文件夹 `last_scan_at` 变化（或轮次用尽）。 */
  async function handleScan(folder: MaterialFolder) {
    try {
      scanBaselineRef.current[folder.id] = folder.last_scan_at;
      scanRoundsRef.current = 0;
      const result = await scanMaterialFolder(folder.id);
      message.success(result.first_scan ? "已开始首次扫描（后台执行）" : "已开始重新扫描");
      if (scanPollRef.current !== null) window.clearInterval(scanPollRef.current);
      scanPollRef.current = window.setInterval(async () => {
        scanRoundsRef.current += 1;
        try {
          const list = await fetchMaterialFolders();
          setFolders(list);
          const target = list.find((f) => f.id === folder.id);
          const changed = target && target.last_scan_at !== scanBaselineRef.current[folder.id];
          if (changed || scanRoundsRef.current >= SCAN_MAX_ROUNDS) {
            if (scanPollRef.current !== null) window.clearInterval(scanPollRef.current);
            scanPollRef.current = null;
            if (changed) {
              message.success(`扫描完成：${target.file_count} 个文件`);
              void loadFiles(activeFolderId, page);
            } else {
              // 超时如实说，不假装还在扫（用户知道要手动刷新）
              message.warning("扫描还在进行，可稍后点「刷新」查看结果");
            }
          }
        } catch {
          // 轮询失败不打断（下一轮再试），但轮次照记 —— 不会无限轮
        }
      }, SCAN_POLL_INTERVAL_MS);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "触发扫描失败");
    }
  }

  async function handleCreate() {
    const name = newName.trim();
    const path = newPath.trim();
    if (!path) {
      message.info("请填写素材目录的完整路径");
      return;
    }
    try {
      const folder = await createMaterialFolder({ name: name || path, path });
      message.success("文件夹已添加，正在后台扫描");
      setCreating(false);
      setNewName("");
      setNewPath("");
      await loadFolders();
      setActiveFolderId(folder.id);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "新建素材文件夹失败");
    }
  }

  async function handleRename() {
    if (!renaming) return;
    const name = renameValue.trim();
    if (!name) {
      message.info("名称不能为空");
      return;
    }
    try {
      await updateMaterialFolder(renaming.id, { name });
      message.success("已重命名");
      setRenaming(null);
      await loadFolders();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重命名失败");
    }
  }

  async function handleDelete(folder: MaterialFolder) {
    try {
      const result = await deleteMaterialFolder(folder.id);
      message.success(`已移除索引（${result.removed_files} 个文件记录），磁盘文件未删除`);
      if (activeFolderId === folder.id) setActiveFolderId(null);
      await loadFolders();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  }

  function toggleSelect(fileId: number) {
    setSelectedIds((current) =>
      current.includes(fileId) ? current.filter((id) => id !== fileId) : [...current, fileId]
    );
  }

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Card
        size="small"
        title="素材文件夹"
        extra={
          <Space size={8}>
            <Tag color="blue">已选 {selectedIds.length} 个文件</Tag>
            <Button
              icon={<ReloadOutlined />}
              size="small"
              onClick={() => {
                void loadFolders().then(() => loadFiles(activeFolderId, page));
              }}
            >
              刷新
            </Button>
            <Button
              type="primary"
              size="small"
              icon={<FolderAddOutlined />}
              onClick={() => setCreating(true)}
            >
              新建文件夹
            </Button>
          </Space>
        }
      >
        {foldersLoading ? (
          <Spin />
        ) : folders.length === 0 ? (
          <Empty description="还没有素材文件夹。点「新建文件夹」选一个本机放图片 / 视频的目录（只索引，不搬移、不删除原文件）" />
        ) : (
          <Space size={8} wrap>
            {folders.map((folder) => {
              const active = folder.id === activeFolderId;
              return (
                <Card
                  key={folder.id}
                  size="small"
                  hoverable
                  style={{
                    width: 260,
                    borderColor: active ? "#1677ff" : undefined,
                    cursor: "pointer",
                  }}
                  onClick={() => {
                    setActiveFolderId(folder.id);
                    setPage(1);
                  }}
                >
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Space size={6} style={{ width: "100%", justifyContent: "space-between" }}>
                      <Text strong ellipsis style={{ maxWidth: 150 }}>
                        {folder.name}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {folder.file_count} 个文件
                      </Text>
                    </Space>
                    <Tooltip title={folder.path}>
                      <Text type="secondary" ellipsis style={{ fontSize: 11, maxWidth: 220, display: "block" }}>
                        {folder.path}
                      </Text>
                    </Tooltip>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {folder.last_scan_at ? `上次扫描 ${folder.last_scan_at}` : "未扫描"}
                    </Text>
                    <Space size={2} onClick={(e) => e.stopPropagation()}>
                      <Button
                        type="link"
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={() => void handleScan(folder)}
                      >
                        重扫
                      </Button>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => {
                          setRenaming(folder);
                          setRenameValue(folder.name);
                        }}
                      >
                        重命名
                      </Button>
                      <Popconfirm
                        title="移除这个素材文件夹？"
                        description="只移除索引记录，磁盘上的原目录与文件不会被删除。"
                        okText="移除"
                        cancelText="取消"
                        onConfirm={() => void handleDelete(folder)}
                      >
                        <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                          移除
                        </Button>
                      </Popconfirm>
                    </Space>
                  </Space>
                </Card>
              );
            })}
          </Space>
        )}
      </Card>

      <Card
        size="small"
        title={`素材文件${activeFolder ? ` · ${activeFolder.name}` : ""}`}
        extra={
          <Space size={8}>
            <Segmented
              size="small"
              value={typeFilter}
              onChange={(value) => {
                setTypeFilter(value as TypeFilter);
                setPage(1);
              }}
              options={[
                { label: "全部", value: "all" },
                { label: "图片", value: "image" },
                { label: "视频", value: "video" },
              ]}
            />
            <Input
              size="small"
              allowClear
              placeholder="搜索文件名"
              prefix={<SearchOutlined />}
              style={{ width: 200 }}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onPressEnter={() => {
                setPage(1);
                void loadFiles(activeFolderId, 1);
              }}
            />
            <Button
              size="small"
              onClick={() => {
                setPage(1);
                void loadFiles(activeFolderId, 1);
              }}
            >
              搜索
            </Button>
          </Space>
        }
      >
        {filesLoading ? (
          <Spin />
        ) : files.length === 0 ? (
          <Empty
            description={
              keyword.trim() || typeFilter !== "all"
                ? "当前条件下没有素材文件"
                : activeFolder
                  ? "这个文件夹里还没有索引到文件（点「重扫」再试，或确认目录里确实有图片 / 视频）"
                  : "先选择或新建一个素材文件夹"
            }
          />
        ) : (
          <>
            <Space size={10} wrap>
              {files.map((file) => {
                const selected = selectedIds.includes(file.id);
                const isImage = file.file_type === "image";
                return (
                  <Card
                    key={file.id}
                    size="small"
                    hoverable
                    onClick={() => toggleSelect(file.id)}
                    style={{
                      width: 168,
                      borderColor: selected ? "#1677ff" : undefined,
                      cursor: "pointer",
                    }}
                    cover={
                      <div
                        style={{
                          height: 120,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          background: "rgba(0,0,0,0.03)",
                          overflow: "hidden",
                        }}
                      >
                        {isImage && file.thumb_url ? (
                          <Image
                            src={materialFileUrl(file.id)}
                            height={120}
                            style={{ objectFit: "cover" }}
                            preview={{ mask: "预览" }}
                          />
                        ) : isImage ? (
                          // 图片索引里但缩略图 URL 为空 ⇒ 用原图端点兜底
                          <Image
                            src={materialFileUrl(file.id)}
                            height={120}
                            style={{ objectFit: "cover" }}
                            preview={{ mask: "预览" }}
                          />
                        ) : (
                          // 视频无缩略图是**设计如此**（后端不为视频抽帧）
                          <Space direction="vertical" size={2} style={{ alignItems: "center" }}>
                            <PlayCircleOutlined style={{ fontSize: 28, opacity: 0.45 }} />
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              视频
                            </Text>
                          </Space>
                        )}
                      </div>
                    }
                  >
                    <Space direction="vertical" size={0} style={{ width: "100%" }}>
                      <Tooltip title={file.filename}>
                        <Text ellipsis style={{ fontSize: 12, maxWidth: 140, display: "block" }}>
                          {file.filename}
                        </Text>
                      </Tooltip>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {formatSize(file.size_bytes)}
                        {file.width && file.height ? ` · ${file.width}×${file.height}` : ""}
                      </Text>
                    </Space>
                  </Card>
                );
              })}
            </Space>
            <div style={{ marginTop: 12, textAlign: "right" }}>
              <Pagination
                size="small"
                current={page}
                pageSize={FILE_PAGE_SIZE}
                total={filesTotal}
                showSizeChanger={false}
                onChange={(next) => setPage(next)}
              />
            </div>
          </>
        )}
      </Card>

      <Modal
                centered
                open={creating}
        title="新建素材文件夹"
        onCancel={() => setCreating(false)}
        onOk={() => void handleCreate()}
        okText="添加"
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            填本机上一个放图片 / 视频的目录。只做索引（不复制、不移动、不删除原文件）。
          </Text>
          <Input
            placeholder="完整目录路径，例如 D:\\素材\\夏季新品"
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
          />
          <Input
            placeholder="名称（留空则用目录路径）"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
        </Space>
      </Modal>

      <Modal
                centered
                open={renaming !== null}
        title="重命名素材文件夹"
        onCancel={() => setRenaming(null)}
        onOk={() => void handleRename()}
        okText="保存"
      >
        <Input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          placeholder="文件夹名称"
        />
      </Modal>
    </Space>
  );
}
