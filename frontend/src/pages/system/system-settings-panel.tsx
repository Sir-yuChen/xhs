/**
 * 系统管理 → 配置管理（`/system/settings`）：**只放系统级**配置。
 *
 * ## 为什么这里不放工作区私有配置（2026-10-25 用户口径）
 *
 * 用户原话：「私有配置，只在对应工作区域内展示，你梳理一下，不错误的展示在
 * 配置管理页面中了」。
 *
 * 判据：**归属哪一级，就在哪一级的页面出现**。
 * 系统管理是「管全站」的地方，把「只对小红书生效」的配置放进来，等于让人以为
 * 那是全站设置 —— 改完其实只影响小红书，而页面本身没有任何东西体现这一点。
 *
 * ⚠️ 这里曾有第二版实现：把工作区配置也做成卡片、标题标「只对小红书生效」。
 * 那个做法同样被用户否掉 —— **标注不等于归属正确**，私有配置该回它自己的工作区。
 *
 * 工作区私有配置随账号范围条出现（见 `components/layout/workspace-shell.tsx`
 * 里「配置」弹窗，只在平台工作区页面渲染）。本页只请求 `scope="system"`。
 *
 * ⚠️ 不在页面上写解释性长文案：用户口径「说明简短，不必要的说明全部移除」。
 */

import { Card } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";
import { usePermission } from "../../hooks/use-permission";

export function SystemSettingsPanel() {
  const { isAdmin } = usePermission();

  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          系统配置
        </span>
      }
    >
      <SettingsTable scope="system" title="系统配置" canEdit={isAdmin} />
    </Card>
  );
}
