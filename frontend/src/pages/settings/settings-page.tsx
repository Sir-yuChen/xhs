import { SafetyCertificateOutlined, WarningOutlined } from "@ant-design/icons";
import { Alert, Card, Col, Row, Space, Typography } from "antd";

import { PageHeader } from "../../components/layout/app-shell";

const { Paragraph, Text, Title } = Typography;

export function SettingsPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Workspace"
        title="设置"
        description="用户空间、安全、文件存储和系统参数会集中在这里。"
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card
            title={<span><SafetyCertificateOutlined style={{ marginRight: 8 }} />安全边界</span>}
          >
            <Paragraph>
              所有资源将通过平台用户和平台账号双重归属校验。Cookie 与模型 Key 由后端统一加密存储。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            title={<span><WarningOutlined style={{ marginRight: 8, color: "#faad14" }} />项目声明</span>}
          >
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="Spider_XHS 为开源学习项目，仅供技术研究和个人学习使用"
            />
            <Paragraph>
              <ul style={{ paddingLeft: 20, margin: 0 }}>
                <li><Text strong>禁止任何形式的商业化使用</Text>，包括但不限于出售、转卖、收费服务</li>
                <li><Text strong>禁止用于任何违法违规活动</Text>，包括但不限于数据贩卖、恶意爬取、侵犯隐私</li>
                <li>使用者需自行承担因使用本项目产生的一切法律责任</li>
                <li>请遵守小红书平台的用户协议和相关法律法规</li>
              </ul>
            </Paragraph>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
