/**
 * LLM 生成配置弹窗（T9 / 设计 17 §7.4、§13.4.5）。
 *
 * 从商品列表「操作」列的「LLM 生成」按钮打开：选提示词模板 + 媒体来源四选一
 * （商品原图 / 素材库 / 参考图 / AI 生成），提交后**入队即返回**（后台执行）。
 *
 * ⚠️ 三条纪律：
 *   - **门禁在后端**：模板不存在 / 服务商没配 / 媒体参数不匹配 —— 都在同步段 400，
 *     这里只做「必填项在不在」的轻校验，不复制后端的业务判据（两份真相源必分叉）；
 *   - **模型不在这里选**：走业务点映射（「模型配置页」把 `generate_note` / `generate_image`
 *     映射到服务商），弹窗只让用户覆盖模型名（可选，留空即用映射）；
 *   - **提交是入队回执**：不给「生成中…」的假进度，进度看队列卡（`LlmGenerateProgressCard`）。
 */
import {
  App,
  Alert,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";

import {
  fetchMaterialFiles,
  fetchPromptTemplates,
  generateLlmNote,
} from "../../../lib/api";
import type { LlmMediaSource, MaterialFile, PromptTemplate } from "../../../types";

const { Text } = Typography;

/** 媒体来源四选一（与后端 `MEDIA_SOURCE_*` 常量同名）。 */
const MEDIA_SOURCE_OPTIONS: { value: LlmMediaSource; label: string; hint: string }[] = [
  {
    value: "product_image",
    label: "商品原图",
    hint: "直接复用商品台账里的原图，零额外生成",
  },
  {
    value: "material",
    label: "素材库",
    hint: "从素材库勾选图片（不落 AIGC 溯源：那是用户素材，不是 AI 生成）",
  },
  {
    value: "reference_image",
    label: "参考图生成",
    hint: "以商品首图为参考图，让模型生成一张新图",
  },
  {
    value: "ai_generate",
    label: "AI 生成",
    hint: "文生图（可填提示词）；带参考图则走图生图。生成张数上限 4",
  },
];

export function LlmGenerateModal({
  open,
  accountId,
  itemId,
  itemTitle,
  onClose,
  onQueued,
}: {
  open: boolean;
  accountId: number | null;
  itemId: string;
  itemTitle: string;
  onClose: () => void;
  /** 入队成功后回调（宿主用它登记轮询）。 */
  onQueued?: (itemId: string) => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [templatesLoaded, setTemplatesLoaded] = useState(false);
  const [materialFiles, setMaterialFiles] = useState<MaterialFile[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [mediaSource, setMediaSource] = useState<LlmMediaSource>("product_image");

  // 打开时拉一次模板（首次打开才拉；切商品不重复拉 —— 模板是用户级的，与商品无关）
  useEffect(() => {
    if (!open || templatesLoaded) return;
    void (async () => {
      try {
        const page = await fetchPromptTemplates({ pageSize: 100 });
        setTemplates(page.items);
      } catch (error) {
        message.error(error instanceof Error ? error.message : "提示词模板加载失败");
      } finally {
        setTemplatesLoaded(true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, templatesLoaded]);

  // 打开即重置（跨商品不串：上一次的提示词 / 素材勾选不能带到下一个商品）
  useEffect(() => {
    if (open) {
      form.resetFields();
      setMediaSource("product_image");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, itemId]);

  // 选「素材库」时才拉素材（不选就不发这个请求）
  useEffect(() => {
    if (!open || mediaSource !== "material") return;
    void (async () => {
      try {
        const page = await fetchMaterialFiles({ pageSize: 100 });
        setMaterialFiles(page.items.filter((f) => f.file_type === "image"));
      } catch (error) {
        message.error(error instanceof Error ? error.message : "素材加载失败");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mediaSource]);

  /** 默认模板（同任务类型只有一个，后端保证）。 */
  const defaultTemplateId = useMemo(
    () => templates.find((t) => t.is_default && t.task_type === "text")?.id,
    [templates]
  );

  async function submit() {
    if (!accountId) return;
    try {
      const values = await form.validateFields();
      setSubmitting(true);

      // media_options 按来源组装（与后端 §13.4.1 的结构一一对应）
      const mediaOptions =
        mediaSource === "material"
          ? { material_file_ids: values.material_file_ids || [] }
          : mediaSource === "ai_generate"
            ? {
                ...(values.ai_prompt ? { prompt: values.ai_prompt } : {}),
                ...(values.ai_count ? { count: values.ai_count } : {}),
              }
            : {};

      const result = await generateLlmNote({
        platformAccountId: accountId,
        itemId,
        templateId: values.template_id,
        ...(values.model_name ? { modelName: values.model_name } : {}),
        mediaSource,
        ...(Object.keys(mediaOptions).length ? { mediaOptions } : {}),
      });

      if (result.already_queued) {
        message.info("这个商品已在生成队列中");
      } else {
        message.success("已加入生成队列（后台执行，进度见商品列表的生成队列卡）");
        onQueued?.(itemId);
      }
      onClose();
    } catch (error) {
      // 表单校验失败不是 Error（antd 已在字段下渲染原因）
      if (error instanceof Error) message.error(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  const activeHint = MEDIA_SOURCE_OPTIONS.find((o) => o.value === mediaSource)?.hint || "";

  return (
    <Modal
      open={open}
      title="LLM 生成笔记"
      width={640}
      onCancel={onClose}
      footer={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={submitting} onClick={() => void submit()}>
            生成
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          商品：{itemTitle || itemId}
        </Text>

        {templatesLoaded && templates.length === 0 && (
          <Alert
            type="info"
            showIcon
            message="还没有提示词模板"
            description="去「笔记工坊 → 提示词模板」建一个（可留空不选，后端会用默认骨架）。"
          />
        )}

        <Form form={form} layout="vertical">
          <Form.Item
            name="template_id"
            label="提示词模板"
            initialValue={defaultTemplateId}
            extra="不选则用后端默认骨架"
          >
            <Select
              allowClear
              placeholder="选择一个模板（可留空）"
              options={templates.map((t) => ({
                value: t.id,
                label: `${t.name}${t.is_default ? "（默认）" : ""}`,
              }))}
            />
          </Form.Item>

          <Form.Item name="model_name" label="模型名（可选）" extra="留空则用该业务点映射到的服务商的默认模型">
            <Input placeholder="留空即用模型配置页里映射的默认模型" maxLength={128} />
          </Form.Item>

          <Form.Item label="媒体来源">
            <Radio.Group
              value={mediaSource}
              onChange={(e) => setMediaSource(e.target.value as LlmMediaSource)}
            >
              <Space direction="vertical" size={6}>
                {MEDIA_SOURCE_OPTIONS.map((option) => (
                  <Radio key={option.value} value={option.value}>
                    {option.label}
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 6 }}>
              {activeHint}
            </Text>
          </Form.Item>

          {mediaSource === "material" && (
            <Form.Item
              name="material_file_ids"
              label="选择素材（图片）"
              rules={[{ required: true, message: "请至少勾选一张素材图片" }]}
            >
              {materialFiles.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="素材库还没有图片（去「笔记工坊 → 素材库」添加并扫描）"
                />
              ) : (
                <Select
                  mode="multiple"
                  placeholder="勾选要用的素材图片"
                  options={materialFiles.map((f) => ({
                    value: f.id,
                    label: f.filename,
                  }))}
                />
              )}
            </Form.Item>
          )}

          {mediaSource === "ai_generate" && (
            <>
              <Form.Item name="ai_prompt" label="图片提示词（可选）" extra="留空则由后端按商品信息拼默认提示词">
                <Input.TextArea rows={3} maxLength={2000} placeholder="例如：浅色木质桌面，俯拍，自然光" />
              </Form.Item>
              <Form.Item name="ai_count" label="生成张数" initialValue={1} extra="上限 4">
                <InputNumber min={1} max={4} style={{ width: 100 }} />
              </Form.Item>
            </>
          )}

          {mediaSource === "reference_image" && (
            <Alert
              type="info"
              showIcon
              message="参考图自动取商品的台账首图"
              description="不用手动填 URL —— 后端按商品 ID 从本地台账取图（避免任意 URL 下载）。商品没有图时会报缺，不会凭空生成。"
            />
          )}
        </Form>
      </Space>
    </Modal>
  );
}
