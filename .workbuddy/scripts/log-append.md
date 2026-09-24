
## 商品 6a581608ab42de00152e782c「内容库缺笔记」二次报告 → 修复（2026-09-23 深夜，代码 + 数据 + 文档）

**用户报告（第二次，同一商品）**：挂链笔记弹窗展示 3 篇，内容库只展示该商品 2 篇，
要求分析原因并修复。傍晚第一次报告时结论是「数据层一致、可发现性问题」（纯数据清理
4 条孤儿关系行 6→2，未动代码）；今晚弹窗变 3 篇是因为页内联同步把被 purge 的平台笔记
`6ab2c68a` 重建为 1967 并经 `link_note_goods` 长回关系。用户再次报告 ⇒ **可发现性
问题也必须用代码修**。

**根因（三层，真实库实测）**：① 数据层无缺失 —— 3 条 `note_goods` 全指向存续笔记，
`q=6a5d4ce7` 能搜到；② 补验层 —— 同步笔记 1075 `hydrated_at IS NULL`、正文空
（待补队列 1874 条没轮到）；③ 发现层 —— 1075 在全库 1964 条里排 889 位，且内容库
**没有按商品反查笔记的入口**。

**修复（三件）**：
1. **数据**：`.workbuddy/scripts/hydrate-note-1075.py` 对 1075 跑 `hydrate_note_detail`
   （唯一实现，只读 GET）⇒ 正文 379 字 / 图 4 张 / 话题 9 个 / `hydrated_at` 落值。
   ⚠️ 沙箱里跑要先清 `HTTP_PROXY`（v2tls 过不了代理，已入 REF §B）。
2. **链路**：`GET /api/notes` 新增 `item_id`（挂链商品，`note_goods` join `shop_products`
   子查询，**不校验存在性**，与 `platform_account_id` 同口径）；前端筛选区第二行加输入框
   （第一行 span 已满 24）+ URL `?item_id=` 惰性初值 + 生效提示条 + 空态文案随筛选切换；
   挂链弹窗加「在内容库中查看」跳转。
3. **文档**：设计 13 新增 **§25.8**（根因三层表 + 修复三件 + 验收）。

**测试**：`test_note_type.py` +5 条 item_id 契约（26 passed）；新前端行为脚本
`tests/frontend/library_item_filter.cjs`（21 PASS + 5 变异开关 trim/urlinit/cfilter/
notice/jump 逐一验证变红）；`test_page_optimizations.py` + `test_api.py` 218 passed；
其余 8 个前端脚本全绿；`tsc --noEmit` + `vite build` 通过；两个文档校验脚本 PASS。

**复核**：杀 uvicorn（PID 32168）等 launcher 重拉（新 PID 41992），`/openapi.json`
出现 `item_id`；真实库端到端：弹窗 3 篇 = `item_id` 筛选 3 篇，集合一致，1075 正文 379 字。

**文件损坏与修复（两次，均为 heredoc 追加中文所致）**：
- `REF_易错与运行坑.md`：头部 9 行 + 第 10 行大半被覆盖 ⇒ 用 Python 字节级重建
  （原 1~9 行从本会话早先 head 输出逐字取回 + 658 字节后原样区保留），重建后 111 行、
  整体合法 UTF-8，新条目（沙箱代理坑）追加在末尾。
- 本日志 2794 字节处一行被早前 heredoc 损坏（首 2 字节 `\x96\x87`，其余完好）⇒ 按上
  下文还原为「**两个**文档校验脚本 PASS；服务已用新 launcher（pid 42480）重启…」
  （与「两个校验脚本都要跑」口径一致）。**此为还原，非原文照片**，特此标注。
- **教训（第三次同类）**：写中文文件一律 Write/Edit 或 Python，**永不用 `cat >> … << EOF`**。
