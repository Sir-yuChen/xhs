import { SafetyCertificateOutlined } from "@ant-design/icons";
import { Card, Col, Row, Typography } from "antd";

import { PageHeader } from "../../components/layout/app-shell";

const { Paragraph } = Typography;

export function SettingsPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Workspace"
        title="设置"
        description="用户空间、安全、文件存储和系统参数会集中在这里。"
      />

      {/*
        2026-09-26 用户口径「设置页面移除项目声明」：原第二张「项目声明」卡
        （开源警告 Alert + 四条禁止条款）**整卡移除**，`WarningOutlined` / `Alert`
        导入随之下线。剩下的「安全边界」是本页唯一一张卡 ⇒ `Row` 里只留一个
        `Col xs={24}`（原 `lg={12}` 会留半行空白）。
      */}
      <Row gutter={[16, 16]}>
        <Col xs={24}>
          <Card
            title={<span><SafetyCertificateOutlined style={{ marginRight: 8 }} />安全边界</span>}
          >
            <Paragraph>
              所有资源将通过平台用户和平台账号双重归属校验。Cookie 与模型 Key 由后端统一加密存储。
            </Paragraph>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
