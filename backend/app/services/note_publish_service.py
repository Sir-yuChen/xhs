"""待发布笔记的发布链路（设计 13 §31，2026-09-23 用户口径；2026-09-25 异步化）。

## 用户口径原文（需求④）

> 「点击发布二次确认，然后再将笔记存储的媒体资源自动下载到本地临时文件夹，
> 每个笔记一个文件夹，发布笔记时重新上传，笔记发布成功后删除本地临时下载的
> 资源，并根据发布成功的笔记 ID，将三方真实的笔记 ID 更新到笔记表和笔记与
> 商品关系表中。如果笔记发布失败则记录失败原因……发布失败的笔记支持重复发布。」

## 2026-09-25 起：异步 + 节流 + 临时目录治理（用户口径）

> 「前端点击发布无需等待，后端异步发布」；「连续快速下载商品的图片或视频资源
> 会导致平台限流……可控制间隔一秒或 1-3 秒随机」；「下载失败后，本次创建的临时
> 目录删除，下载前使用商品 id 作为临时文件名」。

## 流程（异步；批量 = 一个队列线程逐篇跑）

```
start_publish / start_batch_publish（同步，毫秒级返回）
  门禁（归属 / 类型 / 状态 / 媒体 / 商品重复发布 / ark 令牌；发布中 ⇒ 拒绝，不双发）
  → publish_status = publishing + attempts + 1（先落库）
  → 入发布队列（FIFO），立即返回 {ok: true, status: "publishing"}

队列 worker（单线程，逐篇跑；顺序执行 = 节流的第二道保障）
  → 下载媒体到 tmp_note_publish/<note_id>/（张间随机停 1–3 秒；
     文件名 <item_id>_<序号>；下载失败 ⇒ 删本次目录）
  → 上传：upload/web/permit 一次 → 逐文件 PUT ROS
  → note/publish（hash_tag 用平台话题 ID、images_list 用上传 file_id、
    biz_relations 挂商品 sku_id）
  → 成功：note_id 回写平台 ID + published + published_at + 删临时目录
    （按**本地** note_id —— 历史缺陷：曾先改 note_id 再删，删的是不存在的平台目录）
  → 失败：publish_status=failed + publish_error=平台原文；
    下载阶段失败已删目录，上传/预审核/发布阶段保留（重发全量重下、现场可查）
```

终态不靠响应体：前端轮询 `GET /notes/publish-queue`（队列进度）+ `GET /notes/{id}`
看 `publish_status` / `publish_error`。

## 四条硬约定

1. **临时目录生命周期**（§31.3.5）：成功即删；**下载失败即删**；上传 / 预审核 /
   发布阶段失败保留。
2. **单 worker 顺序执行**：队列一次只跑一篇 —— 节流的意义就在于请求之间有间隔，
   并行线程会把间隔叠没（CDN 看到的是聚合速率）。
3. **失败原因是平台原文**（DEF-058：不替换成「请稍后重试」）；
   网络异常带异常类型。
4. **发布账号 = 生成时记录的账号**（笔记行 `platform_account_id` 自带）——
   不提供换账号入口（换账号 = 换店铺上下文，挂链会挂错）。
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.models import Task
from backend.app.models.note import (
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_PUBLISHING,
    PUBLISHABLE_STATUSES,
    NOTE_TYPE_LOCAL_GENERATED,
    Note,
    Topic,
    set_note_publish_status,
)
from backend.app.models.shop import NoteGoods
from backend.app.services import note_queue_service
from backend.app.services.shop_binding_service import qianfan_cookies_for_account

logger = logging.getLogger(__name__)

#: 临时下载根目录（项目数据目录内，不动系统临时目录）。
TMP_SUBDIR = "tmp_note_publish"

#: 单个媒体下载超时（秒）。
_DOWNLOAD_TIMEOUT = 30

#: 下载媒体直链的请求头。
#:
#: ⚠️ **User-Agent 不能省**（2026-10-07 实测定位，用户报「下载媒体失败（第 2 个）：
#: HTTPError 405」）：平台 CDN（tencent-cos 前置）**对没有 UA 的请求回 405**，
#: 而不是 403 —— 症状极具误导性（405 通常读作「方法不对」，实际是「缺 UA」）。
#: 实测矩阵（同一个直链）：
#:
#: | 请求 | 结果 |
#: |---|---|
#: | 无 UA（原实现只有 `Referer: ""`） | **405** |
#: | `Referer: ""` + UA | **200** |
#: | 只带 UA | **200** |
#:
#: ⚠️ 与 `note_detail_ssr._default_user_agent` 是**同一类坑**（那边是「没 UA 被踢到登录页」）
#: —— 项目里已踩过一次，下载媒体这条当时漏了。
_MEDIA_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def _media_download_headers() -> dict[str, str]:
    """下载媒体用的请求头（**唯一实现**：别处不要再手拼这份 header）。

    `Referer` 置空是既有口径（商品图直链带小红书 Referer 会触发防盗链），
    UA 见 `_MEDIA_DOWNLOAD_UA` 的说明。
    """
    return {"Referer": "", "User-Agent": _MEDIA_DOWNLOAD_UA}

#: 单篇笔记的媒体上限（张）。设计 13 §32.2.1（用户 2026-09-23 口径：图片最多 6 张）——
#: 与生成侧的 `product_note_gen_service.MAX_NOTE_IMAGES` 同值：生成时落几张，
#: 发布时就传几张，**上限只在一处定义会漂**，所以两侧常量必须一起看。
MAX_NOTE_IMAGES = 6

#: 视频上限（个）。视频笔记发布链路未接通（§32.7）⇒ 本值当前只用于**拒绝时的说明**，
#: 不参与上传（`_select_media` 遇到「只有视频」的笔记直接给可读失败）。
MAX_NOTE_VIDEOS = 1

#: 命中「商品已发布过笔记」时的失败原因（用户 2026-09-28 口径**原文**，唯一文案）。
GOODS_ALREADY_PUBLISHED_MESSAGE = "挂载的商品已经发布过笔记"

#: 结构化错误码（单篇端点 400 / 批量 `results` 共用）。
GOODS_ALREADY_PUBLISHED_CODE = "GOODS_ALREADY_PUBLISHED"

#: 「该商品已被占用」的发布状态。**含 `publishing` 是刻意的**（§31.5.2）：批量勾选多篇
#: 同商品笔记时，第 1 篇入队后是 `publishing`（还没 `published`）—— 只认 `published`
#: 会让同批 2~N 篇全部放行，用户「不重复发布」的诉求落空。
_GOODS_OCCUPYING_STATUSES = (PUBLISH_STATUS_PUBLISHED, PUBLISH_STATUS_PUBLISHING)

#: 媒体下载节流（2026-09-25 用户口径「可控制间隔一秒或 1-3 秒随机」）：
#: 每两张之间随机停 [min, max] 秒，**首张不停**。随机而非固定值 —— 固定间隔本身
#: 就是机器节奏特征；1–3 秒给 CDN 留呼吸空间，又不把批量发布拖到不可用。
_DOWNLOAD_GAP_MIN_SECONDS = 1.0
_DOWNLOAD_GAP_MAX_SECONDS = 3.0


def _download_gap_seconds() -> float:
    """本次下载间隔（秒）。测试可 monkeypatch 成 0 以免真等。"""
    return random.uniform(_DOWNLOAD_GAP_MIN_SECONDS, _DOWNLOAD_GAP_MAX_SECONDS)


#: 发布队列空闲轮询间隔（秒）。唤醒信号只是**提示**，这个超时是兜底：
#: 任何一次 `set` 丢失，最坏也只晚这么久发现新任务，不会永久停摆（DEF-048）。
PUBLISH_IDLE_POLL_SECONDS = 5.0

#: 批量发布单次上限（篇）。批量是显式动作，超限分多次即可。
PUBLISH_BATCH_MAX_ITEMS = 20

#: worker 线程名（探针与重建都认它；DEF-069 同型：存活判据只能是**线程在不在**）。
PUBLISH_WORKER_THREAD_NAME = "note-publish-worker"

#: 看门狗巡检间隔 / 重建失败指数退避上限（与同步 worker、调度器守活同值）。
PUBLISH_WATCHDOG_INTERVAL_SECONDS = 30.0
PUBLISH_WATCHDOG_MAX_BACKOFF_SECONDS = 600.0

#: 队列持久化（设计 13 §35.9）：真相源是 `tasks` 表里的 `xhs_note_publish_item` 行，
#: **内存里不再有队列 / 结果 / 序号** —— 只留一个唤醒信号 `_publish_wakeup`。
#: 进程退出后未跑的篇还是 `pending` 行，下次启动接着跑（用户口径「应用退出再次
#: 启动后可继续执行」）。
_publish_wakeup = threading.Event()
_publish_queue_lock = threading.Lock()
_publish_worker: Optional[threading.Thread] = None
#: worker 收尾 / 测试专用出口（置位 + set wakeup ⇒ 不再认领新任务即退；生产不置位）。
_publish_worker_stop = threading.Event()
_publish_watchdog_stop = threading.Event()

#: 入队时的 adapter 工厂缓存（`tasks.id -> factory`）。worker 在**本进程内**重建
#: adapter 用它 —— 端点的工厂是 FastAPI 依赖注入（测试 override 出来的假适配器），
#: worker 线程拿不到注入层，只能由入队路径把它带进来。
#: ⚠️ **是缓存不是真相源**：进程重启后为空，`_publish_adapter_factory()` 自动回落到
#: 默认千帆工厂 —— 那正是重启续跑需要的来源（旧进程的注入层早已不在）。
#: 跑完即 pop，不无限增长。
_publish_adapter_factories: dict[int, Any] = {}

#: 起发临界区锁：检查状态 → 置 publishing → 落库必须原子。两个并发请求同时看到
#: pending 时，只有第一个能进（SQLite 单写者也挡不住「读-改-写」竞态）。
_publish_gate_lock = threading.Lock()

#: 后台线程自建 Session 的工厂（测试注入临时库的 sessionmaker）。
_session_factory = SessionLocal


class NotePublishError(Exception):
    """发布失败（门禁 / 下载 / 上传 / 平台）。`code` 给前端做结构化呈现。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _temp_root() -> Path:
    root = Path(get_settings().storage_dir) / TMP_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _note_temp_dir(note_id: str) -> Path:
    return _temp_root() / note_id


