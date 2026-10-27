/**
 * **数据包列规则**管理弹窗（设计 40，用户口径 2026-10-14）。
 *
 * 用户原话：
 * > 「新增表，在商品 sku 表格 tab 下新增**提取数据规则**按钮，使用弹窗**全屏的 60%**
 * >   实现 CRUD，操作列启动/停用，规则与规则包使用**父子表**展示，均有启用停用功能」
 * > 「目前需求只有**提取 sku 名称**规则，其它可能支持设置为固定文本，批量加、减、乘、
 * >   乘+加、乘+减 等等，可完全取代当前表格顶部对列的批量操作功能」
 *
 * ## 结构
 *
 * 父 = **规则包**（一类数据包，如「店透SKU预览」）；子 = **规则**（该包里某一列怎么处理）。
 *
 * ## 三条刻意的设计
 *
 * 1. **参数摘要用人话**（`按「;」取第 2 段`），不显示 `{"sep":";","index":2}` ——
 *    用户看不懂 JSON，会以为存的是乱码。
 * 2. **删包二次确认且明示条数**（「将同时删除其下 N 条规则」）—— 级联删除不可逆。
 * 3. **「取第几段」用 1 起数**（与后端一致）—— 显示「第 2 段」而不是「index=2」。
 */

import { useCallback, useEffect, useState } from "react";
import { hasPermission } from "../../../hooks/use-permission";
import {
  App,
  Alert,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined } from "@ant-design/icons";

import {
  createDataPackageRule,
  createDataPackageRuleSet,
  deleteDataPackageRule,
  deleteDataPackageRuleSet,
  fetchDataPackageRuleSets,
  fetchDataPackageRules,
  updateDataPackageRule,
  updateDataPackageRuleSet,
  type DataPackageRule,
  type DataPackageRuleOp,
  type DataPackageRuleSet,
} from "../../../lib/api";

import { dictLabel } from "../../../lib/system-dicts";
import { WaitMask } from "../../../components/ui/wait-mask";
const { Text } = Typography;

/** 运算的一步（`math_scale`）：运算符 + 数值。 */
export type MathStep = { op: "mul" | "add" | "sub"; value: number };

/** 操作类型的中文名（**只列本轮实现的**；其余字段已定型但后端会 400）。 */
/**
 * 规则类型的**镜像兜底** —— 展示走字典 `xhs_package_rule_op`（设计 63：枚举全量字典化）。
 * 保留这份只为字典未加载时下拉仍有得选。
 */
const OP_LABELS: Record<string, string> = {
  extract_segment: "按分隔符取第 N 段",
  extract_between: "取包裹符之间的内容",
  extract_strip: "去前缀 / 去后缀",
  fixed_text: "固定数值 / 固定文本（整列设为同一个值）",
  math_scale: "批量运算（加减乘可随意搭配）",
  copy_column: "复制列值（把另一列复制到当前列）",
  copy_extract: "复制 + 提取（先复制某列，再按规则提取）",
};

//: ⚠️ 与后端 `_IMPLEMENTED_OPS` **必须同集合** —— 这里漏一个，用户在弹窗里就
//: 选不到那个 op（后端明明支持），是「后端改了前端没跟」的典型漏配。
const OP_IMPLEMENTED: DataPackageRuleOp[] = [
  "extract_segment",
  "extract_between",
  "extract_strip",
  "fixed_text",
  "math_scale",
  "copy_column",
  "copy_extract",
];

//: **需要选「来源列」**的 op（与后端 `OPS_NEED_SOURCE_COLUMN` 同集合）——
//: 在**表头弹窗**里选，不在这里配（那边才知道当前是哪一列）。
const SOURCE_COLUMN_OPS: DataPackageRuleOp[] = [
  "math_scale",
  "copy_column",
  //: ⚠️ 与后端 `OPS_NEED_SOURCE_COLUMN` **必须同集合** —— 漏一个，用户在表头弹窗
  //: 就选不到来源列，规则静默按当前列执行（结果看起来「没生效」）。
  "copy_extract",
];
export const needsSourceColumn = (op: DataPackageRuleOp | undefined): boolean =>
  !!op && SOURCE_COLUMN_OPS.includes(op);

