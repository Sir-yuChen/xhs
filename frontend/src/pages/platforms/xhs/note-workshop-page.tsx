/**
 * 笔记工坊（2026-09-25 §36，用户口径「将图片坊，视频坊，草稿坊，合并为一个页面
 * 视图 tab 自然隔离，新增菜单名称为笔记工坊」）。
 *
 * 一页三个页签（图片工坊 / 视频工坊 / 草稿工坊），各自的面板组件自带状态与
 * 数据加载 —— **tab 即隔离**（视图隔离，不是把三套状态塞进一个组件）。
 *
 * tab 状态进 URL query（`?tab=images|videos|drafts`）：可分享、刷新保持；
 * 旧路由（`/image-studio`、`/video-studio`、`/drafts`）由 router 重定向到
 * 对应 tab（深链与书签不死）。默认页签 = `images`（tab 视觉序第一项）。
 *
 * antd `Tabs` 默认保活（`destroyInactiveTabPane=false`）：页签首次激活后跨切换
 * 不丢面板内 state；未激活过的页签不渲染 ⇒ 不会三个工坊一起打接口。
 */
import {
  FileTextOutlined,
  FolderOpenOutlined,
  HighlightOutlined,
  PictureOutlined,
  ToolOutlined,
  VideoCameraOutlined,
} from "@ant-design/icons";
import { Tabs } from "antd";
import { useSearchParams } from "react-router-dom";

import { DraftsWorkshopPanel } from "./drafts-workshop-panel";
import { ImageStudioPanel } from "./image-studio-panel";
import { MaterialLibraryPanel } from "./material-library-panel";
import { PromptTemplatePanel } from "./prompt-template-panel";
import { VideoStudioPanel } from "./video-studio-panel";

/** tab key 与 URL query 取值（旧路由重定向按它落 tab；未知值回落 images）。 */
const NOTE_WORKSHOP_TABS = ["images", "videos", "drafts", "materials", "prompts"] as const;
type NoteWorkshopTab = (typeof NOTE_WORKSHOP_TABS)[number];

function normalizeTab(value: string | null): NoteWorkshopTab {
  const key = (value ?? "").trim();
  return (NOTE_WORKSHOP_TABS as readonly string[]).includes(key) ? (key as NoteWorkshopTab) : "images";
}

export function XhsNoteWorkshopPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = normalizeTab(searchParams.get("tab"));

  return (
    <div>
      {/* v2.29.2：**页头整块移除**（用户口径 2026-10-03：
          「XHS Note Workshop / 笔记工坊 / 图片生成、视频处理与草稿改写在一个页面内
           分区完成，互不干扰 —— 这些文本和标题全部移除，占用了太多空间」）。
          ⇒ 三行（英文小标题 / 中文大标题 / 灰说明）全删，页面直接从 Tabs 开始。
          ⚠️ 侧栏菜单项仍叫「笔记工坊」（那是导航标识，与页头不是一回事，
          删了用户就找不到入口了）；`PageHeader` 组件本身保留 ——
          其它页面还在用（别处删它会连带报错）。 */}
      <Tabs
        activeKey={activeTab}
        onChange={(key) => {
          const next = new URLSearchParams(searchParams);
          next.set("tab", key);
          setSearchParams(next, { replace: true });
        }}
        items={[
          {
            key: "images",
            label: (
              <span>
                <PictureOutlined />
                图片工坊
              </span>
            ),
            children: <ImageStudioPanel />,
          },
          {
            key: "videos",
            label: (
              <span>
                <VideoCameraOutlined />
                视频工坊
              </span>
            ),
            children: <VideoStudioPanel />,
          },
          {
            key: "drafts",
            label: (
              <span>
                <FileTextOutlined />
                草稿工坊
              </span>
            ),
            children: <DraftsWorkshopPanel />,
          },
          {
            key: "materials",
            label: (
              <span>
                <FolderOpenOutlined />
                素材库
              </span>
            ),
            children: <MaterialLibraryPanel />,
          },
          {
            key: "prompts",
            label: (
              <span>
                <HighlightOutlined />
                提示词模板
              </span>
            ),
            children: <PromptTemplatePanel />,
          },
        ]}
      />
    </div>
  );
}
