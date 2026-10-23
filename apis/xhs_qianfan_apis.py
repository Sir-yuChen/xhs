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
import os

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

#: 违规管理域的 Referer（设计 42，2026-10-18 真机抓包逐字）。
#: 平台对**整个**违规域（含只读 GET 枚举）强制签名；签名材料里的 referer 取真机页。
VIOLATION_REFERER = "/app-violation/violation-manage/list"

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


def _dsl_pair_or_empty() -> str:
    """取一个 `_dsl` 并拼成 `<ms>;<dsl>`；**取不到就退空串**（不阻断请求）。

    `_dsl` 由平台下发（5 分钟缓存），只参与 `x-s-common` 的生成。写接口实测
    **不校验 `x-s-common`**（去掉照样 200），所以这里刻意**不做重试、不抛异常** ——
    为了一个非必需字段把整次写操作判死是错的取舍。

    ⚠️ 用 `xhs_pc.dsl.get_dsl` 而不是 `xhs_creator` 的那个：前者是 PC Web 的
    `window._dsl`（ark 与 PC 同属 `www.xiaohongshu.com` 域下的 Web 侧），
    creator 那个是「服务端 DS 程序」形态（要 `_dsf`），本链路用不上。
    """
    try:
        import time as _time

        from xhs_utils.xhs_pc.dsl import get_dsl

        value = str(get_dsl() or "").strip()
        if not value:
            return ""
        return f"{int(_time.time() * 1000)};{value}"
    except Exception:  # noqa: BLE001 - 非必需字段，取不到就退空
        return ""


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

    def _get(
        self, api: str, params: dict | None, referer: str, *, timeout: float | None = None,
    ) -> tuple[bool, str, dict | None]:
        try:
            response = _session_request(
                "GET",
                self.base_url + api,
                params=params,
                headers=self._headers(referer),
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

    #: 数据中心「笔记数据」blockKey → category（2026-10-06 真机抓包）。
    #: 商品笔记 / 普通笔记两个页面各自一个 DataList 数据块。
    NOTE_DATA_BLOCK_KEYS: dict[str, str] = {
        "goods": "arkSellerNoteGoodsNoteDataList",
        "normal": "arkSellerNoteNormalNoteDataList",
    }

    def get_note_data_list(
        self,
        *,
        category: str,
        page: int = 1,
        size: int = 50,
        date_select_type: str = "recent_1",
        date_type: int = 1,
        start_date: str = "",
        end_date: str = "",
    ) -> tuple[bool, str, dict | None]:
        """千帆数据中心「笔记数据」列表一页（`POST /api/edith/butterfly/data`）。

        2026-10-06 真机抓包：页面 `/app-datacenter/note-data/goods`（商品笔记）与
        `/normal`（普通笔记）的「笔记」数据块。**无签名直连**（与 `get_goods_note_page`
        同构；下载 xlsx 的 `long_task/task/submit` 才要 datacenterark 签名，C4 不接入）。

        `category` ∈ `{"goods", "normal"}` ⇒ 映射到对应 `blockKey`。分页走
        `page` / `size`（页面可选 10/20/30/40/50）。

        时间维度（2026-10-06 前端 JS 取证，取值枚举见下）：
        `date_select_type` ∈ `{now, custom, recent_1, recent_7, recent_14, recent_15,
        recent_30, recent_90, recent_180, day, week, month, quarter, year, activity,
        sofar, multiMonth}`；`date_type`(int) 映射 `recent_1→1 / recent_7→2 /
        recent_30→3 / recent_90→90 / recent_180→180 / now→-1 / day→1 / week→4 / month→5`。

        「**自定义日期范围**」（`date_select_type="custom"`）额外带 `start_date` /
        `end_date`（`YYYY-MM-DD`），写入 filterMap 的 `startDate` / `endDate` ——
        平台前端 JS 取证（`D` 函数）确认这个字段名。

        返回 `data[0].data[]`：每行是 `{"value": ..., "ratio": ...}` 包裹结构
        （`ex` 偶见于比率字段）。⚠️ `value` 可能是 0 / 空串 / None / 嵌套 dict，
        调用方解包时按缺值口径处理（不造假）。
        """
        block_key = self.NOTE_DATA_BLOCK_KEYS.get(category)
        if block_key is None:
            return False, f"未知笔记类型：{category}（合法值：goods / normal）", None
        filter_map: dict[str, object] = {
            "dateSelectType": str(date_select_type),
            "dateType": int(date_type),
            "publishTimeWindowCode": 0,
        }
        # 自定义日期范围：额外带 startDate / endDate（平台字段名，前端 JS 取证）。
        if start_date:
            filter_map["startDate"] = str(start_date)
        if end_date:
            filter_map["endDate"] = str(end_date)
        body = {
            "requestBody": {
                "blockElements": [{
                    "filterMap": filter_map,
                    "orderBy": [{"field": "notePayGmv", "orderBy": "desc"}],
                    "page": max(1, int(page)),
                    "size": max(1, min(int(size), 50)),
                    "blockKey": block_key,
                }]
            }
        }
        return self._post(
            f"/api/edith/butterfly/data?type={block_key}",
            body,
            f"/app-datacenter/note-data/{category}",
        )


    def search_material_space(
        self,
        body: dict,
        *,
        referer: str = "/app-system/pictureSpace",
    ) -> tuple[bool, str, dict | None]:
        """图片空间列表一页（`POST /api/edith/product/material_space/search_directory_manageable`）。

        **⚠️ 必须带签名**（`_signed_post`）——这是本项目里**第一个「只读但需要签名」**的
        千帆端点。2026-10-06 真机对照（同一 Cookie、同一 body）：

        | 请求 | 结果 |
        |---|---|
        | 只带 Cookie（无签名） | **`{"code":-1,"success":false}`** |
        | 带 `x-s`/`x-t`（ark 档位） | **`{"code":200,...,"data":{"total":4910,...}}`** |

        ⇒ 与 `search_item_v2`（只读、无签名可直连）**不同**，本端点平台强制签名。
        签名机制复用 `_signed_post`（写链路在用的同一实现，`appId=itemark` /
        `signVersion=4.4.4` / `tier=0101`），**但本端点本身是只读查询**（不改平台状态）。
        硬约束②的判据是「有没有副作用」，不是「带没带签名」。

        `body` 由调用方（路由层）按白名单组装，逐字见真机抓包：

        ```json
        {"filter": {"keyword": "", "statuses": [1], "basicTypes": [1, 2],
                    "fatherDirectoryId": ""},
         "order": {"orderList": [{"field": "name", "asc": true}]},
         "pageIndex": 1, "pageSize": 100, "option": {"withDetail": true}}
        ```

        * `basicTypes`：**1=文件夹 2=文件**；
        * `statuses`：`[1]`=正常；
        * `fatherDirectoryId`：`""`=全部 / `"0"`=根下；
        * 返回 `data.directoryManageables[]`（条目字段见 `material_space_items()`）。
        """
        return self._signed_post(
            "/api/edith/product/material_space/search_directory_manageable",
            body,
            referer,
        )

    def get_material_directory(
        self,
        seller_id: str,
        *,
        referer: str = "/app-system/pictureSpace",
    ) -> tuple[bool, str, dict | None]:
        """图片空间**目录树**（`POST /api/edith/product/material_space/get_directory`）。

        无签名直连（2026-10-06 真机实测 200）。`option.withEntireTree=true` 取整棵树；
        返回 `data.directoryManageable`（单根对象，`additionalData.isLeaf` 判有无子级）。
        """
        body = {"sellerId": str(seller_id), "option": {"withEntireTree": True}}
        return self._post("/api/edith/product/material_space/get_directory", body, referer)

    def get_material_space_usage(
        self,
        *,
        referer: str = "/app-system/pictureSpace",
    ) -> tuple[bool, str, dict | None]:
        """图片空间**配额**（`GET /api/edith/product/material_space/get_seller_space_usage`）。

        无签名直连。返回 `data.{totalSpace, usedSpace, whiteSeller}`（**注意 `code` 是 `0`
        不是 `200`** —— `_get` 的 `code not in (0, None)` 判据已兼容）。
        """
        return self._get(
            "/api/edith/product/material_space/get_seller_space_usage", None, referer
        )

    # ------------------------------------------------------------------ #
    # 图片空间**写链路**（设计 22 §1.5，2026-10-06「操作按钮功能全部要做」）
    #
    # ⚠️ 与只读探测的边界（硬约束②）：「探测 / 调研 / 取证」一律只读，而这里是
    # **用户在本系统内对自己图片空间的显式操作**（与商品写链路 `batch_update_availability`
    # / 发货同一性质）⇒ 走「用户显式点击 + 本店归属 + 两层确认」。全部**必须签名**
    # （真机实测无签名 `code=-1`）。
    # ------------------------------------------------------------------ #

    def add_material_directories(
        self, seller_id: str, items: list[dict]
    ) -> tuple[bool, str, dict | None]:
        """**新建文件夹**（`batch_add_directory_manageable`）—— 需签名。

        `items` 逐条形状（真机 JS bundle 取证）：

        ```json
        {"sellerId": "<seller>", "type": 1, "name": "<文件夹名>", "size": 0,
         "additionalData": {"level": "0", "productCount": "0", "isLeaf": "true"}}
        ```
        """
        body = {
            "sellerId": str(seller_id),
            "directoryManageables": items,
            "option": {"async": False},
        }
        return self._signed_post(
            "/api/edith/product/material_space/batch_add_directory_manageable",
            body,
            "/app-system/pictureSpace",
        )

    def edit_material_manageables(
        self, seller_id: str, items: list[dict]
    ) -> tuple[bool, str, dict | None]:
        """**移动 / 重命名 / 删除（进回收站）/ 还原**（`batch_edit_directory_manageable`）—— 需签名。

        ⚠️ **平台要求传完整条目**（不是只传 id）。三种用法**字段完全不同**
        （2026-10-07 真机 JS bundle `332.d754c099.js` 取证，见各 builder 的 docstring）：

        * **移动** → `{…原条目…, fatherDirectoryId: <目标目录 id>}`（**不动 `status`**）；
        * **删除** → `{…原条目…, status: 4}`（`4 = RECYCLE_BIN`，进回收站）；
        * **还原** → `{…原条目…, status: 1}`（`1 = USABLE`，从回收站还原）。

        ⚠️ 曾经的错误实现：移动也置 `status:4` ⇒ **把文件移进回收站**（数据丢失级 bug）。
        """
        body = {
            "sellerId": str(seller_id),
            "directoryManageables": items,
            "option": {"async": False},
        }
        return self._signed_post(
            "/api/edith/product/material_space/batch_edit_directory_manageable",
            body,
            "/app-system/pictureSpace",
        )

    def delete_material_manageables(
        self, seller_id: str, elements: list[dict]
    ) -> tuple[bool, str, dict | None]:
        """**彻底删除**（`batch_delete_directory_manageable`）—— 需签名，**不可还原**。

        ⚠️ 请求键是 **`directoryManageableElements`**（不是 `directoryManageables`！），
        每条只要 `{id, type}`（真机 JS bundle `417.d61d3cd8.js` 逐字取证：

        ```json
        {"sellerId": "…", "directoryManageableElements": [{"id": "…", "type": 2}],
         "option": {"async": true}}
        ```

        此前用 `directoryManageables` 键发，平台一律回「文件信息不能为空」⇒ 被**误判**
        成「平台不接受该端点」。选对键后它正是回收站的「彻底删除」。
        """
        body = {
            "sellerId": str(seller_id),
            "directoryManageableElements": elements,
            "option": {"async": True},
        }
        return self._signed_post(
            "/api/edith/product/material_space/batch_delete_directory_manageable",
            body,
            "/app-system/pictureSpace",
        )

    def download_material_files(
        self, seller_id: str, links: list[dict], *, name: str = "", is_async: bool = True
    ) -> tuple[bool, str, dict | None]:
        """**批量下载**（`batch_download_files`，平台打包成任务）—— 需签名。

        ⚠️ 请求键是 **`links`**（不是 `directoryManageables`）；每条形状
        （真机 JS bundle `332.d754c099.js` `case 9` 逐字取证）：

        ```json
        // 文件：   {"type": 2, "link": "<additionalData.link 的 https url>", "name": "<名>", "extension": "<.ext>"}
        // 文件夹： {"type": 1, "directoryId": "<id>"}
        ```

        ⚠️ `isAsync` **必传**（真机实测缺它 ⇒ `code=-1 required param check: isAsync`）。
        返回任务 id，轮询 `query_material_task(task_id)` 取打包结果地址。
        """
        body = {
            "sellerId": str(seller_id),
            "links": links,
            "isAsync": bool(is_async),
        }
        if name:
            body["name"] = str(name)
        return self._signed_post(
            "/api/edith/product/material_space/batch_download_files",
            body,
            "/app-system/pictureSpace",
        )

    def query_material_task(self, task_id: str) -> tuple[bool, str, dict | None]:
        """查图片空间的异步任务（`GET /api/edith/product/query_task_status`）—— 无需签名。

        返回 `data.task`（下载打包等异步任务的进度 / 结果）。
        """
        return self._get(
            "/api/edith/product/query_task_status",
            {"taskId": str(task_id)},
            "/app-system/pictureSpace",
        )

    def check_resize_material_space(self) -> tuple[bool, str, dict | None]:
        """**查扩容条件**（`POST material_space/check_resize_material_space`）—— 需签名。

        真机 JS bundle `332.d754c099.js` 取证：`CheckResizeStorageModal` 打开时调它，
        返回 `conditionResults[]`，每条：

        ```json
        {"type": 1|…|4, "checkPass": true|false, "desc": "<条件原文>", "current": "<当前值>"}
        ```

        * `checkPass` 全为真 ⇒ **可申请扩容**（`i` 计算式：`0===len || some(!checkPass)`）；
        * `type === 4` 的 `current` = 「当前空间大小」（数字）——决定是否还需扩容；
        * 平台 UI 用 `Alert` 逐条渲染（`checkPass ? success : danger`，`description=desc`）。
        """
        return self._signed_post(
            "/api/edith/product/material_space/check_resize_material_space",
            {},
            "/app-system/pictureSpace",
        )

    def resize_material_space(self) -> tuple[bool, str, dict | None]:
        """**执行扩容**（`POST material_space/resize_material_space`）—— 需签名。

        ⚠️ 真机 JS bundle：`resize_material_space` **body 为空 `{}`**（无需参数；
        扩容额度由平台按条件判定）。成功后前端 `getStorage()` 重取配额 + toast「扩容成功」。
        """
        return self._signed_post(
            "/api/edith/product/material_space/resize_material_space",
            {},
            "/app-system/pictureSpace",
        )

    def register_material_files(
        self, seller_id: str, items: list[dict]
    ) -> tuple[bool, str, dict | None]:
        """把**已上传到云的文件**登记进图片空间（`batch_add_directory_manageable`，`type:2`）。

        图片空间上传 = 两步（与发布链路同形）：

        1. `upload_web_permit` 拿预分配 `fileId` + `uploadAddr` + `token`；
        2. `upload_file` 把字节 PUT 上去；
        3. **本方法**把上传结果登记成图片空间条目（`type:2`），列表才看得到。

        `items` 逐条形状（真机 JS bundle 的 add 调用体：文件也是 `batch_add`，
        与文件夹共用端点，靠 `type` 区分）：

        ```json
        {"sellerId": …, "type": 2, "fatherDirectoryId": "0",
         "name": "<无扩展名基名>", "size": <字节>,
         "additionalData": {"width": "800", "height": "800", "extension": ".jpeg",
                            "path": "material_space/<fileId>", "uploaderInfoModel": "…"}}
        ```
        """
        body = {
            "sellerId": str(seller_id),
            "directoryManageables": items,
            "option": {"async": False},
        }
        return self._signed_post(
            "/api/edith/product/material_space/batch_add_directory_manageable",
            body,
            "/app-system/pictureSpace",
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

    # ------------------------------------------------------------------ #
    # 订单二期写操作（设计 16 §7.4，M7）—— **真实平台写链路、不可撤回**
    #
    # ⚠️ 与发布（`publish_note`）同性质：仅限**用户本人店铺**、**用户显式点击
    # 触发**（每次独立二次确认）。**绝不自动触发**（不进定时任务 / 工作流）。
    #
    # ⚠️ **端点存在性已证实**（2026-10-04 只读探测，设计 16 §7.4.1），但
    # **精确请求体 schema 未取证** —— 只读抓不到提交体 ⇒ 下面这些 `body`
    # 是**按探测到的端点名 + 页面字段名的最优推断**，首次真实发货前必须
    # 用一笔真实订单核对（`probe_m7_ship_endpoints.py` 的后续步骤）。
    # ------------------------------------------------------------------ #

    def pre_check_before_delivery(self, body: dict) -> tuple[bool, str, dict | None]:
        """发货前校验（`POST /api/edith/fulfillment/pre_check_before_delivery`）。

        只读语义的**预检**（不落任何状态）—— 真实发货前先问平台「这单能不能发」，
        避免「点了发货才发现不可发」。响应 `code=0` 即通过。
        """
        return self._post(
            "/api/edith/fulfillment/pre_check_before_delivery",
            body if isinstance(body, dict) else {},
            "/app-fulfillment-order/order/shipping",
            timeout=WRITE_TIMEOUT,
        )

    def single_online_delivery(self, body: dict) -> tuple[bool, str, dict | None]:
        """**单个订单发货**（`POST /api/edith/fulfillment/single_online_delivery`）。

        ⚠️ **真实写操作、不可撤回** —— 调用前必须已完成显式 + 二次确认。

        `body` 字段（按页面字段名推断，**首发时核对**）：`package_id`（包裹 id）、
        `express_company_code`（物流公司编码）、`express_no`（运单号）等。
        """
        return self._post(
            "/api/edith/fulfillment/single_online_delivery",
            body if isinstance(body, dict) else {},
            "/app-fulfillment-order/order/shipping",
            timeout=WRITE_TIMEOUT,
        )

    def after_sale_audit(self, body: dict) -> tuple[bool, str, dict | None]:
        """**售后审核**（`POST /api/edith/after-sales/audit/`）—— 同意 / 拒绝。

        ⚠️ **真实写操作、不可撤回**。`body` 字段（推断，**首发时核对**）：
        `after_sale_id`、`audit_type`（同意/拒绝）、`reject_reason` 等。
        """
        return self._post(
            "/api/edith/after-sales/audit/",
            body if isinstance(body, dict) else {},
            "/app-fulfillment-aftersale",
            timeout=WRITE_TIMEOUT,
        )

    # ------------------------------------------------------------------ #
    # 商品**写**接口（2026-10-05 真机打通，设计 20 §11）
    #
    # ⚠️ 与只读接口的**关键差别**：这条链路的请求**必须带签名**
    # （`x-s` / `x-t` / `x-s-common`）。只带 Cookie 会被平台返 **406**
    # （`{"code":-1,"success":false}`，不是登录态失效）—— 详见 `_signed_post`。
    #
    # ⚠️ 这是**交易侧写操作**：会影响在售商品、删除不可恢复。
    # 调用方（`shop_item_operate_service`）必须已完成显式确认 + 二次确认，
    # 且**只对用户自己店铺**、**用户显式触发**（不进定时自动链路）。
    # ------------------------------------------------------------------ #

    def batch_update_availability(
        self, item_ids: "list[str] | tuple[str, ...] | set[str]", *, available: bool
    ) -> tuple[bool, str, dict | None]:
        """**批量上架 / 下架**（`POST /api/edith/product/batch_update_availability_v3`）。

        真机取证（2026-10-05，账号 8）：浏览器「批量操作 → 上架 / 下架」两个菜单项
        **同源同一函数**（`eh(true)` / `eh(false)` → `onBatchEditShelf`），发的就是本端点。

        | 字段 | 语义 |
        |---|---|
        | `available` | `True` = 上架，`False` = 下架 |
        | `itemIds` | 商品 ID 列表（**只传 item 级**；sku 级上下架走 `skuIds`，本项目不用） |
        | `skuIds` | 固定空数组（规格级上下架不在本项目范围） |
        | `asyncExec` | `True` ⇒ 平台返回 `data.taskId`（长任务）；`False` ⇒ 同步执行 |
        | `byModifyTool` | 固定 `True`（页面实抓值 —— 走的是「批量操作工具」这条链） |

        响应：成功 `{"code":200,"success":true,"data":{"taskId":"81432"}}`（真机原文）。
        部分失败时平台在 `data.failData: [{itemId, reasons:[{message}]}]` 里逐条给原因
        （与 `batch_delete_items` 同形）。

        ⚠️ **`asyncExec: True` 时返回的 `taskId` 是平台侧长任务号** —— 本项目**不轮询**
        它（要再逆一个 `long_task/task/detail`），以**自己的逐条调用结果**为准；
        `taskId` 只记入任务 payload 留痕（设计 20 §11 D15）。
        """
        ids = [str(item).strip() for item in (item_ids or []) if str(item).strip()]
        if not ids:
            return False, "item_ids 为空", None
        body = {
            "available": bool(available),
            "itemIds": ids,
            "skuIds": [],
            # ⚠️ 与页面一致：批量工具走异步通道（平台侧对大批量本来就是异步）。
            "asyncExec": True,
            "byModifyTool": True,
        }
        return self._signed_post(
            "/api/edith/product/batch_update_availability_v3",
            body,
            "/app-item/list/shelf",
        )

    def batch_delete_items(
        self, item_ids: "list[str] | tuple[str, ...] | set[str]"
    ) -> tuple[bool, str, dict | None]:
        """**批量删除商品**（`POST /api/edith/product/batch_delete_sku`）。

        真机取证（2026-10-05）：千帆「批量操作 → 删除」→ `onBatchDel` 发的就是本端点。

        | 字段 | 语义 |
        |---|---|
        | `itemIds` | 商品 ID 列表 |
        | `skuIds` | 固定空数组（规格级删除不在本项目范围） |
        | `options.asyncExec` | `True` ⇒ 平台异步（返回 `taskId`） |

        ⚠️ **不可恢复**：平台侧删除商品没有撤销入口。调用方必须先过
        「显式确认 + 二次确认」（设计 20 §11 D3）。
        """
        ids = [str(item).strip() for item in (item_ids or []) if str(item).strip()]
        if not ids:
            return False, "item_ids 为空", None
        body = {"itemIds": ids, "skuIds": [], "options": {"asyncExec": True}}
        return self._signed_post(
            "/api/edith/product/batch_delete_sku",
            body,
            "/app-item/list/shelf",
        )

    def batch_delete_notes(
        self, note_ids: "list[str] | tuple[str, ...] | set[str]"
    ) -> tuple[bool, str, dict | None]:
        """**批量删除笔记**（`POST /api/edith/note/batch_delete_note`）。

        真机取证（2026-10-06，账号 8）：千帆「笔记管理」页列表顶部**勾选笔记 →
        顶部「删除」按钮**，发的就是本端点。body 字段名是 **`note_ids`**（下划线，
        平台参数校验原话 ``check: note_ids, name: note_ids: list params value is
        required`` 钉死了这个键名，不是 JS 里的 camelCase `noteIds`）。

        ⚠️ 与商品删除（`batch_delete_items`）的关键差别：

        * **不需要签名**：本接口走 `_post`（纯 Cookie）即可，与只读的
          `goods-note/list` 同等级 —— 实测带不带签名都返回同样的参数校验响应
          （说明平台对这条链路**不强制 `x-s`**，与商品写接口的 406 强制不同）。
        * **不可恢复**：平台侧删除笔记没有撤销入口。调用方（无流量清理）必须
          已完成显式确认，且只对**用户自己店铺**、用户显式触发。

        | 字段 | 语义 |
        |---|---|
        | `note_ids` | 笔记 ID 列表（**下划线**，复数） |

        响应：成功 ``{"code":0,"success":true,"msg":"成功","data":{}}``（真机原文）。
        """
        ids = [str(item).strip() for item in (note_ids or []) if str(item).strip()]
        if not ids:
            return False, "note_ids 为空", None
        body = {"note_ids": ids}
        return self._post("/api/edith/note/batch_delete_note", body, "/app-note/note-list")

    # ------------------------------------------------------------------ #
    # 商品**新增（存草稿）**链路（设计 23，2026-10-07 真机打通）
    #
    # ⚠️ **本条链路只到草稿箱，没有「发布」这一格** —— 不是保守，是两层原因：
    #   ① 用户红线：新增的商品只能存草稿，不允许直接上架；
    #   ② 平台侧「提交商品」走另一条两段写链路（`publish_async` +
    #      `publish_async_operate`），本模块**不接入**（设计 23 §6）。
    #   所以下面没有 `publish_*` 方法 —— 契约层面就不存在那个开关。
    #
    # 全部**必须带签名**（与商品写链路同一档：
    # `appId=itemark` / `signVersion=4.4.4` / `tier=0101`）。
    # ------------------------------------------------------------------ #

    #: 平台**每分钟最多 2 个草稿**（2026-10-07 真机原文：
    #: `提示：单店铺每分钟限制发布2个草稿！`，`code=10001`）。
    #: 调用方按此排队（批量新增时），本层如实把该错误带回。
    DRAFT_RATE_LIMIT_CODE: int = 10001

    @classmethod
    def is_draft_rate_limited(cls, msg: object) -> bool:
        """平台是否答「草稿限流」。**唯一实现**（判据只此一处，不散落 `in` 判断）。"""
        text = str(msg or "")
        return "每分钟限制" in text or "限流" in text

    def publish_render(
        self, params: dict, *, referer: str = "/app-item/single-good/create"
    ) -> tuple[bool, str, dict | None]:
        """商品**表单结构 / 草稿回读**（`POST /api/edith/product/publish_render`）—— 需签名。

        ⚠️ **必须带签名**（2026-10-07 实测）：无签名直连 **406
        `{"code":-1,"success":false}`**；带 `x-s`/`x-t` 则 200。
        这与设计 20 §0 里「`publish_render` 不可得」的旧结论**不冲突**：
        那条结论说的是 2026-09-21 时**只读边界下**拿不到，而现在写链路
        （`_signed_post`）已经在项目里跑通了（设计 20 §11.2），
        所以同一个端点**换个档位就通了**。

        `params` 三种用法（真机逐条验证）：

        | 用法 | params | 回什么 |
        |---|---|---|
        | **新增**（空白表单） | `{"publishType":1,"sourceType":1}` | 28KB 空表单树（类目 61 个 + 卖家配置 + 物流计划） |
        | **编辑既有商品** | `{"publishType":2,"sourceType":1,"itemId":"…"}` | 完整商品（45 字段 + 204 条 SKU + 图集） |
        | **回读草稿** | `{"publishType":1,"sourceType":4,"draftId":"…"}` | 草稿里**实际存下的**字段（校验建草稿是否真落） |

        `params` 会被序列化成 **JSON 字符串**放进 `{"data": "<json>"}`
        （平台要的是「字符串里的 JSON」，不是嵌套对象）。

        响应 `data` 是**字符串**（不是对象）—— 调用方 `json.loads` 再用。
        """
        body = {"data": json.dumps(params, ensure_ascii=False)}
        return self._signed_post("/api/edith/product/publish_render", body, referer)

    def create_product_draft(
        self, draft: dict, *, referer: str = "/app-item/single-good/create"
    ) -> tuple[bool, str, dict | None]:
        """**把商品存进草稿箱**（`POST /api/edith/product/create_product_drafts`）—— 需签名。

        ⚠️ 2026-10-11 真机 bundle 修正（推翻 2026-10-07 旧结论）：body 形状
        **`{"draft": <完整 itemData 对象>}`**（`draft` 是**嵌套对象**，不是 JSON 字符串）。
        bundle `onSaveDraft` 逐字：`if(this.draftId) postSaveProductDrafts({draft:t})
        else postCreateProductDrafts({draft:t})` —— 新建走本接口、更新走 save。

        成功响应（真机原文）：

        ```json
        {"code":200,"success":true,"msg":"ok",
         "data":{"response":{"success":true,"code":200},
                 "productDraftId":"6ac673ae424eb00001f69ae9"}}
        ```

        ## ⚠️ 草稿体只吃这五个字段（真机逐字段试出来的）

        | 键 | 吃得下 | 回读位置 |
        |---|---|---|
        | `categoryId` | ✅ | `productDetail.item.categoryId` |
        | `name` | ✅ | `productDetail.item.itemName` |
        | `standardImages` | ✅ | `productDetail.item.standardImages`（**主图位**） |
        | `longImages` | ✅ | `productDetail.item.longImages`（**详情图位**） |
        | `skuData` | ✅ | `productDetail.skuList` |

        **静默丢弃**（传了不报错、回读为 `[]` —— 本链路最大的坑）：
        `itemName` / `properties` / `images` / `imageDescriptions` / **`skuList`**。

        ⚠️ **`skuData` 与 `skuList` 是两个不同的字段**：**写**用 `skuData`、
        **读**回是 `skuList`。写的时候用 `skuList` 会被**静默丢弃**（skuCount=0，
        接口照样 200）。这个不对称坑过一次，故在此写死。

        ⚠️ **SKU 的 `price` 单位是「元」不是「分」**（与商品列表的
        `price_from_fen` 相反）。真机：传 `1280` 回读 `price: 1280`。
        写成分（`128000`）不报错，但价格会变成 12.8 万元 —— **静默错值**。

        ⚠️ 平台限流：**单店铺每分钟 2 个草稿**（`code=10001`）。
        调用方按 :meth:`is_draft_rate_limited` 判定并重试。
        """
        body = {"draft": draft}
        return self._signed_post(
            "/api/edith/product/create_product_drafts", body, referer
        )

    def save_product_draft(
        self, draft: dict, *, referer: str = "/app-item/single-good/create"
    ) -> tuple[bool, str, dict | None]:
        """**保存商品草稿**（`POST /api/edith/product/save_product_drafts`）—— 需签名。

        ⚠️ 2026-10-11 真机抓包：现在的「保存草稿」按钮走的是 **本接口**（不是旧的
        `create_product_drafts`）。body 形状 **`{"draft": <完整 itemData 对象>}`**（`draft` 是
        **嵌套对象**，不是 JSON 字符串），结构与发布体 `batch_create_item` 的 `itemData[0]`
        **一致**（含 `videos` / `ename` / `description` / `attributes` / `skuData` 全部字段）。

        ⚠️ 与旧的 `create_product_drafts`（`{draft: "<5键json字符串>"}`，只吃
        `categoryId/name/standardImages/longImages/skuData`）**完全不同**：旧接口已被新接口
        取代，旧接口收不下视频/货号/英文名/属性等完整字段。
        """
        body = {"draft": draft}
        return self._signed_post(
            "/api/edith/product/save_product_drafts", body, referer
        )

    def query_product_draft_list(
        self,
        *,
        page_no: int = 1,
        page_size: int = 20,
        search_param: dict | None = None,
        sort_field: str | None = None,
        order: str | None = None,
    ) -> tuple[bool, str, dict | None]:
        """**草稿箱列表**（`POST /api/edith/product/query_product_draft_list_v3`）。

        ⚠️ **不需要签名**（2026-10-07 实测：无签名 200，与只读接口同档）——
        与 `create_product_drafts`（**必须签名**）刻意不同。走 `_post`。

        请求体取自 bundle `8591.80981179.js` 的 `fetchList`：

        ```json
        {"pageNo":1,"pageSize":20,
         "searchParam":{"keyword":"…","createTimeFrom":<秒>,"createTimeTo":<秒>,
                        "updateTimeFrom":<秒>,"updateTimeTo":<秒>,
                        "lvl2CategoryIds":[…],"lvl3CategoryIds":[…],"topCategoryIds":[…]},
         "sortField":"create_time","order":"desc"}
        ```

        响应 `data`：`{"total": N, "draftIds": [...], "productDrafts": [
        {"id","name","status","createTime","updateTime","productUnionType","image"}]}`。
        ⚠️ `draftIds` 与 `productDrafts` **同时下发**且顺序一致 —— 用
        `productDrafts`（它带名字），`draftIds` 只是同物的 id 投影。

        ⚠️ 草稿的 `status`：`"0"` = 待提交，`"-1"` = 已删除
        （bundle：`"-1"===status` ⇒ tag 显示「已删除」，否则「待提交」）。
        """
        body: dict = {
            "pageNo": max(1, int(page_no)),
            "pageSize": max(1, min(int(page_size), 100)),
        }
        if search_param:
            body["searchParam"] = search_param
        if sort_field:
            body["sortField"] = str(sort_field)
        if order:
            body["order"] = str(order)
        return self._post(
            "/api/edith/product/query_product_draft_list_v3",
            body,
            "/app-item/list/draft",
        )

    def batch_delete_draft(
        self,
        draft_ids: "list[str] | tuple[str, ...] | set[str]",
        *,
        referer: str = "/app-item/list/draft",
    ) -> tuple[bool, str, dict | None]:
        """**批量删除草稿**（`POST /api/edith/product/batch_delete_draft`）—— 需签名。

        真机 bundle `8591.80981179.js` 逐字取证：body `{productDraftIds:[…], channel:2}`。
        ⚠️ **不可恢复**（草稿删了没有撤销入口）——调用方必须显式确认。

        与「删除商品」（`batch_delete_sku`）是**两个端点**：草稿在草稿箱，
        商品在商品列表，别混用。
        """
        ids = [str(x).strip() for x in (draft_ids or []) if str(x).strip()]
        if not ids:
            return False, "productDraftIds 为空", None
        body = {"productDraftIds": ids, "channel": 2}
        return self._signed_post("/api/edith/product/batch_delete_draft", body, referer)

    def search_category(
        self, keyword: str, *, referer: str = "/app-item/single-good/create"
    ) -> tuple[bool, str, dict | None]:
        """按关键词查**类目**（`POST /api/edith/product/search_category`）—— 无需签名。

        ⚠️ **无签名直连可通**（2026-10-07 实测 200，属查询语义）。

        响应 `data.categories[]`，每条含 `id` / `name` / `level` /
        **`isLeaf`**（只有 `isLeaf=true` 的才能作为商品类目）/
        `needBrand`（该类目是否强制品牌）/ `channelList`。

        调用方**只能选 `isLeaf=true`** —— 选非叶子类目建草稿会被平台打回
        （类目树是分层的，商品必须挂在叶子上）。
        """
        return self._post(
            "/api/edith/product/search_category",
            {"keyword": str(keyword or "").strip()},
            referer,
        )

    def predict_product_property(
        self,
        category_id: str,
        name: str = "",
        *,
        image_urls: "list[dict] | None" = None,
    ) -> tuple[bool, str, dict | None]:
        """**属性预测**（千帆 `predictProductProperty`，只读）。

        ## ⚠️ 入参以真机抓包为准（2026-10-09 复测）

        ```
        {"title": "<商品标题>", "categoryId": "<叶子类目 id>",
         "imageUrls": [{"key","type":"spaceImage","materialId","name",
                        "width","height","size","extension","path","link",
                        "uploaderInfoModel":{…},"errors":[]}]}
        ```

        实测三态（账号 8 · 类目 `65f997293e946300016b2ec4`）：

        | 入参 | 结果 |
        |---|---|
        | `{categoryId}` | `predictItemProperties: []` |
        | `{title, categoryId}` | `predictItemProperties: []` |
        | `{title, categoryId, imageUrls[…]}` | **有值**（`风格=明清古典` …） |

        ⇒ **不给图 = 平台没料可预测**，恒回空数组。第一条入参名是 `title`，
        用 `name` 平台答 `success:false`（实测）。

        响应形态（每条）：
        ```
        {"fillType":6, "inputType":1, "propertyId":"…", "propertyName":"风格",
         "propertyValueList":[{"value":"明清古典","valueId":"66264f7d…"}]}
        ```
        ⚠️ 值是 **`propertyValueList` 数组**（不是标量）—— 解析在后端端点做。
        """
        param: dict = {"categoryId": category_id}
        if name:
            param["title"] = str(name)
        if image_urls:
            param["imageUrls"] = list(image_urls)
        body = {
            "data": json.dumps(
                {"publishType": 1, "sourceType": 1, "actions": [{"action": "predictProductProperty", "param": param}]},
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, "/app-item/good/create")

    def get_seller_carriage_template(self) -> tuple[bool, str, dict | None]:
        """取**物流模板候选**（千帆 `getSellerCarriageTemplate`，只读）。

        实测入参为空即可；返回 `shippingTemplateList[]` + `defaultShippingTemplate`。
        """
        body = {
            "data": json.dumps(
                {"publishType": 1, "sourceType": 1, "actions": [{"action": "getSellerCarriageTemplate", "param": {}}]},
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, "/app-item/good/create")

    def get_support_after_sale(self, category_id: str) -> tuple[bool, str, dict | None]:
        """取类目的**售后服务规则**（千帆 `getSupportAfterSale`，只读）。

        ## 真机实测（2026-10-09，八个类目对过）

        响应落在 `data.fulfillmentInfo.afterSaleRule[]`，每项：

        ```
        {"name":"支持七天无理由退货","type":"freeReturn",
         "defaultSupport":true,"mustSupport":true}
        ```

        ⚠️ **`mustSupport` 按类目不同** —— 这就是「七天无理由是否可编辑」的判据：

        | 类目 | mustSupport | 含义 |
        |---|---|---|
        | 其他宠物服务 / 梳妆台桌 / 斗柜 | `true` | **必须支持** ⇒ 勾选强制、不可取消 |
        | 实体服务 / 求职留学咨询 | `false` | **可选** ⇒ 用户可自己决定 |

        ⚠️ 规则集本身也按类目变：如「搬家服务券」给的是
        `未核销随时退（refundableIfUnredeemed）` + `超期自动退（autoRefundAfterExpiry）`
        （后者带 `autoRefundAfterExpiryTimeRule[]` 时限），**不是**必有一项 freeReturn。
        ⇒ 前端不得写死「支持七天无理由退货」这一条，要按本接口的 `name`/`type` 渲染。
        """
        body = {
            "data": json.dumps(
                {
                    "publishType": 1,
                    "sourceType": 1,
                    "actions": [
                        {"action": "getSupportAfterSale", "param": {"categoryId": category_id}}
                    ],
                },
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, "/app-item/good/create")

    def generate_item_short_title(
        self,
        name: str,
        *,
        category_id: str = "",
        referer: str = "/app-item/good/create",
    ) -> tuple[bool, str, dict | None]:
        """生成**导购短标题**（`publish_async` 的 `generateItemShortTitle`）。

        真机（2026-10-08）：发布页「导购短标题」右侧有「智能生成」按钮，
        走的就是这条 action —— 与商品标题的 `getRecommendItemTitle` 同款口径：
        **能自动生成的就走平台生成**，不拿本地文本冒充。

        ## ⚠️ 入参以真机抓包为准（2026-10-09 复测，见 `.hermes/tmp/evidence_publish/all_edith_requests.json`）

        ```
        {"title": "<商品标题>", "categoryId": "<叶子类目 id>"}
        ```

        **不是** `name`（那是 `predictProductProperty` 的参数名），
        也**没有** `mainImages` —— 平台只吃标题 + 类目。
        传错参数名不会报错、只会**静默返回空短标题**（实测），所以这里按真机原样写死。

        响应取值口径：`data.product.productTitle.itemShortTitle.generatedShortTitle`
        （解析在后端端点做，本方法只负责发请求）。

        该 action **需签名**（与其它写语义 action 同档）。

        ## 红线

        只调这一个生成文案的 action，**不碰**发布提交链路。
        """
        param: dict = {"title": str(name or "")}
        if category_id:
            param["categoryId"] = str(category_id)

        body = {
            "data": json.dumps(
                {
                    "publishType": 1,
                    "sourceType": 1,
                    "actions": [
                        {"action": "generateItemShortTitle", "param": param}
                    ],
                },
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, referer)

    def recommend_category(
        self,
        name: str,
        *,
        image_url: str = "",
        uploader_info_model: "dict | None" = None,
        referer: str = "/app-item/good/create",
    ) -> tuple[bool, str, dict | None]:
        """按**标题 + 主图**推荐商品类目（`publish_async` 的 `getRecommendCategory`）。

        ## 为什么是这一条（2026-10-08 真机取证）

        千帆发布页在**标题采用之后**自动带出类目。抓包显示那一刻
        **没有**类目请求 —— 因为服务端把类目**和标题一起**算好了：
        `getRecommendItemTitle` 的响应里 `category.categoryPropertyInfo`
        留了位置；前端另有一次 `getRecommendCategory`（bundle 逆出）。

        入参（bundle `8091.*.js` 逆出，与真机一致）：

        ```
        {"name": "<商品标题>",
         "imageUrl": "<主图签名链接>",
         "uploaderInfoModel": {"url": "…", "bizName": "ark", "scene": "goods_image",
                               "fileId": "arkgoods/<id>", "isSecret": true, "cloudType": 4}}
        ```

        响应 `data.recommendCategories[]`，每项：
        `{categoryId, categoryName, marketable, score, path[]}`
        —— `path` 就是「住宅家具 > 桌类 > 梳妆台/桌」。

        ⚠️ **要签名**（只带 Cookie 重放 ⇒ 406 `{"code":-1}`，已实测）。

        ## 红线

        只调这一个推荐 action（纯查询语义），**不碰**发布提交链路。
        """
        payload: dict = {"name": str(name or "")}
        if image_url:
            payload["imageUrl"] = image_url
        if uploader_info_model:
            payload["uploaderInfoModel"] = uploader_info_model

        body = {
            "data": json.dumps(
                {
                    "publishType": 1,
                    "sourceType": 1,
                    "actions": [
                        {"action": "getRecommendCategory", "param": payload}
                    ],
                },
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, referer)

    def recommend_item_title(
        self,
        main_images: "list[dict]",
        *,
        referer: str = "/app-item/good/create",
    ) -> tuple[bool, str, dict | None]:
        """按**主图**推荐商品标题（`publish_async` 的 `getRecommendItemTitle` action）。

        ## 为什么必须这么调（2026-10-08 真机抓包）

        千帆发布页在上传主图后会**自动**触发一次该 action，入参是**主图对象数组**：

        ```
        {"mainImages": [{
           "type": "uploadImage", "name": "主图02", "width": 750, "height": 1000,
           "extension": ".jpeg", "size": 195285,
           "link": "https://ros-preview.xhscdn.com/arkgoods/<id>?sign=…&t=…",
           "path": "arkgoods/<id>",
           "uploaderInfoModel": {"url": "<同一个签名地址>", "bizName": "ark",
                                 "scene": "goods_image", "fileId": "arkgoods/<id>",
                                 "isSecret": True, "cloudType": 4},
           "status": "success"}]}
        ```

        ⚠️ **`link` 是签名图链** —— 平台把**图喂给模型**，所以标题才与图相符。
        只传文件名（或纯文本线索）会让模型看不到图，生成出与图无关的标题
        （实测：数据包是梳妆台，却生成了「现代简约书桌…」）。

        ⚠️ 该 action **要签名**（只带 Cookie 重放 ⇒ **406 `{"code":-1}`**，已实测）。

        ## 红线

        本方法**只调这一个只读语义的 action**（生成标题文本），
        **不碰**发布链路的 `itemAuditPreCheck` / 提交类 action ——
        `publish_async` 整体仍不在本模块的接入范围（设计 23 §6）。

        响应里的标题字段名随平台可能变化，调用方按 `data` 内的
        `title` → `itemTitle` → `recommendTitle` 依次取（**取不到就不猜**）。
        """
        body = {
            "data": json.dumps(
                {
                    "publishType": 1,
                    "sourceType": 1,
                    "actions": [
                        {
                            "action": "getRecommendItemTitle",
                            "param": {"mainImages": list(main_images)},
                        }
                    ],
                },
                ensure_ascii=False,
            )
        }
        return self._signed_post("/api/edith/product/publish_async", body, referer)

    def query_task_status(
        self, task_id: str, *, referer: str = "/app-item/single-good/create"
    ) -> tuple[bool, str, dict | None]:
        """查**异步任务**状态（`GET /api/edith/product/query_task_status`）—— 需签名。

        ⚠️ 与 `create_product_draft`（POST、需签名）不同：本端点是 **GET 且需签名**
        （真机实测：不带签名 `{"code":-1}`，带签名 200）。这是仓库里**第一个
        「GET 但强制签名」**的千帆端点 —— 商品的异步任务（`batch_create_item` /
        `batch_update_availability_v3`）都靠它取回执。

        签名覆盖的串是 **含 query 的完整路径**
        （`/api/edith/product/query_task_status?taskId=…`）—— 与 POST 的
        「路径 + JSON body」不同；:meth:`_signed_get` 负责这个差异。

        响应 `data.task`：`status`（1/2 进行中，3 成功，4/5 失败或部分失败）/
        `progress` / `totalCount` / `finishedCount` / `result`（`extraJson` 里
        逐条 `failData[]`，形如 `[{"index":1,"name":"未知","message":"…"}]`）。
        """
        from urllib.parse import quote

        path = f"/api/edith/product/query_task_status?taskId={quote(str(task_id))}"
        return self._signed_get(path, referer)

    def batch_create_item(
        self,
        items: list[dict],
        *,
        publish_mode: int = 1,
        referer: str = "/app-item/single-good/create",
    ) -> tuple[bool, str, dict | None]:
        """**提交商品发布**（`POST /api/edith/product/batch_create_item`）—— 需签名。

        ⚠️⚠️ **这是真实上架写操作**：成功即产生一个**在售商品**。
        调用方必须（设计 24）：
        ① 用户显式触发 + 二次确认；② 平台回执成功**第一时间下架**
        （`batch_update_availability(available=False)`）；③ 全程留痕在 `tasks`。

        ⚠️ **`publishMode`**（bundle 逆出）：`1` = 普通发布（默认）/ `2` = 基础图
        创建 / `3` = AI 助手 —— 本项目只用 `1`。

        ⚠️ **`itemData[]` 每条是一份完整的商品表单**（45+ 字段），形状必须与
        `publish_render` 回读的 `productDetail.item` **一致**
        （`itemName` / `categoryId` / `categoryPath` / `images` / `standardImages` /
        `imageDescriptions` / `properties` / `shippingTemplateId` / `sellPoint` …）。
        **本方法不组装 itemData** —— 那是 `shop_draft_service` 的职责
        （它会用 `publish_render` 的回读骨架 + 用户填的值合并，见设计 24 §4）。

        响应：`{"code":200,"data":{"taskId":"…"}}` —— 异步任务，结果用
        :meth:`query_task_status` 轮询。
        """
        ids = [item for item in (items or []) if isinstance(item, dict)]
        if not ids:
            return False, "itemData 为空", None
        body = {
            "itemData": ids,
            "options": {"asyncExec": True, "withChild": True, "retrieveResult": True},
            "publishMode": int(publish_mode),
        }
        return self._signed_post(
            "/api/edith/product/batch_create_item", body, referer
        )

    def _signed_get(
        self, path_with_query: str, referer: str
    ) -> tuple[bool, str, dict | None]:
        """**带签名的 GET**（`:meth:`query_task_status` 用）。

        ⚠️ 与 `_signed_post` 的两处差异（都来自真机取证）：
        * 签名材料里的 `api` 是**含 query 的完整路径**（不含 host）；
        * 请求发 GET，`data` 为空串（签名算法对 GET 用 `md5(api)`）。

        实现上把 `_signed_post` 的签名段抽成 `_sign(api, data)`，本方法复用同
        一份签名运行时（**不复制一份签名逻辑** —— 两份必然漂移）。
        """
        headers, error, signed = self._sign(path_with_query, None, referer)
        if error is not None:
            return False, error, None
        try:
            response = _session_request(
                "GET",
                self.base_url + path_with_query,
                headers=headers,
                cookies=self._cookie_dict(),
                timeout=WRITE_TIMEOUT,
            )
        except Exception as error_obj:  # noqa: BLE001
            return False, f"千帆写接口请求失败：{type(error_obj).__name__}: {error_obj}", None
        if response.status_code == 406:
            return (
                False,
                "平台拒绝了本次请求的签名（HTTP 406）。通常是平台改版导致签名档位变化，"
                "需要更新签名参数；重登账号不会解决。",
                None,
            )
        try:
            payload = response.json()
        except ValueError:
            return False, f"千帆写接口返回了非 JSON 响应（HTTP {response.status_code}）", None
        if not isinstance(payload, dict):
            return False, "千帆写接口返回了非预期的响应体", None
        code = payload.get("code")
        if self.is_credential_error(code):
            return False, str(payload.get("msg") or "千帆登录态失效"), payload
        if code not in (0, 200, None) or payload.get("success") is False:
            return False, str(payload.get("msg") or f"千帆写接口返回 code={code}"), payload
        return True, str(payload.get("msg") or "成功"), payload

    def _sign(
        self, api: str, body: dict | None, referer: str
    ) -> tuple[dict[str, str] | None, str | None, dict | None]:
        """生成签名头。**唯一实现**（`_signed_post` / `_signed_get` 共用）。

        返回 `(headers, error_message, signed_raw)`；成功时 `error_message is None`。
        """
        import subprocess

        from xhs_utils.xhs_core.runtime import _JS_DIR as _CORE_JS_DIR

        cookie = (self.cookies or "").strip()
        if "a1=" not in cookie:
            return (
                None,
                "签名材料不可用：该账号的千帆 Cookie 里没有 a1，无法生成请求签名"
                "（这不是登录态问题，请重新导出完整 Cookie）。",
                None,
            )

        # 会话态（seq / envConst / envFpTail / loadts）由 profile 现取 —— 每次请求
        # 一个 `seq`，与浏览器一致（同一 seq 连发多个请求会被平台判重）。
        try:
            from xhs_utils.xhs_pc.state import PcDeviceProfile

            profile = PcDeviceProfile(cookies=cookie)
            ctx = profile.next_sign_context(api)
            payload = {
                "api": api,
                #: ⚠️ GET 用空串（签名算法对 GET 走 `md5(api)`），POST 用 body。
                "data": "" if body is None else body,
                "cookie": cookie,
                "a1": profile.cookie_map.get("a1", ""),
                "b1": str(profile.current_b1(ctx["now"])),
                "dslPair": _dsl_pair_or_empty(),
                "tier": "0101",
                "now": ctx["now"],
                "version": ctx["version"],
                "loadts": ctx["loadts"],
                "seq": ctx["seq"],
                "envConst": ctx["envConst"],
                "envFpTail": ctx["envFpTail"],
                "webBuild": ctx["webBuild"],
                "signVersion": "4.4.4",
                "appId": "itemark",
                "platform": "Windows",
            }
        except Exception as error:  # noqa: BLE001 - 组装失败也要变成可读原因
            return (
                None,
                f"签名材料不可用：{type(error).__name__}: {error}（这不是登录态问题）",
                None,
            )

        # ⚠️ **签名入参必须走 `@file` 形态，不能当命令行参数传**（2026-10-07 实测）：
        # Windows 命令行上限 32767 字符，而 `payload` 里含**整个请求体**
        # （商品草稿体实测 ~10KB+，含图片条目 + SKU 列表）⇒ 直接当 argv 传会命中
        # `OSError: [WinError 206] 文件名或扩展名太长`，表现为「签名运行时不可用」，
        # **与真实原因（body 太大）完全指错方向**。
        #
        # ⚠️⚠️ **文件名必须每个请求唯一**（DEF-101，2026-10-09）：前端并行拉
        # 「运费模板 + 物流模板」两个签名请求（都是 `_signed_post`），若共用一个固定
        # `.sign_input.json`，两个线程会**同时**写/删同一个文件 —— A 写、B 覆盖、A 读
        # 到半截 JSON、B 删掉 A 正在读的文件 ⇒ `node sign.js` 读到空/残缺输入 ⇒
        # 报 `cookie required` / `Unexpected end of JSON input`（错误尾巴被截成
        # `Node.js v22.23.2`）。实测 16 并发 15 失败、两个下拉恒空。
        # 用 `tempfile.mkstemp`（原子创建、名字带 pid+随机后缀）彻底隔离并发。
        import tempfile

        fd, sign_input = tempfile.mkstemp(
            prefix=".sign_input_", suffix=".json", dir=_CORE_JS_DIR
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
        except OSError as error:
            try:
                os.remove(sign_input)
            except OSError:
                pass
            return (
                None,
                f"签名材料不可用：写入签名临时文件失败 {type(error).__name__}: {error}"
                "（这不是登录态问题）",
                None,
            )

        try:
            proc = subprocess.run(
                [_node_exe(), "sign.js", "@" + os.path.basename(sign_input)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_CORE_JS_DIR,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return (
                None,
                f"签名运行时不可用（Node）：{type(error).__name__}: {error}（这不是登录态问题）",
                None,
            )
        finally:
            # ⚠️ **必须清掉临时文件**：里面含 Cookie（签名入参的一部分），
            # 留在磁盘上等于把凭据写进工作目录（硬约束②：不落盘 / 不打印）。
            try:
                os.remove(sign_input)
            except OSError:
                pass

        signed: dict | None = None
        for line in reversed([s.strip() for s in (proc.stdout or "").splitlines() if s.strip()]):
            if line.startswith("{") and '"x3"' in line:
                try:
                    signed = json.loads(line)
                except ValueError:
                    continue
                break
        if not signed or not signed.get("xs"):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return (
                None,
                "请求签名生成失败：" + (" / ".join(tail[-3:]) or "sign.js 无输出")
                + "（这不是登录态问题）",
                None,
            )

        headers = {
            **self._headers(referer),
            "x-s": str(signed["xs"]),
            "x-t": str(signed.get("xt") or ""),
        }
        if signed.get("xs_common"):
            headers["x-s-common"] = str(signed["xs_common"])
        return headers, None, signed

    def _signed_post(
        self, api: str, body: dict, referer: str
    ) -> tuple[bool, str, dict | None]:
        """**带签名的 POST**（商品写接口专用；只读接口不需要它）。

        ## 为什么只读不需要、写必须

        2026-10-05 真机对照（同一 Cookie、同一端点）：

        | 请求 | 结果 |
        |---|---|
        | 只带 Cookie（只读 `search_item_v2`） | **200 code=200** |
        | 只带 Cookie（写 `batch_update_availability_v3`） | **406 `{"code":-1,"success":false}`** |
        | 带 `x-s`/`x-t`/`x-s-common`（写） | **200 `{"code":200,"success":true,"data":{"taskId":"81432"}}`** |

        ⇒ 写链路的**签名由平台强制**（就是设计 20 §0 里那句「必须带 `X-s`/`X-t`」）。

        ## 签名参数（三个必须偏离 PC 默认值，否则 406）

        复用本仓 `xhs_utils/xhs_core/js/sign.js`（PC / creator 两条链路在用的同一套
        Node 签名运行时），但 **ark 这第三个 appId 有它自己的档位**：

        | 参数 | PC 默认 | **ark 实测可用** |
        |---|---|---|
        | `appId` | `xhs-pc-web` | **`itemark`** |
        | `signVersion` | `4.3.7` | **`4.4.4`** |
        | `tier` | `0301` | **`0101`** |

        浏览器真实 `X-s` 解出来是
        `{"x0":"4.4.4","x1":"itemark","x2":"Windows","x3":"mns0101_…","x4":"object","x5":"<md5>"}`
        —— 与上面三个值逐字段对齐（`x5` = `md5(api + JSON.stringify(body))`，
        已用真机流量核对命中）。

        ## 失败语义（**必须可分辨**）

        * 拿不到签名材料（无 `a1` / DS 取不到 / Node 不在）⇒ `(False, "签名材料不可用…", None)`
          —— **不是**登录态问题，重登没用；
        * 平台返 406 ⇒ `(False, "平台拒绝了请求签名…", payload)` —— 同上；
        * 平台返 `code=-13000/-13004` ⇒ 走既有的凭据判据（`is_credential_error`）。

        ⚠️ **不打印 Cookie / 签名值**（硬约束②）。
        """
        headers, error, _signed = self._sign(api, body, referer)
        if error is not None:
            return False, error, None
        #: 写请求必须显式声明 JSON（签名头已由 `_sign` 带齐）。
        headers["Content-Type"] = "application/json"

        try:
            response = _session_request(
                "POST",
                self.base_url + api,
                data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                headers=headers,
                cookies=self._cookie_dict(),
                timeout=WRITE_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001
            return False, f"千帆写接口请求失败：{type(error).__name__}: {error}", None

        # ⚠️ **406 是签名被拒**，不是登录态失效（真机原文
        # `{"code":-1,"success":false}`）。必须与凭据问题分开报 ——
        # 错归因会让用户白重登一百次。
        if response.status_code == 406:
            return (
                False,
                "平台拒绝了本次请求的签名（HTTP 406）。通常是平台改版导致签名档位变化，"
                "需要更新签名参数；重登账号不会解决。",
                None,
            )
        try:
            payload = response.json()
        except ValueError:
            return False, f"千帆写接口返回了非 JSON 响应（HTTP {response.status_code}）", None
        if not isinstance(payload, dict):
            return False, "千帆写接口返回了非预期的响应体", None
        code = payload.get("code")
        if self.is_credential_error(code):
            return False, str(payload.get("msg") or "千帆登录态失效"), payload
        if code not in (0, 200, None) or payload.get("success") is False:
            return False, str(payload.get("msg") or f"千帆写接口返回 code={code}"), payload
        return True, str(payload.get("msg") or "成功"), payload

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

    def upload_web_permit(
        self, file_count: int = 1, *, biz_name: str = "spectrum", scene: str = "image",
        referer: str = "/app-note/publish", file_format: str = "",
        timeout: float | None = None,
    ) -> tuple[bool, str, dict | None]:
        """申请上传凭证（`GET /api/edith/upload/web/permit`）。

        ⚠️ **`biz_name` / `scene` 必须按业务点传对**（2026-10-07 真机抓包）：
        图片空间（`/app-system/pictureSpace`）用的是 **`biz_name=ark&scene=goods_image`**
        —— 预分配 `fileIds` 落在 **`arkgoods/<id>`** 桶，登记后链接
        `https://qimg.xiaohongshu.com/arkgoods/<id>` **可正常显示**；
        发布链路（`/app-note/publish`）才是 `spectrum/image`（落 `spectrum/<id>`）。
        用错 scene ⇒ 文件 PUT 到 `spectrum/` 桶而图片空间按 `arkgoods/` 取 ⇒ **裂图**。

        ⚠️⚠️ **视频也是 `goods_video`，不是 `common_file`**（真机 2026-10-22 抓包坐实，
        此前依据 `9515.*.js` 里的一句 `scene:"common_file"` 判错，已推翻）：

            申请凭证  GET  /api/edith/upload/web/permit?biz_name=ark&scene=goods_video&file_count=1&version=1
            上传      POST https://ros-upload.xiaohongshu.com/?uploads&prefix=arkgoods/<id>
            分片      POST …/arkgoods/<id>?partNumber=1|2|3&uploadId=…
            完成      POST …/arkgoods/<id>?uploadId=…
            登记      POST /api/edith/product/material_space/batch_add_directory_manageable

        ⇒ 视频与图片**同桶**（`arkgoods/`），所以**必须**走图片空间登记拿 `materialId`；
        用 `common_file` 会落进 `ark-common/`，登记恒答「获取上传文件[v]信息失败」
        ⇒ `materialId` 空 ⇒ 商品发布后主图视频是空白框、播不了。

        ⚠️ **`file_format`**：9515 上传器把扩展名作为 query 参数传给本接口
        （`params:{…, fileFormat: e.fileFormat}`）—— 视频要传（如 `.mp4`）。

        响应 `data.uploadTempPermits[]` 每项含 `uploadAddr` / `token` / `fileIds[]` /
        `expireTime`；**按 `qos` 降序取第一个**（平台前端 `sort((a,b)=>b.qos-a.qos)[0]`）。
        """
        params = {
            "biz_name": str(biz_name),
            "scene": str(scene),
            "file_count": max(1, int(file_count)),
            "version": 1,
        }
        if file_format:
            params["fileFormat"] = str(file_format)
        #: ⚠️ 视频场景（`goods_video`）实测偶发 >15s（默认 `REQUEST_TIMEOUT`）⇒ 超时。
        #: 申请凭证是**写链路的前置**，按 `WRITE_TIMEOUT` 给足。
        return self._get(
            "/api/edith/upload/web/permit",
            params,
            referer,
            timeout=timeout,
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
        # ⚠️ **PUT 的响应头里带 `X-Ros-Preview-Url`**（2026-10-07 真机抓包）——
        # 登记条目 `uploaderInfoModel` 要的就是这个**带签名的预览地址**；丢弃它会让
        # 图片空间条目登记成功但按公开 CDN 取图 **404**（裂图）。
        return True, "成功", {
            "preview_url": response.headers.get("X-Ros-Preview-Url", ""),
            "etag": response.headers.get("Etag", ""),
            "content_type": response.headers.get("Content-Type", ""),
        }

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
        #: ⚠️ **Referer 要带 AGI 场景参数**（2026-10-19 真机实测）：千帆「选择商品发笔记」
        #: 的真实页面 URL 是 `/app-note/publish?step=notePublish&mainPublishType=agi
        #: &itemId=…&skuId=…&agi=true` —— `mainPublishType=agi` 是「这条笔记挂在商品下」
        #: 的场景标识。只发裸标 `/app-note/publish` 时，平台照样回 `success`、笔记也发出去，
        #: 但**不挂商品、不进商品笔记列表**。
        _referer = "/app-note/publish"
        body = dict(body or {})
        _meta = body.pop("_meta", None) or {}
        try:
            _common_ref = (body or {}).get("common") or {}
            _biz_ref = _common_ref.get("biz_relations") or []
            if _biz_ref:
                _biz_id = str((_biz_ref[0] or {}).get("biz_id") or "")
                _extra = _meta
                _item_id = str(_extra.get("item_id") or "")
                _sku_name = str(_extra.get("sku_name") or "")
                _referer = (
                    "/app-note/publish?step=notePublish&mainPublishType=agi"
                    f"&itemId={_item_id}&skuId={_biz_id}"
                    f"&skuName={quote(_sku_name)}&agi=true"
                )
        except Exception:  # noqa: BLE001 - Referer 拼装失败退回裸标
            _referer = "/app-note/publish"
        return self._post(
            "/api/edith/note/publish",
            body if isinstance(body, dict) else {},
            _referer,
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

    # ------------------------------------------------------------------ #
    # 店铺违规管理（设计 42，2026-10-18）—— **只读域**，但平台对整域强制签名
    #
    # 真机实测（账号 8，同一 Cookie 同一 body）：
    #   无签名 `POST /api/edith/ark/violation/list` ⇒ HTTP 406 `{"code":-1}`
    #   带 `X-s`/`X-t`（ark 档位 itemark/4.4.4/0101）⇒ 200 `{"counts":56,...}`
    # ⇒ 本域**所有**方法都走 `_signed_post` / `_signed_get`，不允许退化成裸
    #   `_post`（守卫 `test_violation_manage` 用 AST 钉住这条）。
    #
    # ⚠️ **写接口一个都不在这层**（设计 42 §1.3）：
    #   `/ark/violation/read_mark`（标已读）与申诉提交（`appeal/*`）刻意缺席 ——
    #   加进来就等于让「打开详情」顺手改平台状态，这不是只读边界。
    # ------------------------------------------------------------------ #

    def post_violation_list(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """违规列表 / 预警列表（`POST /api/edith/ark/violation/list`）—— 需签名。

        **同一端点双场景**：`warnFlag=0` 违规、`=1` 预警，其余过滤字段语义一致。
        请求体由 `violation_service.build_list_body()` 组装（唯一实现）。
        返回 `data.{dtos, counts}`。
        """
        return self._signed_post("/api/edith/ark/violation/list", body, referer)

    def post_violation_info(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """违规详情（`POST /api/edith/ark/violation/info`）—— 需签名。

        body `{violationKey, tenant, tagId, source:"violation_detail_ark", dataFlag:true}`
        （逐字来自真机抓包）。返回 `data.detail.info`。
        """
        return self._signed_post("/api/edith/ark/violation/info", body, referer)

    def get_violation_appeal_record(
        self, violation_id: str, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """申诉记录（`GET /api/edith/violation/appeal/record?violationId=`）—— 需签名。

        返回 `data.appealInfos[]`（本店全部为空数组）。
        """
        from urllib.parse import quote

        path = f"/api/edith/violation/appeal/record?violationId={quote(str(violation_id))}"
        return self._signed_get(path, referer)

    def post_violation_evidence(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """违规证据（`POST /api/edith/ark/violation/evidence`）—— 需签名。

        body `{violationKey, violationEntityId, violationEntityType, tagId, tenant}`。
        账号类违规实测 `data:null`（平台没有给证据）—— 缺值如实为 `null`。
        """
        return self._signed_post("/api/edith/ark/violation/evidence", body, referer)

    def get_violation_status_enum(self) -> tuple[bool, str, dict | None]:
        """处罚状态枚举（`GET /api/edith/violation/status_enum`）—— 需签名。

        `0 待处罚 / 1 已处罚 / 2 处罚中 / -1 已取消`。
        """
        return self._signed_get("/api/edith/violation/status_enum", VIOLATION_REFERER)

    def get_violation_entity_enum(self) -> tuple[bool, str, dict | None]:
        """违规对象枚举（`GET /api/edith/violation/entity_enum`）—— 需签名。

        `999 全部 / 0 店铺 / 1 商品 / 2 账号 / 3 笔记 / 4 直播间`。
        """
        return self._signed_get("/api/edith/violation/entity_enum", VIOLATION_REFERER)

    def post_violation_user_tag(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """违规原因 → tagId 分组（`POST /api/edith/ark/violation/user_tag`）—— 需签名。

        body `{violationEntityType: 999[, bizScene: "SPOT_CHECK"]}`。
        返回 `data.tags[{tagName, tagIds[]}]`。

        ⚠️ **tagName 可能带尾空格**（真机：`违反法律法规` 与 `违反法律法规 ` 是**两条**
        不同 tag，tagIds 分别是 `80001101…` 与 `80005677`）⇒ 调用方**不得** trim
        或按名字去重，否则两组合并、按名字过滤会漏数据。
        """
        return self._signed_post("/api/edith/ark/violation/user_tag", body, referer)

    def post_violation_risk_list(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """店铺体检「违规风险」（`POST /api/edith/ark/violation/risk/list`）—— 需签名。

        body `{dataFlag:true, warnFlag:0, pageNo:1, pageSize:100}`。
        """
        return self._signed_post("/api/edith/ark/violation/risk/list", body, referer)

    def post_violation_risk_advice(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """店铺体检「经营建议」（`POST /api/edith/violation/risk/advice`）—— 需签名。"""
        return self._signed_post("/api/edith/violation/risk/advice", body, referer)

    def post_negative_feedback_badcase(
        self, kind: str, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """店铺体检「负反馈风险」（`shop/score/{item/quality|logistics}/negative/badcase/list`）。

        `kind`：`"quality"` ⇒ 商品品质；`"logistics"` ⇒ 物流。两者**是两个不同端点**
        （不是同一端点的参数差异）。
        """
        if kind == "quality":
            path = "/api/edith/shop/score/item/quality/negative/badcase/list"
        elif kind == "logistics":
            path = "/api/edith/shop/score/logistics/negative/badcase/list"
        else:
            raise ValueError(f"unknown negative feedback kind: {kind!r}")
        return self._signed_post(path, body, referer)

    def post_seller_high_price_info(
        self, body: dict | None = None, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """高价商品概览（`POST /api/edith/product/query_seller_high_price_info`）—— 需签名。"""
        return self._signed_post(
            "/api/edith/product/query_seller_high_price_info", body or {}, referer
        )

    def post_compensate_list(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """赔付列表（`POST /api/edith/governance/traction/query_compensate_list`）—— 需签名。

        body `{sellerId, pageNo, pageSize}`；返回 `data.compensateList[] / total`。
        """
        return self._signed_post(
            "/api/edith/governance/traction/query_compensate_list", body, referer
        )

    def post_arbitration_list(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """仲裁列表（`POST /api/edith/arbitration/list`）—— 需签名。

        body `{sellerId, pageNo, pageSize}`；返回 `data.records[] / total`。
        """
        return self._signed_post("/api/edith/arbitration/list", body, referer)

    def post_arbitration_taglist(
        self, body: dict | None = None, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """仲裁原因下拉（`POST /api/edith/arbitration/taglist`）—— 需签名。"""
        return self._signed_post("/api/edith/arbitration/taglist", body or {}, referer)

    def post_spot_check_list(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """抽检违规列表（`POST /api/edith/common/violation/list`）—— 需签名。

        body `{pageNo, pageSize, bizScene:"SPOT_CHECK"}`；返回 `data.vos[] / counts`。
        """
        return self._signed_post("/api/edith/common/violation/list", body, referer)

    def post_exemption_overview(
        self, body: dict | None = None, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """免罚机会——现货爆单豁免概览（`POST /api/edith/query/explosive/spot/total`）。"""
        return self._signed_post("/api/edith/query/explosive/spot/total", body or {}, referer)

    def post_exemption_items(
        self, body: dict, *, referer: str = VIOLATION_REFERER
    ) -> tuple[bool, str, dict | None]:
        """免罚机会——爆单明细（`POST /api/edith/query/explosive/spot/details`）。

        body `{pageNo, pageSize, startDtm, endDtm}`（`YYYY-MM-DD`）。
        """
        return self._signed_post("/api/edith/query/explosive/spot/details", body, referer)


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
    "VIOLATION_REFERER",
    "SKU_BATCH_SIZE",
    "AIGC_TASK_STATUS_RUNNING",
    "AIGC_TASK_STATUS_COMPLETED",
]



def _node_exe() -> str:
    """定位 node 可执行文件：**下载目录 → 包内 → PATH**（2026-10-23）。

    ⚠️ **唯一实现在 `xhs_utils.xhs_core.runtime.node_exe()`** —— 这里只做转发，
    不再自己写第二份定位逻辑（同一语义两份实现 = 迟早不一致，本仓 DEF-044 口径）。
    转发的意义是**实时**：每次调用都重新查一次 `deps/node`，用户在应用里下完 node
    之后**无需重启**即可签名。
    """
    from xhs_utils.xhs_core.runtime import node_exe

    return node_exe()
