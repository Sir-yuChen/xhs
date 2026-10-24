"""数据包暂存清理 —— 定时任务入口（v1.1.0 / 2026-10-24 新增）。

## 为什么需要这个任务

`package_staging_service.purge_expired()` 早就写好了，`STAGING_TTL = 2 小时`
也早就定义了，**但全仓没有一处调用它** —— 模块 docstring 写着
「过期由清理任务删」，而那个清理任务从来不存在。

桌面单机形态下这不致命（用户自己机器，重启就清空）。但用户口径
「后续部署在服务器，web 用户在浏览器操作」之后，这条会变成**磁盘被撑满**：

* 每个用户上传一个数据包 = 几十到几百 MB（实测一个包 118 图 + 1 视频）；
* 用户关掉浏览器就走了，暂存目录留在服务器上；
* 只有手动 `DELETE /api/xhs/package-precheck/{token}` 才会立刻清；
  正常用户不会去点那个按钮（甚至不知道它存在）。

⇒ 必须由定时任务兜底。**每小时一次**：TTL 是 2 小时，清扫频率跟着 TTL 走，
实际占盘峰值控制在「2~3 小时的上传量」，而不是无限累积。

## 为什么不用 `user_scoped=True`

暂存目录在磁盘上按 `u{user_id}` 分目录，但**清理是全局的**（一个进程扫一遍所有
用户目录即可，没必要每个用户各跑一遍任务行）。而且 `purge_expired(user_id=None)`
本身就是「扫全部」的语义。

⚠️ 不传 `user_id` 是**有意的**：本任务不涉及任何用户数据读写，只是删过期临时文件。
这与 DEF-066「数据隔离」不冲突 —— 它不做任何跨用户的数据**读取或修改**。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.services import package_staging_service

logger = logging.getLogger(__name__)


def run_package_staging_cleanup(user_id: int | None = None) -> dict[str, Any]:
    """删除过期（超过 `STAGING_TTL`）的数据包暂存目录。

    :param user_id: 忽略。保留该参数只为兼容 runner 的 `user_scoped` 注入签名
        （若将来改成按用户跑，直接用它即可）。传什么值都扫全量 —— 见模块
        docstring「为什么不用 user_scoped」。

    返回 `{"removed": n}`。**永不抛异常**：定时任务因清理失败而整轮不跑，
    会让磁盘继续涨，比少删一轮糟得多。
    """
    try:
        removed = package_staging_service.purge_expired()
    except Exception:  # noqa: BLE001 - 兜底，见 docstring
        logger.warning("数据包暂存清理失败（下轮再试）", exc_info=True)
        return {"removed": 0, "error": True}

    if removed:
        logger.info("数据包暂存清理：删除 %s 个过期目录", removed)
    return {"removed": removed}
