/**
 * 系统管理 → 配置管理（`/system/settings`，设计 64 §1.6）：**系统级**配置面板。
 *
 * 设计 62 之前本路由 302 到 `/settings`（当时配置只有用户级一份）；三级化后
 * 系统级有了自己的归属 —— 管理员在这里维护全站基线，用户级/工作区级覆盖仍可
 * 各自优先（`SettingsTable scope="system"`）。
 */

import { Card, Typography } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";
import { usePermission } from "../../hooks/use-permission";

const { Paragraph } = Typography;

export function SystemSettingsPanel() {
  const { isAdmin } = usePermission();

  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          配置管理（系统级）
        </span>
      }
    >
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        系统级配置对**全站所有用户**直接生效（用户级/工作区级覆盖仍可各自优先）。
        改完立即生效，无需重启；标记「实时生效」的键有运行时读点，
        「仅存档」的键没有读点、只作记录。
      </Paragraph>
      <SettingsTable
        scope="system"
        title="配置管理（系统级）"
        canEdit={isAdmin}
      />
    </Card>
  );
}