/** 规则参数的**表单形状**（按 op 动态渲染，不一次性摆所有字段）。 */
interface RuleFormValues {
  rule_name: string;
  op: DataPackageRuleOp;
  sep?: string;
  index?: number;
  open?: string;
  close?: string;
  occurrence?: number;
  prefix?: string;
  suffix?: string;
  is_regex?: boolean;
  //: ── `fixed_text` ──
  text?: string;
  //: ── `math_scale`：**步骤列表**（加减乘随意搭配、按顺序执行，用户口径 2026-10-15）──
  //: 每步 `{ op: "mul"|"add"|"sub", value: number }`；用 `Form.List` 增删。
  steps?: MathStep[];
  //: ── `copy_extract`：**两步**（用户口径 2026-10-15）──
  //: 第一步「复制哪一列」是执行参数（表头弹窗选）；下面是第二步「怎么提取」。
  //: ⚠️ 前缀统一用 `cx_`（**c**opy e**x**tract）—— 避免与顶层 `sep` / `open` /
  //: `prefix` 撞名（同一张表单里两套参数，撞名 = 编辑时互相覆盖）。
  cx_step?: "segment" | "between" | "strip";
  cx_sep?: string;
  cx_index?: number;
  cx_open?: string;
  cx_close?: string;
  cx_occurrence?: number;
  cx_prefix?: string;
  cx_suffix?: string;
  enabled: boolean;
}

/** 表单值 ⇄ 后端 `params` 的互转（**单一实现**，新增/编辑共用）。 */
function paramsFromForm(v: RuleFormValues): Record<string, unknown> {
  if (v.op === "extract_segment") {
    return { sep: v.sep ?? "", index: v.index ?? 1 };
  }
  if (v.op === "extract_between") {
    return v.close
      ? { open: v.open ?? "", close: v.close, occurrence: v.occurrence ?? 1 }
      : { open: v.open ?? "", occurrence: v.occurrence ?? 1 };
  }
  if (v.op === "fixed_text") {
    return { text: v.text ?? "" };
  }
  if (v.op === "copy_column") {
    //: 来源列是**执行参数**（表头弹窗里选）⇒ 规则自身没有参数。
    return {};
  }
  if (v.op === "copy_extract") {
    //: ⚠️ **两步**：第一步「复制哪一列」是**执行参数**（表头弹窗里选，不进 params）；
    //: 第二步「怎么提取」是**规则参数**（`extract`，在这里配）。
    //: 形状与后端 `COPY_EXTRACT_STEPS` 三选一同集合。
    const step = v.cx_step ?? "segment";
    if (step === "segment") {
      return { extract: { op: "segment", sep: v.cx_sep ?? "", index: v.cx_index ?? 1 } };
    }
    if (step === "between") {
      const ex: Record<string, unknown> = { op: "between", open: v.cx_open ?? "" };
      if (v.cx_close) ex.close = v.cx_close;
      if (v.cx_occurrence) ex.occurrence = v.cx_occurrence;
      return { extract: ex };
    }
    return { extract: { op: "strip", prefix: v.cx_prefix ?? "", suffix: v.cx_suffix ?? "" } };
  }
  if (v.op === "math_scale") {
    //: ⚠️ **原样透传步骤列表**（含顺序）—— 后端按数组顺序逐步执行，
    //: 前端不做排序/归并/去零（`+0` 是用户显式配的，去掉会改变语义）。
    const steps = (v.steps ?? [])
      .filter((st) => st && st.op)
      .map((st) => ({ op: st.op, value: Number(st.value ?? 0) }));
    return { steps };
  }
  return {
    prefix: v.prefix ?? "",
    suffix: v.suffix ?? "",
    is_regex: !!v.is_regex,
  };
}

/** 把 `copy_extract` 的 `params.extract` **摊平**成表单字段（编辑回显用）。
 *
 *  ⚠️ 与 `paramsFromForm` 的 `copy_extract` 分支**互为逆运算** ——
 *  改一边必须改另一边，否则「存下去再打开」会丢参数（这类静默丢失最难查）。
 */
