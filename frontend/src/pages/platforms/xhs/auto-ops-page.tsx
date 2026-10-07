/**
 * 自动运营宿主页（设计 22 §3，2026-10-06）—— 「自动运营」与「自动化工作流」合并入口。
 *
 * ## 为什么合并
 *
 * 两者是**同一个心智**下的两层能力（用户口径「自动化运行和自动化工作流合并」）：
 *
 * - 「自动运营」= **单点动作**（采集 → AI 改写 → 生成草稿）；
 * - 「自动化工作流」= **多节点编排链路**（选题 → 生成 → 审核 → 发布 → 监控）。
 *
 * ## ⚠️ 只合前端口子，**不合并后端**
 *
 * 两套数据面完全不同（`auto_tasks` 表 vs `workflows` / `workflow_runs` / `workflow_nodes`），
 * 合并成一个端点会把「一个入口两套 UI」变成「一个端点两套分支」（DEF-044 反面）。
 * 所以本页只做**挂载**：两个 panel 各自独立成文件，宿主只加 Tab。
 *
 * tab 状态进 URL query（`?tab=ops|workflows`）：可分享、刷新保持；
 * 旧路由 `/workflows` 由 router 重定向到 `?tab=workflows`（深链不死）。
 */
import { PartitionOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { Tabs } from "antd";
import { useSearchParams } from "react-router-dom";

import { AutoOpsPanel } from "./auto-ops-panel";
import { WorkflowPanel } from "./workflow-page";

const AUTO_OPS_TABS = ["ops", "workflows"] as const;
type AutoOpsTab = (typeof AUTO_OPS_TABS)[number];

function normalizeTab(value: string | null): AutoOpsTab {
  const key = (value ?? "").trim();
  return (AUTO_OPS_TABS as readonly string[]).includes(key) ? (key as AutoOpsTab) : "ops";
}

export function AutoOpsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = normalizeTab(searchParams.get("tab"));

  return (
    <Tabs
      activeKey={activeTab}
      onChange={(key) => {
        const next = new URLSearchParams(searchParams);
        next.set("tab", key);
        setSearchParams(next, { replace: true });
      }}
      items={[
        {
          key: "ops",
          label: (
            <span>
              <ThunderboltOutlined />
              自动运营
            </span>
          ),
          children: <AutoOpsPanel />,
        },
        {
          key: "workflows",
          label: (
            <span>
              <PartitionOutlined />
              自动化工作流
            </span>
          ),
          children: <WorkflowPanel />,
        },
      ]}
    />
  );
}
