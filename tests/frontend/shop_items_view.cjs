/**
 * 行为验证：商品列表本地化改造的**纯逻辑**（设计 13 §30 / §33）。
 *
 * 盯的八件事（都是「不许出现某种行为」，源码断言证明不了）：
 *
 * 1. **账号下拉不按类型过滤**：pc 类型的小红书账号必须出现在候选里 ——
 *    门禁归后端（未绑店 → 400），前端按 `sub_type` 预判就是第二份真相源；
 * 2. **选项标签只显示账号名称**（2026-09-24 用户口径：「所有账号的下拉，只展示
 *    账号名称」）：类型后缀（v1.32 已去）、店铺标注、`(#id)` 都不再进 label ——
 *    账号名是用户认账号的唯一依据，其余信息该由页面主体回答；
 * 3. **审核态筛选有安静说明**：平台列表响应不含 card_type（真机 1974 条 0 命中），
 *    四个审核态本地无法区分 ⇒ 一行说明，不假装能筛；
 * 4. **SKU 缺值渲染 `-` 不渲染 0**：「读不到」≠「读到 0」（硬约束⑤）；
 * 5. **只有「上架时间」可排、且默认不排序**（§33.3 / §33.11）：进来没有箭头，
 *    点一下才排（升 → 降 → **取消**）；非上架时间的列被点也不给排序 ——
 *    表头有箭头 = 后端真会按它排，多一个箭头就多一份误导；
 * 6. **前置校验 = 零门禁**（ADR-021 + 2026-09-30 用户口径「商品列表页查询本地数据库，
 *    无需校验千帆是否可用」）：本页是纯本地台账读 ⇒ **没有任何拦截**，`ok` 恒为 true；
 *    ark 是否可用只作为**附加信号**（`reason: "ark_unavailable"`）回给调用方
 *    （页面据此加一行说明 / 禁用「同步商品」按钮），**不拦列表请求**。
 *    * 演进记录（原句保留）：*「§33.4：没绑店铺 / 千帆确定不可用 ⇒ 不通过（页面因此
 *      零请求）；`unknown`（未检测）必须放行」* —— **被上面这条取代**
 *      （本地读免门禁；`unbound` 取值随 `shops` 退场不再产生）；
 * 7. **挂链笔记数筛选**（§33.8）：比较符默认「小于」，**留空 = 不筛** ——
 *    兜成「小于 0」会让一进页面就把有挂链笔记的商品全筛掉（缺省态不许筛数据）；
 * 8. **批量删除的前置判据**（§33.10）：勾选去重 / 没勾拦住 / 超上限拦住（**不静默
 *    截断**）；确认框正文必须把「关系一并删」「同步会写回」两个连带后果说出来。
 *
 * 做法与 `sync_gates.cjs` 同源：esbuild 打包**真实** `lib/shop-items-view.ts`，
 * 直接调用它（纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/shop_items_view.cjs                  # 期望全 PASS
 *   SHOP_ITEMS_MUTATE=typefilter <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=labelsuffix <node> …                     # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=hint       <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=skuzero    <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=sortany    <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=sortdefault <node> …                     # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=gateoff    <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=noteop     <node> …                      # 期望 FAIL
 *   SHOP_ITEMS_MUTATE=delmax     <node> …                      # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/shop-items-view.ts");

const MUTATE = process.env.SHOP_ITEMS_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的
// 表现是「变异静默不生效」，脚本照常全绿，比变红更危险）。
const RESERVED = {
  typefilter: 'return accounts.filter((account) => account.platform === "xhs");',
  labelsuffix: '  return (nickname || "").trim() || "未命名账号";',  hint: '    return "本地台账暂无法区分审核态，该筛选查不到数据";',
  skuzero: '  return value === null || value === undefined ? "-" : String(value);',
  sortany:
    'export const SORT_FIELD_BY_COLUMN: Record<string, string> = {\n  on_shelf_time: "on_shelf_time",\n};',
  sortdefault:
    '  if (!field || (order !== "ascend" && order !== "descend")) {\n    return null;\n  }',
  gateoff: '  if (channelUnavailable(channels, "ark")) {',
  noteop:
    '  if (value === null || value === undefined || Number.isNaN(Number(value))) {\n    return {};\n  }',
  delmax: '  if (ids.length > MAX_BATCH_DELETE) {\n    return "too_many";\n  }',
};

const MUTATE_TO = {
  // 把类型过滤加回来 ⇒ 前端预判「哪类账号有店铺」（第二份真相源）
  typefilter:
    'return accounts.filter((account) => account.platform === "xhs" && account.sub_type === "creator");',
  // 标签把店铺后缀加回来 ⇒ 「只展示账号名称」的口径被破坏（同一关系的第二处展示）。
  // ⚠️ 变异体必须**自洽**（自己声明 shop）：早期版本直接用 `shop` 变量，
  //    抛 ReferenceError 而不是断言失败 —— 脚本确实「变红」了，但红在崩溃而不是
  //    红在断言，等于守卫有没有承重根本看不出来。
  labelsuffix:
    '  const shop = (_shopName || "").trim();\n  return shop ? `${(nickname || "").trim() || "未命名账号"} · ${shop}` : `${(nickname || "").trim() || "未命名账号"} · 未绑店铺`;',
  // 审核态不再给说明 ⇒ 用户选了查不到还以为系统坏
  hint: '    return "";',
  // 缺值渲染 0 ⇒ 把「读不到」说成「读到 0」
  skuzero: '  return value === null || value === undefined ? "0" : String(value);',
  // 把「只有上架时间可排」放开成多列 ⇒ 表头出现一个后端**不会**照办的箭头
  sortany:
    'export const SORT_FIELD_BY_COLUMN: Record<string, string> = {\n  on_shelf_time: "on_shelf_time",\n  price_min_fen: "price_min_fen",\n};',
  // 「取消排序」兜成某个默认序（v1.36 之前的老行为）⇒ 用户点了第三下箭头还挂着，
  //   而列表已经换了序 —— 界面说的和实际排的对不上
  sortdefault:
    '  if (!field || (order !== "ascend" && order !== "descend")) {\n    return { field: "on_shelf_time", order: "descend" };\n  }',
  // 门禁短路 ⇒ 未绑店铺 / 令牌失效也放行，页面又变成「发一个必然 400 的请求 + 一串错误」
  // ⚠️ ADR-021 + 2026-09-30 零门禁语义后本变异仍然承重，但测的东西变了：
  //    它不再测「门禁有没有拦请求」（`ok` 本来恒真），而是测 `ark_unavailable`
  //    这个**附加信号**有没有如实产出 —— 短路掉这一段，页面在 ark 失效时就
  //    少一行说明、也没法禁用「同步商品」按钮（信号丢失，不是拦截失效）。
  gateoff: "  if (false) {",
  // 留空兜成「小于 0」⇒ 一进页面就把有挂链笔记的商品全筛掉（缺省态偷偷筛数据）
  noteop:
    '  if (value === null || value === undefined || Number.isNaN(Number(value))) {\n    return { note_count_op: DEFAULT_NOTE_COUNT_OP, note_count: 0 };\n  }',
  // 上限检查失效 ⇒ 用户以为拦住了，其实发出去（后端才 400）：界面承诺落空
  delmax: "  if (false) {\n    return \"too_many\";\n  }",
};

const mutatePlugin = {
  name: "shop-items-view-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]shop-items-view\.ts$/ }, (args) => {
      let source = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
      if (MUTATE) {
        const needle = RESERVED[MUTATE];
        if (!needle) throw new Error(`未知变异：${MUTATE}`);
        if (!source.includes(needle)) {
          throw new Error(`变异锚点未命中（实现已改？）：${needle}`);
        }
        source = source.replace(needle, MUTATE_TO[MUTATE]);
      }
      return { contents: source, loader: "ts" };
    });
  },
};

let passed = 0;
let failed = 0;

function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
    console.log(`PASS  ${label}`);
  } else {
    failed += 1;
    console.log(
      `FAIL  ${label}\n      期望：${JSON.stringify(expected)}\n      实际：${JSON.stringify(actual)}`
    );
  }
}

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "shop-items-view-"));
  const bundled = await esbuild.build({
    entryPoints: [MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [mutatePlugin],
  });
  const outFile = path.join(tmpDir, "shop-items-view.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const {
    selectableAccounts,
    accountLabel,
    statusFilterHint,
    skuCell,
    UNDERIVABLE_STATUSES,
    resolveSort,
    SORT_FIELD_BY_COLUMN,
    DEFAULT_SORT,
    ledgerPrerequisite,
    QIANFAN_PREREQUISITE_HINT,
    NOTE_COUNT_OPS,
    DEFAULT_NOTE_COUNT_OP,
    noteCountFilter,
    MAX_BATCH_DELETE,
    normalizeDeleteIds,
    batchDeleteBlock,
    batchDeleteHint,
  } = mod;

  if (typeof selectableAccounts !== "function") {
    console.error("FATAL 未导出 selectableAccounts —— 若已重命名，请同步更新本脚本");
    return 1;
  }

  // A：账号下拉不按类型过滤（v1.32 用户口径）
  const accounts = [
    { id: 1, nickname: "观隅好物", sub_type: "creator", platform: "xhs" },
    { id: 2, nickname: "我的PC号", sub_type: "pc", platform: "xhs" },
    { id: 3, nickname: "别的平台", sub_type: "oasis", platform: "oasis" },
  ];
  check("A1 pc 类型的小红书账号也在候选里", selectableAccounts(accounts).map((a) => a.id), [1, 2]);
  check("A2 非小红书账号被滤掉", selectableAccounts(accounts).some((a) => a.platform !== "xhs"), false);

  // B：选项标签**只显示账号名称**（2026-09-24 用户口径）
  check("B1 标签就是账号名称", accountLabel("观隅好物", "观隅好物的店"), "观隅好物");
  check("B2 店铺标注不再进标签", accountLabel("观隅好物", ""), "观隅好物");
  check("B3 空昵称有兜底", accountLabel("", "店"), "未命名账号");
  check("B4 名称首尾空白被裁掉", accountLabel("  观隅好物  ", "店"), "观隅好物");

  // C：审核态四相的安静说明（平台不回 card_type，本地无法区分）
  check("C1 审核态给说明", UNDERIVABLE_STATUSES.map(statusFilterHint).every((t) => t.length > 0), true);
  check("C2 可推导状态不噪声", [statusFilterHint("on_shelf"), statusFilterHint("all"), statusFilterHint("off_shelf"), statusFilterHint("frozen")], ["", "", "", ""]);

  // D：SKU 单元格缺值渲染 `-`，0 是 0
  check("D1 缺值是 -", [skuCell(null), skuCell(undefined)], ["-", "-"]);
  check("D2 0 是 0", skuCell(0), "0");
  check("D3 正常值", skuCell(341), "341");

  // E：只有「上架时间」可排，且**默认不排序**（§33.3 / §33.11，用户 2026-09-23 上机口径
  //    「默认不排序，只有点击触发后进行排序，参考 antd 组件中的排序」）
  check("E1 可排的列只有上架时间", Object.keys(SORT_FIELD_BY_COLUMN), ["on_shelf_time"]);
  check("E2 默认**无**排序（进来不带箭头、请求里也不带 sort_field）", DEFAULT_SORT, null);
  check(
    "E3 上架时间列可升可降",
    [resolveSort("on_shelf_time", "ascend"), resolveSort("on_shelf_time", "descend")],
    [
      { field: "on_shelf_time", order: "ascend" },
      { field: "on_shelf_time", order: "descend" },
    ]
  );
  check(
    "E4 第三下「取消排序」回到无排序（antd 三级循环，不回某个默认序）",
    [resolveSort("on_shelf_time", null), resolveSort(undefined, undefined)],
    [null, null]
  );
  check(
    "E5 非白名单列被点也不给排序（表头没箭头的列不许改顺序）",
    [
      resolveSort("price_min_fen", "ascend"),
      resolveSort("stock", "descend"),
      resolveSort("sales_30d", "ascend"),
      resolveSort("title", "ascend"),
    ],
    [null, null, null, null]
  );

  // F：前置校验（**ADR-021 + 2026-09-30 用户口径后的零门禁语义**）
  //   ⚠️ 用户原话：「商品列表（千帆）页查询的本地数据库，无需校验千帆是否可用」——
  //     本页是纯本地台账读（后端 `/items` 等端点已去门禁），所以**没有任何前置拦截**：
  //     `ok` 恒为 true。ark 是否可用**不影响放行**，只作为附加信息回给调用方
  //     （页面据此加一行说明 / 禁用「同步商品」按钮），所以这里钉的是
  //     「ok 恒真 + reason 如实反映 ark 可用性」两件事。
  const arkChannel = (state) => ({ ark: { state, reason: "", probed: false, label: "千帆商家后台" } });
  check("F1 未登录（ark missing）仍放行（本地读免门禁）", ledgerPrerequisite(arkChannel("missing")), {
    ok: true,
    reason: "ark_unavailable",
  });
  check("F2 ark 过期（expired）仍放行", ledgerPrerequisite(arkChannel("expired")), {
    ok: true,
    reason: "ark_unavailable",
  });
  check(
    "F3 非登录态（missing / guest）一律放行，reason 如实标注",
    [ledgerPrerequisite(arkChannel("missing")), ledgerPrerequisite(arkChannel("guest"))],
    [
      { ok: true, reason: "ark_unavailable" },
      { ok: true, reason: "ark_unavailable" },
    ]
  );
  check(
    "F4 unknown / 未提供 channels 都放行（DEF-057：没问到 ≠ 拦住；undefined 也不许拦）",
    [ledgerPrerequisite(arkChannel("unknown")), ledgerPrerequisite(undefined)],
    [
      { ok: true, reason: "" },
      { ok: true, reason: "ark_unavailable" },
    ]
  );
  check("F5 ark 可用 → 放行且无附加信息", ledgerPrerequisite(arkChannel("valid")), {
    ok: true,
    reason: "",
  });
  check(
    "F6 提示文案仍在（页面在 ark 失效时用它做一行说明，不是拦截文案）",
    QIANFAN_PREREQUISITE_HINT === "请在账号信息卡片中扫码登录千帆后再来查看。",
    true
  );

  // G：挂链笔记数筛选（§33.8，用户 2026-09-23 口径「默认小于指定篇数」）
  check("G1 默认比较符是「小于」", DEFAULT_NOTE_COUNT_OP, "lt");
  check(
    "G2 比较符顺序：小于排第一、白名单五个",
    NOTE_COUNT_OPS,
    ["lt", "lte", "eq", "gte", "gt"]
  );
  check(
    "G3 留空 = 不筛（不许兜成「小于 0」把数据筛掉）",
    [noteCountFilter(undefined, undefined), noteCountFilter("lt", null), noteCountFilter("gt", undefined)],
    [{}, {}, {}]
  );
  check(
    "G4 填了值就带上比较符与篇数",
    [noteCountFilter("lt", 3), noteCountFilter("gt", 0)],
    [
      { note_count_op: "lt", note_count: 3 },
      { note_count_op: "gt", note_count: 0 },
    ]
  );
  check(
    "G5 非白名单比较符落到默认（后端另有 400 硬闸）",
    noteCountFilter("whatever", 2),
    { note_count_op: "lt", note_count: 2 }
  );
  check("G6 篇数取整且不为负", [noteCountFilter("eq", 2.7), noteCountFilter("eq", -5)], [
    { note_count_op: "eq", note_count: 2 },
    { note_count_op: "eq", note_count: 0 },
  ]);

  // H：批量删除（v1.36，用户 2026-09-23 口径「商品列表新增批量删除操作」）
  check("H1 勾选键去重保序、去空白、丢空串", normalizeDeleteIds(["B", "A", "B", " ", "", "C"]), [
    "B",
    "A",
    "C",
  ]);
  check("H2 没勾选 → 拦住（不是静默发一个空请求）", batchDeleteBlock([]), "empty");
  check(
    "H3 超过上限 → 拦住（不许静默截断成 200 条）",
    batchDeleteBlock(Array.from({ length: MAX_BATCH_DELETE + 1 }, (_, i) => `I-${i}`)),
    "too_many"
  );
  check(
    "H4 上限内 → 放行（边界值本身要放行）",
    batchDeleteBlock(Array.from({ length: MAX_BATCH_DELETE }, (_, i) => `I-${i}`)),
    ""
  );
  check("H5 上限与后端同值（200）", MAX_BATCH_DELETE, 200);
  check(
    "H6 确认框正文把两个连带后果都说了（关系一并删 / 同步会写回）",
    [/2 个商品行/, /关联关系一并删除/, /重新写回台账/].map((re) => re.test(batchDeleteHint(2))),
    [true, true, true]
  );

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  return failed === 0 ? 0 : 1;
}

main().then((code) => process.exit(code));
