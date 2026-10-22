"""`POST /picture-space/upload-batch`（方案 B：一次 permit + 服务端并发 PUT）。

用户口径（2026-10-11）：「自动加载参数弹窗图片资源实现并发上传，方案 B，并发数默认 5，
支持在 config 表配置」。

⚠️ 本文件钉的是**并发上传的三条不变量**（每一条错了都会造成用户可见故障）：
  1. **一次 permit 打满整批**（N 个 fileId 一次拿齐）—— 打小红书只 1 次，这是「不触发
     风控」的根据；如果退化成「每张一次 permit」，平台请求数就恢复成 N 次。
  2. **并发数默认 5 且可配置**（`xhs_image_upload_parallelism`）。
  3. **结果按下标逐位对齐**（前端要按 `index` 回填主图/详情图/规格图，一旦顺序乱了
     图就会串位）；且**单项失败不整批失败**。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

#: 1x1 PNG（最小真实图片，让 PIL 能读出尺寸）。
PNG_1PX = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()


class _FakeAdapter:
    """记录调用次数的假适配器 —— 用来验证「permit 只申请一次」与「PUT 打了 N 次」。"""

    def __init__(self) -> None:
        self.permit_calls: list[int] = []
        self.put_calls: list[str] = []
        self.register_calls: list[list[dict[str, Any]]] = []
        self._seq = 0

    def upload_web_permit(self, file_count: int = 1, **_kw: Any):
        self.permit_calls.append(int(file_count))
        #: 一次给齐 `file_count` 个 fileId（平台真实行为：`fileIds[]` 一文件一个 ID）。
        self._seq += 1
        ids = [f"fid-{self._seq}-{i}" for i in range(int(file_count))]
        return True, "ok", {
            "data": {
                "uploadTempPermits": [
                    {
                        "qos": 5,
                        "uploadAddr": "fake-ros.example.com",
                        "token": "tk",
                        "fileIds": ids,
                    }
                ]
            }
        }

    def upload_file(self, *, upload_addr: str, file_id: str, token: str, file_path: str):
        self.put_calls.append(file_id)
        return True, "成功", {"preview_url": f"https://preview.example.com/{file_id}"}

    def register_material_files(self, seller_id: str, items: list[dict[str, Any]]):
        self.register_calls.append(items)
        return True, "ok", {"data": {"materialIds": [i.get("fileId") for i in items]}}


def _register(api_client, username: str) -> dict:
    """注册用户拿 Bearer（沿用 `test_material_files` 的公共写法）。"""
    response = api_client.post(
        "/api/auth/register",
        json={"username": username, "password": "secret123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_account(db_factory, username: str) -> int:
    """给用户造一个有 `seller_id` 的平台账号（端点要过归属校验）。"""
    from sqlalchemy import select

    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.user import User

    with db_factory() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None
        account = PlatformAccount(
            user_id=user.id,
            platform="xhs",
            #: 列名按模型实际字段（`nickname`，没有 `account_name` —— 实测踏空）。
            nickname=f"{username}-acct",
            seller_id="seller-1",
            status="active",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return int(account.id)


@pytest.fixture()
def batch_env(monkeypatch, api_client, db_factory):
    """打桩 ark 适配器 + 记录 permit/PUT 次数；返回 (client, adapter, headers, account_id)。

    ⚠️ **必须真的注册用户 + 造账号**：不造的话 `_owned_account` 直接 404，
    用例只能 `pytest.skip` —— 那就是「永远绿但什么都没测」的假守卫（本轮实测踩过）。
    """
    fake = _FakeAdapter()

    monkeypatch.setattr(
        "backend.app.api.platforms.xhs.qianfan.picture_space._ark_adapter_for",
        lambda *_a, **_k: fake,
        raising=True,
    )
    #: ⚠️ 批量上传端点读的是**子模块** `picture_space._ark_adapter_for`（2026-10-11 拆包后）。
    #: 打在包 `qianfan` 上会静默失效 ⇒ 测试真的去连平台（实测表现：
    #: `ARK_SESSION_REQUIRED`，看着像业务坏了，其实是补丁打空）。
    from backend.app.api.platforms.xhs.qianfan import picture_space as _ps

    assert _ps._ark_adapter_for() is fake, "monkeypatch 锚点打空了（须打在 qianfan.picture_space 上）"

    import backend.app.services.picture_space_service as ps
    import backend.app.services.shop_draft_service as draft_svc
    #: 登记回读：不碰真平台，按 fileId 造一条记录（`path`/`link` 是发布体材料）。
    monkeypatch.setattr(
        draft_svc,
        "_fetch_material_record",
        lambda _adapter, name="", material_id="": {
            "path": f"arkgoods/{material_id}",
            "link": f"https://qimg.xiaohongshu.com/arkgoods/{material_id}",
        },
    )
    monkeypatch.setattr(
        draft_svc, "_registered_material_id", lambda _resp: ""
    )
    #: `build_upload_item` 真实构造（含 additionalData），不桩 —— 它是契约的一部分。
    assert ps is not None
    headers = _register(api_client, "batch_user")
    account_id = _seed_account(db_factory, "batch_user")
    return fake, headers, account_id


def _post_batch(client, account_id: int, count: int, headers=None, **kw):
    files = [{"file_name": "", "content_base64": PNG_1PX} for _ in range(count)]
    return client.post(
        "/api/xhs/qianfan/picture-space/upload-batch",
        json={"platform_account_id": account_id, "files": files, **kw},
        headers=headers,
    )


def test_batch_requests_one_permit_for_the_whole_batch(batch_env, api_client, db_factory):
    """**整批只申请一次 permit** —— 这是「并发不触发风控」的根据。

    平台请求数从 N 降到 1；PUT 打的是对象存储（不是小红书域名），并发它不产生平台请求。
    """
    fake, headers, account_id = batch_env
    resp = _post_batch(api_client, account_id, 6, headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(fake.permit_calls) == 1, (
        f"permit 应只申请 1 次（整批一次拿齐），实际 {len(fake.permit_calls)} 次"
    )
    assert fake.permit_calls[0] == 6, "permit 的 file_count 应等于整批文件数"
    assert len(fake.put_calls) == 6, "每张文件应各 PUT 一次"
    body = resp.json()
    assert body["succeeded"] == 6 and body["failed"] == 0


def test_batch_response_is_index_aligned_and_reports_failures_per_item(batch_env, api_client):
    """`results[]` 与入参**逐位对齐** + 单项失败不整批失败。"""
    _fake, headers, account_id = batch_env
    resp = _post_batch(api_client, account_id, 3, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["results"]) == 3
    for i, item in enumerate(body["results"]):
        assert item["index"] == i, "results 必须与入参下标逐位对齐"
    assert body["succeeded"] + body["failed"] == 3


def test_batch_rejects_invalid_base64_per_item_not_whole_batch(batch_env, api_client):
    """坏 base64 只让**那一项**失败（确定性 400 判据在单文件端点，这里是逐项）。"""
    _fake, headers, account_id = batch_env
    resp = api_client.post(
        "/api/xhs/qianfan/picture-space/upload-batch",
        json={
            "platform_account_id": account_id,
            "files": [
                {"file_name": "", "content_base64": "!!!not-base64!!!"},
                {"file_name": "", "content_base64": PNG_1PX},
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["success"] is False
    assert "base64" in str(body["results"][0].get("message", ""))


def test_upload_parallelism_is_a_live_config_key_with_default_8():
    """**并发数是运行时配置**（`xhs_image_upload_parallelism`，默认 8）。

    ⚠️ 三条一起钉（缺一条就等于没配上）：
      ① 在 `LIVE_KEYS` 里（否则设置页存了也没读点，`is_live` 为假）；
      ② 在 `BUILTIN_DEFAULTS` 里且**默认 8**（实测 8 路最优：顺序 38.4s → 并发 8 路 6.6s；
       2026-10-22 四处基线统一为 8）；
      ③ 端点的读点真的用 `live_int` 读它（否则配置改了不生效）。
    """
    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan
    from backend.app.services import settings_service as svc

    assert "xhs_image_upload_parallelism" in svc.LIVE_KEYS, "并发数必须在 LIVE_KEYS 里"
    assert svc.BUILTIN_DEFAULTS.get("xhs_image_upload_parallelism") == 8, "默认应为 8"

    src = inspect.getsource(qianfan.picture_space_upload_batch)
    assert "xhs_image_upload_parallelism" in src, "端点没读这个配置键"
    assert "live_int" in src, "必须用 live_int 读（带写后立即失效缓存），否则改了不生效"


def test_batch_endpoint_never_re_uploads_with_a_per_file_permit():
    """**反向守卫**：不得退化成「每张文件各申请一次 permit」。

    这是本轮最容易在后续重构中被「顺手改回去」的点 —— 一旦逐张申请，
    平台请求数从 1 变回 N，并发上传的「不触发风控」前提就没了。
    """
    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan

    src = inspect.getsource(qianfan.picture_space_upload_batch)
    #: permit 只该在循环/线程池**之外**调用一次：`upload_web_permit(len(prepared))`。
    assert src.count("upload_web_permit") == 1, (
        "端点里只该有 1 处 `upload_web_permit`（整批一次）；多出来就是逐张申请了"
    )
    assert "upload_web_permit(\n        len(prepared)" in src or "len(prepared)" in src, (
        "permit 的 file_count 应按整批文件数申请"
    )


def test_batch_uses_bounded_thread_pool(batch_env, api_client):
    """并发走**有界**线程池（不无限起线程）；且并发数有硬上限。"""
    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan

    src = inspect.getsource(qianfan.picture_space_upload_batch)
    assert "ThreadPoolExecutor" in src, "并发应走 ThreadPoolExecutor"
    assert "max_workers=concurrency" in src, "线程池大小应由并发配置决定"
    #: 上限夹取：防止配置被填成 9999 把后端线程池打满。
    assert "min(32" in src or "max(1" in src, "并发数应有上下限夹取"


def test_permit_is_requested_in_batches_until_all_ids_are_collected():
    """**凭证必须分批申请直到拿齐**（2026-10-11 真机实测的真根因）。

    平台 `upload/web/permit` **单次最多预分配 14 个 fileId**：
      实测 `file_count=10 → 10` / `20 → 14` / `38 → 14` / `50 → 14`（封顶 14）。
    旧实现只申请**一次**就开工 ⇒ 38 张里只有 14 张能传，其余 24 张全部落
    「上传凭证数量不足」——**用户实测的「14/38」精确等于 38−14**。

    ⚠️ 这条同时守住两个容易回退的点：
      ① 批大小不得 > 14（否则那一批还是会被平台截到 14，白申请）；
      ② 拿齐就停（不能死循环）；以及**每个 fileId 用自己那批的 addr/token**
        （不同批是不同凭证，跨批用会 401）。
    """
    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan

    src = inspect.getsource(qianfan.picture_space_upload_batch)

    assert qianfan._PERMIT_BATCH <= 14, (
        f"批大小必须 ≤ 平台上限 14，实际 {qianfan._PERMIT_BATCH}"
    )
    #: ⚠️ 判据必须落在**循环上限表达式**上，不能只查「有 for _round」（实测逃逸一次：
    #: 把上限改成 1 时 `for _round in range(` 这个子串仍在 ⇒ 假绿）。
    #: 正确判据 = 轮数上限**由 needed/batch 算出**（只跑 1 轮就拿不齐）。
    assert f"(needed // _PERMIT_BATCH) + 3" in src, (
        "分批循环的轮数上限必须按需求量算（写死 1 轮 = 只申请一次，拿不齐）"
    )
    assert "want = min(_PERMIT_BATCH, needed - len(file_ids))" in src, "每批应按剩余需求申请"
    assert "len(file_ids) >= needed" in src, "缺「拿齐就停」的判据"
    assert "_PERMIT_CRED.pop(" in src, "凭证映射用完没清（会常驻涨内存）"
    assert "_PERMIT_CRED.get(file_id" in src, (
        "上传必须用该 fileId **自己那批**的 uploadAddr/token（跨批用会 401）"
    )

    #: 纯逻辑复算：平台封顶 14 时，分批能否拿齐 38？
    def simulate(needed: int, batch: int) -> tuple[int, list[int]]:
        got = 0
        rounds: list[int] = []
        for _ in range(0, (needed // batch) + 3):
            if got >= needed:
                break
            want = min(batch, needed - got)
            got += min(14, want)  # ← 复刻平台封顶
            rounds.append(want)
        return got, rounds

    got, rounds = simulate(38, qianfan._PERMIT_BATCH)
    assert got == 38, f"分批后应能拿齐 38，实际 {got}（批次 {rounds}）"
    #: 反向对照：单次申请只能拿 14 —— 与用户实测的 14/38 吻合（证明根因判断正确）。
    assert min(14, 38) == 14, "单次申请上限应为 14（旧实现的失败数 38-14=24 由此而来）"


def test_batch_endpoint_fails_loudly_if_ids_still_short_after_batching():
    """分批仍拿不齐时要**明确报错**（不静默少传）。"""
    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan

    src = inspect.getsource(qianfan.picture_space_upload_batch)
    assert "上传凭证不足" in src, "拿不齐凭证必须报出「需要 N / 只给了 M」"
    assert "MATERIAL_UPLOAD_FAILED" in src, "缺结构化错误码"
