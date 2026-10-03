"""千帆（`ark.xiaohongshu.com`）接口封装。

## 为什么这里没有签名

项目里已有的两条链路（`xhs_pc` / `xhs_creator`）都要过一遍签名运行时。
千帆**不需要**：2026-09-19 实测，把千帆自己的 Cookie
（`access-token-ark.xiaohongshu.com` / `x-user-id-ark.xiaohongshu.com` 等）
直接放进请求，不带页面渲染用的 `x-s` / `x-t` / `x-s-common`，接口照样返回 200：

```
GET /api/edith/seller/info/v2        -> 200 {"code":0,...,"seller_name":"观隅好物的店"}
GET /api/edith/goods-note/list       -> 200 {"code":0,...,"note_infos":[{...}]}
```

所以本模块只做 cookie + 普通头，**不引入第二套签名实现**——这正是二开
「不新增签名维护面」这条硬约定的直接落地（`01_需求文档/02_二开需求规格_商品笔记自动化.md` C4）。

## 只读边界与写链路的适用域（设计 13 §31.2）

* **探测 / 调研 / 适配器取证**：一律只读（店铺信息 / 商品笔记列表 / 商品列表 / SKU 明细）。
* **商品笔记智能生成与发布**（2026-09-23 用户显式要求）：写链路 ——
  `aigc-publish/attributes`（只读）、`note/sellerpoint/aigc`（生成内容，异步任务）、
  `note/aigctask/result`（只读）、`upload/web/permit` + ROS `PUT`（上传媒体）、
  `note/publish`（发布笔记）。**仅限用户本人店铺、用户显式点击触发**；
  令牌只从账号自己的 ark 通道取，不借会话；不打印 Cookie / token。

## 返回约定

与 `apis/xhs_pc_apis.py` 一致：`(success, msg, payload)` 三元组，
**失败不抛异常**，把平台的 `code` / `msg` 原样带出去给上层判定。
"""
from __future__ import annotations

import json
import logging

import requests
from xhs_utils.http_util import REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

#: `get_skus_by_item_ids` 的批量上限（2026-09-21 真机实测：5/8/10 → 200，
#: **20 → 服务端 500**）。同步服务按这个值分片，SDK 内也硬截断兜底。
SKU_BATCH_SIZE = 10

#: AI 生成任务的轮询状态（设计 13 §31.1.1；页面轮询间隔 1.5s，调用方定上限）。
AIGC_TASK_STATUS_RUNNING = "RUNNING"
AIGC_TASK_STATUS_COMPLETED = "COMPLETED"

#: 写链路（上传 / 预审核 / 发布）的超时（秒）。**比读接口长**：图片 PUT 是大
#:  body、发布要等平台落库，15s 实测不够（写超时 / 读超时都踩过，2026-09-23）。
WRITE_TIMEOUT = 60

#: 订单同步的单页上限（`fulfillment/order/page` 的 `page_size`）。
#: 订单页自己发的是 20；设 50 让一轮同步少打几页。平台未实测拒绝过大值，
#: 保守取 50 与商品同步的 `DEFAULT_PAGE_SIZE` 同口径。
#:
#: ⚠️ 定义在**类之前**（`get_order_page` 的默认参数在类体求值时引用它）。
ORDER_PAGE_SIZE_LIMIT = 50

#: 进程级共享会话（设计 13 §18.18.8）。`requests.Session` 是线程安全的（urllib3
#: 连接池自带锁），复用它让同一 host 的连续请求省掉 DNS + TLS + HTTP/2 握手 ——
#: 实测「新建连接 157–213 ms」vs「复用连接 40–48 ms」（令牌探针链路只读实测）。
#:
#: ⚠️ 共享会话**只借连接、不借凭据**：千帆令牌是每次请求**显式传入**的
#: （`cookies=self._cookie_dict()`），若让会话自己攒下 Set-Cookie，下一个账号的请求
#: 就会带上上一个账号的 Cookie（串号）。所以 `_session_request` 前后都清空会话 Cookie。
_http_session = requests.Session()


def _session_request(method: str, url: str, **kwargs):
    """走共享会话发一次请求，**前后都清空会话 Cookie**（只借连接，不借凭据）。"""
    _http_session.cookies.clear()
    try:
        return _http_session.request(method, url, **kwargs)
    finally:
        _http_session.cookies.clear()


