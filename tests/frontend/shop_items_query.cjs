/**
 * 行为验证：商品列表的**请求构造**（`lib/shop-items-query.ts`，DEF-093）。
 *
 * 盯的是「**列表与导出必须是同一个集合**」这件事 —— 它不是靠人读代码保证的，
 * 而是靠「两个调用方拿到同一个对象」保证的。源码断言证明不了这一点
 * （DEF-093 的根因正是源码看起来两边都「对」），所以这里 esbuild 打包**真实模块**
 * 直接调它，把两个形态的**键集合**摆出来比对。
 *
 * 九条判据：
 *
 * 1. **导出形态的键 ⊆ 后端 `ItemsExportRequest` 字段**（跨层契约，键名逐字一致）；
 * 2. **列表形态的键 ⊆ 后端 `ShopItemsRequest` 字段**；
 * 3. 导出与列表在**同一表单值**下，除 `page_no` / `page_size` 外**逐字段相同**
 *    （⇒「导出的 CSV 行数 == 列表命中总数」的前端侧条件）；
 * 4. 日期键名必须是 `create_time_from` / `create_time_to`（**不是**缓存键
 *    `create_from` / `create_to` —— DEF-093 就是这两个名字混用）；
 * 5. 挂链笔记数**留空 = 不带参数**（不是「小于 0」）；
 * 6. 无排序态**不带** `sort_field` / `order`（让后端用自己的稳定默认序）；
 * 7. 关键词首尾空白被裁掉；空关键词 ⇒ 不带（后端按空串处理）；
 * 8. 导出形态的键清单**冻结**（15 个）—— 加字段必须回来改这份清单，
 *    强制读者去核对后端模型（第 1/2 条会同时红）。
 *
 * ⚠️ 原「⑦ 分组哨兵」一条**已随分组退场删除**（2026-10-05 用户口径「彻底移除商品分组
 * 相关逻辑」）—— `group_id` 不再是本模块的参数，那条判据的载体不存在了
 * （与 `test_shop_items_query_contract.py` 的 `groupsentinel` 变异一起走）。
 *
 * ⚠️ 第 1/2 条需要后端模型字段表，由 `tests/backend/test_shop_items_query_contract.py`
 * 把它从 Pydantic 里取出来、经 `--models` 传进来（node 不 import 后端）。
 * 不带 `--models` 时这两条**跳过**（其余七条照跑），便于单独手工执行。
 *
 * 运行：
 *   <node> tests/frontend/shop_items_query.cjs
 *   <node> tests/frontend/shop_items_query.cjs --models '{"list":[...],"export":[...]}'
 *   <node> tests/frontend/shop_items_query.cjs --dump           # 只打印 JSON（给 pytest 用）
 *   SHOP_ITEMS_QUERY_MUTATE=draftkeys <node> …                 # 期望 FAIL（DEF-093 回退）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/shop-items-query.ts");

const argv = process.argv.slice(2);
const DUMP = argv.includes("--dump");
const modelsIdx = argv.indexOf("--models");
const MODELS = modelsIdx >= 0 ? JSON.parse(argv[modelsIdx + 1]) : null;

const MUTATE = process.env.SHOP_ITEMS_QUERY_MUTATE || "";

/**
 * 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的表现是
 * 「变异静默不生效」，脚本照常全绿，比变红更危险）。
 */
const RESERVED = {
  // ① 退回 DEF-093 的老行为：日期用**缓存草稿的键名**发出去
  draftkeys:
    '    create_time_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : undefined,\n    create_time_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : undefined,',
  // ② 导出也带上分页 ⇒ 导出的只是「一页」，不是列表命中总数
  exportpaged: "    ...(pageNo !== undefined ? { page_no: pageNo } : {}),",
  // ③ 留空兜成「小于 0」⇒ 一进页面就把有挂链笔记的商品全筛掉
  noteop: "    ...noteCountFilter(values.note_count_op, values.note_count),",
  // ④ 无排序态兜一个默认序 ⇒ 前端替后端做主（第二份真相源）
  sortdefault: "    ...(sortArg\n      ? { sort_field: sortArg.field, order: sortArg.order === \"ascend\" ? \"asc\" : \"desc\" }\n      : {}),",
  // ⑤ 分组哨兵不再换算 ⇒ 「全部分组」也把 -1 发出去（后端只认 >= 0，这是非法值）
};

