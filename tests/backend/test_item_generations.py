"""生成结果 ↔ 商品的**关联**（v2.30 §13.3.11）。

用户口径（2026-10-03）：
> 「根据商品信息生成素材如何与商品产生关联，在商品列表操作列新增查看入口」
> 「因为**进入素材库后无法再区分与产品的关系**」

## 本文件守什么

这是**结构缺口**的修复（不是「查询优化」）：

* 修前 `aigc_media_assets` **没有商品字段**，唯一线索在 `tasks.payload.item_id`；
* 而 `tasks` 是**过程记录、有保留期** ⇒ 任务被清理后关联**永久丢失**。

⚠️ 判据不只是「端点能查到」，更要钉住「**写入时真的落了两列**」——
只加查询端点、忘了写入，端点永远返回空且**不报错**（页面显示「还没有生成记录」，
用户以为是自己没生成过，最难发现的那种坏法）。

⚠️ 本文件用**新范式**（`api_client` + `db_factory` 夹具），
**不复用 `test_api.py` 里的 `_seed_*` helper** —— 那套是旧范式
（依赖模块级 `client` / `_test_session_factory`），在新夹具下会连到**另一个库**
（实测报 `no such table: users`）。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.models import AigcMediaAsset, PlatformAccount, ShopProduct, User

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

def _register(api_client, username: str) -> dict:
    response = api_client.post(
        "/api/auth/register",
        json={"username": username, "password": "secret123"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _user_by_name(db_factory, username: str) -> User:
    with db_factory() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None
        return user


def _seed_account(db_factory, user_id: int) -> int:
    with db_factory() as db:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", nickname="生成记录测试账号", sub_type="pc"
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account.id


def _seed_product(db_factory, user_id: int, account_id: int, item_id: str) -> None:
    """建一条**商品台账行** —— 生成端点会校验「本账号台账里有没有这个商品」。

    ⚠️ `raw_json.images` 的**元素是对象**（`{"link": ...}`）—— `product_image_links()`
    读的是 `link`/`url` 两个键（`_first_text`），**不是裸字符串数组**。
    写成裸数组会让「可选参考图」为空 ⇒ 参考图白名单校验必然 400
    （真机实测：`REFERENCE_IMAGE_NOT_FOUND`）。
    """
    import json

    with db_factory() as db:
        db.add(
            ShopProduct(
                user_id=user_id,
                platform_account_id=account_id,
                item_id=item_id,
                item_title="测试商品（生成记录）",
                images_json=json.dumps(["https://cdn.test/ref.png"]),
                raw_json=json.dumps({"images": [{"link": "https://cdn.test/ref.png"}]}),
            )
        )
        db.commit()


def _seed_image_provider_and_mapping(api_client, headers) -> None:
    """建一个 image 类服务商 + 把 `generate_product_detail_image` 映射到它。

    ⚠️ 走**真端点**（`POST /gateway-configs` + `PUT /business-providers`）而不是直连 ORM：
    这两步都是**有归属校验的业务动作**，绕过去就测不到「配了才生效」的真实前提。
    """
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "生成记录用图像服务商",
            "base_url": "http://127.0.0.1:9/v1",
            "access_token": "sk-test",
            "models": [
                {"model_name": "fake-image-model", "model_type": "image", "platform": ["xhs"]}
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    provider_key = resp.json()["provider_key"]

    resp2 = api_client.put(
        "/api/llm/business-providers",
        json={
            "business_key": "generate_product_detail_image",
            "provider_key": provider_key,
            "model_name": "fake-image-model",
            "platform": "xhs",
        },
        headers=headers,
    )
    assert resp2.status_code == 200, resp2.text


def test_generation_writes_item_link_onto_asset_row(api_client, db_factory):
    """**生成时把商品关联写进资产行**（整件事的地基）。

    ⚠️ 只加查询端点、忘了写入 = 端点永远返回空且**不报错** ——
    那种坏法最难发现（页面显示「还没有生成记录」，用户以为是自己没生成过）。
    """
    import backend.app.api.platforms.xhs.qianfan as qianfan

    class _Fake:
        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            return [{"url": f"https://cdn.test/g{i}.png", "raw": {}} for i in range(count)]

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            return {"url": "https://cdn.test/g.png", "raw": {}}

    headers = _register(api_client, "item-gen")
    user = _user_by_name(db_factory, "item-gen")
    account_id = _seed_account(db_factory, user.id)
    _seed_product(db_factory, user.id, account_id, "ITEM-GEN-1")
    _seed_image_provider_and_mapping(api_client, headers)

    with db_factory() as db:
        qianfan.generate_detail_image(
            payload=qianfan.GenerateDetailImageRequest(
                platform_account_id=account_id,
                item_id="ITEM-GEN-1",
                group_settings={"product": {"prompt": "", "count": 2}},
                count=2,
                reference_image_urls=["https://cdn.test/ref.png"],
                refine_prompt=False,
            ),
            current_user=_user_by_name(db_factory, "item-gen"),
            db=db,
            image_ai_client=_Fake(),
        )
        rows = db.scalars(
            select(AigcMediaAsset).where(AigcMediaAsset.item_id == "ITEM-GEN-1")
        ).all()
        assert len(rows) == 2, f"生成 2 张，但有商品关联的只有 {len(rows)} 条"
        for row in rows:
            assert row.platform_account_id == account_id, (
                "资产行没写 platform_account_id —— 多账号下相同 item_id 会串号"
            )


def test_item_generations_endpoint_is_account_scoped(api_client, db_factory):
    """端点**按账号**收口（同一商品 ID 在不同账号下不能互相串）。"""
    headers = _register(api_client, "item-gen-scope")
    user = _user_by_name(db_factory, "item-gen-scope")
    account_id = _seed_account(db_factory, user.id)

    with db_factory() as db:
        # 一行属于本账号、一行属于**别的账号**（同 item_id）
        db.add(
            AigcMediaAsset(
                user_id=user.id, asset_type="image", local_path="", url="https://cdn.test/mine.png",
                prompt="p", model_name="m", item_id="ITEM-GEN-1", platform_account_id=account_id,
            )
        )
        db.add(
            AigcMediaAsset(
                user_id=user.id, asset_type="image", local_path="", url="https://cdn.test/other.png",
                prompt="p", model_name="m", item_id="ITEM-GEN-1", platform_account_id=99999,
            )
        )
        db.commit()

    response = api_client.post(
        "/api/xhs/qianfan/item-generations",
        headers=headers,
        json={"platform_account_id": account_id, "item_id": "ITEM-GEN-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1, f"跨账号的生成记录被带出来了：{body}"
    assert body["items"][0]["url"].endswith("mine.png")


def test_item_generations_ignores_unlinked_assets(api_client, db_factory):
    """**没有关联的老行不出现在结果里**（不猜、不回溯，缺值不造假）。

    真机背景：v2.30 之前生成的资产 `item_id` 为空 —— 如果把「空」也当作匹配，
    每个商品的生成记录都会带上别人的图（最坏的一种错法）。
    """
    headers = _register(api_client, "item-gen-empty")
    user = _user_by_name(db_factory, "item-gen-empty")
    account_id = _seed_account(db_factory, user.id)

    with db_factory() as db:
        db.add(
            AigcMediaAsset(
                user_id=user.id, asset_type="image", local_path="", url="https://cdn.test/legacy.png",
                prompt="p", model_name="m", item_id=None, platform_account_id=account_id,
            )
        )
        db.commit()

    response = api_client.post(
        "/api/xhs/qianfan/item-generations",
        headers=headers,
        json={"platform_account_id": account_id, "item_id": "ITEM-GEN-1"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0, "无关联的老行被算进来了（item_id 为空的不能匹配）"
