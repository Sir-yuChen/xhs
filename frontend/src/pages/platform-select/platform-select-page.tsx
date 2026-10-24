/*
 * 工作区选择页（设计 62 §2.2 —— **后端驱动**，取代前端写死卡片）。
 *
 * ## 这一版推翻了什么
 *
 * 之前卡片 = `fetchPlatforms()`（平台注册表）+ 一张**前端写死**的「大模型网关」卡。
 * 硬编码做不到两件事：
 *   ① 「用户存在什么菜单就展示什么，动态控制」—— 无权限的工作区照样亮卡片，
 *      点进去 403（「看得见摸不着」）；
 *   ② 「新增工作区要改代码发版」。
 *
 * 现在数据来自 `GET /api/system/menus/workspaces`（登录即可，无权限码）：
 * 后端按「顶级可见菜单 ∧ 用户在该片有授权」产出卡片清单，
 * `normal ∧ !enterable` 的卡片**后端照样下发但前端不渲染**（无任何授权 ⇒ 不出现）。
 *
 * ## 徽标规则（设计 62 §2.2）
 *
 *   `workspace ∈ {ai, system}` → 公共（不隶属任何平台的平台级能力）；
 *   `enterable=true`          → Active；
 *   `status="disabled"`       → Coming Soon（预留入口照旧，不可点）；
 *   `normal ∧ !enterable`     → 卡片不渲染。
 *
 * ## 图标 / 配色
 *
 * 复用页面现有的映射：平台卡片 = 首字 + accent 底色（与原 `PlatformSelector`
 * 一致），公共能力 = antd 图标 + 主题底色（大模型网关原为 ApiOutlined + 紫）。
 * 未知 workspace 走兜底（灰底 + 名称首字），不报错不缺卡。
 *
 * ## 点击落点（设计 62 §2.2）
 *
 *   xhs → /platforms/xhs；system → /platforms/system（AppShell + /system/users）；
 *   ai → /llm-gateway（独立公共模块）；其余 enterable → /platforms/<workspace>。
 *   不可进入的卡片**不可点**。
 */

import {
  ApiOutlined,
  ArrowRightOutlined,
  ClockCircleOutlined,
  LogoutOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Alert, Button, Card, Col, Row, Tag, Typography } from "antd";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useThemePalette } from "../../app/providers";
import { useAuth } from "../../hooks/use-auth";
import { fetchMyWorkspaces } from "../../lib/api";
import type { WorkspaceCard } from "../../lib/api";
import {
  PUBLIC_WORKSPACES,
  workspaceEntryPath,
} from "../../lib/workspaces";
import { WaitMask } from "../../components/ui/wait-mask";

const { Text, Title } = Typography;

/** 已知 workspace 的 antd 图标（公共能力用图标，平台用首字 —— 与旧页面一致）。 */
const WORKSPACE_ICONS: Record<string, ReactNode> = {
  ai: <ApiOutlined />,
  system: <SettingOutlined />,
};

/** 已知 workspace 的 accent 底色（取自原平台注册表 / 大模型网关卡片的配色）。 */
const WORKSPACE_COLORS: Record<string, string> = {
  xhs: "#ff2442",
  ai: "#722ed1",
  system: "#1668dc",
  douyin: "#111111",
  kuaishou: "#ff7a00",
  weibo: "#e6162d",
  xianyu: "#ffe100",
  taobao: "#ff5000",
};

/** 副标题英文名（缺值口径：未知的原样下发 key，不造名）。 */
const WORKSPACE_EN: Record<string, string> = {
  xhs: "XiaoHongShu",
  ai: "LLM Gateway",
  system: "System",
};

const FALLBACK_COLOR = "#8c8c8c";

/** 徽标；返回 null = 卡片不渲染（normal 但用户没有任何授权）。 */
export function workspaceBadgeOf(item: WorkspaceCard): {
  label: string;
  color: string;
} | null {
  if (PUBLIC_WORKSPACES.has(item.workspace)) {
    return { label: "公共", color: "purple" };
  }
  if (item.enterable) return { label: "Active", color: "blue" };
  if (item.status === "disabled") return { label: "Coming Soon", color: "default" };
  return null;
}

