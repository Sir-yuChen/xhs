"""蒲公英 · 分销商广场 请求头与请求体模板。

⚠️ 命名说明（原文件名为 ``xhs_qianfan_util.py``，**名不副实**）：
本模块**不是千帆（商家后台）**，全部 `authority` 均为 ``pgy.xiaohongshu.com``（蒲公英）。
调用方是「蒲公英分销商广场」的达人/分销商检索接口，与商家后台无关。

平台域名对照（避免再次混淆）：
    千帆   = ``ark.xiaohongshu.com``        商家后台：商品、订单、店铺、发布笔记
    蒲公英 = ``pgy.xiaohongshu.com``        买手/达人合作（本模块所属）
    乘风   = ``chengfeng.*``
    聚光   = ``ad.*``

重命名历史：2026-09-18 T-101（缺陷 DEF-015 / N-1）。原 `QianFanAPI` /
`xhs_qianfan_util` / `apis/xhs_qianfan_apis.py` 三处命名已同步更正，
以便为真千帆（ark）代码腾出「千帆」这个名字而不产生歧义。
"""

import random
from xhs_utils.xhs_util import generate_x_b3_traceid


def get_pugongying_distributor_headers_template():
    return {
        "authority": "pgy.xiaohongshu.com",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": "https://pgy.xiaohongshu.com/microapp/distribution/live-broadcast/kol",
        "sec-ch-ua": "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", \"Google Chrome\";v=\"122\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "x-b3-traceid": generate_x_b3_traceid()
    }


def get_pugongying_distributor_user_detail_headers_template(user_id):
    return {
        "authority": "pgy.xiaohongshu.com",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://pgy.xiaohongshu.com",
        "pragma": "no-cache",
        "referer": f"https://pgy.xiaohongshu.com/microapp/distribution/live-blogger-info/{user_id}",
        "sec-ch-ua": "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", \"Google Chrome\";v=\"122\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "x-b3-traceid": generate_x_b3_traceid()
    }


def generate_pugongying_distributor_data(choice, distribution_category, page):
    if choice == "-1":
        data = {
            "buyer_activity": [],
            "live_plan_range": [],
            "seed": random.randint(1000, 9999),
            "page": page,
            "limit": 20
        }
    else:
        choice = choice.split("-")
        live_first_category = []
        live_second_category = []
        for cate_category in choice:
            cate_category_temp = cate_category.split("(")
            live_first_category.append(distribution_category[int(cate_category_temp[0])]["first_category"])
            if len(cate_category_temp) > 1:
                live_second_category_temp = cate_category_temp[1][:-1].split(",")
                for second_category_index in live_second_category_temp:
                    live_second_category.append(
                        distribution_category[int(cate_category_temp[0])]["second_category"][
                            int(second_category_index)])
            else:
                live_second_category.extend(distribution_category[int(cate_category_temp[0])]["second_category"])
        data = {
            "buyer_activity": [],
            "live_plan_range": [],
            "live_first_category": live_first_category,
            "live_second_category": live_second_category,
            "seed": random.randint(1000, 9999),
            "page": page,
            "limit": 20
        }
    return data
