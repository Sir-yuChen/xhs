/*
 * 系统管理工作区（v1.1.0 / 设计 46 §4 / 49 模块一~九）。
 *
 * ## 一个页面 + 5 个 Tab，不是 5 个独立路由
 *
 * 理由：这 5 件事是**同一个工作区**的五个方面（用户 / 角色 / 菜单 / 字典 / 日志），
 * 且互相引用（改完角色要去看用户、改完菜单要去看角色授权）。拆成 5 个路由
 * 会让「改完 A 要去 B」变成两次整页跳转，把上下文丢掉。
 *
 * 深链：`/system/users`、`/system/roles` … 各自直接落到对应 Tab，
 * 侧栏菜单项的 `path` 就是这些地址（种子里的 C 行），所以从侧栏点进来
 * 仍然是「一个页面、五个 Tab」的体验。
 *
 * ## 权限判据（页级）
 *
 * 页面本身要求「至少有一个 `system:*:view` 码」—— 由后端 `menus/mine` 决定
 * 侧栏是否显示它。直接输 URL 时，各 Tab 内**每个请求**都会 403
 * （后端 `require_perm`），页面上呈现为 Tab 内的错误提示。
 *
 * ⚠️ 这里**不做前端路由守卫**（不写 `<Navigate to="/403">`）：
 * 因为「能不能进这个页面」本质上由「有没有某个 view 码」决定，
 * 而那件事已经有后端兜底。前端再拦一次只会让「管理员临时收回权限」
 * 时出现两层错误提示（一层前端跳转 + 一层接口 403），反而更糊涂。
 *
 * ## Tab 的 label 不带权限徽标
 *
 * 用户口径：「不要出现统计数值徽标等冗余元素」。Tab 只写名字。
 */

import {
  AppstoreOutlined,
  BookOutlined,
  FileSearchOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Card, Tabs, Typography } from "antd";
import { useMemo } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { usePermission } from "../../hooks/use-permission";
import { SystemDictPanel } from "./system-dict-panel";
import { SystemLogsPanel } from "./system-logs-panel";
import { SystemMenusPanel } from "./system-menus-panel";
import { SystemRolesPanel } from "./system-roles-panel";
import { SystemUsersPanel } from "./system-users-panel";

const { Text } = Typography;

/**
 * Tab 定义。`path` 是**路由**（种子菜单里的 C 行就指向它），
 * `key` 是 Tab 标识 —— 两者一致，所以 `?tab=` 与 `/system/roles` 表达同一件事。
 */
const TABS = [
  { key: "users", path: "/system/users", label: "用户管理", icon: <TeamOutlined />, perm: "system:user:view" },
  { key: "roles", path: "/system/roles", label: "角色管理", icon: <SafetyCertificateOutlined />, perm: "system:role:view" },
  { key: "menus", path: "/system/menus", label: "菜单管理", icon: <AppstoreOutlined />, perm: "system:menu:view" },
  { key: "dicts", path: "/system/dicts", label: "字典管理", icon: <BookOutlined />, perm: "system:dict:view" },
  //: ⚠️ 「配置管理」在 v1.1.0 里**复用现有的设置页**（`/settings`，已有 9 个
  //: LIVE_KEYS 与「应用到所有用户」），不在系统管理里重做一套。
  //:
  //: **2026-10-24 梳理补齐**：这一栏原来缺失 —— 种子菜单里有 `/system/settings`
  //: 且权限码 `system:setting:view` 已登记，但本页 TABS 里没有对应项 ⇒
  //: 用户点侧栏「配置管理」会落到本页却**看不到任何对应页签**（只有其它 5 个），
  //: 而 `system:setting:view` 也成了**永远没有读者的死码**。
  //: `externalPath` 让该页签点击后跳到 `/settings`（不复制一套设置 UI）。
  {
    key: "settings",
    path: "/system/settings",
    label: "配置管理",
    icon: <SettingOutlined />,
    perm: "system:setting:view",
    externalPath: "/settings",
  },
  { key: "logs", path: "/system/logs", label: "日志查看", icon: <FileSearchOutlined />, perm: "system:log:view" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export function SystemPage() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { has, isReady } = usePermission();

  //: 只渲染**有权限**的 Tab（admin 全有）。未 ready 时先渲染全部、由各面板自己 403，
  //: 避免「首屏闪一下空白 Tab」—— 那会让人以为页面坏了。
  const visibleTabs = useMemo(
    () => TABS.filter((tab) => !isReady || has(tab.perm)),
    [has, isReady]
  );

  //: 当前 Tab 从**两个来源**判定（顺序即优先级）：
  //:   ① URL 路径（`/system/roles` —— 侧栏菜单点的就是这个）
  //:   ② `?tab=`（页签自己切换时写，也可直接分享）
  //: 两者表达同一件事；路径优先是因为它是「用户从侧栏点进来的那一项」。
  const pathHit = TABS.find((tab) => tab.path === location.pathname)?.key;
  const queryHit = params.get("tab") as TabKey | null;
  const active: TabKey =
    (pathHit as TabKey | undefined) ??
    (visibleTabs.find((t) => t.key === queryHit)?.key as TabKey | undefined) ??
    (visibleTabs[0]?.key as TabKey | undefined) ??
    "users";

  if (isReady && visibleTabs.length === 0) {
    return (
      <Card>
        <Text type="secondary">
          你没有系统管理域的任何查看权限。如需访问，请联系管理员分配对应角色。
        </Text>
      </Card>
    );
  }

  return (
    <Tabs
      activeKey={active}
      //: 切 Tab ⇒ **改路由**（不是只改 query）。这样刷新后停在同一栏，
      //: 且 URL 与侧栏菜单项一致（面包屑 / 收藏 / 分享都对）。
      onChange={(key) => {
        const target = TABS.find((tab) => tab.key === key);
        if (!target) return;
        //: `externalPath` 的页签（配置管理 ⇒ `/settings`）**整页跳出去**：
        //: 它是复用既有设置页，不是本页内的 panel —— 硬留在这里会渲染空内容。
        navigate(("externalPath" in target && target.externalPath) || target.path);
      }}
      items={visibleTabs.map((tab) => ({
        key: tab.key,
        label: (
          <span>
            {tab.icon}
            <span style={{ marginLeft: 6 }}>{tab.label}</span>
          </span>
        ),
        children: renderPanel(tab.key),
      }))}
    />
  );
}

function renderPanel(key: TabKey) {
  switch (key) {
    case "users":
      return <SystemUsersPanel />;
    case "roles":
      return <SystemRolesPanel />;
    case "menus":
      return <SystemMenusPanel />;
    case "dicts":
      return <SystemDictPanel />;
    case "logs":
      return <SystemLogsPanel />;
    case "settings":
      //: 「配置管理」复用 `/settings`（`externalPath` 已在 onChange 里整页跳出去）。
      //: 这里给一个兜底提示，覆盖「用户直接落在 /system/settings 而没有触发 onChange」
      //: 的情形（深链 / 刷新）—— 空内容会让人以为页面坏了。
      return (
        <Card>
          <Text type="secondary">
            配置管理复用「设置」页（每用户一份配置）。点击上方页签或访问 /settings 查看。
          </Text>
        </Card>
      );
    default:
      return null;
  }
}