def _cleanup_temp_dir(note_id: str) -> None:
    """删这篇笔记的临时目录（幂等）。删不掉不阻断发布结果 —— 目录是过程产物。"""
    shutil.rmtree(_note_temp_dir(note_id), ignore_errors=True)


def _assets_of(db: Session, note_pk: int) -> list:
    from backend.app.models.note import NoteAsset

    return list(
        db.scalars(
            select(NoteAsset).where(NoteAsset.note_id == note_pk).order_by(NoteAsset.sort_order, NoteAsset.id)
        ).all()
    )


def _owned_note(db: Session, user_id: int, note_pk: int) -> Note:
    note = db.get(Note, note_pk)
    if note is None or note.user_id != user_id:
        raise NotePublishError("NOTE_NOT_FOUND", "笔记不存在或不属于当前用户。")
    return note


def _check_publishable(note: Note) -> None:
    if note.note_type != NOTE_TYPE_LOCAL_GENERATED:
        raise NotePublishError(
            "NOTE_NOT_PUBLISHABLE", "只有「本地生成」的笔记可以发布（采集 / 同步笔记不走本链路）。"
        )
    if note.publish_status == PUBLISH_STATUS_PUBLISHED:
        raise NotePublishError("NOTE_ALREADY_PUBLISHED", "这篇笔记已经发布过了，不能重复发布。")
    if note.publish_status not in PUBLISHABLE_STATUSES:
        raise NotePublishError(
            "NOTE_NOT_PUBLISHABLE", f"笔记当前状态（{note.publish_status or '未知'}）不可发布。"
        )


def _select_media(db: Session, note: Note) -> list:
    """媒体取舍（设计 13 §31.5 步 0 / §32.2.1）—— **门禁与下载同一份**。

    * **图片**（当前唯一被平台接受的形态）：最多 `MAX_NOTE_IMAGES` 张；
    * **视频**：生成侧「视频优先」会产出「只有视频」的笔记，但视频笔记发布链路
      未接通 ⇒ 这里**明确拒绝**并给可读原因（`NOTE_VIDEO_UNSUPPORTED`），
      不发半成品、不静默失败。

    同步门禁与后台下载都调它（不写两份，DEF-044/058）。
    """
    assets = _assets_of(db, note.id)
    if not assets:
        raise NotePublishError(
            "NOTE_MEDIA_EMPTY", "这篇笔记没有媒体素材（商品图为空），平台不接受发布。请换有图的商品重新生成。"
        )
    images = [asset for asset in assets if asset.asset_type != "video" and asset.url]
    videos = [asset for asset in assets if asset.asset_type == "video"]
    if not images and videos:
        raise NotePublishError(
            "NOTE_VIDEO_UNSUPPORTED",
            "这篇笔记只有视频素材：视频笔记发布链路尚未接通（设计 13 §32.7），请换有图片的商品重新生成。",
        )
    if not images:
        raise NotePublishError(
            "NOTE_MEDIA_EMPTY", "这篇笔记的图片素材都没有链接，无法下载。请换有图的商品重新生成。"
        )
    return images[:MAX_NOTE_IMAGES]


def _item_id_for(db: Session, note: Note) -> str:
    """商品 ID（临时文件名用，用户 2026-09-25 口径）。

    `raw_json["item_id"]` 优先（生成时落库）；兜底走 `note_goods → shop_products`；
    都取不到返空串（调用方退回纯序号命名，不造假）。
    """
    item_id = str((_parse_raw(note).get("item_id") or "")).strip()
    if item_id:
        return item_id
    # ⚠️ 2026-09-27（§26.7）：关系行**直接就是平台商品 ID** ⇒ 不必再查商品表。
    relation = db.scalars(
        select(NoteGoods).where(
            NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
        )
    ).first()
    if relation is not None:
        return str(relation.item_id or "").strip()
    return ""


def _goods_item_ids_for(db: Session, note: Note) -> list[str]:
    """该笔记**挂载的商品 ID** 集合（平台商品 ID，设计 13 §31.5.2）。

    以 `note_goods` 关系行为准（§26.7：关系表存的就是**平台商品 ID**），并把
    `raw_json.item_id`（生成时落库的那一份）一并并入 —— 关系行尚未落库时仍认得出
    「这篇挂的是什么商品」。都取不到 ⇒ 空列表 ⇒ 门禁放行（缺值不造假，硬约束⑤）。
    """
    ids = {
        str(row.item_id or "").strip()
        for row in db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
            )
        ).all()
    }
    raw_item_id = str(_parse_raw(note).get("item_id") or "").strip()
    if raw_item_id:
        ids.add(raw_item_id)
    return sorted(item_id for item_id in ids if item_id)


def _occupied_goods_item_ids(db: Session, note: Note, item_ids: list[str]) -> list[str]:
    """这批商品里，哪些已经被**别的**笔记占用（已发布 / 正在发布，§31.5.2）。

    join 用 `(user_id, note_id)` 而不是本地主键：`note_goods` 的键就是**平台笔记 ID**
    （§26.7，两个外键已整条去掉）⇒ 必须显式给 ON 条件。发布成功的笔记关系行已迁到平台
    note_id（`_move_note_goods_relations`），正在发布的笔记两边都还是本地 ID ⇒ 两种
    形态都能对上。

    ⚠️ 排除自己（`Note.id != note.id`）：重发场景下本篇自己可能是 `failed`，本就不是
    占用方；但**已 `published` 的自己**会先被 `_check_publishable` 拦下，不靠这里兜。
    """
    if not item_ids:
        return []
    statement = (
        select(NoteGoods.item_id)
        .join(
            Note,
            and_(Note.user_id == NoteGoods.user_id, Note.note_id == NoteGoods.note_id),
        )
        .where(
            NoteGoods.user_id == note.user_id,
            NoteGoods.item_id.in_(item_ids),
            Note.publish_status.in_(_GOODS_OCCUPYING_STATUSES),
            Note.id != note.id,
        )
    )
    return sorted({str(item_id) for item_id in db.scalars(statement).all() if str(item_id or "")})


def _allow_duplicate_goods_publish(db: Session, user_id: int) -> bool:
    """商品笔记是否**允许重复发布**（运行时配置 `xhs_allow_duplicate_goods_publish`，
    默认关 = 不允许重复）。

    **唯一读点**（`settings_service.LIVE_KEYS` 的 `xhs_allow_duplicate_goods_publish`
    指向本函数，由 `test_app_settings.py::test_live_keys_are_real_read_points` 钉住
    「函数体里真的有这个键名」）：设置页改完**立即生效**（设计 10 §7.4），不需要重启、
    不需要重跑种子。读配置失败退 `default=False`（保守侧 = 仍然拦截），绝不把一次发布
    请求炸成 500。

    ⚠️ 键名**写成字面量**而不是模块级常量：本项目的读点守卫按「函数体里出现该键名」
    判定接线（`inspect.getsource`）—— 走常量会让守卫看不见这个读点（设计 13 §38.2 的
    `rate_limit_per_minute_for_key` 等三个读点同款写法）。
    """
    from backend.app.services import settings_service

    return settings_service.live_bool(
        db, user_id, "xhs_allow_duplicate_goods_publish", default=False
    )


