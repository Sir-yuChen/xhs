/**
 * 用户配置页（`/settings`，设计 64 §1.6：用户级 = 个人覆盖）。
 *
 * 设计 64 三级化后本页职责收窄为 **`scope="user"` 的展示与编辑**（表格组件
 * `SettingsTable` 三处挂载点共用：系统管理 → 配置管理挂 system 级、头像下拉
 * 「用户配置」弹窗挂 user 级、WorkspaceShell 齿轮挂工作区级）。
 * 原「应用到所有用户」随端点退场（410，系统级取代其语义）。
 */

import { Card, Typography } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";

const { Paragraph } = Typography;

export function SettingsPage() {
  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          用户配置
        </span>
      }
    >
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        这里是**你的个人覆盖**：对全站生效的基线由管理员在「系统管理 → 配置管理」维护；
        你在这里建的键只对你自己的账号优先生效（高于系统级）。
        标记「实时生效」的参数改完立即作用于运行中的任务（无需重启）。
      </Paragraph>
      <SettingsTable scope="user" title="用户配置" />
    </Card>
  );
}
