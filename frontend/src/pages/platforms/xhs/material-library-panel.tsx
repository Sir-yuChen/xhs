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
  Checkbox,
  Empty,
  Image,
  Input,
  Modal,
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
  LeftOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createMaterialFolder,
  deleteMaterialFile,
  deleteMaterialFilesBatch,
  deleteMaterialFolder,
  fetchMaterialFiles,
  fetchMaterialFolders,
  materialFileUrl,
  scanMaterialFolder,
  updateMaterialFolder,
  materialThumbUrl,
  fetchMaterialImageObjectUrl,
  uploadMaterialFile,
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

/**
 * 素材缩略图（v2.29.2）—— **必须走 axios 取 blob**，不能用 `<img src="/api/...">`。
 *
 * 真机根因：后端只认 `Authorization: Bearer`（**没有 cookie 鉴权**），
 * 而 `<img src>` 发不出自定义请求头 ⇒ 那个请求必然 401 ⇒ 破图。
 * 所以这里用 `fetchMaterialImageObjectUrl`（axios 拦截器自动带 token）
 * 拿 `blob:` URL 给 `<img>`。
 *
 * ⚠️ **必须 revoke**：组件卸载 / url 变化时释放，否则列表翻页会累积 blob 内存。
 */
function MaterialThumb({
  url,
  fallbackId,
  previewUrl,
  title,
  onOpenPreview,
}: {
  url: string;
  fallbackId: number;
  /** v2.29.5：**大图预览地址**（原图端点）—— 用户口径「点击应支持大图预览」。 */
  previewUrl?: string;
  /** 预览弹窗标题（文件名）。 */
  title?: string;
  /**
   * v2.31：点图时**通知父级开预览**（而不是自己开弹窗）。
   *
   * ⚠️ 为什么预览上提到父级：用户要「左右切换」⇒ 弹窗必须知道**同列表的其他图**，
   * 而本组件只拿到自己这一张（自己开弹窗就切不了 = 上一版的做法）。
   */
  onOpenPreview?: () => void;
}) {
  const [objectUrl, setObjectUrl] = useState<string>("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let revoked = false;
    let created = "";
    setFailed(false);
    if (!url) {
      // 没有缩略图 URL（老索引行 / 视频）⇒ 回落到原图端点
      setObjectUrl("");
      return;
    }
    void (async () => {
      try {
        const objectUrlValue = await fetchMaterialImageObjectUrl(url);
        if (revoked) {
          window.URL.revokeObjectURL(objectUrlValue);
          return;
        }
        created = objectUrlValue;
        setObjectUrl(objectUrlValue);
      } catch {
        setFailed(true);
      }
    })();
    return () => {
      revoked = true;
      if (created) window.URL.revokeObjectURL(created);
    };
  }, [url, fallbackId]);

  if (failed || !objectUrl) {
    // 加载中 / 取不到 ⇒ 占位（**不留破图** —— 破图比空位更让人以为坏了）
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "rgba(0,0,0,0.03)",
        }}
      >
        <Text type="secondary" style={{ fontSize: 11 }}>
          {failed ? "缩略图取不到" : "加载中…"}
        </Text>
      </div>
    );
  }
  return (
    <>
      <img
        src={objectUrl}
        alt=""
        onClick={(event) => {
          // ⚠️ 必须 stopPropagation：卡片的 onClick 是「选中」——
          // 不拦住的话点图会同时选中 + 开预览（用户点「看大图」不该改变勾选）。
          event.stopPropagation();
          onOpenPreview?.();
        }}
        style={{
          width: "100%",
          height: "100%",
          // v2.29.6：**`contain` 而不是 `cover`**（用户口径「只缩放不裁切」）。
          // `cover` 会为填满正方形格子而**裁掉图片边缘**（竖图裁上下、横图裁左右）——
          // 后端已经改成等比缩放了，前端再裁一次就是**双重裁切**，
          // 且商品主体常在边缘（被切掉）⇒ 完整显示优先，留白可接受。
          objectFit: "contain",
          display: "block",
          cursor: previewUrl ? "zoom-in" : "default",
        }}
      />
    </>
  );
}


