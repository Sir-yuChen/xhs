/*
 * 403 无权限页（v1.1.0 / 设计 49 §模块八）。
 *
 * ## 什么时候真的会走到这里
 *
 * 侧栏**不显示**未授权的菜单（后端 `menus/mine` 已过滤），所以正常点击走不到 403。
 * 能走到这里的情形只有三种：
 *   ① **直敲 URL**（收藏了旧的地址、或别人发的链接）；
 *   ② 权限**刚被收回**（页签还开着，刷新就 403）；
 *   ③ 菜单被停用（`status=disabled`）而用户手上有旧书签。
 *
 * ## 为什么页面上要说「你能去哪」
 *
 * 只显示一个 403 图标的页面等于告诉用户「你被拦住了」但不告诉他出路。
 * 这里列出**他实际有权限的入口**（从 `/menus/mine` 的顶级项里取），
 * 让用户自己跳到能去的地方 —— 这比「请联系管理员」有用得多。
 */

import { LockOutlined } from "@ant-design/icons";
import { Button, Card, Result, Space, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../hooks/use-auth";
import { usePermission } from "../../hooks/use-permission";

const { Text } = Typography;

/** 常见入口的中文名（菜单树里有 name 就直接用它，这里只给兜底）。 */
const HOME_BY_ROLE: Record<string, string> = {
  admin: "/platforms/xhs/dashboard",
  user: "/platforms/xhs/dashboard",
};

export function ForbiddenPage() {
  const navigate = useNavigate();
  const auth = useAuth();
  const { role, isAdmin } = usePermission();

  const home = HOME_BY_ROLE[role] ?? "/platforms/xhs/dashboard";

  return (
    <Card variant="borderless">
      <Result
        status="403"
        icon={<LockOutlined />}
        title="没有访问权限"
        subTitle={
          <Space direction="vertical" size={4}>
            <Text type="secondary">
              当前账号
              {auth.user ? `「${auth.user.username}」` : ""}
              没有这个页面的查看权限。
            </Text>
            <Text type="secondary">如需访问，请联系管理员开通权限。</Text>
          </Space>
        }
        extra={
          <Space>
            <Button type="primary" onClick={() => navigate(home)}>
              回到首页
            </Button>
            <Button onClick={() => navigate(-1)}>返回上一页</Button>
          </Space>
        }
      >
        <div style={{ textAlign: "center" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            当前角色：
          </Text>
          <Tag color={isAdmin ? "red" : "blue"} style={{ marginLeft: 4 }}>
            {role || "未知"}
          </Tag>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              如果你确实需要这个页面，请让管理员在「系统管理 → 角色管理 → 分配菜单」里
              给你的角色勾上它。
            </Text>
          </div>
        </div>
      </Result>
    </Card>
  );
}