def _check_goods_publish_guard(db: Session, note: Note) -> None:
    """**商品重复发布门禁**（设计 13 §31.5.2，2026-09-28 用户口径）。

    开关关（默认）且「挂载的商品已有**别的**笔记（已发布 / 正在发布）」⇒ **不再发布**：
    该笔记落 `publish_status=failed` + 原因「挂载的商品已经发布过笔记」，再抛
    :class:`NotePublishError`（端点转 400 `GOODS_ALREADY_PUBLISHED`）。

    ⚠️ 与其余门禁（类型 / 状态 / 无媒体 / 无令牌）**刻意不同**：那些是「这篇笔记本身
    能不能发」的确定性答案 ⇒ 400 且**不动状态**；本条是**策略性拒绝**（笔记本身没问题，
    是「同一商品不许重复」的运营规则）—— 用户口径明确要求**落 `failed`**，好让用户在
    内容库里看得见「为什么没发出去」。判据不同、处置不同，不是两套实现。

    ⚠️ **零平台请求**：本函数在下载 / 上传 / 入队**之前**跑。
    ⚠️ 挂载为空 ⇒ 放行（没挂商品就没有「重复」可言）。
    ⚠️ 调用方是**同步门禁段**（`_begin_publish`，单篇与批量共用）⇒ 批量里命中篇只进
    `results` 的失败项，**不阻断其余篇**。
    """
    if _allow_duplicate_goods_publish(db, note.user_id):
        return
    item_ids = _goods_item_ids_for(db, note)
    if not item_ids:
        return
    if not _occupied_goods_item_ids(db, note, item_ids):
        return
    # 先落库再抛：用户口径要的是「该笔记设置为发布失败」（`_mark_failed` 是失败落库的
    # 唯一出口：状态 + 投影 + `publish_error` 一次写入，§32.3.2）。
    _mark_failed(db, note, GOODS_ALREADY_PUBLISHED_MESSAGE)
    logger.info(
        "note publish blocked by goods guard (note_pk=%s, item_ids=%s)", note.id, item_ids
    )
    raise NotePublishError(GOODS_ALREADY_PUBLISHED_CODE, GOODS_ALREADY_PUBLISHED_MESSAGE)


