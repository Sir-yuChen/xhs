/**
 * 「违规词」弹窗 —— 商品台账的**本地违规词库**管理（设计 25 §2）。
 *
 * ## 用户口径（2026-10-08）
 *
 * 「违规词新增**分组**逻辑，点击预检支持选择指定违规词分组，支持多组，默认全部分组」
 * 「违规词**添加修改使用弹窗**使用，列表顶部添加**搜索区域和搜索条件**，
 *  分组下拉，关键词模糊下拉，列表使用 antd 的**父子列表**展示」
 * 「违规词的匹配方式**默认精确**」
 *
 * ## 为什么是**本地**词库而不是接千帆的 `check_forbidden_keyword`
 *
 * 平台那个端点是**校验器**（拿一段文字问平台有没有违规），它没有词表的
 * 增删改查，也没有「停用 / 启用」语义 —— 用户要的四个动作在平台上无处落地。
 * 真机调研（`8091.31a2a571.js` + `all_edith_requests.json`）确认平台侧只提供
 * 校验入口，所以词库落本地表 `forbidden_words`，**图片 OCR 在本地过滤**。
 *
 * ## 三个结构性决定
 *
 * 1. **增改走子弹窗**（`WordEditor`）—— 分组/词/匹配/备注四个字段横排会把
 *    顶部挤成一团，且「正在编辑第几行」的状态与列表混在一起。
 * 2. **搜索区在顶部**：分组走**服务端**筛（`group_name` 参数），关键词走
 *    **本地模糊**（词 + 备注）。前者是数据隔离/规模问题，后者是即时交互问题。
 * 3. **列表是 antd 父子列表**：分组 = 父行、词 = 子行，默认全展开。
 *
 * ## 「不可重复」由后端 + 数据库保证
 *
 * 前端**不**做去重预检 —— 后端把 `Best` / `best` / `ＢＥＳＴ` 判为同一个词
 * （归一化后比较），前端自建一套规则必然与服务端口径漂移。重复时后端回
 * **409 `FORBIDDEN_WORD_DUPLICATE`**，这里把 `detail.message` 原样透出。
 * ⚠️ 去重范围是**同一分组内** —— 同一个词进不同分组是正常需求。
 *
 * ⚠️ 说明文案**禁止 `**`**（守卫覆盖整个 `frontend/src`）。
 * ⚠️ 弹窗尺寸按**视口百分比**（用户偏好），本弹窗 **60%**（发布弹窗是 75%）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App,
  AutoComplete,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
} from "@ant-design/icons";

import {
  createForbiddenWord,
  deleteForbiddenWord,
  fetchForbiddenWordGroups,
  fetchForbiddenWords,
  setForbiddenWordEnabled,
  setForbiddenWordGroupEnabled,
  updateForbiddenWord,
  type ForbiddenWord,
  type ForbiddenWordGroup,
} from "../../../lib/api";

const { Text } = Typography;

/**
 * 匹配模式选项（1=精确 / 2=包含）。
 *
 * ⚠️ 默认 **1（精确）** —— 用户口径 2026-10-08：「违规词的匹配方式默认精确」。
 * 排在第一项的是默认值（表单初值也取 1），两者必须一致。
 */
const MATCH_MODE_OPTIONS = [
  { value: 1, label: "精确（默认）" },
  { value: 2, label: "包含" },
];

/** 新增时的默认匹配模式（与上面第一项、后端 `MATCH_MODE_EXACT` 同值）。 */
const DEFAULT_MATCH_MODE = 1;

/** 「未分组」的展示文案（后端用空串表示，界面不能显示成空白）。 */
const UNGROUPED_LABEL = "未分组";

type ForbiddenWordsModalProps = {
  open: boolean;
  onClose: () => void;
};

type EditFormValues = {
  word: string;
  match_mode: number;
  remark?: string;
  group_name?: string;
};

/** 父行（分组）。`enabled` = 整组开关（停用 ⇒ 组内词全部退出匹配）。 */
type GroupRow = {
  key: string;
  __isGroup: true;
  name: string;
  label: string;
  count: number;
  enabled: boolean;
  children: WordRow[];
};

/** 子行（词）。 */
type WordRow = ForbiddenWord & { __isGroup: false; key: string };

