import { Result } from "antd";
import { useParams } from "react-router-dom";

const sectionInfo: Record<string, { title: string; description: string }> = {
  accounts: {
    title: "账号矩阵",
    description: "管理 PC 与 Creator 账号、Cookie 状态、健康检查和账号作用域。",
  },
  discovery: {
    title: "笔记发现",
    description: "关键词搜索、URL 直达、账号笔记抓取和批量入库会在这里汇合。",
  },
  library: {
    title: "内容库",
    description: "视觉笔记卡、标签、筛选、批量导出和素材下载的统一资产库。",
  },
  // ⚠️ `analytics` / `monitoring` 条目已移除（2026-10-28：洞察两页与监控中心整体退场，
  // 该 path 下已无路由 ⇒ 兜底页永不可达）。
  publish: {
    title: "发布中心",
    description: "草稿、素材上传、立即发布、定时发布、失败重试和历史记录。",
  },
};

export function XhsSectionPage() {
  const { section = "discovery" } = useParams();
  const info = sectionInfo[section] ?? sectionInfo.discovery;

  return (
    <Result
      status="info"
      title={info.title}
      subTitle={info.description}
    />
  );
}