class XHS_Qianfan_Apis:
    """千帆商家后台的只读客户端（Cookie 即登录态）。"""

    base_url = "https://ark.xiaohongshu.com"

    #: 平台语义：**凭据类**错误码（未登录 / 登录态失效）—— 全项目**唯一**的凭据判据来源。
    #: 区分它与「接口变了」是本层的主要职责（判错方向 = 错归因：把「平台变了」说成
    #: 「你需要重新登录」，用户重登一百次也没用）。
    #:
    #: | 码 | 平台语义 | 真机证据 |
    #: |---|---|---|
    #: | `-13000` | 缺失登录凭证 | 没带 jar / 带的是别的域的 jar（观隅好物账号 8） |
    #: | `-13004` | 登录凭证**已过期** | 2026-09-30 实测（账号 8/10 令牌过期）：`msg="登录凭证已过期, 请重新登录"` |
    #:
    #: ⚠️ 只收**真机见过**的码 —— 新码要先复现再往里加（DEF-084 就是这么漏掉 `-13004` 的：
    #: 只认 `-13000` ⇒ 过期被归成「未得到结论」⇒ 卡片永久停在「未检测」且没有重登入口）。
    CREDENTIAL_ERROR_CODES: frozenset[int] = frozenset({-13000, -13004})

    #: 兼容旧名（历史只认识「缺失登录凭证」那一个码）。
    #: ⚠️ 新代码请用 `is_credential_error()` / `CREDENTIAL_ERROR_CODES`，别再用这个名字。
    CREDENTIAL_ERROR_CODE = -13000

    @classmethod
    def is_credential_error(cls, code: object) -> bool:
        """平台是否明确答复「凭据问题」（缺失 / 过期）。**唯一实现**。

        四个调用点都调它（三平台探针的 ark 分支 + 两条商品链路）——
        同一个语义不留两份实现（DEF-044）：留两份就会出现「这里说过期、
        那里说未检测」，且**加一个码要改四处、漏一处静默错**。
        """
        try:
            return int(code) in cls.CREDENTIAL_ERROR_CODES
        except (TypeError, ValueError):
            return False

    def __init__(self, cookies: str):
        self.cookies = cookies or ""

    def _cookie_dict(self) -> dict[str, str]:
        """Cookie 串 → dict。

        两种形态都要吃：`k=v; k=v`（接口/浏览器复制）与 `{...}`（本项目的存储形态）。
        解析失败就退化成空 jar，让接口如实返回「缺失登录凭证」而不是在这里抛异常。
        """
        raw = (self.cookies or "").strip()
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                return {str(key): str(value) for key, value in data.items()}
        jar: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                jar[key] = value.strip()
        return jar

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.base_url}{referer}",
            "Origin": self.base_url,
        }

    def _get(self, api: str, params: dict | None, referer: str) -> tuple[bool, str, dict | None]:
        try:
            response = _session_request(
                "GET",
                self.base_url + api,
                params=params,
                headers=self._headers(referer),
                cookies=self._cookie_dict(),
                timeout=REQUEST_TIMEOUT,
            )
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 网络/解析失败都要变成可读原因
            return False, f"千帆接口请求失败：{type(error).__name__}: {error}", None

        if not isinstance(payload, dict):
            return False, "千帆接口返回了非预期的响应体", None
        code = payload.get("code")
        if self.is_credential_error(code):
            return False, str(payload.get("msg") or "千帆登录态失效"), payload
        if code not in (0, None) or payload.get("success") is False:
            return False, str(payload.get("msg") or f"千帆接口返回 code={code}"), payload
        return True, str(payload.get("msg") or "成功"), payload

    def _post(
        self, api: str, body: dict, referer: str, *, timeout: int | None = None
    ) -> tuple[bool, str, dict | None]:
        """POST 版本（`_get` 的孪生兄弟，错误语义完全一致）。

        `timeout` 供写链路（预审核 / 发布）覆写 —— 平台落库比读接口慢，
        用默认 15s 会误杀（2026-09-23 实测读超时）。
        """
        try:
            response = _session_request(
                "POST",
                self.base_url + api,
                json=body,
                headers={**self._headers(referer), "Content-Type": "application/json"},
                cookies=self._cookie_dict(),
                timeout=timeout or REQUEST_TIMEOUT,
            )
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 网络/解析失败都要变成可读原因
            return False, f"千帆接口请求失败：{type(error).__name__}: {error}", None

        if not isinstance(payload, dict):
            return False, "千帆接口返回了非预期的响应体", None
        code = payload.get("code")
        if self.is_credential_error(code):
            return False, str(payload.get("msg") or "千帆登录态失效"), payload
        if code not in (0, 200, None) or payload.get("success") is False:
            return False, str(payload.get("msg") or f"千帆接口返回 code={code}"), payload
        return True, str(payload.get("msg") or "成功"), payload

    def get_seller_info(self) -> tuple[bool, str, dict | None]:
        """店铺信息（`/api/edith/seller/info/v2`）。

        用途有二：① 绑定千帆时校验 Cookie 有效性；② 取店铺名与千帆侧的 user_id。
        """
        return self._get("/api/edith/seller/info/v2", None, "/app-system/home")

    def get_goods_note_page(
        self,
        page_num: int = 1,
        page_size: int = 20,
        *,
        note_ids: "list[str] | None" = None,
        mix_search_word: "str | None" = None,
    ) -> tuple[bool, str, dict | None]:
        """商品笔记列表的一页（`/api/edith/goods-note/list`）。

        这是「笔记挂载了哪个商品」的**唯一**来源：创作者平台的笔记列表里
        `goods_info` 恒为 `{"goods_name": []}` 空壳（2026-09-19 实测 6 页 60 条，
        且创作平台笔记管理页自身也不渲染任何商品标识）。

        ## `mix_search_word`：按标题关键词搜索（2026-09-19 千帆后台实测）

        千帆「笔记管理」页搜索框（placeholder = 请输入笔记标题关键词/笔记ID）发的就是
        `GET /api/edith/goods-note/list?page_num=1&page_size=20&mix_search_word=地垫`。
        实测生效：搜「地垫」返回的条目标题全部含地垫（卫生间地垫 / 马年浴室地垫…），
        是**服务端过滤**而不是本地筛选。与 `note_ids` 正交。

        ## `note_ids`：按笔记精确查询的口子（2026-09-19 实测）

        传**复数** `note_ids`（逗号分隔多个）时平台**真的在过滤**：

        | 输入 | total | 结论 |
        |---|---|---|
        | 一条真实 note_id | 1 | 精确命中，`item_id` 与店铺口径一致 |
        | 一条不存在的 id | 0 | 真过滤（不是「恰好一条」） |
        | `a,b,c…`（实测 2/6/10/20/50 个） | = 个数 | **一页笔记一次请求**的依据 |
        | 混入不存在的 id | 仍是真实条数 | 不污染结果 |

        ⚠️ 三个必须记住的坑：

        1. **是复数 `note_ids`**：单数 `note_id`、`keyword`、`search_filter.*` 等 15 个候选
           参数名全部被平台**忽略**（total 恒为店铺总数）。早前「该接口没有按 note_id
           查询的口子」的结论就是只试了单数得出的，此处更正。
        2. **带 `note_ids` 时响应里的 `total` 是过滤后条数**，不是店铺总数 ——
           拿它当覆盖率分母会把分母写坏（1968 → 2）。
        3. 与分页**正交**：`note_ids` 命中 N 条时仍受 `page_size` 约束
           （实测 2 条 + `page_size=1` → `total=2` 但只回 1 行），所以批量查询要把
           `page_size` 设成 ≥ 传入的 id 个数。
        """
        params: dict = {"page_num": int(page_num), "page_size": int(page_size)}
        if note_ids:
            params["note_ids"] = ",".join(str(item) for item in note_ids if item)
        if mix_search_word:
            params["mix_search_word"] = str(mix_search_word).strip()
        return self._get(
            "/api/edith/goods-note/list",
            params,
            "/app-note/note-list",
        )


    def get_skus_by_item_ids(
        self, item_ids: "list[str] | tuple[str, ...] | set[str]"
    ) -> tuple[bool, str, dict | None]:
        """按商品 ID 批量取 **SKU 级明细**（`POST /api/edith/product/get_skus_by_item_ids`）。

        2026-09-21 真机调研（设计 13 §19.1.2）：商品**编辑页**真正加载既有商品数据
        走的是 `product/publish_render`（`publishType=2 + itemId`），但它**必须带
        `X-s`/`X-t`/`X-S-Common` 签名**（无签名直连 406，实测四组对照全挡）——
        复现签名违反只读边界 / C4（`ssologin` 同型先例），不接入。本接口是**无签名**
        的替代来源：返回 `data.skusMap: {item_id: [sku, ...]}`，SKU 含
        `skuId/skuName/variantName/barcode/erpCode/basePriceInfo{price,originalPrice,tax}
        /stock/images/specImage/attributes/variants/weight/freeze/buyable…`。

        ⚠️ **批量上限 10**（2026-09-21 实测）：传 5 / 8 / 10 个 id → 200
        （10 个 ≈ 700 SKU、响应 5.4MB）；传 **20 个 → code 500**（服务端错）。
        所以本方法**硬截断到** :data:`SKU_BATCH_SIZE`，多传的 id 直接不发的
        同时记 warning —— 调用方（同步服务）按批量分片，正常不会触发截断；
        真触发了说明有调用方没看文档，让它响得明显一点。

        响应可能**部分缺失**（某个 id 不在 skusMap 里）：按「这个商品没有 SKU
        明细」处理，由调用方决定是留空还是重试，本层不猜。
        """
        ids = [str(item).strip() for item in (item_ids or []) if str(item).strip()]
        if not ids:
            return False, "item_ids 为空", None
        if len(ids) > SKU_BATCH_SIZE:
            logger.warning(
                "get_skus_by_item_ids: %d ids 超过批量上限 %d，多出的 %d 个未发送",
                len(ids), SKU_BATCH_SIZE, len(ids) - SKU_BATCH_SIZE,
            )
            ids = ids[:SKU_BATCH_SIZE]
        return self._post(
            "/api/edith/product/get_skus_by_item_ids",
            {"itemIds": ids},
            "/app-item/list/shelf",
        )

    def search_items(
        self,
        *,
        page_no: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        status: str = "on_shelf",
        price_from_fen: int | None = None,
        price_to_fen: int | None = None,
        stock_from: int | None = None,
        stock_to: int | None = None,
        sales_from: int | None = None,
        sales_to: int | None = None,
        create_time_from: int | None = None,
        create_time_to: int | None = None,
        sort_field: str = "create_time",
        order: str = "desc",
        is_channel: bool = False,
    ) -> tuple[bool, str, dict | None]:
        """商品列表一页（`POST /api/edith/product/search_item_v2`）。

        ## 参数名从哪来

        全部**照抄千帆商品列表页自己发的请求**（2026-09-19 用 CDP 抓它的 XHR 得到），
        不是猜的：

        ```
        POST {"page_no":1,"page_size":20,
              "search_order":{"sort_field":"create_time","order":"desc"},
              "search_filter":{"card_type":2,"is_channel":false,
                               "whole_keywords":"地垫",      # 关键词（名称/ID/货号/商家编码）
                               "price_from":10000,"price_to":20000,   # 价格，单位 = 分
                               "stock_from":10,"stock_to":200,        # 库存
                               "sales_from":11,"sales_to":50}}        # 销量
        ```

        实测各筛选对 `total` 的影响：关键词「地垫」→ 92；价格 100~200 元 → 654；
        默认（售卖中）→ 1994；创建时间 2026-07-01~07-16 → 1176。

        ## 三个必须记住的坑

        1. **价格单位是分**：页面填「100」发出去的是 `price_from: 10000`。
           本方法因此只接受 `*_fen`，把换算责任留在调用方一处显式转换。
        2. **`card_type` 缺失 ≠ 全部**：页面点「全部」时干脆不发请求；
           按状态过滤时该值必须取自 :data:`QIANFAN_ITEM_STATUSES`。
        3. **创建时间单位是秒**（`create_time_from` / `create_time_to`，Unix 秒）。
           2026-09-19 判定实验：同一组键传**毫秒**会被下游 ES 拒掉（HTTP 500
           `search_phase_execution_exception`），传**秒**则 200 且 `total` 随之变化
           （2020 空窗 → 0；2026-07-01~07-16 → 1176）。单边传（只给 from 或只给 to）也生效。
        """
        search_filter: dict[str, object] = {"is_channel": bool(is_channel)}
        card_type = QIANFAN_ITEM_STATUSES.get(str(status))
        if card_type is not None:
            search_filter["card_type"] = card_type
        if keyword:
            search_filter["whole_keywords"] = str(keyword).strip()
        for key, value in (
            ("price_from", price_from_fen),
            ("price_to", price_to_fen),
            ("stock_from", stock_from),
            ("stock_to", stock_to),
            ("sales_from", sales_from),
            ("sales_to", sales_to),
            ("create_time_from", create_time_from),
            ("create_time_to", create_time_to),
        ):
            if value is not None:
                search_filter[key] = int(value)

        body = {
            "page_no": max(1, int(page_no)),
            "page_size": max(1, min(int(page_size), 100)),
            "search_order": {"sort_field": sort_field or "create_time", "order": order or "desc"},
            "search_filter": search_filter,
            # 只有在售卖中 tab 上页面才会要这几个增强字段；列表页需要「近 30 天销量」
            # 与「库存风险」，所以固定带上，多出来的键对平台无害（实测同 tab 一致）。
            "search_item_detail_option": {
                "with_product_quality_score": True,
                "with_hot_item_award_text_info": True,
                "with_ai_publish_note_permission": True,
                "with_inventory_risk_info": True,
                "with_item_lock_info": True,
            },
        }
        return self._post("/api/edith/product/search_item_v2", body, "/app-item/list/shelf")

    # ------------------------------------------------------------------ #
    # 商品笔记智能生成与发布（设计 13 §31，2026-09-23 真机抓包）
    # 全部 Cookie 直连、无签名；调用方：services/product_note_gen_service.py
    # （生成）与 services/note_publish_service.py（发布）。
    # ------------------------------------------------------------------ #

    def aigc_publish_attributes(
        self, item_id: str, *, sku_id: str = ""
    ) -> tuple[bool, str, dict | None]:
        """商品笔记 AI 生成的**属性面**（`POST /api/edith/aigc-publish/attributes`）。

        2026-09-23 真机抓包（发布笔记页 `mainPublishType=batchAgi` 加载时发）。
        请求体：`{"aigc_note_creation_query": [{"item_id": …, "sku_id": ""}]}`
        （`sku_id` 传空 = 让平台回该商品全部 SKU，调用方取第一个）。

        响应 `data` 关键字段：

        * `sku_list[]`：`sku_id` / `item_image` / `sku_image` / `item_name` / `sku_specification`；
        * `seller_point.seller_point_info[]`：卖点词（「韩系」「防水」…）；
        * `note_style`：`好物推荐` / `产品测评` / `攻略教程`（带 `default_style`）；
        * `note_scene`：`新品种草` / `活动大促` / `爆品孵化` / `日常宣发`；
        * `topic_list.topic_list_info[]`：**平台推荐话题**（`type=1` 带 `hot_value` 热度，
          `type=2` 为活动话题）；
        * `text_words_count`：可选正文字数档位。
        """
        body = {"aigc_note_creation_query": [{"item_id": str(item_id or ""), "sku_id": str(sku_id or "")}]}
        return self._post("/api/edith/aigc-publish/attributes", body, "/app-note/publish")

    def sellerpoint_aigc(
        self, *, biz_id: str, aigc_context: dict
    ) -> tuple[bool, str, dict | None]:
        """发起一次**异步**笔记文本生成（`POST /api/edith/note/sellerpoint/aigc`）。

        请求体：`{"biz_id": "<sku_id>", "biz_name": "sellerPointNoteText",
        "aigc_context_d_t_o": {"requirement": …, "crowd": …, "topic": …,
        "selling_point_list": [...], "direction_list": [...]}}`。

        响应：`data.task_id`（形如 `PARENT_E8E5…`）—— **只是任务句柄**，结果要轮询
        :meth:`aigc_task_result`。`biz_name` 固定 `sellerPointNoteText`（页面实抓；
        换值平台不认 —— 这不是自由文本）。
        """
        body = {
            "biz_id": str(biz_id or ""),
            "biz_name": "sellerPointNoteText",
            "aigc_context_d_t_o": aigc_context if isinstance(aigc_context, dict) else {},
        }
        return self._post("/api/edith/note/sellerpoint/aigc", body, "/app-note/publish")

    def aigc_task_result(self, task_id: str) -> tuple[bool, str, dict | None]:
        """轮询异步生成任务的结果（`GET /api/edith/note/aigctask/result`）。

        响应 `data`：`task_status`（`RUNNING` / `COMPLETED`）与 `task_detail`
        —— **后者是 JSON 字符串**，解析后为
        `{"success": bool, "errMsg": str, "title": str, "content": str,
        "topics": [{"id": 平台话题ID, "name": 话题名}]}`。
        调用方负责「还没好就再轮询」的节奏（建议 1.5s 间隔、~90s 上限）。
        """
        return self._get(
            "/api/edith/note/aigctask/result",
            {"task_id": str(task_id or "")},
            "/app-note/publish",
        )

    def upload_web_permit(self, file_count: int = 1) -> tuple[bool, str, dict | None]:
        """申请图片上传凭证（`GET /api/edith/upload/web/permit`）。

        2026-09-23 真机抓包（发布页选文件后发）：
        `?biz_name=spectrum&scene=image&file_count=1&version=1`。
        响应 `data.uploadTempPermits[]` 每项含：

        * `uploadAddr`：ROS 上传域名（实测 `ros-upload.xiaohongshu.com`）；
        * `token`：上传令牌（**不打印、不落库**，只透传给 :meth:`upload_file`）；
        * `fileIds[]`：**预分配**的文件 ID（形如 `spectrum/2Ip9…`）—— 上传目标路径
          与最终 `images_list.file_id` 都是它；
        * `expireTime` / `startTime`：凭证有效期（毫秒）。

        ⚠️ 一次申请对应 `file_count` 个文件；批量上传按文件数一次申请、
        逐文件 PUT，不要一个文件一次申请（页面自己也是一次多文件）。
        """
        return self._get(
            "/api/edith/upload/web/permit",
            {"biz_name": "spectrum", "scene": "image", "file_count": max(1, int(file_count)), "version": 1},
            "/app-note/publish",
        )

    def upload_file(self, *, upload_addr: str, file_id: str, token: str, file_path: str) -> tuple[bool, str, dict | None]:
        """把本地文件字节 PUT 到 ROS（`https://<upload_addr>/<file_id>`）。

        令牌走 **`x-cos-security-token` 头**（2026-09-23 实测：其余 5 种格式
        （`Authorization: UpToken/Bearer/裸 token`、`x-ros-security-token`、
        query `token`）全部 401 `no security token`；只有 COS 风格的
        `x-cos-security-token` 返 200）。成功响应为 ROS 的 `{"code":0,…}` 形态；
        失败时返回 `(False, 可读原因, None)`，**不抛异常**（与模块其他方法同契约）。
        """
        try:
            with open(file_path, "rb") as handle:
                payload = handle.read()
        except OSError as error:
            return False, f"读取本地文件失败：{error}", None
        try:
            response = requests.put(
                f"https://{upload_addr}/{file_id}",
                data=payload,
                headers={
                    "User-Agent": self._headers("/app-note/publish")["User-Agent"],
                    "x-cos-security-token": token,
                    "Content-Type": "application/octet-stream",
                },
                timeout=WRITE_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001 - 网络/解析失败都要变成可读原因
            return False, f"上传文件失败：{type(error).__name__}: {error}", None
        # 成功时 ROS 返 **空 body + 200**（实测）—— 先按状态码判，再尝试解析 JSON
        # （失败时才有 `{"code":1,"msg":"..."}` 可读原因）。
        if response.status_code != 200:
            try:
                body = response.json()
                reason = str(body.get("msg") or f"HTTP {response.status_code}") if isinstance(body, dict) else f"HTTP {response.status_code}"
            except ValueError:
                reason = f"HTTP {response.status_code} {response.text[:120]}"
            return False, f"上传文件失败：{reason}", None
        return True, "成功", None

    def publish_note(self, body: dict) -> tuple[bool, str, dict | None]:
        """发布商品笔记（`POST /api/edith/note/publish`）。

        请求体关键字段（2026-09-23 抓包 + bundle 结构分析）：

        * `title` / `desc`：标题与正文（话题以 ` #名称[话题]# ` 缀在 desc 尾部）；
        * `hash_tag[]`：`{id, name, type: "topic"}` —— **id 必须是平台话题 ID**
          （AI 生成结果 `topics[].id` 直接给），自己拼名字平台不认；
        * `images_list[]`：`{file_id, width, height}`（`file_id` 来自
          :meth:`upload_web_permit` 预分配 + :meth:`upload_file` 上传）；
        * `biz_relations[]`：`{biz_id: 商品 item_id, biz_type, extra_info}` ——
          **商品挂载（挂链）的唯一载体**；
        * `post_loc`：**不带**（浏览器实抓：未选地点时 common 里不带该键，发空对象
          会被服务端 -9059 打回）。

        响应：`{"id": "<平台 note_id>"}` —— 三方真实笔记 ID（回写 notes.note_id 的依据）。
        """
        return self._post(
            "/api/edith/note/publish",
            body if isinstance(body, dict) else {},
            "/app-note/publish",
            timeout=WRITE_TIMEOUT,
        )

    def preaudit_note(
        self,
        *,
        title: str,
        content: str,
        topic_names: list[str],
        image_list: list[dict],
        text_gen_task_id: str = "",
        image_gen_task_id: str = "",
    ) -> tuple[bool, str, dict | None]:
        """发布前**内容预审核**（`POST /api/edith/note/preaudit`；bundle 逆出）。

        页面自身流程：先预审核、后发布。请求体（**参数名是下划线风格** —— 实测
        `auditId` 会被平台参数校验打回 `required param check: audit_id`）：

        ```json
        {"audit_id": "<24 位随机字母数字>",
         "note_content": {"title": "…", "content": "…",
                          "topic_list": ["话题名", …], "image_list": [<图片>]},
         "image_gen_task_id": "…", "text_gen_task_id": "…"}
        ```

        响应 `data.auditResult`：`titleResult` / `contentResult` / `topicResult` /
        `imageResults[]` 各带 `checkPass` 与 `failMsg`。**任一 `checkPass === false`**
        ⇒ 页面弹 `failMsg` 并**终止发布** —— 平台侧的内容 moderation，比本地护栏
        更权威；本方法把判定原样带回，由调用方决定呈现。
        """
        import random
        import string

        audit_id = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(24))
        body = {
            "audit_id": audit_id,
            "note_content": {
                "title": title or "",
                "content": content or "",
                "topic_list": [str(t) for t in (topic_names or [])],
                "image_list": image_list if isinstance(image_list, list) else [],
            },
            "image_gen_task_id": str(image_gen_task_id or ""),
            "text_gen_task_id": str(text_gen_task_id or ""),
        }
        return self._post("/api/edith/note/preaudit", body, "/app-note/publish", timeout=WRITE_TIMEOUT)

    # ------------------------------------------------------------------ #
    # 生成上下文的「需求详情」链（设计 13 §31.4：尽力而为，失败只降级不阻断）。
    # 页面自身串的三级只读请求；没有它们生成质量下降（缺真实卖点/灵感/人群），
    # 但不该成为硬门槛。
    # ------------------------------------------------------------------ #

    def batch_get_category(self, item_ids: list[str]) -> tuple[bool, str, dict | None]:
        """按商品取经营类目（`POST /api/edith/item/batch_get_category`）。

        响应 `data.item_list[]`：`item_id` / `title` / `image_url` /
        `category_list[]`（`category_id` / `name` / `level` / `leaf`）。
        调用方取 `leaf=true` 的 `category_id` 去查需求清单。
        """
        ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
        if not ids:
            return False, "item_ids 为空", None
        return self._post("/api/edith/item/batch_get_category", {"item_ids": ids}, "/app-note/publish")

    def get_category_demand_list(self, leaf_category_id: str) -> tuple[bool, str, dict | None]:
        """类目需求清单（`GET /edith/api/seller/get_category_demand_list`）。

        响应 `data.demand_list[]`：`demand_id` / 需求名。调用方取第一个
        `demand_id` 查详情（页面自身也是取默认需求）。
        """
        return self._get(
            "/edith/api/seller/get_category_demand_list",
            {"leaf_category_id": str(leaf_category_id or "")},
            "/app-note/publish",
        )

    def get_demand_detail(self, demand_id: str) -> tuple[bool, str, dict | None]:
        """需求详情（`GET /edith/api/seller/get_demand_detail`）。

        响应 `data.demand_detail`：`related_selling_point_list[]`（卖点，
        `is_risk_word=true` 的**过滤不用**）、`creative_inspiration_list[]`
        （创作灵感）、`crowd_profile_list[]`（人群画像）。
        """
        return self._get(
            "/edith/api/seller/get_demand_detail",
            {"demand_id": str(demand_id or "")},
            "/app-note/publish",
        )

    # ------------------------------------------------------------------ #
    # 订单模块（设计 16 §7，M6 方向 6 · **一期只读**）
    #
    # 真机实测（2026-10-04，账号 8「观隅好物的店」3 笔真实订单）：
    # 这批接口**与商品接口同链路 —— 无需 `X-s` / `X-t` / `X-S-Common` 签名**，
    # 只带千帆自己的 Cookie 即可（订单页自己发的请求带签名，去掉签名后
    # 接口照样 `code=0` 返回真实数据，对照实验见下方 `get_order_page` 的
    # 真机记录）。这与 `get_seller_info` / `search_items` 完全同构。
    #
    # ⚠️ **本节全部是只读探测接口**。发货 / 售后处理（写操作）在 M7，
    # 且必须显式 + 二次确认（设计 16 §7.4），不在这里。
    # ------------------------------------------------------------------ #

    def get_order_page(
        self,
        page_no: int = 1,
        page_size: int = ORDER_PAGE_SIZE_LIMIT,
        *,
        start_time_ms: int = 0,
        end_time_ms: int = 0,
    ) -> tuple[bool, str, dict | None]:
        """订单列表一页（`POST /api/edith/fulfillment/order/page`，只读）。

        ## 真机实测（2026-10-04，账号 8，无签名直连）

        | 请求 | 结果 |
        |---|---|
        | `start_time=0, end_time=0` | **15s 读超时**（窗口不合法，平台挂死） |
        | 近 90 天窗口 | `code=0`，`total=0`（窗口内确实无单） |
        | 近 365 天窗口 | `code=0`，`total=3`，`packages` 3 条真实订单 |
        | `start_time=1`（近全部） | `code=0`，`total=3`，同上 |

        ⚠️ **`time_range_list` 的窗口不能是 `0/0`** —— 平台不报错而是挂到超时。
        增量同步调用方必须保证窗口有界（同步服务用「同步游标 → 当前时间」，
        全量用「近 365 天」，见 `order_sync_service`）。

        ## 响应结构（`data`）

        ```jsonc
        {
          "total": 3,
          "packages": [{                // 一个 package = 一个订单的主包裹
            "orderId": "79878431779939393",
            "packageId": "P798784317799393931",
            "status": 7, "statusDesc": "已完成",
            "afterSaleStatus": 1, "afterSaleStatusDesc": "无售后",
            "typeDesc": "现货订单",
            "totalOrderAmount": 61.72, "actualPaid": 61.72, "shippingFee": 0,
            "orderedAt": "2026-07-05 14:18:37", "paidAt": "...",
            "shippedAt": "...", "finishedAt": "...", "promiseLastShipAt": "...",
            "updatedAt": "...",
            "expressNo": "79016621445635", "expressCompanyName": "中通速递",
            "sellerMarkInfo": {"sellerMarkNote": "...", "operator": "...", "updated_at": "..."},
            "skus": [{ "skuId", "itemId", "skuName", "skuSpecification",
                       "skuQuantity", "skuSoldPrice", "skuTotalPaidAmount",
                       "afterSaleStatus", "afterSaleStatusDesc", "image", ... }],
            "userInfo": { "nickName", "name", "phone", "province", "city",
                          "district", "town", "address", "street" }
          }]
        }
        ```

        ⚠️ **`orderId`（订单号）是幂等键**；`packageId` 是取详情/物流的键。
        一个 `orderId` 对应一个主 `packageId`（`skus` 在 package 内）。

        ⚠️ 隐私字段（`userInfo.name/phone/address`）千帆默认**部分脱敏**
        （`及*` / `184***3034` / 尾段 `***`），虚拟号订单更只有脱敏串 ——
        平台给什么存什么，不猜不补（缺值口径）。完整值场景在 M7 发货链路。
        """
        body: dict = {
            "page_no": int(page_no),
            "page_size": int(page_size),
            "order_tag_list": [],
            "order_type_list": [],
            "status": [],
            "seller_mark_priority_list": [],
            "seller_mark_note_status_list": [],
            "overdue_status": -2,
            "sort_by": {"sort_field": "ordered_at", "desc": True},
            "need_declare_info": True,
            "need_declare_times": True,
            "allow_es_fallback": True,
        }
        if start_time_ms or end_time_ms:
            body["time_range_list"] = [
                {"time_type": 3, "start_time": int(start_time_ms), "end_time": int(end_time_ms)}
            ]
        return self._post(
            "/api/edith/fulfillment/order/page",
            body,
            "/app-order/order/query",
            timeout=WRITE_TIMEOUT,
        )

    def get_order_detail(self, package_id: str) -> tuple[bool, str, dict | None]:
        """订单详情（`POST /api/edith/fulfillment/order/detail`，只读）。

        ⚠️ **入参是 `package_id`（不是 `order_id`）** —— 真机实测：
        传 `order_id` 平台回 `code=-1, "required param check: package_id"`。

        响应 `data.fulfillment_package`：字段是列表条目的**超集** ——
        多出物流轨迹（`delivery_packages`）、售后单（`after_sale_list`）、
        运费险（`carriage_insurance`）、支付方式（`pay_unit_lite`）、
        签收时间（`confirmed_at` / `finished_at`）等。同步服务用它补详情。
        """
        return self._post(
            "/api/edith/fulfillment/order/detail",
            {"package_id": str(package_id or "")},
            "/app-order/order/query",
            timeout=WRITE_TIMEOUT,
        )

    def get_order_stats(self, start_time_ms: int, end_time_ms: int) -> tuple[bool, str, dict | None]:
        """订单状态统计（`POST /api/edith/fulfillment/order_stats`，只读）。

        响应 `data.data` 是一组计数（各状态/标签的订单数），顺序与平台
        tab 一致。只用于「订单监控」的概览数字；**列表不同步它**（列表页
        的分页数据由 `get_order_page` 给）。
        """
        body = {
            "conditions": [
                {
                    "order_start_time": int(start_time_ms),
                    "order_end_time": int(end_time_ms),
                    "status_list": [1, 2, 21, 26, 4, 5, 55, 6, 65, 998],
                }
            ]
        }
        return self._post(
            "/api/edith/fulfillment/order_stats",
            body,
            "/app-order/order/query",
            timeout=WRITE_TIMEOUT,
        )

    def get_after_sale_page(
        self,
        page: int = 1,
        number: int = 20,
        *,
        status_in: tuple[int, ...] = (1, 3, 12),
    ) -> tuple[bool, str, dict | None]:
        """售后单列表一页（`GET /api/edith/after-sales/returns/v3`，只读）。

        真机实测（2026-10-04，账号 8）：默认查询参数照抄售后工作台页自己
        发的请求（`page / number / pending_handle_duration_in=18 /
        goods_source[]=[1,2] / sort=deadline_for_sort_v1 / order=asc /
        status_in=1,3,12`），响应 `data.after_sales[]` + 一组状态计数。

        `status_in` 是平台售后状态码：`1` 待商家处理 / `3` 待商家收货 /
        `12` 平台介入…（页面筛选档）。**注意这是售后工作台的码，与订单的
        `after_sale_status` 不是同一套**。

        ⚠️ 售后列表是**独立维度**：一期只把 `after_sales` 表挂在订单详情的
        `after_sale_list` 上（同步订单时顺带落），本接口供 M7 处理链路定位
        待处理售后单用；M6 同步**不依赖**它。
        """
        params = {
            "page": int(page),
            "number": int(number),
            "pending_handle_duration_in": 18,
            "sort": "deadline_for_sort_v1",
            "order": "asc",
            "status_in": ",".join(str(s) for s in status_in),
        }
        return self._get(
            "/api/edith/after-sales/returns/v3",
            params,
            "/app-order/aftersale/list",
        )


