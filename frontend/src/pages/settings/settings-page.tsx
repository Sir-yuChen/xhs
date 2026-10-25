/**
 * 用户配置页（`/settings`）：**只放用户级**配置（该用户下公共的）。
 *
 * 设计 65 三级拆分（2026-10-25 用户口径「不要混乱展示」）：
 * * 系统级 → 系统管理 → 配置管理（管理员维护，全站生效）
 * * **用户级 → 本页 / 头像下拉「用户配置」**（只对本人生效）
 * * 工作区级 → 侧栏工作区齿轮（仅该工作区生效）
 *
 * ⚠️ 不在页面写解释性长文案：用户口径「说明简短，不必要的说明全部移除」。
 * 分区由后端 `SETTING_SCOPES` 定死，本页只请求 `scope="user"`。
 */

import { Card } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";

export function SettingsPage() {
  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          个人配置
        </span>
      }
    >
      <SettingsTable scope="user" title="个人配置" />
    </Card>
  );
}
