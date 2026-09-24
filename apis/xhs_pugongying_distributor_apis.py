"""蒲公英 · 分销商广场 API。

⚠️ 命名说明（原文件名为 ``xhs_qianfan_apis.py``，**名不副实**）：
本模块**不是千帆（商家后台）**。全部请求打的是 ``pgy.xiaohongshu.com``，
路径前缀为 ``/api/draco/distributor-square/...``，方法是 ``get_user_*``
（分销商 / 达人维度）——属于**蒲公英分销商广场**的检索接口。

真正的千帆是 ``ark.xiaohongshu.com``（商家后台：商品、订单、店铺、笔记中心）。
两者是完全不同的业务与域名，此前同名造成的混淆见缺陷 DEF-015 / N-1。

平台域名对照：
    千帆   = ``ark.xiaohongshu.com``        商家后台（二开目标，见 T-201 ~ T-204）
    蒲公英 = ``pgy.xiaohongshu.com``        买手/达人合作（本模块所属）
    乘风   = ``chengfeng.*``
    聚光   = ``ad.*``

⚠️ 当前状态（2026-09-18 核查）：
    - **backend 零引用**：本模块未被任何后端代码导入，属独立采集脚本。
    - **未接入项目签名栈**：仅用裸 ``requests`` + 静态头，未走 ``xhs_utils`` 的
      ``run_signer`` 签名体系（有时代断层，参考 `02_设计文档/09` §7）。
    - ``choose_categories`` 含 ``input()`` 交互式调用，说明它原本是 CLI 直跑脚本，
      不是服务化接口。若要接入后端需先重构该交互点。

重命名历史：2026-09-18 T-101（缺陷 DEF-015 / N-1）。
"""

import json
import requests
from loguru import logger
from xhs_utils.http_util import REQUEST_TIMEOUT
from xhs_utils.xhs_pugongying_distributor_util import (
    get_pugongying_distributor_headers_template,
    generate_pugongying_distributor_data,
    get_pugongying_distributor_user_detail_headers_template,
)

# 平台域名常量：集中一处，避免再次出现「改了名字没改域名」的混淆
PGY_AUTHORITY = "pgy.xiaohongshu.com"
PGY_BASE = f"https://{PGY_AUTHORITY}"


class PugongyingDistributorAPI:
    """蒲公英分销商广场检索（非千帆）。"""

    def get_all_categories(self, cookies):
        headers = get_pugongying_distributor_headers_template()
        url = f"{PGY_BASE}/api/draco/distributor-square/distributors-tags"
        params = {
            "types": "content_category,distribution_category,user_design_tag,content_tag"
        }
        response = requests.get(url, headers=headers, cookies=cookies, params=params, timeout=REQUEST_TIMEOUT)
        distribution_category = response.json()["data"]['distributor_tag_map']["distribution_category"]
        return distribution_category

    def choose_categories(self, cookies):
        """交互式类目选择。

        注意：本方法含 ``input()``，仅供 CLI 直跑使用，**不可在服务端调用**。
        """
        distribution_category = self.get_all_categories(cookies)
        for first_index, first_category_temp in enumerate(distribution_category):
            logger.info(f'{first_index}: {first_category_temp["first_category"]}')
            for second_index, second_category_temp in enumerate(first_category_temp["second_category"]):
                logger.info(f'---- {second_index}: {second_category_temp}')
        choice = input(
            "请选择您的类目：如果输入-1则为全部类目，输入1-2-4代表整个美妆/个护，服饰鞋包，母婴用品类目，输入1(1,3,4)-2代表美妆/个护类目下的1,3,4子类目和服饰鞋的全部\n")
        return choice, distribution_category

    def get_user_by_page(self, choice, distribution_category, page, cookies):
        headers = get_pugongying_distributor_headers_template()
        url = f"{PGY_BASE}/api/draco/distributor-square/distributors"
        data = generate_pugongying_distributor_data(choice, distribution_category, page)
        data = json.dumps(data, separators=(',', ':'))
        response = requests.post(url, headers=headers, cookies=cookies, data=data, timeout=REQUEST_TIMEOUT)
        res_json = response.json()
        total = res_json["data"]["total"]
        user_list = res_json["data"]["list"]
        return user_list, total

    def get_some_user(self, choice, distribution_category, num, cookies):
        user_list = []
        page = 1
        while len(user_list) < num:
            user_list_temp, total = self.get_user_by_page(choice, distribution_category, page, cookies)
            user_list.extend(user_list_temp)
            page += 1
            if page > total / 20 + 1:
                break
        if len(user_list) > num:
            user_list = user_list[:num]
        return user_list

    def get_user_detail(self, user_id, cookies):
        headers = get_pugongying_distributor_user_detail_headers_template(user_id)
        url = f"{PGY_BASE}/api/draco/distributor-square/distributor/detail/overview/v2"
        data = {
            "buyer_id": user_id,
            "date_type": 2
        }
        data = json.dumps(data, separators=(',', ':'))
        response = requests.post(url, headers=headers, cookies=cookies, data=data, timeout=REQUEST_TIMEOUT)
        return response.json()

    def get_user_cooperation(self, user_id, cookies):
        headers = get_pugongying_distributor_user_detail_headers_template(user_id)
        url = f"{PGY_BASE}/api/draco/distributor-square/distributor/cooperative/category/v2"
        data = {
            "buyer_id": user_id,
            "first_live_category": "",
            "second_live_category": "",
            "date_type": 2,
            "page": 1,
            "size": 10
        }
        data = json.dumps(data, separators=(',', ':'))
        response = requests.post(url, headers=headers, cookies=cookies, data=data, timeout=REQUEST_TIMEOUT)
        return response.json()

    def get_user_shop(self, user_id, cookies):
        headers = get_pugongying_distributor_user_detail_headers_template(user_id)
        url = f"{PGY_BASE}/api/draco/distributor-square/distributor/cooperative/shop/v2"
        data = {
            "buyer_id": user_id,
            "first_live_category": "",
            "second_live_category": "",
            "date_type": 2,
            "page": 1,
            "size": 10
        }
        data = json.dumps(data, separators=(',', ':'))
        response = requests.post(url, headers=headers, cookies=cookies, data=data, timeout=REQUEST_TIMEOUT)
        return response.json()

    def get_user_item(self, user_id, cookies):
        headers = get_pugongying_distributor_user_detail_headers_template(user_id)
        url = f"{PGY_BASE}/api/draco/distributor-square/distributor/cooperative/item/v2"
        data = {
            "buyer_id": user_id,
            "first_live_category": "",
            "second_live_category": "",
            "date_type": 2,
            "page": 1,
            "size": 10
        }
        data = json.dumps(data, separators=(',', ':'))
        response = requests.post(url, headers=headers, cookies=cookies, data=data, timeout=REQUEST_TIMEOUT)
        return response.json()

    def get_user_fans(self, user_id, cookies):
        headers = get_pugongying_distributor_user_detail_headers_template(user_id)
        url = f"{PGY_BASE}/api/draco/distributor-square/distribuitor/detail/fans"
        params = {
            "distributor_id": user_id,
            "date_type": "2"
        }
        response = requests.get(url, headers=headers, cookies=cookies, params=params, timeout=REQUEST_TIMEOUT)
        return response.json()