XHSQianfanApis = XHS_Qianfan_Apis

#: 千帆订单状态码（`fulfillment/order/page` 的 `status`）—— 真机实测
#: （2026-10-04 账号 8 的 3 笔真实订单 + 订单页 `order_stats` 请求的
#: `status_list` 筛选档，与页面 tab 同源）：
#:
#: | 码 | 含义 | 证据 |
#: |---|---|---|
#: | 1 | 待付款 | `order_stats` 请求的条件组 |
#: | 2 / 21 / 26 | 待发货 | 同上（21/26 是待发货的子档） |
#: | 4 / 5 / 55 | 已发货待收货 | 同上 |
#: | 6 / 65 | 已完成 | 同上 |
#: | 7 | 已完成 | 真实订单 79878431779939393 的 `status=7, statusDesc=已完成` |
#: | 998 | 已关闭/已取消 | 真实订单 80398468036720022 / 79991062927303330 |
#:
#: ⚠️ 本表**只做排障对照**：本地状态归属由 `models/order.py::ORDER_STATUS_MAP`
#: 与 `resolve_order_status()` 唯一承载（状态映射逐值钉死、不散落 if）。
QIANFAN_ORDER_STATUS_CODES: dict[int, str] = {
    1: "待付款",
    2: "待发货",
    21: "待发货",
    26: "待发货",
    4: "已发货待收货",
    5: "已发货待收货",
    55: "已发货待收货",
    6: "已完成",
    65: "已完成",
    7: "已完成",
    998: "已关闭/已取消",
}