export function PlatformSelectPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const p = useThemePalette();
  // 失败**不静默**（T-17 / DEF-023 同纪律）：错误态必须说原因，不许用空列表伪装。
  const [workspaces, setWorkspaces] = useState<WorkspaceCard[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchMyWorkspaces()
      .then((items) => {
        if (cancelled) return;
        setWorkspaces(items);
        setIsLoading(false);
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setIsLoading(false);
        setLoadError(
          err instanceof Error
            ? `工作区清单加载失败：${err.message}`
            : "工作区清单加载失败 —— 后端服务可能未启动或不可达。"
        );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // normal ∧ !enterable ⇒ 不渲染（动态控制：无授权的工作区连卡片都没有）
  const visible = workspaces.filter((w) => workspaceBadgeOf(w) !== null);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: p.pageBg,
        padding: "48px 40px",
      }}
    >
      <div
        style={{
          maxWidth: 960,
          margin: "0 auto",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            marginBottom: 40,
          }}
        >
          <div>
            <Text
              type="secondary"
              style={{
                fontSize: 12,
                textTransform: "uppercase",
                letterSpacing: 1,
                display: "block",
                marginBottom: 4,
              }}
            >
              Choose Workspace
            </Text>
            <Title level={2} style={{ margin: "0 0 8px" }}>
              选择平台工作区
            </Title>
            <Text type="secondary">
              你有哪些工作区由权限决定，此处动态展示。
            </Text>
          </div>
          <Button
            icon={<LogoutOutlined />}
            onClick={() => void auth.logout()}
          >
            退出登录
          </Button>
        </div>

        {/* 加载 / 失败态必须显式：空卡片会被理解成「什么权限都没有」 */}
        {isLoading ? (
          <WaitMask open title="正在加载工作区清单" />
        ) : loadError ? (
          <Alert
            type="error"
            showIcon
            message="工作区清单加载失败"
            description={loadError}
            style={{ marginBottom: 24 }}
          />
        ) : null}

        {!isLoading && !loadError && (
          <Row gutter={[20, 20]}>
            {visible.map((workspace) => {
              const badge = workspaceBadgeOf(workspace)!;
              const enterable = workspace.enterable;
              const icon = WORKSPACE_ICONS[workspace.workspace] ?? null;
              const accent = WORKSPACE_COLORS[workspace.workspace] ?? FALLBACK_COLOR;

              return (
                <Col key={workspace.workspace} xs={24} sm={12} md={8} lg={8}>
                  <Card
                    hoverable={enterable}
                    style={{
                      borderColor: p.border,
                      cursor: enterable ? "pointer" : "not-allowed",
                    }}
                    styles={{ body: { padding: "20px 24px" } }}
                    onClick={() => {
                      if (enterable) navigate(workspaceEntryPath(workspace.workspace));
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                      <div
                        style={{
                          width: 44,
                          height: 44,
                          borderRadius: 12,
                          background: accent,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontWeight: 800,
                          fontSize: icon ? 20 : 18,
                          color: "#fff",
                          flexShrink: 0,
                        }}
                      >
                        {icon ?? (workspace.name || workspace.workspace).slice(0, 1)}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <Title level={5} style={{ margin: 0, fontSize: 16 }}>
                          {workspace.name || workspace.workspace}
                        </Title>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {WORKSPACE_EN[workspace.workspace] ?? workspace.workspace}
                        </Text>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <Tag color={badge.color} style={{ margin: 0, borderRadius: 4 }}>
                          {badge.label}
                        </Tag>
                        {enterable ? (
                          <ArrowRightOutlined style={{ color: p.textTertiary, fontSize: 14 }} />
                        ) : (
                          <ClockCircleOutlined style={{ color: p.textMuted, fontSize: 14 }} />
                        )}
                      </div>
                    </div>
                  </Card>
                </Col>
              );
            })}
          </Row>
        )}
      </div>
    </div>
  );
}