def _safe_name(text: str) -> str:
    """文件名安全化：只留安全字符并截断；`.` / `..` / 空 ⇒ 空串（退回纯序号）。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "_", str(text or "")).strip("._-")[:64]
    return "" if cleaned in {"", ".", ".."} else cleaned


def _temp_file_name(item_id: str, index: int, ext: str) -> str:
    """临时文件名 = 商品 ID + 序号（下载前定名，确保遗留文件可归属）。

    **序号必须保留**：一篇笔记的同商品图最多 6 张，只用商品 ID 会互相覆盖。
    """
    safe = _safe_name(item_id)
    prefix = f"{safe}_" if safe else ""
    return f"{prefix}{index + 1:03d}{ext}"


def _download_media(db: Session, note: Note) -> list[dict]:
    """下载媒体到临时文件夹（一个笔记一个文件夹），返回本地文件信息。

    节流（2026-09-25，用户口径）：**每两张之间随机停 1–3 秒**（首张不停）——
    连续快速下载商品图直链会触发平台 CDN 限流；随机值避免机器节奏特征。

    失败即抛 :class:`NotePublishError`——**先删本次临时目录**再抛（§31.3.5：
    重发是全量重下，半截文件对重发零价值）。
    """
    images = _select_media(db, note)
    item_id = _item_id_for(db, note)
    temp_dir = _note_temp_dir(note.note_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[dict] = []
    try:
        for index, asset in enumerate(images):
            if index > 0:
                gap = _download_gap_seconds()
                logger.debug("note publish download gap %.2fs (note_pk=%s, #%d)", gap, note.id, index + 1)
                time.sleep(gap)
            ext = _extension_for(asset.url)
            target = temp_dir / _temp_file_name(item_id, index, ext)
            try:
                response = requests.get(
                    asset.url,
                    timeout=_DOWNLOAD_TIMEOUT,
                    headers=_media_download_headers(),
                )
                response.raise_for_status()
                content = response.content
            except requests.RequestException as error:
                raise NotePublishError(
                    "PUBLISH_UPSTREAM_ERROR", f"下载媒体失败（第 {index + 1} 个）：{type(error).__name__}: {error}"
                )
            if len(content) < 100:
                raise NotePublishError("PUBLISH_UPSTREAM_ERROR", f"下载媒体失败（第 {index + 1} 个）：内容过小或为空。")
            target.write_bytes(content)
            # ⚠️ **不回写 `asset.local_path`**（§31.3.4）：临时路径随目录删除而失效，
            # 落库只会留下一批指向不存在文件的路径（第二份真相源）。`downloads` 列表
            # 在本次调用内自足。
            downloaded.append({"asset": asset, "path": target})
    except BaseException:
        # 下载阶段失败：删本次临时目录（§31.3.5）。含键盘中断等一切请求级中断形态
        # —— 进程被杀删不了（daemon 线程随进程死），但失败必须不留半截文件。
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return downloaded


def _extension_for(url: str) -> str:
    lowered = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"):
        if lowered.endswith(ext):
            return ext
    return ".jpg"


def _upload_media(adapter, downloaded: list[dict]) -> list[dict]:
    """上传全部媒体（一次 permit + 逐文件 PUT），返回 `images_list` 条目。

    ⚠️ permit 结构（2026-09-23 实测）：响应有**两个条目**（storageType 5 / 1，
    不同上传域），每个条目的 `fileIds[]` 是**一文件一个 ID**（不是一条目一文件）。
    取第一个条目，用它的 `uploadAddr` + `token` 把所有文件传完。
    """
    ok, message, permit_raw = adapter.upload_web_permit(len(downloaded))
    if not ok or not isinstance(permit_raw, dict):
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "申请上传凭证失败")
    permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
    if not permits:
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", "平台没有返回上传凭证。")
    permit = permits[0]
    file_ids = permit.get("fileIds") or []
    if len(file_ids) < len(downloaded):
        raise NotePublishError(
            "PUBLISH_UPSTREAM_ERROR",
            f"上传凭证数量不足（需要 {len(downloaded)} 个，只返了 {len(file_ids)} 个）。",
        )
    upload_addr = str(permit.get("uploadAddr") or "")
    token = str(permit.get("token") or "")

    images: list[dict] = []
    for index, item in enumerate(downloaded):
        file_id = str(file_ids[index])
        # 单文件重试一次：ROS 偶发 SSL EOF / 连接重置（2026-09-23 实测第 6 张踩到），
        # 整篇重来代价太大，单文件重试足够。
        last_message = ""
        uploaded = False
        for attempt in range(2):
            ok, message, _ = adapter.upload_file(
                upload_addr=upload_addr,
                file_id=file_id,
                token=token,
                file_path=str(item["path"]),
            )
            if ok:
                uploaded = True
                break
            last_message = message
        if not uploaded:
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", f"上传第 {index + 1} 个媒体失败：{last_message}")
        # `url` 带上（不进请求体）：`_build_publish_body` 要用它按顺序对齐商品图宽高。
        images.append({"file_id": file_id, "url": str(item["asset"].url or ""), "width": 0, "height": 0})
    return images


def _parse_raw(note: Note) -> dict:
    """`notes.raw_json` → dict。**JSON 列可能是 dict 也可能是历史遗留的字符串**
    （两种都吃过），统一在这里归一，别处不再各写一份。"""
    raw = note.raw_json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _note_topics(db: Session, note: Note) -> tuple[list[str], list[Topic]]:
    """该笔记当前的话题（§34.6）：`note_topics` 关系表 join `topics` 字典表。

    返回 `(话题名列表, 话题行列表)`（关系行 id 排序 = 落库顺序）。话题行带
    `platform_id`（商品笔记 AI 生成时存下的平台话题 ID）—— `hash_tag` 只认
    平台 ID，没有 ID 的话题只能走 desc 文本回填。

    ⚠️ 名字**一律剥掉平台标记**（`strip_topic_marker`，唯一实现）：存量行里有历史
    脏名（`拼车[话题`），不剥就会把标记写进发布体（`#拼车[话题][话题]#`）。
    """
    from backend.app.services.note_topic_service import (
        note_topic_names,
        note_topic_rows,
        strip_topic_marker,
    )

    names = [strip_topic_marker(name) for name in note_topic_names(db, note.id)]
    return names, note_topic_rows(db, note.id)


def _desc_with_topics(desc: str, topic_names: list[str]) -> str:
    """把话题以**平台形态** `#名字[话题]#` 回填进正文末尾（§34.6-3 修订）。

    ⚠️ 2026-09-25 修（用户报「发布成功的笔记均没有话题」）：平台认的正文话题形态
    是 `#名字[话题]#`（富文本标记）—— 依据两条：① 从平台同步回来的正文原文就是
    这个形态（`#拼车[话题]# #顺风车[话题]#`）；② 项目既有的创作者端发布实现
    `adapters/xhs/creator_api_adapter.py` 用同一形态回填。原实现写 ` #名字`
    （原句保留备查），平台当普通文本 ⇒ **发布的笔记一个话题都没有**。

    * 只追加**尚未出现在正文里**的话题（`parse_topic_tokens` 同时认平台形态与
      老形态），不重复追加；
    * 追加在正文末尾，` #名字[话题]#` 逐个隔开；
    * 话题为空 / 全部已存在 ⇒ 正文原样返回（最小改动）。
    """
    text = str(desc or "")
    if not topic_names:
        return text
    from backend.app.services.note_topic_service import parse_topic_tokens, topic_token

    present = {name.lower() for name in parse_topic_tokens(text)}
    missing = [name for name in topic_names if name.lower() not in present]
    if not missing:
        return text
    suffix = "".join(f" {topic_token(name)}" for name in missing)
    if text and not text.endswith((" ", "\n")):
        text += " "
    return text + suffix.lstrip()


def _build_publish_body(db: Session, note: Note, images: list[dict]) -> dict:
    """组装 `note/publish` 请求体（设计 13 §31.5 步 5）。

    ⚠️ **结构照抄 2026-09-23 bundle 实抓**（`index.605d6c8b.js` 发布函数）**且键名
    全部转下划线**（bundle 的 camelCase 由页面 `transform` 层转换；直连平台只认
    下划线 —— camelCase 会被 -9150「技术升级中」打回，实测）：

    ```json
    {"common": {"type": "normal", "title": "…", "desc": "…",
                "hash_tag": [{"id","name","type":"topic"}], "ats": [],
                "goods_info": {"comment": "",
                               "extension": {"is_adjust_ai": "0",
                                             "check_content_id": "<sku_id>",
                                             "goods_note_publish_type": "single_ai_publish",
                                             "publish_source": "qian_fang_agi",
                                             "feature": "{\"sellerScene\":\"ARK_PC_NOTE_AIGC\",\"aigcTaskId\":\"…\"}"}},
                "biz_relations": [{"biz_type": "GOODS_SELLER_V2", "biz_id": "<sku_id>",
                                   "extra_info": "{\"goods_id\":…,\"goods_name\":…,…}"}]},
     "image_info": {"images": [{"file_id": "…", "width": 0, "height": 0, "metadata": {"source": 0}}]}}
    ```

    三个要点：① **挂链粒度是 SKU**（`biz_id` / `check_content_id` 都是 `sku_id`，
    不是 item_id）；② `feature` 带 **AI 溯源**（`aigcTaskId`）—— 平台据此认这是
    「千帆 AGI 生成的商笔」；③ **话题给两条路**：`hash_tag` 收**带平台 ID** 的
    （`note_topics` join `topics`，与 `raw_json.aigc.topics` 按名字合并去重），
    `desc` 末尾把**全部**话题以平台形态 `#名字[话题]#` 回填 —— 平台正文认的是这个
    形态（实证见 :func:`_desc_with_topics`），少任何一条都会让笔记丢话题。
    """
    from backend.app.services.note_topic_service import strip_topic_marker

    raw = _parse_raw(note)
    aigc = raw.get("aigc") or {}
    aigc_topics = [
        {"id": str(t.get("id") or ""), "name": str(t.get("name") or "")}
        for t in aigc.get("topics", [])
        if isinstance(t, dict) and t.get("name") and t.get("id")
    ]
    # §34.6（2026-09-25 修订）：**查询该笔记下的话题**（note_topics 关系表 join
    # topics 字典表）—— 话题与正文已拆分，发布时**两条路一起回填**：
    # ① `hash_tag` 只收**带平台话题 ID** 的（aigc 自带的 + 话题行存了
    #    `platform_id` 的），按名字去重 —— 结构化参数，平台据此精确关联话题；
    # ② `desc` 末尾追加**全部**话题的平台形态 `#名字[话题]#`（含没平台 ID 的）——
    #    `hash_tag` 是参数，`desc` 才是平台正文里的展示 / 解析载体，**两者要同时给**
    #    （创作者端实现 `creator_api_adapter` 同款：hash_tag append + desc 追加）。
    #    ⚠️ 原口径「进过 hash_tag 的不在 desc 以文本重复」已被推翻：那会让 desc 里
    #    一个 `#` 都没有 ⇒ 发布的笔记**没有任何话题**（2026-09-25 用户报障）。
    topic_names, topic_rows = _note_topics(db, note)
    name_set = {name.lower() for name in topic_names}
    hash_tag: list[dict] = []
    hash_tag_seen: set[str] = set()
    for topic in aigc_topics:
        key = topic["name"].lower()
        if key in name_set and key not in hash_tag_seen:
            hash_tag.append({"id": topic["id"], "name": topic["name"], "type": "topic"})
            hash_tag_seen.add(key)
    for row in topic_rows:
        # 行名字也要剥标记（存量脏行 `拼车[话题`）—— `row.name` 是 ORM 属性，**不
        # 改它**（会被 flush 写库），只在局部用干净名。
        name = strip_topic_marker(row.name)
        key = name.lower()
        platform_id = str(row.platform_id or "")
        if key and platform_id and key not in hash_tag_seen:
            hash_tag.append({"id": platform_id, "name": name, "type": "topic"})
            hash_tag_seen.add(key)
    desc_topic_names = list(topic_names)
    sku_id = str(aigc.get("sku_id") or "")
    sku_name = str(aigc.get("sku_name") or note.title or "")
    task_id = str(aigc.get("task_id") or "")

    # 宽高从商品 raw_json 回填（平台 image_info 要宽高；缺失传 0，不造假）。
    # ⚠️ 2026-09-27（§26.7）：关系行存的是**平台商品 ID** ⇒ 按 `item_id` 找本地商品行；
    # 台账里没有这件商品 ⇒ `product=None`，宽高按缺值走（**不是**发布失败）。
    relations = list(
        db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
            )
        ).all()
    )
    product = None
    if relations:
        from backend.app.services.shop_goods_service import products_by_item_id

        product = products_by_item_id(db, int(note.user_id or 0), [relations[0].item_id]).get(
            str(relations[0].item_id or "")
        )
    dims = (
        _image_dims_for(product, [str(image.get("url") or "") for image in images])
        if product is not None
        else [{}] * len(images)
    )
    image_entries = []
    for index, image in enumerate(images):
        dim = dims[index] if index < len(dims) else {}
        image_entries.append(
            {
                "file_id": image["file_id"],
                "width": int(dim.get("width") or 0),
                "height": int(dim.get("height") or 0),
                "metadata": {"source": 0},
            }
        )

    common: dict = {
        "type": "normal",
        "title": note.title or "",
        # §34.6（2026-09-25 修订）：desc = 正文 + **全部**话题的平台形态
        # `#名字[话题]#` 回填 —— 平台正文认这个形态；`hash_tag` 是并行的参数，
        # 两者同时给（少 desc 这条 = 笔记没有话题）。
        "desc": _desc_with_topics(note.content or "", desc_topic_names),
        "hash_tag": hash_tag,
        "ats": [],
    }
    if sku_id:
        common["goods_info"] = {
            "comment": "",
            "extension": {
                "is_adjust_ai": "0",
                "check_content_id": sku_id,
                "goods_note_publish_type": "single_ai_publish",
                "publish_source": "qian_fang_agi",
                "feature": json.dumps(
                    {
                        "sellerScene": "ARK_PC_NOTE_AIGC",
                        "aigcTaskId": task_id,
                        "aigcImageTaskId": "",
                    },
                    ensure_ascii=False,
                ),
            },
        }
        common["biz_relations"] = [
            {
                "biz_type": "GOODS_SELLER_V2",
                "biz_id": sku_id,
                "extra_info": json.dumps(
                    {
                        "goods_id": sku_id,
                        "goods_name": sku_name,
                        "goods_type": "goods_seller",
                        "tab_id": 1,
                        "image_type": "spec",
                        "left_bottom_type": "BUY_GOODS",
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    return {"common": common, "image_info": {"images": image_entries}}


def _image_dims_for(product, image_urls: list[str]) -> list[dict]:
    """按**上传顺序**取商品图宽高（`raw_json.images[]` 的 link 与资产 URL 对齐）。

    入参是 URL 列表而不是「资产行 + 数量」：图片可能被 `MAX_NOTE_IMAGES` 截断、
    也可能与视频资产在同一篇里，按下标取资产行会错位（第 N 张配到别人的宽高）。
    """
    try:
        raw_images = json.loads(product.raw_json or "{}").get("images")
    except ValueError:
        raw_images = None
    dims_by_url: dict[str, dict] = {}
    if isinstance(raw_images, list):
        for entry in raw_images:
            if isinstance(entry, dict) and entry.get("link"):
                dims_by_url[str(entry["link"])] = {
                    "width": entry.get("width") or 0,
                    "height": entry.get("height") or 0,
                }
    return [dims_by_url.get(url, {}) for url in image_urls]


def _clear_temp_paths(db: Session, note: Note) -> None:
    """清掉历史版本写在资产行上的临时路径（`local_path`）。

    当前实现**不再回写** `local_path`（本模块 `_download_media` 的约定：临时路径
    随目录删除即失效，落库只会留下指向不存在文件的路径）。这个清理是给老数据
    收尾的 —— 留着它，「这篇笔记有没有本地文件」就有两个答案。
    """
    for asset in _assets_of(db, note.id):
        if asset.local_path:
            asset.local_path = ""


def _mark_failed(db: Session, note: Note, message: str) -> None:
    """统一的失败落库：状态 + 投影 + `publish_error` + 历史 `local_path` 清空。"""
    set_note_publish_status(note, PUBLISH_STATUS_FAILED, error=message)
    _clear_temp_paths(db, note)
    db.commit()


def _preaudit(adapter, note: Note, raw: dict, images: list[dict], topic_names: list[str]) -> None:
    """发布前预审核（平台侧 moderation；设计 13 §31.1.2 链路补步）。

    页面自身流程：**先 preaudit、后 publish**。任一 `check_pass=false` ⇒
    抛 :class:`NotePublishError` 带平台 `fail_msg` 原文（比本地护栏更权威的
    内容 moderation，用户看到的就是平台原话）。

    ⚠️ 响应键名是**下划线风格**（实测响应：`audit_result` / `check_pass` /
    `fail_msg` / `all_pass`）—— 与请求侧一致，别按 bundle 的 camelCase 读。
    """
    ok, message, result = adapter.preaudit_note(
        title=note.title or "",
        content=note.content or "",
        topic_names=topic_names,
        image_list=images,
        text_gen_task_id=str((raw.get("aigc") or {}).get("task_id") or ""),
    )
    if not ok or not isinstance(result, dict):
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "平台预审核失败")
    audit_result = (result.get("data") or {}).get("audit_result") or {}
    if not audit_result:
        return
    title_result = audit_result.get("title_result") or {}
    content_result = audit_result.get("content_result") or {}
    topic_result = audit_result.get("topic_result") or {}
    image_results = audit_result.get("image_results") or []
    image_failed = [r for r in image_results if isinstance(r, dict) and r.get("check_pass") is False]
    if (
        title_result.get("check_pass") is False
        or content_result.get("check_pass") is False
        or topic_result.get("check_pass") is False
        or image_failed
    ):
        reason = (
            title_result.get("fail_msg")
            or content_result.get("fail_msg")
            or topic_result.get("fail_msg")
            or (image_failed[0].get("fail_msg") if image_failed else "")
            or "笔记内容未通过平台预审核"
        )
        raise NotePublishError("PUBLISH_AUDIT_REJECTED", str(reason))


# --------------------------------------------------------------------------- #
# 异步发布：起发（同步）与执行（后台线程）
# --------------------------------------------------------------------------- #


def _begin_publish(db: Session, user_id: int, note_pk: int) -> Note:
    """同步门禁 + 状态迁移（设计 13 §31.5 步 0-1）。返回 note。

    门禁全部同步做完：调用方能**立即**知道「这篇发得发不了」，不把数据问题
    （无媒体 / 无令牌）拖到后台。

    ⚠️ 这里**不再构造 adapter**（§35.9 持久化后队列只存 `note_pk`）：adapter 在
    worker 里按**库里当前有效**的 ark 令牌重建（`_publish_adapter_for`）—— 重启
    续跑时旧实例早已失效，凭据必须现取。
    """
    with _publish_gate_lock:
        note = _owned_note(db, user_id, note_pk)
        _check_publishable(note)
        _select_media(db, note)  # 无媒体 / 只有视频 ⇒ 同步拒绝（§31.5 步 0）
        # 商品重复发布门禁（§31.5.2，2026-09-28）：开关默认关 ⇒ 同一商品已有笔记
        # （已发布 / 正在发布）时不再发布，本篇落 failed + 原因，然后抛结构化错误。
        # ⚠️ 放在 `_select_media` **之后**：数据性问题（笔记本身有毛病）优先于策略性
        # 拒绝 —— 无媒体的笔记不该被标成「商品已发布过」。
        _check_goods_publish_guard(db, note)
        cookies = qianfan_cookies_for_account(db, note.platform_account_id)
        if not cookies:
            raise NotePublishError(
                "QIANFAN_CREDENTIAL_MISSING", "该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。"
            )
        # 状态先落库：发布中崩溃重启后不会被误判为可重发。状态与 `published` 投影
        # 一次写入（唯一入口，§32.3.2）。
        set_note_publish_status(note, PUBLISH_STATUS_PUBLISHING)
        note.publish_attempts = (note.publish_attempts or 0) + 1
        db.commit()
    return note


def start_publish(db: Session, user_id: int, note_pk: int, *, adapter_factory=None) -> Note:
    """单篇异步发布入口（端点调）：门禁 + 入队，**毫秒级返回**。

    后台 worker 跑完下载 / 上传 / 预审核 / 发布并落终态；调用方把返回的 note
    （`publish_status=publishing`）直接序列化回前端，终态由前端轮询获取。

    `adapter_factory` = worker 在**本进程内**重建 adapter 用的工厂（见
    :data:`_publish_adapter_factories`）。传 `None`（或进程重启后进队列）⇒ worker
    回落到默认千帆工厂 —— 那正是重启续跑该走的来源。
    """
    note = _begin_publish(db, user_id, note_pk)
    # 一次动作 = 一个批次（2026-09-26 用户口径「批次逻辑不再移除，生成唯一批次号」）：
    # 单篇点击也开一个新批次号（一条明细）、**不复用**任何已有批次 —— 批次号是
    # 「这一次点击」的标识，复用就回到被淘汰的「轮次记账」形态。
    publish_batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
        source=note_queue_service.SOURCE_NOTE_PUBLISH_SINGLE,
    )
    _enqueue_publish(
        db,
        user_id=user_id,
        note=note,
        adapter_factory=adapter_factory,
        source=note_queue_service.SOURCE_NOTE_PUBLISH_SINGLE,
        batch=publish_batch,
    )
    db.commit()
    # ⚠️ 唤醒放在**提交之后**：worker 自建 Session，看不到未提交的行 ——
    # 提前唤醒只会让它空转一轮再回 wait。
    ensure_publish_worker()
    logger.info("note publish enqueued (note_pk=%s, attempts=%s)", note.id, note.publish_attempts)
    return note


def start_batch_publish(
    db: Session,
    user_id: int,
    note_pks: list[int],
    *,
    adapter_factory=None,
    max_items: int = PUBLISH_BATCH_MAX_ITEMS,
) -> dict:
    """批量异步发布入口（设计 13 §31.5 步 8）：逐篇过门禁，通过篇按序入队。

    **一个** worker 顺序跑完队列。立即返回 `{total, started, failed, results}`：
    通过篇 `{ok:true, status:"publishing"}`，门禁失败篇 `{ok:false, error, code}`
    （不阻断其余）。终态走前端轮询。

    来源统一标 `note_publish_batch`（按钮级，2026-09-26 用户口径「来源要具体
    到按钮」）。
    """
    cleaned = [int(pk) for pk in note_pks if str(pk).strip()]
    distinct = list(dict.fromkeys(cleaned))[:max_items]
    # 一次批量 = 一个批次（懒创建：全部门禁不过 ⇒ 不留空批次行）
    publish_batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
        source=note_queue_service.SOURCE_NOTE_PUBLISH_BATCH,
    )
    results: list[dict] = []
    for note_pk in distinct:
        try:
            note = _begin_publish(db, user_id, note_pk)
        except NotePublishError as error:
            results.append({"note_pk": note_pk, "ok": False, "error": error.message, "code": error.code})
            continue
        _enqueue_publish(
            db,
            user_id=user_id,
            note=note,
            adapter_factory=adapter_factory,
            source=note_queue_service.SOURCE_NOTE_PUBLISH_BATCH,
            batch=publish_batch,
        )
        results.append({"note_pk": note.id, "ok": True, "status": "publishing", "note_id": note.note_id})
    db.commit()
    ensure_publish_worker()  # 同上：提交后才唤醒
    started = sum(1 for r in results if r.get("ok"))
    logger.info("note publish batch enqueued (total=%s, started=%s)", len(distinct), started)
    return {"total": len(distinct), "started": started, "failed": len(distinct) - started, "results": results}


def _enqueue_publish(
    db: Session,
    *,
    user_id: int,
    note: Note,
    adapter_factory: Any = None,
    source: str,
    batch: Any = None,
) -> tuple[Task, bool]:
    """入队一篇到 `tasks` 表（幂等）。返回 `(行, created)`。

    `created=False` = 已在队列 / 执行中（不重复请求、不重复发布）。

    `note.title` 快照随行落库（§35.9：入队即完整落库 —— 队列记录页直接显示
    主体名，不为每行回头拉笔记）；`source` = **按钮级来源**（必填，单条 / 批量
    入口各传各的，2026-09-26 用户口径「来源要具体到按钮」）。

    `batch` = 这次动作的批次上下文（2026-09-26 批次层）：同一动作的每次调用传
    同一个 `BatchRequest`，底座据此把真落库的篇挂到同一个唯一批次号下。

    ⚠️ 不 commit、**不唤醒 worker**：提交归调用方（`_begin_publish` 的 commit 会
    连带提交，`start_*` 末尾再 commit 收尾），唤醒一律在提交之后。
    """
    row, created = note_queue_service.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
        note_pk=note.id,
        account_id=note.platform_account_id,
        note_title=str(note.title or ""),
        source=source,
        batch=batch,
    )
    if created and adapter_factory is not None:
        # 只缓存工厂（不缓存 adapter 实例）：实例可能带 cookies 等状态，且重启后
        # 必然失效 —— 每篇现造一个，与改造前的「每篇一个实例」一致。
        with _publish_queue_lock:
            _publish_adapter_factories[row.id] = adapter_factory
    return row, created


def ensure_publish_worker() -> None:
    """确保有一条活着的 worker（惰性起；已活则只唤醒它）。

    公开（不是 `_` 前缀）：**启动恢复**也要用它 —— `main.py::lifespan` 复位完
    中断行后立刻唤醒，把上次进程留下的 `pending` 项接着跑（否则要等看门狗 30 秒
    后巡检才发现「没 worker」）。
    """
    with _publish_queue_lock:
        if _publish_worker is None or not _publish_worker.is_alive():
            _spawn_publish_worker()
    _publish_wakeup.set()


def _publish_worker_loop() -> None:
    """DB 队列 worker：醒来 → 认领一条 → 跑一条 → 再认领 …… 认不到就回去等。

    ⚠️ 唤醒信号只是**提示**：`wait(timeout=PUBLISH_IDLE_POLL_SECONDS)` 带兜底超时
    ⇒ 任何一次 `set` 丢失，最坏也只晚 5 秒发现新任务，不会永久停摆（DEF-048）。

    ⚠️ DEF-069 同型缺口（本轮补）：改造前的发布 worker **只有 `except Exception`
    内网、没有外层网、没有看门狗、没有停止出口** —— 线程被 `SystemExit` 之类
    带走（threading 对它静默）就永久停摆，队列里的篇没人跑、笔记永远卡在
    `publishing`。本版补齐四件：外层 `BaseException` 网、看门狗
    （`start_publish_worker_watchdog`）、停止出口、以及「认领失败也不死」的退避。

    `_publish_worker_stop` 是**收尾 / 测试专用出口**：置位（并 `set` 唤醒）后
    **不再认领新任务**即退。生产永不置位。⚠️ 「不丢正在排队的篇」由**库**保证
    （未跑的项是 `pending` 行，下次启动接着跑），不靠 worker 排空。
    """
    while not _publish_worker_stop.is_set():
        _publish_wakeup.wait(timeout=PUBLISH_IDLE_POLL_SECONDS)
        _publish_wakeup.clear()
        while not _publish_worker_stop.is_set():
            item_id = _claim_one_publish_item()
            if item_id is None:
                break
            try:
                _run_one_publish(item_id)
            except BaseException as error:  # noqa: BLE001 - 线程不死（DEF-069）
                logger.error(
                    "note publish worker: run escaped the inner net (task=%s): %s",
                    item_id,
                    error,
                    exc_info=True,
                )


def _claim_one_publish_item() -> Optional[int]:
    """认领一条待发布项，返回其 `tasks.id`（无活可干 / 认领出错 ⇒ None）。

    认领用**独立 Session**（`claim_next_item` 内部提交换状态，用完即关）；认领期
    的 DB 异常在这里收住 —— 认领失败只是这一轮空转（外层 `wait(timeout=…)` 天然
    退避），不许把 worker 带走。
    """
    db = _session_factory()
    try:
        item = note_queue_service.claim_next_item(
            db, item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM
        )
        return int(item.id) if item is not None else None
    except BaseException:  # noqa: BLE001 - 认领失败退避，不死 worker
        logger.exception("note publish worker: claim failed")
        return None
    finally:
        db.close()


def _default_publish_adapter_factory() -> Any:
    """worker 重建 adapter 的**默认**工厂（进程重启续跑走的来源）。

    惰性导入：`get_qianfan_api_adapter_factory()` 定义在适配器模块（避免与路由层
    成环），这里按需取，不在 import 期固化。
    """
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    return get_qianfan_api_adapter_factory()


def _publish_adapter_for(db: Session, item_id: int, note: Note) -> Any:
    """重建本篇要用的 adapter：优先用入队时缓存的工厂，否则默认千帆工厂。

    **为什么必须重建**：adapter 不可序列化，且重启后旧实例早已失效 —— 凭据要
    按**库里当前有效**的 ark 令牌现取（§18.14：一个账号一个平台一个令牌；取不到
    就是 400 级失败，**不借会话、不发探活**）。这也是重启续跑的必要条件。

    ⚠️ 抛 :class:`NotePublishError` 表示「凭据拿不到」—— 调用方按失败记账。
    """
    with _publish_queue_lock:
        factory = _publish_adapter_factories.get(item_id)
    cookies = qianfan_cookies_for_account(db, note.platform_account_id)
    if not cookies:
        raise NotePublishError(
            "QIANFAN_CREDENTIAL_MISSING", "该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。"
        )
    return (factory or _default_publish_adapter_factory())(cookies)


def _run_one_publish(item_id: int) -> None:
    """跑一条（按 `tasks.id`）：自建 Session；任何漏网异常都不许把队列卡死。

    安全网不条件性（DEF-048）：**任何**异常路径都必须把这条写成终态，否则它
    永远挂在 `running` 上（进度卡「发布中」永远转圈），且笔记会停在 `publishing`。
    """
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None:
            return  # 行已被清理任务删掉 / 不存在：无事可做
        note_pk = note_queue_service.note_pk_of(item)
        note = db.get(Note, note_pk) if note_pk else None
        if note is None:
            note_queue_service.finish_item(
                db, item, ok=False, reason="笔记不存在", code="NOTE_NOT_FOUND"
            )
            return
        try:
            adapter = _publish_adapter_for(db, item_id, note)
        except NotePublishError as error:
            _mark_failed(db, note, error.message)
            note_queue_service.finish_item(
                db, item, ok=False, reason=error.message, code=error.code
            )
            return
        outcome = _execute_publish(db, note, adapter)
        ok = bool(outcome.get("ok"))
        note_queue_service.finish_item(
            db,
            item,
            ok=ok,
            reason="" if ok else str(outcome.get("error") or ""),
            code="" if ok else str(outcome.get("code") or "PUBLISH_FAILED"),
        )
    except BaseException as error:  # noqa: BLE001 - 兜底安全网：SystemExit 也不许把队列卡死
        logger.warning("note publish worker crashed (task=%s): %s", item_id, error)
        try:
            db.rollback()  # 失败的 commit 留待回滚状态；不回滚则恢复动作也会炸
        except Exception:  # noqa: BLE001 - 恢复也失败只能留痕，不能再抛
            logger.exception("note publish rollback failed (task=%s)", item_id)
        try:
            item = db.get(Task, item_id)
            if item is not None:
                note_queue_service.finish_item(
                    db,
                    item,
                    ok=False,
                    reason=f"{type(error).__name__}: {error}",
                    code="PUBLISH_FAILED",
                )
                # 笔记若还卡在 publishing ⇒ 一并标失败（`_execute_publish` 已自标的
                # 那条不重复：它会把状态写成 published / failed）。
                note = db.get(Note, note_queue_service.note_pk_of(item))
                if note is not None and note.publish_status == PUBLISH_STATUS_PUBLISHING:
                    _mark_failed(db, note, f"{type(error).__name__}: {error}")
        except Exception:  # noqa: BLE001 - 连记账都失败：留痕，行留 running 由启动恢复捡回
            logger.exception("note publish failed to record failure (task=%s)", item_id)
    finally:
        db.close()
        with _publish_queue_lock:
            _publish_adapter_factories.pop(item_id, None)


# --------------------------------------------------------------------------- #
# 发布 worker 守活（DEF-069 同型缺口的另一半，2026-09-25）
# --------------------------------------------------------------------------- #
def count_open_publish_items(db: Session) -> int:
    """还有多少条待发布 / 在发布（测试与启动判断用）。"""
    return note_queue_service.count_open_items(
        db, item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM
    )


def _spawn_publish_worker() -> None:
    """起一条新的 worker 线程（调用方持 `_publish_queue_lock`）。

    起新世代时清掉历史停止位 —— 重建出来的 worker 不该被上一世代的收尾位挡住。
    """
    global _publish_worker
    _publish_worker_stop.clear()
    _publish_worker = threading.Thread(
        target=_publish_worker_loop,
        name=PUBLISH_WORKER_THREAD_NAME,
        daemon=True,
    )
    _publish_worker.start()


def publish_worker_thread_alive() -> bool:
    """进程里有没有**活着的** `note-publish-worker` 线程（唯一可信存活判据，DEF-069）。

    为什么不看 `_publish_worker` 模块变量：线程带异常 / `SystemExit` 死后它仍指着
    那个 `Thread` 对象，而 `ensure_publish_worker` 只在**入队时**检查一次 —— 队列
    跑起来之后线程死了，没有任何现存路径会发现（同 `scheduler_service` DEF-067、
    同步 worker DEF-069 的教训：状态位会骗人，线程本身不会）。
    """
    return any(
        thread.name == PUBLISH_WORKER_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def stop_publish_worker(*, timeout: float = 5.0) -> None:
    """让当前 worker **立即停手退出**（**测试 / 进程收尾专用**，生产不调用）。

    只置停止位不够：worker 常态卡在 `_publish_wakeup.wait(timeout=…)` 上，必须再
    `set` 一下 wakeup 才能唤醒它回来检查停止位。唤醒后它**不再认领新任务**、手上
    那条跑完即退。⚠️ 「不丢正在排队的篇」现在**由库保证**（未跑的项是 `pending`
    行），不靠 worker 排空。
    """
    _publish_worker_stop.set()
    _publish_wakeup.set()
    worker = _publish_worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)


def _revive_publish_worker() -> None:
    """worker 线程死了 ⇒ 复位它卡住的那条 + 重建 worker（不丢篇）。

    线程死后，它认领的那条 `tasks` 行**永远停在 `running`**（`_publish_worker_loop`
    的收尾都没机会跑）⇒ 不复位的话它既不会被重跑、也永远算在「发布中」里。复位
    就是把它改回 `pending`（`recover_interrupted` 只动 `running` 行）。

    ⚠️ **顺序：先复位、再起新 worker**。反过来新 worker 可能刚认领一条就被复位
    抢回 `pending` ⇒ 同一条被跑两遍。

    ⚠️ 不在这里复位笔记的 `publishing`：那条笔记会被重跑的 `_execute_publish`
    覆盖终态；而全局复位 `publishing`（`recover_publishing_notes`）只在**启动时**
    调用一次 —— 正常运行时 `publishing` 是「正在跑」的合法状态。
    """
    recovered = {"requeued": 0, "failed": 0}
    try:
        db = _session_factory()
        try:
            recovered = note_queue_service.recover_interrupted(
                db, item_task_types=(note_queue_service.TASK_TYPE_PUBLISH_ITEM,)
            )
        finally:
            db.close()
    except BaseException:  # noqa: BLE001 - 复位失败也要把 worker 建起来
        logger.exception("发布 worker 重建：中断行复位失败（继续重建 worker）")
    with _publish_queue_lock:
        if _publish_worker is None or not _publish_worker.is_alive():
            _spawn_publish_worker()
    _publish_wakeup.set()  # 新 worker 可能已在 wait 上：不等下一批也要立刻跑积压
    logger.error(
        "note-publish-worker 已死 —— 已重建（中断行 requeued=%s failed=%s）",
        recovered.get("requeued"),
        recovered.get("failed"),
    )


def start_publish_worker_watchdog(
    *, interval: float = PUBLISH_WATCHDOG_INTERVAL_SECONDS
) -> threading.Thread:
    """守活线程：worker 死了就重建（DEF-069 同型缺口，2026-09-25）。

    发布 worker 原文（改造前）：**只有 `except Exception` 内网** —— `SystemExit`
    是 `BaseException` 子类，`except Exception` 抓不到，threading 对它静默
    （不打 traceback、不重启线程）⇒ 线程无声消失、队列剩余篇永久停摆、笔记
    永远卡在 `publishing`。与 DEF-067（APScheduler）/ DEF-069（同步 worker）同型：
    单线程组件没有自愈，任何够不着的死法都是永久停摆。

    看门狗自己绝不被一次失败带走（`except BaseException` + 失败指数退避，上限
    `PUBLISH_WATCHDOG_MAX_BACKOFF_SECONDS`）。进程收尾调
    :func:`stop_publish_worker_watchdog`。
    """
    _publish_watchdog_stop.clear()

    def _loop() -> None:
        backoff = interval
        while not _publish_watchdog_stop.is_set():
            if _publish_watchdog_stop.wait(backoff):  # 可被 stop 打断的等待
                return
            try:
                if publish_worker_thread_alive():
                    backoff = interval
                    continue
                _revive_publish_worker()
                backoff = interval
            except BaseException:  # noqa: BLE001 - 看门狗自己也不能死
                logger.exception("发布 worker 看门狗重建失败，%.0f 秒后重试", backoff)
                backoff = min(backoff * 2, PUBLISH_WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name="publish-worker-watchdog", daemon=True)
    thread.start()
    return thread


def stop_publish_worker_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_publish_worker_watchdog` 自动复位）。"""
    _publish_watchdog_stop.set()


