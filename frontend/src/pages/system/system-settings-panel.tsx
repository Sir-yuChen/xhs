/**
 * 系统管理 → 配置管理（`/system/settings`）：**系统级 + 工作区级**两块。
 *
 * 设计 65/66 三级拆分（2026-10-25 用户口径：「系统级只展示系统管理中展示系统公共的，
 * 用户级别也是该用户下公共的配置，工作区域级别的是指定工作区域私有的，
 * **不要混乱展示**」）。
 *
 * 分区由后端 `SETTING_SCOPES` 定死，本页只请求 `scope="system"` 与
 * `scope="workspace"` ⇒ 不会出现「同一个键在三个页面都列一遍」或「某键哪都不显示」。
 *
 * ⚠️ **工作区配置为什么在这里、不在顶栏齿轮**（2026-10-25 用户口径
 * 「齿轮逻辑是不正确的就不应该存在」）：齿轮是**全局**入口，而工作区配置是
 * **某个工作区私有**的 —— 一个不带工作区选择的全局按钮点了必然要猜「配哪个」，
 * 猜错就是配错地方（实测：在系统管理页点开显示「工作区配置 · system」，
 * 而 `system` 是管理工作区，根本没有私有配置）。
 * 配置归属必须**看得见** ⇒ 放在系统管理里、标题写明是哪个工作区。
 *
 * ⚠️ 不在页面上写解释性长文案：用户口径「说明简短，不必要的说明全部移除」。
 */

import { Card, Space, Typography } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";
import { usePermission } from "../../hooks/use-permission";

const { Text } = Typography;

/** 有私有配置的业务工作区（设计 66：目前只有小红书）。 */
const WORKSPACES: ReadonlyArray<{ key: string; label: string }> = [
  { key: "xhs", label: "小红书" },
];

export function SystemSettingsPanel() {
  const { isAdmin } = usePermission();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
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

      {WORKSPACES.map((ws) => (
        <Card
          key={ws.key}
          title={
            <Space>
              <span>{ws.label}配置</span>
              <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                只对{ws.label}生效
              </Text>
            </Space>
          }
        >
          <SettingsTable
            scope="workspace"
            workspace={ws.key}
            title={`${ws.label}配置`}
            canEdit={isAdmin}
          />
        </Card>
      ))}
    </div>
  );
}