export function ForbiddenWordsModal({ open, onClose }: ForbiddenWordsModalProps) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<ForbiddenWord[]>([]);
  const [groups, setGroups] = useState<ForbiddenWordGroup[]>([]);
  const [loading, setLoading] = useState(false);

  //: 搜索条件（顶部区域）。
  const [filterGroup, setFilterGroup] = useState<string | undefined>(undefined);
  const [filterKeyword, setFilterKeyword] = useState("");

  //: 分页（用户口径 2026-10-08：「违规词分页，每页 20/30/50，**按照组分页**」）。
  //: ⚠️ 分页单位是**分组（父行）**，不是词行 —— 一页里不会出现「半个分组」。
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  //: 编辑弹窗（`editing === null` = 新增）。
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ForbiddenWord | null>(null);

  /** 409 的 `detail.message` 是**人话**（"违规词「最佳」已存在"），优先透出。 */
  const describeError = useCallback((error: unknown): string => {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message);
    }
    return error instanceof Error ? error.message : "未知错误";
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      //: 分组过滤走**服务端**（`group_name` 查询参数）—— 数据隔离与规模
      //: 都该由后端收口，前端只负责把选择传下去。
      const [list, groupList] = await Promise.all([
        fetchForbiddenWords(false, filterGroup || undefined),
        fetchForbiddenWordGroups(),
      ]);
      setRows(list);
      setGroups(groupList);
    } catch (error) {
      void message.error(`加载违规词失败：${describeError(error)}`);
    } finally {
      setLoading(false);
    }
  }, [describeError, filterGroup, message]);

  useEffect(() => {
    if (!open) {
      return;
    }
    //: 换分组条件 = 换数据集 ⇒ 回到第一页（不然会停在一个已不存在的页码上）。
    setPage(1);
    void load();
  }, [open, load]);

  //: 关键词筛选也是换数据集 ⇒ 回第一页。
  useEffect(() => {
    setPage(1);
  }, [filterKeyword]);

  /** 关键词模糊（本地）—— 已按分组筛过的结果里再过滤词与备注。 */
  const visibleRows = useMemo(() => {
    const needle = filterKeyword.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      `${row.word} ${row.remark ?? ""}`.toLowerCase().includes(needle),
    );
  }, [rows, filterKeyword]);

  /**
   * 拍成 antd 的**父子列表**：分组父行 + 词子行。
   *
   * ⚠️ 空分组不出现（词都被筛没了就没必要留个空壳）；「未分组」排最后
   * —— 它是个兜底桶，不该抢在真正分好的组前面。
   *
   * ⚠️ **分页在分组这一层做**（用户口径 2026-10-08：「按照组分页」）：
   * 先切分组，再由 Table 把这一页的分组的子行全部展开 —— 所以一页里
   * 不会出现「某个分组只显示一半词」。
   */
  /** 组名 → 整组启用态（没有组行的按「启用」——与后端 `enabled_groups` 同口径）。 */
  const groupEnabled = useMemo(() => {
    const map = new Map<string, boolean>();
    for (const g of groups) map.set(g.name, g.enabled);
    return map;
  }, [groups]);

  const allGroupRows = useMemo<GroupRow[]>(() => {
    const byGroup = new Map<string, WordRow[]>();
    for (const row of visibleRows) {
      const name = row.group_name || "";
      const bucket = byGroup.get(name) ?? [];
      bucket.push({ ...row, __isGroup: false, key: `w-${row.id}` });
      byGroup.set(name, bucket);
    }
    return [...byGroup.entries()]
      .sort(([a], [b]) => (a === "" ? 1 : b === "" ? -1 : a.localeCompare(b, "zh")))
      .map(([name, children]) => ({
        key: `g-${name || "__ungrouped"}`,
        __isGroup: true as const,
        name,
        label: name || UNGROUPED_LABEL,
        count: children.length,
        //: 整组开关（停用 ⇒ 组内词全部退出匹配）。
        enabled: groupEnabled.get(name) ?? true,
        children,
      }));
  }, [visibleRows, groupEnabled]);

  /** 当前页的分组（分页单位 = 分组）。 */
  const treeData = useMemo(() => {
    const start = (page - 1) * pageSize;
    return allGroupRows.slice(start, start + pageSize);
  }, [allGroupRows, page, pageSize]);

  const openCreate = () => {
    setEditing(null);
    setEditorOpen(true);
  };

  const openEdit = (row: ForbiddenWord) => {
    setEditing(row);
    setEditorOpen(true);
  };

  const save = async (values: EditFormValues) => {
    if (editing === null) {
      await createForbiddenWord({
        word: values.word,
        match_mode: values.match_mode,
        remark: values.remark ?? "",
        group_name: values.group_name ?? "",
      });
      void message.success("已新增违规词");
    } else {
      await updateForbiddenWord(editing.id, {
        word: values.word,
        match_mode: values.match_mode,
        remark: values.remark ?? "",
        group_name: values.group_name ?? "",
      });
      void message.success("已保存修改");
    }
    setEditorOpen(false);
    setEditing(null);
    await load();
  };

  /** ⚠️ 整组启停：组停用 ⇒ 组内词**全部**退出 OCR 匹配（后端真收口）。 */
  const toggleGroup = async (row: GroupRow) => {
    try {
      await setForbiddenWordGroupEnabled(row.name, !row.enabled);
      void message.success(
        row.enabled ? `已停用分组「${row.label}」` : `已启用分组「${row.label}」`,
      );
      await load();
    } catch (error) {
      void message.error(describeError(error));
    }
  };

  const toggleEnabled = async (row: ForbiddenWord) => {
    try {
      await setForbiddenWordEnabled(row.id, !row.enabled);
      void message.success(row.enabled ? `已停用「${row.word}」` : `已启用「${row.word}」`);
      await load();
    } catch (error) {
      void message.error(describeError(error));
    }
  };

  const remove = async (row: ForbiddenWord) => {
    try {
      await deleteForbiddenWord(row.id);
      void message.success(`已删除「${row.word}」`);
      await load();
    } catch (error) {
      void message.error(describeError(error));
    }
  };

  /** 分组下拉：已有分组 + 一个「全部分组」。 */
  const groupOptions = useMemo(
    () => [
      { value: "", label: "全部分组" },
      ...groups.map((g) => ({
        value: g.name,
        label: `${g.name || UNGROUPED_LABEL}（${g.count}）`,
      })),
    ],
    [groups],
  );

  /** 已有分组名（供编辑弹窗手选；不含未分组）。 */
  const groupNames = useMemo(() => groups.map((g) => g.name).filter(Boolean), [groups]);

  return (
    <>
      <Modal
        open={open}
        title="违规词"
        //: 用户口径 2026-10-08：「违规词弹窗固定**全屏的 60%**」——
        //: 宽高都是视口百分比（不是固定像素），各种屏幕比例一致。
        //: ⚠️ 与发布弹窗的 75% 刻意不同。
        width="60vw"
        //: 全站弹窗垂直居中（口径 2026-10-03）—— `centered` 与 `style.top` 互斥，
        //: 留 `top: 24` 会把 `centered` 顶掉（守卫会点出「假防线」）。
        centered
        styles={{
          body: {
            height: "60vh",
            maxHeight: "60vh",
            overflowY: "auto",
            //: 藏滚动条外观，保留滚动能力（与发布弹窗同一口径）。
            scrollbarWidth: "none",
            msOverflowStyle: "none",
          },
        }}
        className="hermes-hide-scrollbar"
        onCancel={onClose}
        footer={null}
        destroyOnClose
      >
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 12 }}>
          本词库用于数据包图片的违规词过滤：每张媒体资源都会被识别文字，
          命中启用中的词即标红并置灰不可选。停用的词不参与匹配。
        </Text>

        {/* 搜索区域：分组下拉（服务端筛） + 关键词模糊（本地筛） + 新增 */}
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            style={{ width: 200 }}
            value={filterGroup ?? ""}
            options={groupOptions}
            onChange={(value) => setFilterGroup(value || undefined)}
            placeholder="分组"
          />
          <Input
            allowClear
            style={{ width: 220 }}
            placeholder="关键词模糊搜索（词 / 备注）"
            value={filterKeyword}
            onChange={(e) => setFilterKeyword(e.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增违规词
          </Button>
        </Space>

        <Table
          size="small"
          rowKey="key"
          loading={loading}
          dataSource={treeData}
          //: ⚠️ **按组分页**（用户口径 2026-10-08）：`total` = 分组数，
          //: 不是词数 —— 分页器上显示的「共 N 条」是 N 个分组。
          pagination={{
            current: page,
            pageSize,
            total: allGroupRows.length,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
            pageSizeOptions: [20, 30, 50],
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 个分组`,
          }}
          scroll={{ y: "36vh" }}
          //: 藏滚动条外观，保留滚动（与全站口径一致）。
          className="hermes-hide-scrollbar"
          //: 父子列表**默认全展开** —— 打开弹窗就是要看词，不是看折叠条。
          expandable={{ defaultExpandAllRows: true }}
          locale={{ emptyText: "还没有违规词，点右上「新增违规词」" }}
          columns={[
            {
              title: "分组 / 违规词",
              dataIndex: "label",
              //: 用户口径 2026-10-08：「分组 / 违规词列**固定 180px**」。
              //: 固定宽度（不是 minWidth）—— 列宽不随内容抖动。
              width: 180,
              //: 固定宽度下长词/长分组名必须省略，否则会把列撑开（那就不固定了）。
              ellipsis: true,
              render: (_: unknown, row: GroupRow | WordRow) =>
                row.__isGroup ? (
                  //: 父行是分组名，长了也要能看全 ⇒ Tooltip 兜。
                  <Tooltip title={row.label}>
                    <Space size={6}>
                      <Text strong ellipsis style={{ maxWidth: 120 }}>
                        {row.label}
                      </Text>
                      <Tag>{row.count}</Tag>
                    </Space>
                  </Tooltip>
                ) : (
                  <Tooltip title={row.word}>
                    <span
                      style={{
                        display: "inline-block",
                        paddingLeft: 8,
                        maxWidth: 160,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        verticalAlign: "bottom",
                      }}
                    >
                      {row.word}
                    </span>
                  </Tooltip>
                ),
            },
            {
              title: "匹配",
              dataIndex: "match_mode",
              width: 90,
              render: (mode: number, row: GroupRow | WordRow) =>
                row.__isGroup ? null : (
                  <Tag color={mode === 1 ? "blue" : "default"}>
                    {mode === 1 ? "精确" : "包含"}
                  </Tag>
                ),
            },
            {
              title: "状态",
              dataIndex: "enabled",
              width: 80,
              render: (enabled: boolean, row: GroupRow | WordRow) =>
                //: 父行也有状态（整组开关）—— 用户口径「组新增启用停用功能」。
                //: ⚠️ 组停用时**子行标灰**（见下面 CSS 类），一眼看出这组不参与匹配。
                row.__isGroup ? (
                  <Tag color={enabled ? "green" : "default"}>{enabled ? "启用" : "停用"}</Tag>
                ) : (
                  <Tag color={enabled ? "green" : "default"}>{enabled ? "启用" : "停用"}</Tag>
                ),
            },
            {
              title: "备注",
              dataIndex: "remark",
              //: 用户口径：「备注列 180px，使用 tip 展示完整内容」。
              //: 单行省略 + Tooltip 看全 —— 长备注不能把表格挤变形。
              width: 180,
              render: (value: string | null | undefined, row: GroupRow | WordRow) => {
                if (row.__isGroup) return null;
                const text = value ?? "";
                if (!text) return <Text type="secondary">-</Text>;
                return (
                  <Tooltip title={text}>
                    <span
                      style={{
                        display: "inline-block",
                        maxWidth: 168,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        verticalAlign: "bottom",
                      }}
                    >
                      {text}
                    </span>
                  </Tooltip>
                );
              },
            },
            {
              title: "操作",
              key: "ops",
              width: 190,
              render: (_: unknown, row: GroupRow | WordRow) =>
                row.__isGroup ? (
                  //: 父行只给「整组停用/启用」—— 组级动作与词级动作分开摆。
                  <Button
                    size="small"
                    icon={row.enabled ? <StopOutlined /> : <CheckCircleOutlined />}
                    onClick={() => void toggleGroup(row)}
                  >
                    {row.enabled ? "停用整组" : "启用整组"}
                  </Button>
                ) : (
                  <Space size={4}>
                    <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)}>
                      修改
                    </Button>
                    <Button
                      size="small"
                      icon={row.enabled ? <StopOutlined /> : <CheckCircleOutlined />}
                      onClick={() => void toggleEnabled(row)}
                    >
                      {row.enabled ? "停用" : "启用"}
                    </Button>
                    <Popconfirm
                      title={`删除「${row.word}」？`}
                      description="删除不可恢复"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void remove(row)}
                    >
                      <Tooltip title="删除（不可恢复）">
                        <Button size="small" danger icon={<DeleteOutlined />}>
                          删除
                        </Button>
                      </Tooltip>
                    </Popconfirm>
                  </Space>
                ),
            },
          ]}
        />
      </Modal>

      {/* 新增 / 修改**各自一个弹窗**（用户口径：「添加修改使用弹窗」） */}
      <WordEditor
        open={editorOpen}
        editing={editing}
        groupNames={groupNames}
        onCancel={() => {
          setEditorOpen(false);
          setEditing(null);
        }}
        onSave={save}
        describeError={describeError}
      />
    </>
  );
}

/* ------------------------------------------------------------------ *
 * 新增 / 修改弹窗
 * ------------------------------------------------------------------ */

function WordEditor(props: {
  open: boolean;
  editing: ForbiddenWord | null;
  /** 已有分组名（供下拉直接选，也可手输新分组）。 */
  groupNames: string[];
  onCancel: () => void;
  onSave: (values: EditFormValues) => Promise<void>;
  describeError: (error: unknown) => string;
}) {
  const { open, editing, groupNames, onCancel, onSave, describeError } = props;
  const { message } = App.useApp();
  const [form] = Form.useForm<EditFormValues>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (editing) {
      form.setFieldsValue({
        word: editing.word,
        match_mode: editing.match_mode,
        remark: editing.remark,
        group_name: editing.group_name ?? "",
      });
    } else {
      form.resetFields();
      //: 默认匹配模式 **精确**（用户口径 2026-10-08）。
      form.setFieldsValue({
        word: "",
        match_mode: DEFAULT_MATCH_MODE,
        remark: "",
        group_name: "",
      });
    }
  }, [open, editing, form]);

  const submit = async () => {
    let values: EditFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return; // 校验失败的提示由 antd 挂在字段上
    }
    setSaving(true);
    try {
      await onSave(values);
    } catch (error) {
      void message.error(describeError(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={editing ? `修改违规词「${editing.word}」` : "新增违规词"}
      width="40vw"
      centered
      onCancel={onCancel}
      onOk={() => void submit()}
      okText={editing ? "保存" : "新增"}
      cancelText="取消"
      confirmLoading={saving}
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="word"
          label="违规词"
          rules={[
            { required: true, message: "请输入违规词" },
            { max: 120, message: "最多 120 个字" },
          ]}
        >
          <Input placeholder="如：最佳" />
        </Form.Item>

        {/*
          ⚠️ 这里**不能**用 `<Select mode="tags">` 来拿「可选可输」：
          `tags` 形态的受控值是**数组**，而 `Form.Item name="group_name"` 绑的是
          **字符串** —— antd 拿不到对应值，输入的文字不会回填，表现为
          「输入框不展示输入的内容」（用户 2026-10-08 实测报出）。

          改用 `AutoComplete`：值本来就是字符串（与表单字段同型，无需转换），
          既能从已有分组里选，也能直接敲一个新名字。
        */}
        <Form.Item
          name="group_name"
          label="分组"
          extra="可从已有分组里选，也可直接输入新分组名；留空 = 未分组"
        >
          <AutoComplete
            allowClear
            placeholder={`留空 = ${UNGROUPED_LABEL}`}
            options={groupNames.map((name) => ({ value: name }))}
            filterOption={(input, option) =>
              String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())
            }
          />
        </Form.Item>

        <Form.Item
          name="match_mode"
          label="匹配方式"
          extra="精确 = 整段文字完全相同才命中；包含 = 出现该词即命中"
        >
          <Select options={MATCH_MODE_OPTIONS} />
        </Form.Item>

        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} maxLength={255} showCount placeholder="可选" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export default ForbiddenWordsModal;