def _backfill_detail_ref(db: Session, note: Note, adapter: Any) -> None:
    """发布成功后 best-effort 把**详情地址**补进 `raw_json`（§25.4，2026-09-25）。

    为什么需要：平台 `note/publish` 响应只回 `{"id": 平台 note_id}`，**不带**
    `note_url` / `xsec_token`。本地生成笔记的 `raw_json` 因此没有详情地址 ⇒
    内容库「同步」按钮（`sync_note_from_platform` → `hydrate_note_detail`）的
    `_note_detail_url` 取不到地址 ⇒ 用户看到「没有可用的详情地址 / 缺详情令牌」——
    按钮在、功能不在（真实库 id 1967「中古风桌旗…」）。⚠️ **这条回查就是缺令牌的
    自救路径**：§19.13.9（2026-09-28）起不再翻 `user_posted` 兜底，本地生成笔记
    的令牌全靠它按 `note_ids` 精确补上。

    实现**委托** `note_sync_service.backfill_note_detail_ref`（单一实现）：发布
    路径与单条同步路径共用同一份「回查千帆商品笔记列表 → 合并 raw」逻辑，
    不另写一份（DEF-044/047/058 的教训）。区别只在凭据来源：这里用发布同一个
    千帆适配器（凭据已在手），同步路径用账号自己的 `ark` 令牌现场构造。

    best-effort：任何失败只 log 不抛 —— 发布已成功，不能因为补地址失败把笔记
    拖成失败。**不 commit**：在成功回写事务里，由调用方统一提交。
    """
    fetch = getattr(adapter, "get_goods_note_page", None)
    if fetch is None:
        return
    from backend.app.services.note_sync_service import backfill_note_detail_ref

    def _fetch(note_ids: list[str]):
        # 与 `shop_goods_service.sync_note_ids` 同一组参数（note_ids 精确查询）。
        return fetch(page_num=1, page_size=len(note_ids), note_ids=note_ids)

    backfill_note_detail_ref(db, note, _fetch)