function cxFromParams(p: Record<string, unknown>): Pick<
  RuleFormValues,
  "cx_step" | "cx_sep" | "cx_index" | "cx_open" | "cx_close" | "cx_occurrence" | "cx_prefix" | "cx_suffix"
> {
  const ex = (p.extract ?? {}) as Record<string, unknown>;
  const step = (ex.op as "segment" | "between" | "strip" | undefined) ?? "segment";
  return {
    cx_step: step,
    cx_sep: ex.sep as string | undefined,
    cx_index: ex.index as number | undefined,
    cx_open: ex.open as string | undefined,
    cx_close: ex.close as string | undefined,
    cx_occurrence: ex.occurrence as number | undefined,
    cx_prefix: ex.prefix as string | undefined,
    cx_suffix: ex.suffix as string | undefined,
  };
}

function formFromRule(rule: DataPackageRule): RuleFormValues {
  const p = (rule.params ?? {}) as Record<string, unknown>;
  return {
    rule_name: rule.rule_name,
    op: rule.op,
    sep: p.sep as string | undefined,
    index: p.index as number | undefined,
    open: p.open as string | undefined,
    close: p.close as string | undefined,
    occurrence: p.occurrence as number | undefined,
    prefix: p.prefix as string | undefined,
    suffix: p.suffix as string | undefined,
    is_regex: !!p.is_regex,
    text: p.text as string | undefined,
    steps: Array.isArray(p.steps)
      ? (p.steps as MathStep[])
      : //: 兜底：没有 `steps` 时给一步「×1」（空列表后端会 400，不如给个能改的起点）。
        [{ op: "mul", value: 1 }],
    //: ── `copy_extract`：把嵌套的 `extract` 摊平回表单字段（编辑时回显）──
    ...cxFromParams(p),
    enabled: rule.enabled,
  };
}