const MUTATE_TO = {
  // ① 发出缓存键名（后端 `extra="forbid"` ⇒ 真机上 422）
  draftkeys:
    '    create_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : undefined,\n    create_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : undefined,',
  // ② 导出也分页（无条件带上 ⇒ 导出的只是「一页」，不是列表命中总数）
  exportpaged: "    ...{ page_no: pageNo ?? 1, page_size: pageSize ?? 20 },",
  // ③ 兜成 0（留空也发 note_count=0 ⇒ 缺省态偷偷筛数据）
  noteop: "    ...[{ note_count_op: \"lt\", note_count: 0 }],",
  // ④ 兜默认序
  sortdefault:
    '    ...(sortArg\n      ? { sort_field: sortArg.field, order: sortArg.order === "ascend" ? "asc" : "desc" }\n      : { sort_field: "create_time", order: "desc" }),',
};

const mutatePlugin = {
  name: "shop-items-query-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]shop-items-query\.ts$/ }, (args) => {
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
const results = [];

function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
    results.push(`PASS  ${label}`);
  } else {
    failed += 1;
    results.push(
      `FAIL  ${label}\n      期望：${JSON.stringify(expected)}\n      实际：${JSON.stringify(actual)}`
    );
  }
}

/** 表单全填（含日期区间、挂链笔记、排序、分组）—— 覆盖每个字段。 */
const FULL_FORM = {
  keyword: "  桌垫  ",
  status: "on_shelf",
  price_from: 10,
  price_to: 200,
  stock_from: 5,
  stock_to: 1000,
  sales_from: 1,
  sales_to: 500,
  create_time_range: [null, null],
  note_count_op: "gte",
  note_count: 2,
};

