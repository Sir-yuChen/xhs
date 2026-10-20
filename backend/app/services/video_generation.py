"""视频生成客户端（设计 17 §13.4.4 v2.14，2026-10-02）。

与图像客户端（`ai_service.OpenAICompatibleImageClient.generate_image`，一次同步返回）
**根本不同**：视频是**异步任务**——提交拿 id、轮询等状态、终态再下载。

## 协议（OpenAI 兼容风格的视频接口）

```
① POST {base_url}/videos/generations        {"model", "prompt", "image"?, "duration"?, "size"?} → {"id"}
② GET  {base_url}/videos/generations/{id}   → {"status", "url"?}   status ∈ queued/running/succeeded/failed
③ GET  {url}                                 → 视频字节
```

## 三条硬纪律（每条都对应一个已知的坏形态）

1. **总超时必须有**（`VIDEO_POLL_TIMEOUT_SECONDS = 600`）：视频慢，但不能无限等 ——
   无界的等待就是没有刹车点（与 C5「禁无分页插槽的全量方法」同源）。
2. **轮询每轮过一次限流器**：与商品/笔记链路共用 `CrawlRateLimiter`，不在 provider 侧
   裸打（上游也有速率限制，打爆了是 429 不是更快）。
3. **失败不抛给笔记落库**：本模块只把失败**如实返回**（`success=False` + 可读原因），
   由 worker 决定「视频失败但笔记照落」—— 一个坏视频不废掉整篇正文（§13.4.2 同口径）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests

#: 轮询总超时（秒）。10 分钟：覆盖绝大多数文生视频任务，又不至于无限等。
VIDEO_POLL_TIMEOUT_SECONDS: int = 600
#: 轮询间隔（秒）。5s 是常见视频接口的合理节奏（更密无意义、更疏进度差）。
VIDEO_POLL_INTERVAL_SECONDS: int = 5
#: 默认时长（秒）与分辨率（竖版，贴小红书）。
DEFAULT_VIDEO_DURATION: int = 5
DEFAULT_VIDEO_SIZE: str = "720x1280"
#: 单次请求超时（提交 / 单轮轮询）。
REQUEST_TIMEOUT_SECONDS: int = 60

#: 终态判定（大小写不敏感）。上游常见两种命名：succeeded/completed、failed/error。
_SUCCESS_STATES = {"succeeded", "success", "completed", "done"}
_FAILURE_STATES = {"failed", "failure", "error", "cancelled", "canceled"}


class VideoGenerationError(RuntimeError):
    """视频生成失败（带可读原因，供 worker 记进日志与队列项）。"""


def _load_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - 上游可能返回非 JSON（网关 HTML 错误页）
        return {}


def _status_of(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "state"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _url_of(payload: Any) -> str:
    """从轮询响应里取视频 URL（兼容几种常见嵌套）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("url", "video_url", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # 有些实现把结果放在 data / output 里
    for container in ("data", "result", "output"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            found = _url_of(nested)
            if found:
                return found
        if isinstance(nested, list) and nested:
            found = _url_of(nested[0])
            if found:
                return found
    return ""


class OpenAICompatibleVideoClient:
    """OpenAI 兼容风格的视频生成客户端（提交 → 轮询 → 拿 URL）。

    ⚠️ **本类不下载**：只回到 URL 就交棒（下载与落盘在 worker 里，与图片路径共用
    同一套 `storage/media/` 落盘纪律）。
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        on_poll: Optional[Callable[[], bool]] = None,
    ) -> None:
        """`sleep` / `on_poll` 注入点（测试用，避免真等 10 分钟）。

        `on_poll()` 每轮调一次，返回 `False` ⇒ **放弃等待**（worker 用它在任务被取消 /
        停止位被置时立刻退出，而不是等满 600 秒）。默认 `None` = 永不放弃。
        """
        self._sleep = sleep
        self._on_poll = on_poll

    def _validate(self, *, model_config: Any, api_key: str) -> None:
        if not getattr(model_config, "base_url", ""):
            raise ValueError("Video model base_url is required")
        if not getattr(model_config, "model_name", ""):
            raise ValueError("Video model_name is required")
        if not api_key:
            raise ValueError("Video model api_key is required")

    def generate_video(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        reference_image: str = "",
        duration: int = DEFAULT_VIDEO_DURATION,
        size: str = DEFAULT_VIDEO_SIZE,
    ) -> dict[str, Any]:
        """提交 → 轮询 → 返回 `{"url": ..., "raw": ...}`。

        失败抛 `VideoGenerationError`（带可读原因）；超时同样抛（**不返回空 URL 冒充成功**）。
        """
        self._validate(model_config=model_config, api_key=api_key)
        base = str(model_config.base_url).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body: dict[str, Any] = {
            "model": model_config.model_name,
            "prompt": prompt,
        }
        # 图生视频：带参考图（上游字段名以 `image` 为准，与图片接口一致）。
        #: ⚠️ **必须过 `_resolve_image_ref`**（2026-10-16 用户：*「参考图支持选本地或商品原图」*）
        #: —— 用户选的素材图引用是**带鉴权的本地端点**（`/api/xhs/materials/files/{id}/file`），
        #: 上游拉不到；该静态方法会**读盘转 base64 data URL** 就地内联。
        #: ⚠️ 与图片链路**同一实现**（不重抄一份 —— 两处漂移时只有一处会修好，DEF-044）。
        if reference_image:
            from backend.app.services.ai_service import OpenAICompatibleImageClient

            body["image"] = OpenAICompatibleImageClient._resolve_image_ref(reference_image)
        if duration:
            body["duration"] = int(duration)
        if size:
            body["size"] = size

        try:
            submit = requests.post(
                f"{base}/videos/generations",
                headers=headers,
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            submit.raise_for_status()
        except requests.HTTPError as exc:
            detail = ""
            try:
                payload = _load_json(exc.response) if exc.response is not None else {}
                if isinstance(payload, dict):
                    err = payload.get("error")
                    detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
            except Exception:  # noqa: BLE001
                pass
            raise VideoGenerationError(f"视频提交失败: {detail or exc}") from exc
        except requests.RequestException as exc:
            raise VideoGenerationError(f"视频提交网络异常: {exc}") from exc

        submitted = _load_json(submit)
        task_id = ""
        if isinstance(submitted, dict):
            for key in ("id", "task_id", "request_id"):
                value = submitted.get(key)
                if isinstance(value, str) and value.strip():
                    task_id = value.strip()
                    break
        if not task_id:
            raise VideoGenerationError("视频提交响应缺少任务 id（上游未按约定返回）")

        deadline = time.monotonic() + VIDEO_POLL_TIMEOUT_SECONDS
        while True:
            if self._on_poll is not None and not self._on_poll():
                raise VideoGenerationError("视频生成在等待中被取消")
            if time.monotonic() >= deadline:
                raise VideoGenerationError(
                    f"视频生成超时（等待超过 {VIDEO_POLL_TIMEOUT_SECONDS} 秒）"
                )
            try:
                poll = requests.get(
                    f"{base}/videos/generations/{task_id}",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                poll.raise_for_status()
            except requests.HTTPError as exc:
                detail = ""
                try:
                    payload = _load_json(exc.response) if exc.response is not None else {}
                    if isinstance(payload, dict):
                        err = payload.get("error")
                        detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
                except Exception:  # noqa: BLE001
                    pass
                raise VideoGenerationError(f"视频轮询失败: {detail or exc}") from exc
            except requests.RequestException as exc:
                raise VideoGenerationError(f"视频轮询网络异常: {exc}") from exc

            payload = _load_json(poll)
            status = _status_of(payload)
            if status in _SUCCESS_STATES:
                url = _url_of(payload)
                if not url:
                    raise VideoGenerationError("视频已完成但响应里没有 URL")
                return {"url": url, "raw": payload, "task_id": task_id}
            if status in _FAILURE_STATES:
                message = ""
                if isinstance(payload, dict):
                    err = payload.get("error")
                    message = err.get("message", "") if isinstance(err, dict) else str(err or "")
                raise VideoGenerationError(f"视频生成失败: {message or status}")
            # queued / running / 未知状态 ⇒ 继续等（未知状态不当失败：上游可能新增中间态）
            self._sleep(VIDEO_POLL_INTERVAL_SECONDS)