export function DataPackageRulesModal({
  open,
  onClose,
  /** 当前表格里已有的列名（新增规则时给下拉，省得手打）。 */
  columnNames,
}: {
  open: boolean;
  onClose: () => void;
  columnNames?: string[];
}) {
  const { message, modal } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [sets, setSets] = useState<DataPackageRuleSet[]>([]);
  /** 每个包下的规则（懒加载：展开才拉）。 */
  const [rulesOf, setRulesOf] = useState<Record<number, DataPackageRule[]>>({});
  const [expanded, setExpanded] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);

  //: 规则包编辑
  const [setEditorOpen, setSetEditorOpen] = useState(false);
  const [editingSet, setEditingSet] = useState<DataPackageRuleSet | null>(null);
  const [setForm] = Form.useForm<{ name: string; remark: string }>();

  //: 规则编辑
  const [ruleEditorOpen, setRuleEditorOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<DataPackageRule | null>(null);
  const [ruleSetId, setRuleSetId] = useState<number | null>(null);
  const [ruleForm] = Form.useForm<RuleFormValues>();
  //: 按 op 动态渲染参数表单 —— 用 `Form.useWatch` 拿当前选中值。
  const watchingOp = Form.useWatch("op", ruleForm) as DataPackageRuleOp | undefined;
  //: ⚠️ `copy_extract` 第二步的**受控**监听 —— 换提取方式要换下面那组参数栏。
  const cxStep = Form.useWatch("cx_step", ruleForm) as
    | "segment" | "between" | "strip" | undefined;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setSets(await fetchDataPackageRuleSets());
    } catch (error) {
      void message.error(`加载规则包失败：${readReason(error)}`);
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    if (open) {
      void reload();
      setRulesOf({});
      setExpanded([]);
    }
  }, [open, reload]);

  /** 展开某包 ⇒ 拉它的规则（懒加载，避免一次拉全部包的全部规则）。 */
  const loadRules = useCallback(
    async (setId: number) => {
      try {
        const rows = await fetchDataPackageRules(setId);
        setRulesOf((prev) => ({ ...prev, [setId]: rows }));
      } catch (error) {
        void message.error(`加载规则失败：${readReason(error)}`);
      }
    },
    [message],
  );

  const onExpand = useCallback(
    (keys: boolean | readonly (string | number)[], record?: DataPackageRuleSet) => {
      const next = (Array.isArray(keys) ? keys : keys ? [keys] : []).map(Number);
      setExpanded(next);
      if (record && next.includes(record.id)) void loadRules(record.id);
    },
    [loadRules],
  );

  /* ---------------- 规则包 ---------------- */

  const openSetEditor = (row: DataPackageRuleSet | null) => {
    setEditingSet(row);
    setForm.setFieldsValue({ name: row?.name ?? "", remark: row?.remark ?? "" });
    setSetEditorOpen(true);
  };

  const submitSet = async () => {
    const values = await setForm.validateFields();
    setBusy(true);
    try {
      if (editingSet) {
        if (!hasPermission("xhs:rule:edit")) {
          message.warning("没有修改规则包的权限");
          return;
        }
        await updateDataPackageRuleSet(editingSet.id, values);
        message.success("已保存");
      } else {
        if (!hasPermission("xhs:rule:create")) {
          message.warning("没有新建规则包的权限");
          return;
        }
        await createDataPackageRuleSet(values);
        message.success("已新建规则包");
      }
      setSetEditorOpen(false);
      await reload();
    } catch (error) {
      void message.error(`保存失败：${readReason(error)}`);
    } finally {
      setBusy(false);
    }
  };

  /** ⚠️ **删包要二次确认并明示会连带删掉几条规则**（级联删除不可逆）。 */
  const removeSet = (row: DataPackageRuleSet) => {
    const count = rulesOf[row.id]?.length;
    modal.confirm({
      title: "删除规则包",
      content:
        count === undefined
          ? `将删除规则包「${row.name}」及其下的全部规则（不可撤销）。确定？`
          : `将删除规则包「${row.name}」及其下 ${count} 条规则（不可撤销）。确定？`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        const res = await deleteDataPackageRuleSet(row.id);
        message.success(
          res.deleted_rules
            ? `已删除规则包（连带 ${res.deleted_rules} 条规则）`
            : "已删除规则包",
        );
        await reload();
      },
    });
  };

  const toggleSet = async (row: DataPackageRuleSet) => {
    try {
      await updateDataPackageRuleSet(row.id, { enabled: !row.enabled });
      message.success(row.enabled ? "已停用（整包失效）" : "已启用");
      await reload();
    } catch (error) {
      void message.error(`操作失败：${readReason(error)}`);
    }
  };

  /* ---------------- 规则 ---------------- */

  const openRuleEditor = (setId: number, row: DataPackageRule | null) => {
    setRuleSetId(setId);
    setEditingRule(row);
    if (row) {
      ruleForm.setFieldsValue(formFromRule(row));
    } else {
      ruleForm.setFieldsValue({
        rule_name: "",
        op: "extract_segment",
        sep: "",
        index: 1,
        enabled: true,
      } as RuleFormValues);
    }
    setRuleEditorOpen(true);
  };

  const submitRule = async () => {
    const values = await ruleForm.validateFields();
    if (ruleSetId === null) return;
    setBusy(true);
    try {
      const payload = {
        rule_name: values.rule_name,
        op: values.op,
        params: paramsFromForm(values),
        enabled: values.enabled,
      };
      if (editingRule) {
        if (!hasPermission("xhs:rule:edit")) {
          message.warning("没有修改规则的权限");
          return;
        }
        await updateDataPackageRule(editingRule.id, payload);
        message.success("已保存");
      } else {
        if (!hasPermission("xhs:rule:create")) {
          message.warning("没有新建规则的权限");
          return;
        }
        await createDataPackageRule({ set_id: ruleSetId, ...payload });
        message.success("已新建规则");
      }
      setRuleEditorOpen(false);
      await loadRules(ruleSetId);
      //: 展开态与计数要跟着新数据走。
      await reload();
    } catch (error) {
      void message.error(`保存失败：${readReason(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const removeRule = (row: DataPackageRule) => {
    modal.confirm({
      title: "删除规则",
      content: `将删除规则「${row.rule_name}」（${row.summary}）。确定？`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        await deleteDataPackageRule(row.id);
        message.success("已删除");
        await loadRules(row.set_id);
        await reload();
      },
    });
  };

  const toggleRule = async (row: DataPackageRule) => {
    try {
      await updateDataPackageRule(row.id, { enabled: !row.enabled });
      message.success(row.enabled ? "已停用" : "已启用");
      await loadRules(row.set_id);
    } catch (error) {
      void message.error(`操作失败：${readReason(error)}`);
    }
  };

  return (
    <>
      <Modal
        open={open}
        title="数据操作规则"
        centered
        //: 用户口径：**全屏的 60%**（vw/vh，不用固定像素）。
        width="60vw"
        //: ⚠️ 用户口径「全屏的 60%」—— **不能用 `style.top`**：antd 的
        //: `centered` 是靠 `top: 50%` 实现的，显式 `style.top` 会顶掉它 ⇒ 假居中
        //: （项目有守卫 `test_every_modal_is_vertically_centered` 专门抓这个）。
        //: 高度用 `body` 的 `maxHeight` 控。
        styles={{
          body: { height: "60vh", maxHeight: "60vh", overflowY: "auto" },
        }}
        onCancel={onClose}
        footer={
          <Space>
            <Button onClick={onClose}>关闭</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openSetEditor(null)}>
              新建规则包
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="一个「规则包」对应一类数据包；包下的每条规则管一列。配好后在每列表头的设置图标里选它执行。"
        />
        {/* ⚠️ 2026-10-20：**不要用 `<Spin spinning={false}>` 包表格** —— antd 6 的 Spin 默认
            `spinning=true`，会给内容套 `.ant-spin-container` 并蒙一层半透明白底
            （用户实测：弹窗打开只有一片白色、表格内容几乎看不见）。
            等待状态已由下方 `<WaitMask>` 承担，这里只留表格本身。 */}
        <Table<DataPackageRuleSet>
            size="small"
            rowKey="id"
            dataSource={sets}
            pagination={false}
            locale={{ emptyText: <Empty description="还没有规则包，点右下角「新建规则包」" /> }}
            expandable={{
              expandedRowKeys: expanded,
              onExpand: (keys, record) =>
                onExpand(Array.isArray(keys) ? keys : keys ? [record.id] : [], record),
              expandedRowRender: (record) => (
                <RuleTable
                  rows={rulesOf[record.id]}
                  onAdd={() => openRuleEditor(record.id, null)}
                  onEdit={(row) => openRuleEditor(record.id, row)}
                  onDelete={removeRule}
                  onToggle={toggleRule}
                />
              ),
            }}
            columns={[
              {
                title: "规则包",
                dataIndex: "name",
                render: (name: string, row) => (
                  <Space size={6}>
                    <Text strong>{name}</Text>
                    {/* ⚠️ 父停用 ⇒ 整包失效（子行的状态不改写）。 */}
                    {!row.enabled && <Tag color="default">已停用</Tag>}
                  </Space>
                ),
              },
              {
                title: "备注",
                dataIndex: "remark",
                ellipsis: true,
                render: (v: string) => v || <Text type="secondary">-</Text>,
              },
              {
                title: "规则条数",
                key: "count",
                width: 100,
                render: (_: unknown, row) => {
                  const n = rulesOf[row.id]?.length;
                  return n === undefined ? (
                    <Text type="secondary">展开查看</Text>
                  ) : (
                    <Text>{n}</Text>
                  );
                },
              },
              {
                title: "状态",
                dataIndex: "enabled",
                width: 90,
                render: (enabled: boolean) =>
                  enabled ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
              },
              {
                title: "操作",
                key: "op",
                width: 210,
                render: (_: unknown, row) => (
                  <Space size={4}>
                    {/* 操作列的「启动/停用」（用户明确要求）。 */}
                    <Button size="small" type="link" onClick={() => void toggleSet(row)}>
                      {row.enabled ? "停用" : "启动"}
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      icon={<EditOutlined />}
                      onClick={() => openSetEditor(row)}
                    >
                      编辑
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => removeSet(row)}
                    />
                  </Space>
                ),
              },
            ]}
          />
        <WaitMask open={loading} scope="panel" title="正在加载规则" />
      </Modal>

      {/* ---------------- 规则包编辑 ---------------- */}
      <Modal
        open={setEditorOpen}
        title={editingSet ? "编辑规则包" : "新建规则包"}
        centered
        width="40vw"
        onCancel={() => setSetEditorOpen(false)}
        onOk={() => void submitSet()}
        okText="保存"
        cancelText="取消"
        confirmLoading={busy}
        destroyOnHidden
      >
        <Form form={setForm} layout="vertical">
          <Form.Item
            name="name"
            label="规则包名"
            rules={[{ required: true, message: "请填写规则包名" }]}
            extra="同一账号下不能重名。建议用数据包类型命名，如「店透SKU预览」。"
          >
            <Input placeholder="如：店透SKU预览" maxLength={100} />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} maxLength={500} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      {/* ---------------- 规则编辑 ---------------- */}
      <Modal
        open={ruleEditorOpen}
        title={editingRule ? "编辑规则" : "新建规则"}
        centered
        width="45vw"
        onCancel={() => setRuleEditorOpen(false)}
        onOk={() => void submitRule()}
        okText="保存"
        cancelText="取消"
        confirmLoading={busy}
        destroyOnHidden
      >
        <Form form={ruleForm} layout="vertical">
          <Form.Item
            name="rule_name"
            label="规则名称"
            rules={[{ required: true, message: "请填写规则名称" }]}
            extra="给这条规则起个名，同一规则包内不可重复。规则不绑定列，用在哪列由你在列表头选。"
          >
            <Input placeholder="如：取颜色分类" maxLength={100} />
          </Form.Item>
          <Form.Item name="op" label="操作" rules={[{ required: true }]}>
            <Select
              options={OP_IMPLEMENTED.map((op) => ({ label: dictLabel("xhs_package_rule_op", op, OP_LABELS), value: op }))}
              //: 换 op ⇒ 清掉另一套参数，免得带着脏值保存。
              onChange={() => ruleForm.setFieldsValue({} as RuleFormValues)}
              //: ⚠️ 新建时才让选 op；编辑时锁住（换 op 等于换规则语义，应新建一条）。
              disabled={!!editingRule}
            />
          </Form.Item>

          {/* 按 op **动态渲染**参数（不一次性摆所有字段）。 */}
          {watchingOp === "extract_segment" && (
            <>
              <Form.Item
                name="sep"
                label="分隔符"
                rules={[{ required: true, message: "请填写分隔符" }]}
                extra="真机例子：包1 用「;」、包2 用「:」。"
              >
                <Input placeholder="如：;" maxLength={8} />
              </Form.Item>
              <Form.Item
                name="index"
                label="取第几段"
                rules={[{ required: true, message: "请填写第几段" }]}
                extra="从 1 开始数；填负数表示从后往前（-1 = 最后一段）。"
              >
                <InputNumber style={{ width: 160 }} />
              </Form.Item>
            </>
          )}
          {watchingOp === "extract_between" && (
            <>
              <Form.Item
                name="open"
                label="起始符号"
                rules={[{ required: true, message: "请填写起始符号" }]}
              >
                <Input placeholder="如：(" maxLength={8} />
              </Form.Item>
              <Form.Item name="close" label="结束符号" extra="留空表示取到该行末尾。">
                <Input placeholder="如：)" maxLength={8} />
              </Form.Item>
              <Form.Item name="occurrence" label="第几处" extra="默认第 1 处。">
                <InputNumber min={1} style={{ width: 160 }} />
              </Form.Item>
            </>
          )}
          {watchingOp === "extract_strip" && (
            <>
              <Form.Item name="prefix" label="去前缀">
                <Input placeholder="如：SKU图_01_" maxLength={200} />
              </Form.Item>
              <Form.Item name="suffix" label="去后缀">
                <Input placeholder="如：.jpg" maxLength={200} />
              </Form.Item>
              <Form.Item
                name="is_regex"
                valuePropName="checked"
                extra="默认按字面匹配。前缀里含序号（每个文件不同）时才需要打开正则。"
              >
                <Checkbox>按正则匹配</Checkbox>
              </Form.Item>
            </>
          )}

          {watchingOp === "fixed_text" && (
            <Form.Item
              name="text"
              label="固定文本"
              rules={[{ required: true, message: "请填写固定文本" }]}
              extra="整列都会变成这个值 —— 原本为空的行也会被写上。"
            >
              <Input placeholder="如：观隅好物" maxLength={200} />
            </Form.Item>
          )}
          {watchingOp === "math_scale" && (
            <>
              {/* **步骤列表**：加 / 减 / 乘**可随意搭配、不限步数**，**按顺序依次执行**
                  （用户口径 2026-10-15：「计算操作类型的规则，加减乘可随意搭配」）。
                  ⚠️ 顺序是语义的一部分（`+10` 后 `×2` ≠ `×2` 后 `+10`）⇒ 不排序。 */}
              <Form.List
                name="steps"
                rules={[
                  {
                    validator: async (_rule, value) => {
                      if (!value || value.length === 0) {
                        throw new Error("至少加一步运算（如 ×1.7）");
                      }
                    },
                  },
                ]}
              >
                {(fields, { add, remove }, { errors }) => (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ marginBottom: 6 }}>
                      <Text style={{ fontSize: 13 }}>运算步骤</Text>
                      <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                        按从上到下的顺序依次执行
                      </Text>
                    </div>
                    {fields.map((field, index) => (
                      <Space key={field.key} align="center" style={{ marginBottom: 6, display: "flex" }}>
                        <Text type="secondary" style={{ fontSize: 12, width: 34 }}>
                          {`第 ${index + 1} 步`}
                        </Text>
                        <Form.Item
                          name={[field.name, "op"]}
                          rules={[{ required: true, message: "选运算符" }]}
                          style={{ marginBottom: 0 }}
                        >
                          <Select
                            style={{ width: 110 }}
                            options={[
                              { label: "乘 ×", value: "mul" },
                              { label: "加 +", value: "add" },
                              { label: "减 −", value: "sub" },
                            ]}
                          />
                        </Form.Item>
                        <Form.Item
                          name={[field.name, "value"]}
                          rules={[{ required: true, message: "填数值" }]}
                          style={{ marginBottom: 0 }}
                        >
                          <InputNumber style={{ width: 140 }} step={0.1} placeholder="数值" />
                        </Form.Item>
                        {/* ⚠️ 只剩一步时不给删（空列表后端会 400，且界面没有任何可操作项）。 */}
                        <Button
                          type="text"
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          disabled={fields.length <= 1}
                          onClick={() => remove(field.name)}
                        />
                      </Space>
                    ))}
                    <Button
                      type="dashed"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => add({ op: "mul", value: 1 })}
                    >
                      加一步
                    </Button>
                    <Form.ErrorList errors={errors} />
                  </div>
                )}
              </Form.List>
              {/* ⚠️ 计算基础列**不在这里选** —— 用户口径：在**列表头的设置图标
                  tip 弹窗**里指定（那边才知道当前是哪一列，默认当前列、
                  下拉只列数值列）。管理弹窗里配的是「怎么算」，不是「算哪列」。 */}
              <Text type="secondary" style={{ fontSize: 12 }}>
                计算结果写到哪一列、按哪一列算：在商品 SKU 表格的列头设置图标里选
                （默认按当前列算，可换成其它数值列）。
              </Text>
            </>
          )}

          {/* ── 复制 + 提取（**两步**，用户口径 2026-10-15）──
              「新增一个复制加提取规则，分为多步执行，第一步规则是将指定列复制到当前列，
                第二步是将复制过来的列值按照规则提取结果覆盖当前列」。
              ⚠️ **两步分块渲染**（不挤成一行）：用户口径「分块展示」——
                第一步说清「复制哪一列在 SKU 表格列头里选」，这里只配第二步。 */}
          {watchingOp === "copy_extract" && (
            <>
              <Form.Item label="第一步：复制哪一列">
                <Text type="secondary" style={{ fontSize: 12 }}>
                  在商品 SKU 表格的列头设置图标里选（默认复制当前列）。
                </Text>
              </Form.Item>

              <Form.Item
                name="cx_step"
                label="第二步：从复制来的值里提取"
                initialValue="segment"
              >
                <Select
                  options={[
                    { label: "按分隔符取第 N 段", value: "segment" },
                    { label: "取包裹符之间的内容", value: "between" },
                    { label: "去前缀 / 去后缀", value: "strip" },
                  ]}
                />
              </Form.Item>

              {cxStep === "segment" && (
                <>
                  <Form.Item name="cx_sep" label="分隔符" tooltip="如 ; 或 ,">
                    <Input placeholder="如 ;" />
                  </Form.Item>
                  <Form.Item name="cx_index" label="取第几段" initialValue={1}>
                    <InputNumber style={{ width: "100%" }} />
                  </Form.Item>
                </>
              )}

              {cxStep === "between" && (
                <>
                  <Form.Item name="cx_open" label="起始符号">
                    <Input placeholder="如 (" />
                  </Form.Item>
                  <Form.Item name="cx_close" label="结束符号" tooltip="留空 = 取到行尾">
                    <Input placeholder="如 )（留空则取到末尾）" />
                  </Form.Item>
                  <Form.Item name="cx_occurrence" label="第几处" initialValue={1}>
                    <InputNumber min={1} style={{ width: "100%" }} />
                  </Form.Item>
                </>
              )}

              {cxStep === "strip" && (
                <>
                  <Form.Item name="cx_prefix" label="去前缀" tooltip="如 颜色分类:">
                    <Input placeholder="如 颜色分类:" />
                  </Form.Item>
                  <Form.Item name="cx_suffix" label="去后缀">
                    <Input placeholder="如 .jpg" />
                  </Form.Item>
                </>
              )}
            </>
          )}

          <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

/** 某包下的规则表（展开区里渲染）。 */
function RuleTable({
  rows,
  onAdd,
  onEdit,
  onDelete,
  onToggle,
}: {
  rows?: DataPackageRule[];
  onAdd: () => void;
  onEdit: (row: DataPackageRule) => void;
  onDelete: (row: DataPackageRule) => void;
  onToggle: (row: DataPackageRule) => void;
}) {
  if (!rows) return <Spin size="small" />;
  return (
    <div style={{ paddingLeft: 24 }}>
      <Table<DataPackageRule>
        size="small"
        rowKey="id"
        dataSource={rows}
        pagination={false}
        locale={{
          emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这个包下还没有规则" />,
        }}
        columns={[
          { title: "规则名称", dataIndex: "rule_name", width: 180 },
          {
            title: "取值方式",
            dataIndex: "summary",
            //: ⚠️ 显示**人话摘要**，不显示 JSON。
            render: (summary: string) => <Text>{summary}</Text>,
          },
          {
            title: "状态",
            dataIndex: "enabled",
            width: 90,
            render: (enabled: boolean) =>
              enabled ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
          },
          {
            title: "操作",
            key: "op",
            width: 190,
            render: (_: unknown, row) => (
              <Space size={4}>
                <Button size="small" type="link" onClick={() => onToggle(row)}>
                  {row.enabled ? "停用" : "启动"}
                </Button>
                <Button
                  size="small"
                  type="link"
                  icon={<EditOutlined />}
                  onClick={() => onEdit(row)}
                />
                <Button
                  size="small"
                  type="link"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => onDelete(row)}
                />
              </Space>
            ),
          },
        ]}
      />
      <Tooltip title="为该规则包新增一条具名规则">
        <Button
          size="small"
          type="dashed"
          icon={<PlusOutlined />}
          style={{ marginTop: 6 }}
          onClick={onAdd}
        >
          新增规则
        </Button>
      </Tooltip>
    </div>
  );
}

/** 从 axios / 后端结构化错误里取人话（后端把 `{code, message}` 放在 `detail`）。 */
function readReason(error: unknown): string {
  const anyErr = error as {
    response?: { data?: { detail?: { message?: string } | string } };
    message?: string;
  };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return anyErr?.message || "请稍后重试";
}
