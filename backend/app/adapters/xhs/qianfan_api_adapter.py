from __future__ import annotations

from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env


class XhsQianfanApiAdapter:
    """千帆（`ark.xiaohongshu.com`）只读适配层。

    与 `creator_api_adapter` / `pc_api_adapter` 同一形态：只做环境包装 + 委托，
    不吞异常、不改写 `(success, msg, payload)` 三元组 —— 异常语义由 SDK 承载，
    路由层据此区分「登录态失效」与「接口结构变化」。
    """

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def _api(self):
        from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

        return XHS_Qianfan_Apis(self.cookies)

    def get_seller_info(self) -> tuple[bool, str, Any]:
        with direct_xhs_request_env():
            return self._api().get_seller_info()

    def get_goods_note_page(self, **kwargs: Any) -> tuple[bool, str, Any]:
        """商品笔记列表一页（支持 `note_ids` 按笔记精确查询，见 SDK docstring）。"""
        with direct_xhs_request_env():
            return self._api().get_goods_note_page(**kwargs)

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
    ) -> tuple[bool, str, Any]:
        """千帆数据中心「笔记数据」列表一页（`POST /api/edith/butterfly/data`）。

        这是「我的笔记」离线落库的数据源（2026-10-06 真机抓包）：页面
        `/app-datacenter/note-data/goods`（商品笔记）与 `/normal`（普通笔记）的
        「笔记」数据块，由 `blockKey` 区分：

        * `category == "goods"` ⇒ `arkSellerNoteGoodsNoteDataList`（商品笔记）
        * `category == "normal"` ⇒ `arkSellerNoteNormalNoteDataList`（普通笔记）

        返回 `data[0].data[]`，每行字段是 `{"value": ..., "ratio": ...}` 包裹结构
        （`ex` 偶见于比率字段），调用方负责解包 `value`。字段覆盖 xlsx 全部列且更全
        （见 `note_data_service` 的字段映射）。

        ⚠️ 无签名直连（与 `get_goods_note_page` 同构）—— 下载 xlsx 的
        `long_task/task/submit` 要 datacenterark 签名（C4 不接入），列表接口不需要。
        """
        with direct_xhs_request_env():
            return self._api().get_note_data_list(
                category=category,
                page=page,
                size=size,
                date_select_type=date_select_type,
                date_type=date_type,
                start_date=start_date,
                end_date=end_date,
            )

    def search_items(self, **kwargs: Any) -> tuple[bool, str, Any]:
        """商品列表一页（参数语义见 SDK `search_items` 的 docstring）。"""
        with direct_xhs_request_env():
            return self._api().search_items(**kwargs)

    def search_material_space(self, body: dict) -> tuple[bool, str, Any]:
        """图片空间列表一页（**只读查询，但平台强制签名**，见 SDK docstring）。"""
        with direct_xhs_request_env():
            return self._api().search_material_space(body)

    def get_material_directory(self, seller_id: str) -> tuple[bool, str, Any]:
        """图片空间目录树（无签名直连）。"""
        with direct_xhs_request_env():
            return self._api().get_material_directory(seller_id)

    def get_material_space_usage(self) -> tuple[bool, str, Any]:
        """图片空间配额（无签名直连）。"""
        with direct_xhs_request_env():
            return self._api().get_material_space_usage()

    def add_material_directories(self, seller_id: str, items: list) -> tuple[bool, str, Any]:
        """**新建文件夹**（图片空间写链路，见 SDK docstring）。"""
        with direct_xhs_request_env():
            return self._api().add_material_directories(seller_id, items)

    def edit_material_manageables(self, seller_id: str, items: list) -> tuple[bool, str, Any]:
        """**移动 / 重命名**（图片空间写链路）。"""
        with direct_xhs_request_env():
            return self._api().edit_material_manageables(seller_id, items)

    def delete_material_manageables(self, seller_id: str, items: list) -> tuple[bool, str, Any]:
        """**批量删除**（进平台回收站）。"""
        with direct_xhs_request_env():
            return self._api().delete_material_manageables(seller_id, items)

    def download_material_files(self, seller_id: str, items: list, *, name: str = "") -> tuple[bool, str, Any]:
        """**批量下载**（平台打包任务）。"""
        with direct_xhs_request_env():
            return self._api().download_material_files(seller_id, items, name=name)

    def query_material_task(self, task_id: str) -> tuple[bool, str, Any]:
        """查图片空间异步任务（下载打包进度 / 结果）。"""
        with direct_xhs_request_env():
            return self._api().query_material_task(task_id)

    def check_resize_material_space(self) -> tuple[bool, str, Any]:
        """**查扩容条件**（图片空间扩容，见 SDK docstring）。"""
        with direct_xhs_request_env():
            return self._api().check_resize_material_space()

    def resize_material_space(self) -> tuple[bool, str, Any]:
        """**执行扩容**（图片空间扩容，见 SDK docstring）。"""
        with direct_xhs_request_env():
            return self._api().resize_material_space()

    def register_material_files(self, seller_id: str, items: list) -> tuple[bool, str, Any]:
        """把已上传到云的文件登记进图片空间（`batch_add` + `type:2`）。"""
        with direct_xhs_request_env():
            return self._api().register_material_files(seller_id, items)

    def upload_web_permit(self, file_count: int = 1) -> tuple[bool, str, Any]:
        """申请图片上传凭证（图片空间上传第 1 步；复用发布链路的同一实现）。"""
        with direct_xhs_request_env():
            return self._api().upload_web_permit(file_count)

    def upload_file(self, **kwargs: Any) -> tuple[bool, str, Any]:
        """把本地文件 PUT 到 ROS（图片空间上传第 2 步）。"""
        with direct_xhs_request_env():
            return self._api().upload_file(**kwargs)

    def batch_delete_notes(self, note_ids: Any) -> tuple[bool, str, Any]:
        """**批量删除笔记**（千帆 `note/batch_delete_note`，无签名，真实写、不可撤回）。

        供无流量清理按 `note_category=goods` 分流：商业笔记走千帆删除链路。
        详见 SDK `batch_delete_notes` 的 docstring。
        """
        with direct_xhs_request_env():
            return self._api().batch_delete_notes(note_ids)

    def get_skus_by_item_ids(self, item_ids: Any) -> tuple[bool, str, Any]:
        """按商品 ID 批量取 SKU 级明细（≤10 个/批，实测上限；设计 13 §19.1.2）。

        商品编辑页的 `publish_render` 要 `X-s` 签名（C4 不接入），SKU 明细改由
        这个**无签名**端点覆盖。返回 `data.skusMap: {item_id: [sku, ...]}`。
        """
        with direct_xhs_request_env():
            return self._api().get_skus_by_item_ids(item_ids)

    # ------------------------------------------------------------------ #
    # 商品笔记智能生成与发布（设计 13 §31，2026-09-23）—— 写链路，仅本人店铺 +
    # 用户显式触发（§31.2）。与只读方法同一形态：环境包装 + 委托，不改三元组。
    # ------------------------------------------------------------------ #

    def aigc_publish_attributes(self, item_id: str, *, sku_id: str = "") -> tuple[bool, str, Any]:
        """商品笔记 AI 生成属性面（场景 / 风格 / 话题 / SKU / 卖点）。"""
        with direct_xhs_request_env():
            return self._api().aigc_publish_attributes(item_id, sku_id=sku_id)

    def sellerpoint_aigc(self, *, biz_id: str, aigc_context: dict) -> tuple[bool, str, Any]:
        """发起异步笔记文本生成，返回 `data.task_id`。"""
        with direct_xhs_request_env():
            return self._api().sellerpoint_aigc(biz_id=biz_id, aigc_context=aigc_context)

    def aigc_task_result(self, task_id: str) -> tuple[bool, str, Any]:
        """轮询异步生成任务结果（`task_detail` 是 JSON 字符串，调用方解析）。"""
        with direct_xhs_request_env():
            return self._api().aigc_task_result(task_id)

    def upload_web_permit(self, file_count: int = 1) -> tuple[bool, str, Any]:
        """申请图片上传凭证（`uploadTempPermits`：addr + token + 预分配 fileIds）。"""
        with direct_xhs_request_env():
            return self._api().upload_web_permit(file_count)

    def upload_file(self, *, upload_addr: str, file_id: str, token: str, file_path: str) -> tuple[bool, str, Any]:
        """PUT 本地文件到 ROS（token 不打印、不落库）。"""
        with direct_xhs_request_env():
            return self._api().upload_file(
                upload_addr=upload_addr, file_id=file_id, token=token, file_path=file_path
            )

    def publish_note(self, body: dict) -> tuple[bool, str, Any]:
        """发布商品笔记（`biz_relations` 挂商品链接），返回 `{id: 平台 note_id}`。"""
        with direct_xhs_request_env():
            return self._api().publish_note(body)

    def preaudit_note(
        self,
        *,
        title: str,
        content: str,
        topic_names: list,
        image_list: list,
        text_gen_task_id: str = "",
        image_gen_task_id: str = "",
    ) -> tuple[bool, str, Any]:
        """发布前内容预审核（平台侧 moderation；任一 `checkPass=false` 即终止）。"""
        with direct_xhs_request_env():
            return self._api().preaudit_note(
                title=title,
                content=content,
                topic_names=topic_names,
                image_list=image_list,
                text_gen_task_id=text_gen_task_id,
                image_gen_task_id=image_gen_task_id,
            )

    def batch_get_category(self, item_ids: Any) -> tuple[bool, str, Any]:
        """按商品取经营类目（生成上下文的需求详情链第一级，只读）。"""
        with direct_xhs_request_env():
            return self._api().batch_get_category(list(item_ids or []))

    def get_category_demand_list(self, leaf_category_id: str) -> tuple[bool, str, Any]:
        """类目需求清单（需求详情链第二级，只读）。"""
        with direct_xhs_request_env():
            return self._api().get_category_demand_list(leaf_category_id)

    def get_demand_detail(self, demand_id: str) -> tuple[bool, str, Any]:
        """需求详情：卖点 / 创作灵感 / 人群（需求详情链第三级，只读）。"""
        with direct_xhs_request_env():
            return self._api().get_demand_detail(demand_id)

    # ------------------------------------------------------------------ #
    # 订单模块（设计 16 §7，M6 方向 6 · 一期只读）—— 与只读方法同一形态：
    # 环境包装 + 委托，不改 `(success, msg, payload)` 三元组。
    # ------------------------------------------------------------------ #

    def get_order_page(self, **kwargs: Any) -> tuple[bool, str, Any]:
        """订单列表一页（参数语义见 SDK `get_order_page` 的 docstring）。"""
        with direct_xhs_request_env():
            return self._api().get_order_page(**kwargs)

    def get_order_detail(self, package_id: str) -> tuple[bool, str, Any]:
        """订单详情（入参是 `package_id`，见 SDK docstring 的真机记录）。"""
        with direct_xhs_request_env():
            return self._api().get_order_detail(package_id)

    def get_order_stats(self, start_time_ms: int, end_time_ms: int) -> tuple[bool, str, Any]:
        """订单状态统计（订单监控概览用）。"""
        with direct_xhs_request_env():
            return self._api().get_order_stats(start_time_ms, end_time_ms)

    def get_after_sale_page(self, **kwargs: Any) -> tuple[bool, str, Any]:
        """售后单列表一页（参数语义见 SDK `get_after_sale_page` 的 docstring）。"""
        with direct_xhs_request_env():
            return self._api().get_after_sale_page(**kwargs)

    # ------------------------------------------------------------------ #
    # 订单二期写操作（M7，设计 16 §7.4）—— **真实平台写、不可撤回**
    #
    # ⚠️ 与发布（`publish_note`）同性质：仅限本人店铺 + 用户显式触发。
    # 端点存在性已证实（§7.4.1 只读探测）；**请求体 schema 待首发核对**。
    # ------------------------------------------------------------------ #

    def pre_check_before_delivery(self, body: Any) -> tuple[bool, str, Any]:
        """发货前预检（只读语义，不落状态）。"""
        with direct_xhs_request_env():
            return self._api().pre_check_before_delivery(body)

    def single_online_delivery(self, body: Any) -> tuple[bool, str, Any]:
        """**单个订单发货**（真实写、不可撤回）。"""
        with direct_xhs_request_env():
            return self._api().single_online_delivery(body)

    def after_sale_audit(self, body: Any) -> tuple[bool, str, Any]:
        """**售后审核**（同意 / 拒绝；真实写、不可撤回）。"""
        with direct_xhs_request_env():
            return self._api().after_sale_audit(body)


def get_qianfan_api_adapter_factory():
    """千帆适配器工厂（FastAPI 依赖）。

    **定义在适配器模块**（而非路由模块）有两个原因：

    ① **避免循环导入**：`pc.py`（「我的笔记」补价格）也要用千帆商品查询，而
       `qianfan.py` 反向依赖 `pc.py`（`_cookies_to_string`）；工厂留在路由层 = 成环。
    ② **保证打桩生效**：测试用 `dependency_overrides[get_qianfan_api_adapter_factory]`
       打桩，定义在这里能让 `qianfan.py` / `pc.py` 拿到**同一个函数对象**，
       一处打桩对所有端点生效。
    """
    return XhsQianfanApiAdapter