export function MaterialLibraryPanel({
  onSelectionChange,
}: {
  /** 勾选变化回传给宿主（生成配置弹窗的素材来源用它）。 */
  onSelectionChange?: (fileIds: number[]) => void;
}) {
  const { message, modal } = App.useApp();
  const [folders, setFolders] = useState<MaterialFolder[]>([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [activeFolderId, setActiveFolderId] = useState<number | null>(null);

  const [files, setFiles] = useState<MaterialFile[]>([]);
  const [filesTotal, setFilesTotal] = useState(0);
  const [filesLoading, setFilesLoading] = useState(false);
  const [page, setPage] = useState(1);
  /** v2.29.3：还有没有下一页（判据来自后端 `total`；无限滚动的刹车）。 */
  const [hasMore, setHasMore] = useState(false);
  /** 哨兵元素的引用（滚进视口就加载下一页）。 */
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  /** v2.31：上传用的隐藏 `<input type=file>`（点按钮触发它 — 原生文件选择器只能这么开）。 */
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const [keyword, setKeyword] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [creating, setCreating] = useState(false);
  /**
   * v2.31：大图预览 —— **按索引**记（不是记文件对象）。
   *
   * ⚠️ 必须按**索引**：用户要「左右切换」⇒ 要靠 `files[index ± 1]` 取相邻图；
   * 记 file 对象就得每次回列表 `findIndex`（列表还在无限滚动增长，容易对不上）。
   */
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);
  //: 预览大图的 objectUrl（**懒加载**：只有开了预览才去取原图）
  const [previewObjectUrl, setPreviewObjectUrl] = useState<string>("");
  const [previewLoading, setPreviewLoading] = useState(false);
  //: v2.31：上传中（按钮 loading，防重复点）
  const [uploading, setUploading] = useState(false);
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

  /**
   * 拉素材文件（v2.29.3：**无限滚动** —— 用户口径「素材文件不要使用分页，
   * 使用无限滚动下拉」）。
   *
   * `append=false` = 换筛选/换文件夹（**重置**列表）；`append=true` = 滚到底（**追加**）。
   * ⚠️ 追加时**不能覆盖** `files` —— 覆盖会让滚到底变成「内容整段跳掉」（用户会以为丢了）。
   * ⚠️ 用 `nextPage`（而不是 state `page`）发请求：滚到底连续触发时 state 还没更新，
   *    读 state 会**重复请求同一页**（数据重复 + 无限滚动卡住）。
   */
  const loadFiles = useCallback(
    async (folderId: number | null, nextPage = 1, append = false) => {
      setFilesLoading(true);
      try {
        const result = await fetchMaterialFiles({
          ...(folderId ? { folderId } : {}),
          ...(keyword.trim() ? { keyword: keyword.trim() } : {}),
          ...(typeFilter !== "all" ? { fileType: typeFilter } : {}),
          page: nextPage,
          pageSize: FILE_PAGE_SIZE,
        });
        setFiles((current) => (append ? [...current, ...result.items] : result.items));
        setFilesTotal(result.total);
        setPage(nextPage);
        //: 还能不能再拉（**判据来自后端 total**，不猜）
        setHasMore(nextPage * FILE_PAGE_SIZE < result.total);
      } catch (error) {
        if (!append) {
          setFiles([]);
          setFilesTotal(0);
        }
        // 取不到 ⇒ 一行安静说明（不是红块：这是列表查询的正常失败面）
        message.error(error instanceof Error ? error.message : "素材文件加载失败");
        setHasMore(false);
      } finally {
        setFilesLoading(false);
      }
    },
    [keyword, typeFilter, message]
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
    // 换文件夹 / 换筛选 ⇒ **重置**（page=1、覆盖）
    void loadFiles(activeFolderId, 1, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeFolderId, typeFilter]);
  //: 关键词变化也重置（原先靠 `page` 变化触发；无限滚动下页是滚出来的，不能靠它）
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadFiles(activeFolderId, 1, false);
    }, 250); // 输入防抖（每敲一个字就请求一次太吵）
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword]);

  /**
   * v2.29.3：**滚到底自动加载**（无限滚动）—— `IntersectionObserver` 观察哨兵元素。
   *
   * ⚠️ 为什么不用 `onScroll` 算 `scrollTop + clientHeight >= scrollHeight`：
   * 那种写法要在滚动事件里读布局（强制同步回流），且**容易在连续滚动时重复触发**；
   * IntersectionObserver 由浏览器在合成层判定，天然去重、且离屏时不跑。
   *
   * 三重刹车（缺一个就会「滚到底疯狂请求」）：`hasMore` + `filesLoading` + 哨兵存在。
   */
  useEffect(() => {
    const node = loadMoreRef.current;
    if (!node || !hasMore || filesLoading) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          void loadFiles(activeFolderId, page + 1, true);
        }
      },
      { rootMargin: "120px" } // 提前 120px 预加载（滚到底时数据已就位，不闪）
    );
    observer.observe(node);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasMore, filesLoading, page, activeFolderId]);

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

  /**
   * v2.31：打开大图预览（按索引，支持左右切换）。
   *
   * ⚠️ 懒加载：只有开了预览才去取**原图**（列表里几十张 6.5 MB 图全预取会卡死 ——
   * 这正是缩略图存在的理由）。切到下一张时**释放上一张**的 objectUrl。
   */
  useEffect(() => {
    if (previewIndex === null) {
      setPreviewObjectUrl("");
      return;
    }
    const file = files[previewIndex];
    if (!file) {
      setPreviewIndex(null);
      return;
    }
    let revoked = false;
    let created = "";
    setPreviewLoading(true);
    void (async () => {
      try {
        const next = await fetchMaterialImageObjectUrl(materialFileUrl(file.id));
        if (revoked) {
          window.URL.revokeObjectURL(next);
          return;
        }
        created = next;
        setPreviewObjectUrl(next);
      } catch {
        message.error("大图加载失败");
        setPreviewObjectUrl("");
      } finally {
        if (!revoked) setPreviewLoading(false);
      }
    })();
    return () => {
      revoked = true;
      if (created) window.URL.revokeObjectURL(created);
    };
  }, [previewIndex, files, message]);

  /**
   * v2.31：删除一个文件（**真删源文件，但进回收站**）—— 必须二次确认。
   *
   * ⚠️ 确认文案要**写清三件事**（用户口径「添加提示二次校验」）：
   *   ① 删的是哪个文件（文件名）② 会动磁盘 ③ **可捞回**（进回收站，不是销毁）。
   *   只写「确定删除吗？」用户无法判断风险 —— 那是走过场，不是二次确认。
   */
  function handleDeleteFile(file: MaterialFile) {
    modal.confirm({
      title: "删除素材文件",
      content: (
        <div>
          <div>
            将删除：
            <Text strong>{file.filename}</Text>
          </div>
          <div style={{ marginTop: 4 }}>
            <Text type="secondary">
              会真实删除磁盘文件（移入回收站，可在系统回收站还原）。
            </Text>
          </div>
        </div>
      ),
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      centered: true,
      onOk: async () => {
        try {
          await deleteMaterialFile(file.id);
          message.success(`已删除「${file.filename}」（已移入回收站）`);
          setSelectedIds((current) => current.filter((id) => id !== file.id));
          await loadFiles(activeFolderId, 1, false);
        } catch (error) {
          message.error(error instanceof Error ? error.message : "删除失败");
        }
      },
    });
  }

  /**
   * v2.31：批量删除（**上限 50**，后端超限整体拒绝）。
   *
   * ⚠️ 确认文案列**前 5 个文件名 + 总数**：只报「共 N 个」用户无法核对勾对没有，
   * 而批量删除一次动几十个磁盘文件，勾错的代价很大。
   */
  function handleDeleteSelected() {
    if (selectedIds.length === 0) return;
    const names = files.filter((f) => selectedIds.includes(f.id)).map((f) => f.filename);
    const shown = names.slice(0, 5);
    modal.confirm({
      title: `批量删除 ${selectedIds.length} 个素材文件`,
      content: (
        <div>
          <div>将删除以下文件：</div>
          <ul style={{ margin: "6px 0 6px 18px", padding: 0 }}>
            {shown.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
          {names.length > shown.length && (
            <div>
              <Text type="secondary">…等共 {names.length} 个</Text>
            </div>
          )}
          <div style={{ marginTop: 4 }}>
            <Text type="secondary">
              会真实删除磁盘文件（移入回收站，可在系统回收站还原）。一次最多 50 个。
            </Text>
          </div>
        </div>
      ),
      okText: "全部删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      centered: true,
      onOk: async () => {
        try {
          const result = await deleteMaterialFilesBatch(selectedIds);
          const skipped = result.skipped.length;
          message.success(
            skipped > 0
              ? `已删除 ${result.deleted} 个（${skipped} 个已不存在，自动跳过）`
              : `已删除 ${result.deleted} 个（已移入回收站）`
          );
          setSelectedIds([]);
          await loadFiles(activeFolderId, 1, false);
        } catch (error) {
          message.error(error instanceof Error ? error.message : "批量删除失败");
        }
      },
    });
  }

  /** v2.31：上传文件到当前文件夹（`<input type=file>` 选中后触发）。 */
  async function handleUpload(fileList: FileList | null) {
    if (!fileList || fileList.length === 0 || activeFolderId === null) return;
    setUploading(true);
    try {
      for (const file of Array.from(fileList)) {
        // 逐个上传：单个失败**不拖累**其余（如实报哪一个失败）
        try {
          await uploadMaterialFile(activeFolderId, file);
        } catch (error) {
          message.error(
            `「${file.name}」上传失败：${error instanceof Error ? error.message : "未知原因"}`
          );
        }
      }
      message.success("上传完成");
      await loadFiles(activeFolderId, 1, false);
    } finally {
      setUploading(false);
    }
  }

  /** 预览弹窗里切到相邻一张（左右方向键 / 按钮共用）。 */
  function stepPreview(delta: number) {
    setPreviewIndex((current) => {
      if (current === null) return current;
      const next = current + delta;
      // 越界时不循环（用户按到头就该停，循环会让人以为「又回到第一张」是 bug）
      if (next < 0 || next >= files.length) return current;
      return next;
    });
  }

  /**
   * v2.31：预览开着时用**键盘左右键**切换。
   *
   * ⚠️ 必须判定 `previewIndex === null` 直接不挂监听 —— 否则在**列表页**按左右键
   * 也会触发切换（但那时没有预览，`setPreviewIndex` 是 no-op，看似无害），
   * 却会与将来任何列表级键盘交互**抢事件**。
   */
  useEffect(() => {
    if (previewIndex === null) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "ArrowLeft") stepPreview(-1);
      else if (event.key === "ArrowRight") stepPreview(1);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps —— `stepPreview` 每次渲染都是新引用，
    // 放进依赖会让监听每渲染都重挂（没必要；它只读 `files.length`，而 files 变了会走下面的依赖）
  }, [previewIndex, files.length]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Card
        size="small"
        title="素材文件夹"
        extra={
          <Space size={8}>
            <Tag color="blue">已选 {selectedIds.length} 个文件</Tag>
            {/* v2.31（用户口径「复选框勾选」）：加了复选框就必须给**全选 / 清空** ——
                逐个勾几十张是纯体力活，而用户要批量删除时正是「全选再删」这个动作。
                ⚠️ 只在**全选/部分选**两种语义间切换（部分选中时显示「全选」而不是「取消全选」，
                否则用户点一下反而全清空，与他的意图相反）。 */}
            <Button
              size="small"
              disabled={files.length === 0}
              onClick={() => {
                const allSelected = files.length > 0 && files.every((f) => selectedIds.includes(f.id));
                setSelectedIds(allSelected ? [] : files.map((f) => f.id));
              }}
            >
              {files.length > 0 && files.every((f) => selectedIds.includes(f.id))
                ? "取消全选"
                : "全选"}
            </Button>
            {/* v2.31：**上传**（用户口径「添加」）—— 只对**当前选中文件夹**可上传，
                没选文件夹时禁用（上传要有落点，不能猜一个默认目录）。 */}
            <Button
              size="small"
              icon={<UploadOutlined />}
              loading={uploading}
              disabled={activeFolderId === null}
              onClick={() => uploadInputRef.current?.click()}
            >
              上传
            </Button>
            {/* 隐藏的原生文件选择器：浏览器只允许由用户手势直接触发（所以点上面的按钮转调它） */}
            <input
              ref={uploadInputRef}
              type="file"
              accept="image/*,video/*"
              multiple
              style={{ display: "none" }}
              onChange={(event) => {
                void handleUpload(event.target.files);
                // 清空 value：否则连续选**同一个文件**不会触发 change（DOM 认为值没变）
                event.target.value = "";
              }}
            />
            {/* v2.31：**批量删除**（用户口径「删除，批量删除」）—— 没勾选时禁用，
                而不是隐藏（隐藏会让用户以为没这个功能）。 */}
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              disabled={selectedIds.length === 0}
              onClick={handleDeleteSelected}
            >
              批量删除
            </Button>
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
                    // v2.29.3：只改文件夹 —— 「重置到第一页」由 `activeFolderId`
                    // 的 `useEffect` 统一负责（这里再 setPage(1) 会**多发一次请求**）
                    setActiveFolderId(folder.id);
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
                // 同上：重置交给 `typeFilter` 的 useEffect
                setTypeFilter(value as TypeFilter);
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
              onPressEnter={() => void loadFiles(activeFolderId, 1, false)}
            />
            <Button
              size="small"
              onClick={() => void loadFiles(activeFolderId, 1, false)}
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
            {/* v2.29.5：**一行 6 张、宽度自适应**（用户口径 2026-10-03）。
                ⚠️ 用 CSS Grid 而不是 `Space wrap`：`Space` 是固定宽度卡片**靠左排**，
                最后一行不满时左边对齐、右边空 —— 而且卡片不会随容器变宽而变大。
                `repeat(6, 1fr)` 让 6 列**均分容器**（窗口变宽每张跟着变宽），
                `gap` 统一间距（不用 `Space` 的 margin 技巧）。 */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(6, minmax(0, 1fr))",
                gap: 10,
              }}
            >
              {files.map((file, index) => {
                const selected = selectedIds.includes(file.id);
                const isImage = file.file_type === "image";
                return (
                  <Card
                    key={file.id}
                    size="small"
                    hoverable
                    /* v2.31b（用户口径「复选框勾选」）：**卡片不再承担勾选** ——
                       勾选**只**由左上角复选框负责（单一职责）。
                       ⚠️ 这是刻意的**行为收紧**：卡片上同时挂着「点图看大图」，
                       再让整卡可点 ⇒ 想看图的人一动鼠标就把图选中了
                       （用户要显式复选框，正是为了消除这种误选）。
                       变异实测：保留卡片 onClick 时「复选框 onChange 改空函数」**照样绿**
                       —— 那种「假复选框」正是本轮要杜绝的形态。 */
                    style={{
                      // 宽度交给网格（`1fr` 均分，窗口变宽每张跟着变宽）——
                      // 不再固定 168px（那会让「一行 6 张」在窄屏溢出、宽屏留白）
                      width: "100%",
                      borderColor: selected ? "#1677ff" : undefined,
                      cursor: "pointer",
                    }}
                    cover={
                      // v2.29.4：**文字叠在图片上**（用户口径「缩略图小，把图片名称和属性
                      // 放图片上」）⇒ 容器 `position: relative`，文字条绝对定位在底部。
                      // ⚠️ 文字条底色是**半透明黑**（不是纯色块）：图片内容不变，
                      // 只是压一层可读背景 —— 纯色块会遮掉图的下半部分。
                      // ⚠️ 这段注释必须用 `//` 而**不能**用 `{/* */}`：它处在
                      // `cover={...}` 的**表达式位置**，`{...}` 会被解析成对象（真机实测报 TS1005）。
                      <div
                        style={{
                          position: "relative",
                          // v2.29.5：**正方形（1:1）**而不是固定 120px 高 ——
                          // 一行 6 张时每张宽度随窗口变，固定高度会让格子变扁
                          // （越宽越扁）。`aspect-ratio` 让高度**跟着宽度走**，
                          // 无论窗口多宽都是规整的方阵。
                          aspectRatio: "1 / 1",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          background: "rgba(0,0,0,0.03)",
                          overflow: "hidden",
                        }}
                      >
                        {isImage ? (
                          // ⚠️ v2.29.2：**必须走 `MaterialThumb`**（axios 取 blob）——
                          // `<img src="/api/...">` 带不了 Authorization 头，后端只认
                          // Bearer 没有 cookie ⇒ 那种写法必然 401 破图（真机根因）。
                          <MaterialThumb
                            url={file.thumb_url || materialFileUrl(file.id)}
                            fallbackId={file.id}
                            previewUrl={materialFileUrl(file.id)}
                            title={file.filename}
                            /* v2.31：点图**通知父级开预览**（按索引）—— 这样弹窗知道
                               完整列表，才能左右切换（预览上提的理由见 MaterialThumb 注释）。 */
                            onOpenPreview={() => setPreviewIndex(index)}
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
                        {/* 叠在图片底部的信息条（文件名 + 大小 + 尺寸） */}
                        <div
                          style={{
                            position: "absolute",
                            left: 0,
                            right: 0,
                            bottom: 0,
                            padding: "3px 6px",
                            background: "rgba(0,0,0,0.55)",
                            color: "#fff",
                            fontSize: 11,
                            lineHeight: "15px",
                          }}
                        >
                          <Tooltip title={file.filename}>
                            <div
                              style={{
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {file.filename}
                            </div>
                          </Tooltip>
                          <div style={{ opacity: 0.85 }}>
                            {formatSize(file.size_bytes)}
                            {file.width && file.height ? ` · ${file.width}×${file.height}` : ""}
                          </div>
                        </div>
                        {/* v2.31（用户口径「删除放图片右上角」）：删除图标**叠在图片右上角**。
                            ⚠️ 三条要点：
                            ① `stopPropagation` 必须加 —— 不加会连带触发卡片的「选中」；
                            ② 底色是**半透明黑圆底**而不是纯图标：图片内容不可控（浅色图上
                               红色图标会看不清），圆底保证任何图上都可见；
                            ③ **不用「悬停才出现」** —— 触摸屏没有 hover，且用户要的是
                               「一眼看到有删除」。 */}
                        <Tooltip title="删除（移入回收站，可从系统回收站还原）">
                          <span
                            onClick={(event) => {
                              event.stopPropagation();
                              handleDeleteFile(file);
                            }}
                            style={{
                              position: "absolute",
                              top: 4,
                              right: 4,
                              zIndex: 2,
                              width: 22,
                              height: 22,
                              borderRadius: 11,
                              background: "rgba(0,0,0,0.55)",
                              color: "#fff",
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              cursor: "pointer",
                            }}
                          >
                            <DeleteOutlined style={{ fontSize: 12 }} />
                          </span>
                        </Tooltip>
                        {/* v2.31（用户口径「复选框勾选」）：**左上角复选框** ——
                            上一版靠「点卡片即选中」（隐式），用户要求**显式勾选**：
                            隐式选中在「点图看大图」与「选中」共用一个手势时最容易误选。 */}
                        <span
                          onClick={(event) => event.stopPropagation()}
                          style={{
                            position: "absolute",
                            top: 4,
                            left: 4,
                            zIndex: 2,
                            background: "rgba(255,255,255,0.9)",
                            borderRadius: 4,
                            padding: "0 3px",
                            lineHeight: "16px",
                          }}
                        >
                          <Checkbox
                            checked={selected}
                            onChange={() => toggleSelect(file.id)}
                          />
                        </span>
                      </div>
                    }
                  >
                    {/* 卡片 body 留空 —— 文件名/属性已叠在图上（同一信息不留两份）。 */}
                    <div style={{ display: "none" }} />
                  </Card>
                );
              })}
            </div>
            <div style={{ marginTop: 12, textAlign: "right" }}>
              {/* v2.29.3：**无限滚动哨兵**（用户口径「不要使用分页，使用无限滚动下拉」）。
                  滚进视口（提前 120px）就追加下一页；`hasMore === false` 时
                  改为「已全部加载」——那种「到底了」的确认感是分页控件给不了的。 */}
              <div ref={loadMoreRef} style={{ textAlign: "center", padding: "12px 0" }}>
                {filesLoading && <Spin size="small" />}
                {!hasMore && files.length > 0 && (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    已全部加载（共 {filesTotal} 个）
                  </Text>
                )}
              </div>
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

      {/* v2.31：**大图预览 + 左右切换**（用户口径「大图预览，且支持左右切换」）。
          ⚠️ 三条设计决定：
          ① 预览的目标由 `previewIndex`（**索引**）决定 —— 切上一张/下一张才可能实现；
          ② 越界**不循环**（按到头就停 —— 循环会让人以为「又回到第一张」是 bug）；
          ③ 键盘左右键也切（用户看大图时手在键盘上，逼他移回鼠标点箭头是无谓的负担）。 */}
      <Modal
        open={previewIndex !== null}
        title={
          previewIndex !== null && files[previewIndex]
            ? `${files[previewIndex].filename}（${previewIndex + 1} / ${files.length}）`
            : ""
        }
        footer={null}
        width="85vw"
        centered
        onCancel={() => setPreviewIndex(null)}
        styles={{ body: { textAlign: "center", position: "relative" } }}
      >
        <div style={{ position: "relative", minHeight: "60vh" }}>
          {/* 左箭头：到头（第一张）时禁用，而不是隐藏 */}
          <Button
            shape="circle"
            size="large"
            icon={<LeftOutlined />}
            disabled={previewIndex === null || previewIndex <= 0}
            onClick={() => stepPreview(-1)}
            style={{ position: "absolute", left: 8, top: "50%", zIndex: 2 }}
          />
          {previewLoading && <Spin />}
          {!previewLoading && previewObjectUrl && (
            <img
              src={previewObjectUrl}
              alt=""
              style={{ maxWidth: "100%", maxHeight: "75vh", objectFit: "contain" }}
            />
          )}
          {!previewLoading && !previewObjectUrl && (
            <Text type="secondary">大图加载失败</Text>
          )}
          <Button
            shape="circle"
            size="large"
            icon={<RightOutlined />}
            disabled={previewIndex === null || previewIndex >= files.length - 1}
            onClick={() => stepPreview(1)}
            style={{ position: "absolute", right: 8, top: "50%", zIndex: 2 }}
          />
        </div>
      </Modal>
    </Space>
  );
}