/** 表单全空（后端应看到「只有必填 + 默认」的最小集合）。 */
const EMPTY_FORM = { status: "on_shelf" };

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "shop-items-query-"));
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
  const outFile = path.join(tmpDir, "shop-items-query.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const { formValuesToQuery } = require(outFile);

  if (typeof formValuesToQuery !== "function") {
    console.error("FATAL 未导出 formValuesToQuery —— 若已重命名，请同步更新本脚本");
    return 1;
  }

  // 日期区间：用**真实**区间跑一遍（`FULL_FORM` 里是 null，单独造一个）
  const dayjs = require(path.join(PROJECT, "frontend/node_modules/dayjs"));
  const datedForm = {
    ...FULL_FORM,
    create_time_range: [dayjs("2026-01-02"), dayjs("2026-03-04")],
  };

  const list = formValuesToQuery(datedForm, {
    accountId: 8,
    pageNo: 3,
    pageSize: 50,
    sort: { field: "on_shelf_time", order: "descend" },
  });
  const exported = formValuesToQuery(datedForm, {
    accountId: 8,
    sort: { field: "on_shelf_time", order: "descend" },
  });
  const emptyExport = formValuesToQuery(EMPTY_FORM, { accountId: 8 });

  const listKeys = Object.keys(list).sort();
  const exportKeys = Object.keys(exported).sort();

  if (DUMP) {
    console.log(
      JSON.stringify({
        listKeys,
        exportKeys,
        list,
        exported,
        emptyExport,
      })
    );
    return 0;
  }

  //: 「**不会发出去**」的统一判据：axios / JSON 都会丢掉 `undefined` 值的键 ⇒
  //: 判「值为 undefined」而不是 `in`（后者会把正确实现判红，实测踩到过）。
  const absent = (obj, keys) => keys.filter((k) => obj[k] !== undefined);

  // ── ③ 导出与列表：除分页外逐字段相同 ────────────────────────────────────
  const strip = (obj) => {
    const copy = { ...obj };
    delete copy.page_no;
    delete copy.page_size;
    return copy;
  };
  check(
    "③1 导出与列表在**同一表单值**下除分页外逐字段相同（「CSV 行数 == 列表命中总数」的前端侧条件）",
    strip(exported),
    strip(list)
  );
  check("③2 列表带分页参数", [list.page_no, list.page_size], [3, 50]);
  check("③3 导出**不带**分页参数", [exported.page_no, exported.page_size], [undefined, undefined]);

  // ── ④ 日期键名（DEF-093 的两个坏名字不许出现） ─────────────────────────
  check(
    "④1 日期用后端键名 create_time_from / create_time_to",
    [exported.create_time_from, exported.create_time_to],
    ["2026-01-02", "2026-03-04"]
  );
  check(
    "④2 不得出现缓存草稿的键名 create_from / create_to",
    ["create_from", "create_to"].filter((k) => k in exported),
    []
  );

  // ── ⑤ 挂链笔记：留空 = 不带 ─────────────────────────────────────────────
  check(
    "⑤1 留空 ⇒ 不带 note_count 系列参数（不是「小于 0」）",
    absent(emptyExport, ["note_count_op", "note_count"]),
    []
  );
  check("⑤2 填了 ⇒ 带比较符与篇数", [exported.note_count_op, exported.note_count], ["gte", 2]);

  // ── ⑥ 无排序 = 不带 ─────────────────────────────────────────────────────
  check("⑥1 无排序态不带 sort_field / order", absent(emptyExport, ["sort_field", "order"]), []);
  check("⑥2 有排序态才带", [exported.sort_field, exported.order], ["on_shelf_time", "desc"]);

  // ── ⑦ 关键词 ────────────────────────────────────────────────────────────
  //    （原 ⑦ 分组哨兵已随分组退场删除，2026-10-05；编号下调，见文件头注释）
  check("⑦1 关键词首尾空白裁掉", exported.keyword, "桌垫");
  check("⑦2 空关键词 ⇒ 不带（不送空串占位）", absent(emptyExport, ["keyword"]), []);

  // ── ⑧ 导出键清单冻结（15 个；加字段必须回来改这里） ──────────────────────
  check(
    "⑨ 导出形态的键清单冻结（加字段必须同步改本清单与后端模型）",
    exportKeys,
    [
      "create_time_from",
      "create_time_to",
      "keyword",
      "note_count",
      "note_count_op",
      "order",
      "platform_account_id",
      "price_from",
      "price_to",
      "sales_from",
      "sales_to",
      "sort_field",
      "status",
      "stock_from",
      "stock_to",
    ]
  );

  // ── ①② 跨层契约：键 ⊆ 后端模型字段 ──────────────────────────────────────
  if (MODELS) {
    const extraExport = exportKeys.filter((k) => !MODELS.export.includes(k));
    const extraList = listKeys.filter((k) => !MODELS.list.includes(k));
    check(
      "①1 导出键 ⊆ 后端 ItemsExportRequest 字段（多一个键 = 真机 422）",
      extraExport,
      []
    );
    check("①2 列表键 ⊆ 后端 ShopItemsRequest 字段", extraList, []);
    const missingExport = MODELS.export.filter(
      (k) => !exportKeys.includes(k) && !["page_no", "page_size"].includes(k)
    );
    check(
      "② 后端 ItemsExportRequest 的筛选字段**都被前端覆盖**（没有「后端能筛、前端发不出」的字段）",
      missingExport,
      []
    );
  } else {
    results.push("SKIP  ①② 跨层契约（未传 --models）");
  }

  if (process.env.SHOP_ITEMS_QUERY_VERBOSE) results.forEach((l) => console.log(l));
  else {
    results.filter((l) => l.startsWith("FAIL") || l.startsWith("SKIP")).forEach((l) => console.log(l));
    console.log(`---- shop_items_query: PASS ${passed} / FAIL ${failed}`);
  }
  return failed === 0 ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error("FATAL", error);
    process.exit(1);
  });