def _move_note_goods_relations(
    db: Session, note: Note, *, old_note_id: str, new_note_id: str
) -> int:
    """发布成功改写 `notes.note_id` 后，把 `note_goods` 关系行的笔记键一起迁到新 ID（§26.7）。

    **为什么必须做**：本地生成笔记的 `note_id` 是 `local_…` 合成 ID（§31），发布成功后
    平台才给真 ID（`_execute_publish` 里 `note.note_id = platform_note_id`）。关系表
    以**平台笔记 ID** 为键 ⇒ 不迁 = 「笔记一发出去，挂链商品就查不到了」（关系行还在，
    但指向一个不再存在的笔记 ID）。

    幂等 / 去重：新键上可能**已经**有同 `item_id` 的关系行（同步链路按真 ID 落过）⇒
    那种旧行**删掉**（新行已表达同一事实），不撞 `uq_note_goods_user_note_item`。
    返回真正迁移过去的关系行数（调用方只用于日志）。
    """
    old = str(old_note_id or "").strip()
    new = str(new_note_id or "").strip()
    if not old or not new or old == new:
        return 0
    rows = list(
        db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == old
            )
        ).all()
    )
    if not rows:
        return 0
    existing = {
        str(row.item_id or "")
        for row in db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == new
            )
        ).all()
    }
    moved = 0
    for row in rows:
        if str(row.item_id or "") in existing:
            db.delete(row)  # 新键上已有同一关系 ⇒ 旧行多余
            continue
        row.note_id = new
        existing.add(str(row.item_id or ""))
        moved += 1
    db.flush()
    return moved


