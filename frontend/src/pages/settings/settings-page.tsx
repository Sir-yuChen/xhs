/**
 * 设置页（`/settings`，侧栏「设置」）：**工作区私有配置**。
 *
 * ## 三级归属（2026-10-25 用户口径，最终版）
 *
 * | 级别 | 入口 | 位置语义 |
 * |---|---|---|
 * | 系统级 | 系统管理 → 配置管理 | 全站，管理员维护 |
 * | **工作区级** | **本页（侧栏「设置」）** | 只对当前工作区（小红书）生效 |
 * | 个人级 | 头像下拉 →「个人配置」**弹窗** | 本人私有，不属于任何工作区 |
 *
 * ⚠️ 本页曾是「个人配置」（`scope="user"`），2026-10-25 用户截图指出：
 * 「个人配置应在弹窗中展示，目前个人配置展示在了小红书工作区内，
 *  小红书私有配置页没有了」。
 *
 * 判据：**`/settings` 是挂在业务工作区下的页面** ⇒ 它展示的必须是该工作区的
 * 私有配置；个人配置属于「本人」而非任何工作区，放这里会让工作区页被个人
 * 数据占用，同时工作区私有配置无处可看。
 *
 * ⚠️ 不在页面写解释性长文案：用户口径「说明简短，不必要的说明全部移除」。
 * 分区由后端 `SETTING_SCOPES` 定死，本页只请求 `scope="workspace"`。
 */

import { Card } from "antd";
import { SettingOutlined } from "@ant-design/icons";

import { SettingsTable } from "../../components/settings/settings-table";

/** 当前页面所属工作区（`/settings` 挂在 xhs 工作区下）。 */
const WORKSPACE = "xhs";
const WORKSPACE_LABEL = "小红书";

export function SettingsPage() {
  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          {WORKSPACE_LABEL}配置
        </span>
      }
    >
      <SettingsTable
        scope="workspace"
        workspace={WORKSPACE}
        title={`${WORKSPACE_LABEL}配置`}
      />
    </Card>
  );
}
