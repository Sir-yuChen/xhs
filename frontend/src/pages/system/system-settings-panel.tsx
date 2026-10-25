/**
 * 系统管理 → 配置管理（`/system/settings`）：**只放系统级**配置。
 *
 * 设计 65 三级拆分（2026-10-25 用户口径：「系统级只展示系统管理中展示系统公共的，
 * 用户级别也是该用户下公共的配置，工作区域级别的是指定工作区域私有的，
 * **不要混乱展示**」）。
 *
 * 分区由后端 `SETTING_SCOPES` 定死，本页只请求 `scope="system"` ⇒ 不会出现
 * 「同一个键在三个页面都列一遍」或「某键哪都不显示」。
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