#: 千帆售后状态码（`after_sale_status`）—— 真机实测：
#: `1` 无售后 / `2` 售后中 / `3` 售后完成 / `4` 售后关闭。
QIANFAN_AFTERSALE_STATUS_CODES: dict[int, str] = {
    1: "无售后",
    2: "售后中",
    3: "售后完成",
    4: "售后关闭",
}

#: 订单状态码的中文对照（定义见上面 `QIANFAN_ORDER_STATUS_CODES`）。
#: 商品列表的状态筛选（`search_filter.card_type`）。
#: **全部取自页面自己发的请求**（2026-09-19 逐个 tab 点击抓包），不是猜的：
#: 售卖中=2（1994 条）/ 已下架=3 / 已冻结=4 / 审核中=5 / 审核驳回=6 / 审核通过=7 / 整改=10。
#: 「全部」在页面上不发请求（等价于不传 `card_type`），消费方按「不传」处理。
QIANFAN_ITEM_STATUSES: dict[str, int] = {
    "on_shelf": 2,
    "off_shelf": 3,
    "frozen": 4,
    "auditing": 5,
    "audit_rejected": 6,
    "audit_passed": 7,
    "rectify": 10,
}

#: 状态的中文标签（前端与文档共用一份，避免各写各的）。
QIANFAN_ITEM_STATUS_LABELS: dict[str, str] = {
    "all": "全部",
    "on_shelf": "售卖中",
    "off_shelf": "已下架",
    "frozen": "已冻结",
    "auditing": "审核中",
    "audit_rejected": "审核驳回",
    "audit_passed": "审核通过",
    "rectify": "整改",
}

__all__ = [
    "XHS_Qianfan_Apis",
    "XHSQianfanApis",
    "QIANFAN_ITEM_STATUSES",
    "QIANFAN_ITEM_STATUS_LABELS",
    "QIANFAN_ORDER_STATUS_CODES",
    "QIANFAN_AFTERSALE_STATUS_CODES",
    "ORDER_PAGE_SIZE_LIMIT",
    "SKU_BATCH_SIZE",
    "AIGC_TASK_STATUS_RUNNING",
    "AIGC_TASK_STATUS_COMPLETED",
]