def _execute_publish(db: Session, note: Note, adapter) -> dict:
    """执行发布链路（设计 13 §31.5 步 2-6）。调用方已把状态置为 publishing。

    **调用方是单 worker 队列**（一次只跑一篇）—— 顺序执行就是节流的第二道保障：
    张间 1–3s 的间隔不会被并行发布线程叠没。
    """
    local_note_id = note.note_id  # 成功回写前捕获：临时目录按它建、也按它删（历史缺陷）
    try:
        downloaded = _download_media(db, note)
        images = _upload_media(adapter, downloaded)
        body = _build_publish_body(db, note, images)
        raw = _parse_raw(note)
        # §34.6：预审核的话题名同样查话题表（含手动加的话题）—— 发布前审核的
        # 输入必须与发布体一致（两处各取一次必然漂移）。
        topic_names, _ = _note_topics(db, note)
        _preaudit(adapter, note, raw, images, topic_names)
        ok, message, result = adapter.publish_note(body)
        if not ok or not isinstance(result, dict):
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "平台发布失败（未给原因）")
        # 平台 ID 在 `data.id`（实测响应：`{"success":true,"data":{"id":"6ab2…","note_id":"…"}}`）。
        data = result.get("data") or {}
        platform_note_id = str(data.get("id") or data.get("note_id") or "")
        if not platform_note_id:
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", "平台发布成功但没有返回笔记 ID。")
    except NotePublishError as error:
        _mark_failed(db, note, error.message)
        logger.info("note publish failed (note_pk=%s): %s / %s", note.id, error.code, error.message)
        return {
            "ok": False,
            "note_pk": note.id,
            "note_id": local_note_id,
            "error": error.message,
            "code": error.code,
        }
    except Exception as error:  # noqa: BLE001 - 未预期异常也要变成可读失败，不 500
        # 先回滚：上面任一步的 commit 失败（如平台返回的 note_id 撞唯一键）
        # 会把 session 留在待回滚状态，不回滚则记失败的 commit 也会炸 ⇒
        # 笔记卡在 publishing（安全网自己失效）。
        db.rollback()
        message = f"{type(error).__name__}: {error}"
        _mark_failed(db, note, message)
        logger.warning("note publish unexpected error (note_pk=%s): %s", note.id, error)
        return {
            "ok": False,
            "note_pk": note.id,
            "note_id": local_note_id,
            "error": message,
            "code": "PUBLISH_FAILED",
        }

    # ---- 成功回写（一个事务）----
    note.note_id = platform_note_id
    # ⚠️ 2026-09-27（§26.7）：关系表以**平台笔记 ID** 为键 ⇒ `note.note_id` 一改写，
    # 旧键（本地生成的 `local_…` 合成 ID）上的关系行就必须**跟着迁到新 ID**，
    # 否则「笔记一发出去，挂链商品就查不到了」（关系还在，指向一个不再存在的笔记 ID）。
    _move_note_goods_relations(db, note, old_note_id=local_note_id, new_note_id=platform_note_id)
    set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
    _backfill_detail_ref(db, note, adapter)
    _clear_temp_paths(db, note)
    db.commit()
    _cleanup_temp_dir(local_note_id)
    logger.info("note published (note_pk=%s -> %s)", note.id, platform_note_id)
    return {
        "ok": True,
        "note_pk": note.id,
        "note_id": platform_note_id,
        "platform_note_id": platform_note_id,
        "published_at": note.published_at.strftime("%Y-%m-%d %H:%M:%S") if note.published_at else "",
    }


