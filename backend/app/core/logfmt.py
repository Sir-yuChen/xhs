"""统一日志格式（设计 64 §2.1）：全仓**唯一**的行格式与业务事件构造器。

## 行格式（唯一真相源）

```
%(asctime)s [%(levelname)s] [%(name)s] %(message)s
```

业务事件（`biz()` 构造）的 message 形如 `event=auth.login actor=zyu result=ok ...`
—— key=value 平铺，`grep "event=auth.login"` 一发命中。

## 三个消费者

1. `main.lifespan` 用 `configure_root_logger()` 把 **root logger** 的 handler 格式
   统一（uvicorn 的 handler 由 `uvicorn.log_config` 传入同一 FORMATTER）；
2. `business_log_service.record()` 用 `biz()` 构造 DB 行的 summary/文本行；
3. `.hermes/scripts/run-app.py` 只做**文件轮转**（stdout 重定向的行格式由这里保证）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

#: 唯一行格式。`[%(name)s]` 保留模块名（排障定位），业务事件的 name 统一挂 `biz` 域。
LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 业务日志的 logger 名（`business_log_service.record` 的文本行全部走它）。
BIZ_LOGGER_NAME = "biz"

#: uvicorn 各 logger 名（它们的 handler 格式要在 `configure_root_logger` 一并对齐）。
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.access", "uvicorn.error")


def make_formatter() -> logging.Formatter:
    """统一 formatter（每次新建 —— logging.Formatter 可被多 handler 共享但新建更省心）。"""
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def configure_root_logger(*, level: int = logging.INFO) -> None:
    """root + uvicorn 家族 handler 格式统一（设计 64 §2.1）。

    幂等：重复调用不会叠 handler（按是否存在**已配置**的 StreamHandler 判断）。
    ⚠️ 只动**格式**不动去向 —— stdout/stderr 仍交给启动器重定向进 app.log
    （launcher 拥有文件句柄，Python 侧 rotation 管不到 fd，见设计 64 §2.4）。
    """
    formatter = make_formatter()
    root = logging.getLogger()
    root.setLevel(level)
    configured = False
    for handler in list(root.handlers):
        configured = True
        handler.setFormatter(formatter)
    if not configured:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    for name in UVICORN_LOGGER_NAMES:
        uvicorn_logger = logging.getLogger(name)
        for handler in uvicorn_logger.handlers:
            handler.setFormatter(formatter)


def biz(event: str, /, **fields: Any) -> str:
    """业务事件 → 统一格式 message：`event=<名> k1=v1 k2=v2 …`。

    * `None` 值**跳过**（不上屏 `k=None` 噪声）；空串同样跳过；
    * 值里的空白/换行压成单空格（一行制）；
    * 值超 200 字符截断（日志行不是容器，详情进 DB 行的 `detail_json`）。
    """
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value).replace("\n", " ").replace("\r", " ")
        text = " ".join(text.split())
        if len(text) > 200:
            text = text[:200] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)