def publish_queue_snapshot(db: Session, user_id: int, *, platform: Optional[str] = None) -> dict:
    """发布队列快照（进度卡用；2026-09-25 / §35.9 持久化；2026-09-26 常驻化）。

    2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：快照只描述
    **队列实时态 + 近期终态** —— `running` = 正在发布的那篇，`queued` = 排队中的
    （FIFO 序），`total` = 两者 + 近期终态数之和（进度卡停止条件靠它成立）。

    **不再有 `published` / `failed` 计数字段**：「本轮成败」由前端按自己入队的项
    累计（后端不知道用户这一轮点了什么）。终态原因不走快照 —— 发布失败的原因
    落库在笔记行 `publish_error` 上，卡片直接读得到（与同步 / 生成不同）。

    ⚠️ 2026-09-26 用户口径改口径（「**只统计当日发布的数据**……统计队列记录表中的
    数据」）：卡上三数**不再由前端按自己入队的篇累计**（刷新页面就丢、也看不见
    别的入口入队的），改由 `today` 键（`note_queue_service.today_item_counts`：
    当日 `tasks` 行现算 成功 / 失败 / 待处理）承担。原「本轮成败由前端累计」
    对**卡面数字**不再成立；逐篇结果消息仍由前端按 `finished` 收口。

    形状与另两个队列对齐（前端三卡同一套轮询骨架）；实现委托持久化底座，
    本模块不留第二份实现（DEF-044）。
    """
    raw = note_queue_service.snapshot(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
        platform=platform,
    )
    return {
        "running": raw["running"],
        "queued": raw["queued"],
        "total": raw["total"],
        # 进度卡的**当日三数**（2026-09-26 用户口径「只统计当日发布或同步的数据，
        # ……统计队列记录表中的数据」）：来自 `tasks` 表，不是本页累计。
        "today": note_queue_service.today_item_counts(
            db,
            user_id=user_id,
            item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
        ),
    }


def wait_publish_done(note_pk: int, timeout: float = 30.0) -> bool:
    """等某篇的队列项跑到终态（**测试用**；生产代码不调）。超时返 False。

    持久化后「跑完了」的判据落在**库**上（不再有内存完成事件可等）：轮询库里还有
    没有该 `note_pk` 的未终态项。该篇从未入队 ⇒ 也算「不用等」（返 True），与
    改造前 `event is None ⇒ True` 同口径。
    """
    deadline = time.monotonic() + timeout
    while True:
        open_pks: list[int] = [int(note_pk)]  # 查库失败保守当「还在跑」
        db = _session_factory()
        try:
            open_pks = note_queue_service.list_note_pks_by_status(
                db,
                item_task_type=note_queue_service.TASK_TYPE_PUBLISH_ITEM,
                statuses=("pending", "running"),
            )
        except BaseException:  # noqa: BLE001 - 查库失败继续等，不把测试判成「跑完」
            logger.exception("wait_publish_done: 查询发布队列失败")
        finally:
            db.close()
        if int(note_pk) not in open_pks:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
