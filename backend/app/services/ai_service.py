from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import requests

from backend.app.services.llm_gateway_usage_service import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    extract_usage_tokens,
    record_usage_for_model,
)

logger = logging.getLogger(__name__)


class ModelContext(Protocol):
    """调用上游所需的**标量载体**（v2.0，设计 17 §13.2.4 的签名解耦）。

    ⚠️ 本协议存在的唯一理由：`_complete()` / `generate_image()` / `describe_image()`
    原本收 `ModelConfig`（ORM 行），v2.0 起 `model_configs` 表退场，
    调用方改传 `llm_gateway_client.ResolvedModelContext`（纯 dataclass）。
    若服务层仍写死 `ModelConfig`，就把「表已 drop 的 ORM 映射」漏进调用链
    （运行期对不存在的表发 SQL）。本协议让汇聚点只依赖**它真正读的属性**，
    两种载体都能传，且静态检查抓得住少字段。

    只读四个标量：`base_url`（拼 endpoint）、`model_name`（请求体 `model`）、
    `user_id` + `provider_key`（台账记账按 provider_key 解析服务商，§4.3.1）。
    其它字段（`encrypted_api_key` / `is_default` / `name`…）汇聚点一概不碰 ——
    `api_key` 由调用方**解密后显式下传**，服务层不经手密文。
    """

    base_url: str
    model_name: str
    user_id: int
    provider_key: str


#: 任何具有这四个属性的对象都可传入（`ResolvedModelContext` 满足；
#: 一期 `ModelConfig` 也满足，但该表已 drop，新代码不得再构造它）。
ModelLike = ModelContext


class TextAiClient(Protocol):
    def rewrite_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        ...

    def generate_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        topic: str,
        reference: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> dict[str, str]:
        ...

    def generate_product_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        product: dict[str, Any],
        instruction: str = "",
        platform: str = "",
        function: str = "",
    ) -> dict[str, Any]:
        """商品笔记生成（M4 起步 / T-401）：商品专用 Prompt + JSON 解析。

        Prompt 与解析都放在 `services/product_note_service.py`（**唯一实现**）：
        生成、护栏、格式校验三件事共用同一份商品字段与同一套规则 ——
        分开写必然漂移（改了 Prompt 字段名而护栏还在按旧字段比对）。

        `platform` / `function`：台账记账维度（设计 17 §4.3.1），由调用点下传，
        默认空串（不报错，只让台账 token_name 落空）。
        """
        ...

    def generate_titles(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        count: int,
        platform: str = "",
        function: str = "",
    ) -> list[str]:
        ...

    def generate_tags(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        count: int,
        platform: str = "",
        function: str = "",
    ) -> list[str]:
        ...

    def polish_text(
        self,
        *,
        model_config: Any,
        api_key: str,
        text: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        ...


class ImageAiClient(Protocol):
    def generate_cover(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        size: str,
        style: str,
    ) -> dict[str, Any]:
        ...

    def generate_image(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        ...

    def describe_image(
        self,
        *,
        model_config: Any,
        api_key: str,
        image_url: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        ...


def _candidate_response_encodings(response: requests.Response) -> list[str]:
    encodings: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", response.apparent_encoding, response.encoding):
        normalized = (encoding or "").strip()
        if normalized and normalized.lower() not in {item.lower() for item in encodings}:
            encodings.append(normalized)
    return encodings


def _load_json_response(response: requests.Response) -> Any:
    raw = response.content
    last_error: Exception | None = None
    for encoding in _candidate_response_encodings(response):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc

    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        last_error = exc

    raise ValueError("AI response is not valid JSON") from last_error


def _elapsed_ms(started: float) -> int:
    """`time.perf_counter()` 起点 → 整数毫秒（台账 `latency_ms`，设计 17 §4.3.1）。

    向上取整到 ≥0：亚毫秒的调用记 0 而不是负值，前端求和/均值才有意义。
    """
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _brief_error(exc: BaseException) -> str:
    """把异常转成一行可读的台账 `error_message`（截断，不落栈、不落敏感信息）。

    台账只用来看「为什么失败」，不承担排障证据链 —— 完整栈在日志里。
    """
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:500]


class OpenAICompatibleTextClient:
    def _complete(
        self,
        *,
        model_config: Any,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        platform: str = "",
        function: str = "",
    ) -> str:
        """文本链路**唯一汇聚点**：所有文本能力（改写/生成/标题/标签/润色）都走这里。

        v1.7 台账记账挂在本方法（设计 17 §4.3.1）：一次上游调用 ⇒ 一行台账。
        ⚠️ 参数校验失败（缺 base_url/model_name/api_key）**不记账** —— 那一步
        还没发起上游调用，记了就凭空多一次调用量。
        """
        if not model_config.base_url:
            raise ValueError("Text model base_url is required")
        if not model_config.model_name:
            raise ValueError("Text model_name is required")
        if not api_key:
            raise ValueError("Text model api_key is required")

        endpoint = f"{model_config.base_url.rstrip('/')}/chat/completions"
        started = time.perf_counter()
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_config.model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                },
                #: ⚠️ **文本生成 60s 不够**（2026-10-17 真机实证：discovery 服务商
                #: 写一篇完整笔记 —— 标题 + 300 字正文 + 话题 —— 推理常超 60s，
                #: 报 `ReadTimeout ... read timeout=60`；而同一服务 `/v1/models`
                #: 2.3s 就应答 ⇒ **不是服务挂了，是 60s 这个闸太紧**）。
                #: 放宽到 300s：与图片链路 `timeout=180` 同一量级思路（生成类给足）。
                #: ⚠️ 不能无上限：worker 是单线程串行，挂死一次会堵住整个队列。
                timeout=TEXT_COMPLETION_TIMEOUT,
            )
            response.raise_for_status()
            payload = _load_json_response(response)
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("AI response missing choices[0].message.content") from exc
            if not isinstance(content, str) or not content.strip():
                raise ValueError("AI response content is empty")
        except Exception as exc:  # noqa: BLE001 —— 记账后原样抛回，不改业务语义
            self._record_usage_quietly(
                model_config=model_config,
                platform=platform,
                function=function,
                payload=None,
                latency_ms=_elapsed_ms(started),
                status=STATUS_FAILED,
                error_message=_brief_error(exc),
            )
            #: 超时单独翻译成**可读原因**（2026-10-17 真机：用户看到的是
            #: `ReadTimeout: HTTPSConnectionPool(host='...', port=443): Read timed out.
            #: (read timeout=60)` —— 原始异常栈对用户毫无信息量，不知道该改什么）。
            #: ⚠️ 只翻译**超时**这一种，其它异常原样抛出（不吞、不猜测）。
            if isinstance(exc, _REQUESTS_TIMEOUT_ERRORS):
                raise TimeoutError(
                    f"模型响应超时（{TEXT_COMPLETION_TIMEOUT} 秒内没返回）。"
                    "上游服务可能正忙或网络不稳，稍后重试即可；"
                    "若频繁出现，可在「模型配置」换一个响应更快的服务商。"
                ) from exc
            raise

        # HTTP 通了、内容也解析出来了 ⇒ success；token 用量以响应 usage 为准（缺则 0）
        prompt_tokens, completion_tokens = extract_usage_tokens(payload)
        self._record_usage_quietly(
            model_config=model_config,
            platform=platform,
            function=function,
            payload=payload,
            latency_ms=_elapsed_ms(started),
            status=STATUS_SUCCESS,
            error_message="",
            tokens=(prompt_tokens, completion_tokens),
        )
        return content.strip()

    @staticmethod
    def _record_usage_quietly(
        *,
        model_config: Any,
        platform: str,
        function: str,
        payload: Any,
        latency_ms: int,
        status: str,
        error_message: str,
        tokens: tuple[int, int] | None = None,
    ) -> None:
        """记账双保险：本方法**绝不抛异常**，也**不影响返回值**（DEF-048）。

        服务层 `record_usage_for_model()` 已经自己兜了异常，这里再包一层是为了
        连同「取 token / 组装参数」这些**调用侧**逻辑也圈进安全网 ——
        安全网不能条件性，也不能只围住一半（DEF-048/056）。
        """
        try:
            prompt_tokens, completion_tokens = tokens if tokens else extract_usage_tokens(payload)
            record_usage_for_model(
                model_config=model_config,
                platform=platform,
                function=function,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                status=status,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.warning("LLM 台账记账旁路异常（已忽略）", exc_info=True)

    def rewrite_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        return self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt="你是小红书内容运营编辑，负责在保留事实的前提下改写成自然、可发布的种草笔记。",
            user_prompt=(
                f"改写要求：{instruction or '提升表达、增强小红书语感'}\n\n"
                f"标题：{title}\n\n正文：\n{body}"
            ),
            platform=platform,
            function=function,
        )

    def generate_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        topic: str,
        reference: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> dict[str, str]:
        content = self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt="你是小红书内容策划，输出可发布的标题和正文。",
            user_prompt=(
                "请生成一篇小红书笔记，格式必须是：\n标题：...\n正文：...\n\n"
                f"选题：{topic}\n参考材料：{reference or '无'}\n要求：{instruction or '自然、有信息密度'}"
            ),
            platform=platform,
            function=function,
        )
        title = topic
        body = content
        for line in content.splitlines():
            if line.startswith("标题："):
                title = line.replace("标题：", "", 1).strip() or title
                break
        if "正文：" in content:
            body = content.split("正文：", 1)[1].strip()
        return {"title": title, "body": body}

    def generate_product_note(
        self,
        *,
        model_config: Any,
        api_key: str,
        product: dict[str, Any],
        instruction: str = "",
        platform: str = "",
        function: str = "",
    ) -> dict[str, Any]:
        """商品笔记生成（M4 起步 / T-401）：商品专用 Prompt + JSON 解析。

        Prompt 与解析都在 `services/product_note_service.py`（**唯一实现**）：
        生成、护栏、格式校验三件事共用同一份商品字段与同一套规则 ——
        分开写必然漂移（例如 Prompt 换了字段名，护栏还在按旧字段比对）。
        """
        from backend.app.services.product_note_service import (
            PRODUCT_NOTE_SYSTEM_PROMPT,
            build_product_note_prompt,
            parse_generated_payload,
        )

        content = self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt=PRODUCT_NOTE_SYSTEM_PROMPT,
            user_prompt=build_product_note_prompt(product, instruction=instruction),
            temperature=0.6,
            platform=platform,
            function=function,
        )
        return parse_generated_payload(content)

    def generate_titles(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        count: int,
        platform: str = "",
        function: str = "",
    ) -> list[str]:
        content = self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt="你是小红书标题优化专家。",
            user_prompt=f"请给出 {count} 个小红书标题，每行一个。\n原标题：{title}\n正文：{body}",
            platform=platform,
            function=function,
        )
        return [line.strip(" -0123456789.、") for line in content.splitlines() if line.strip()][:count]

    def generate_tags(
        self,
        *,
        model_config: Any,
        api_key: str,
        title: str,
        body: str,
        count: int,
        platform: str = "",
        function: str = "",
    ) -> list[str]:
        content = self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt="你是小红书 SEO 和话题标签专家。",
            user_prompt=f"请给出 {count} 个小红书话题标签，只输出标签，用逗号或换行分隔。\n标题：{title}\n正文：{body}",
            platform=platform,
            function=function,
        )
        separators = content.replace("，", ",").replace("\n", ",").split(",")
        return [item.strip().lstrip("#") for item in separators if item.strip()][:count]

    def polish_text(
        self,
        *,
        model_config: Any,
        api_key: str,
        text: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        return self._complete(
            model_config=model_config,
            api_key=api_key,
            system_prompt="你是小红书正文润色编辑。",
            user_prompt=f"润色要求：{instruction or '更自然、清晰、有种草感'}\n\n原文：\n{text}",
            platform=platform,
            function=function,
        )


#: 文本补全超时（秒）。600 秒级不设，是因为 worker 串行 —— 挂死会堵住整个队列；
#: 300 秒足够「标题 + 300 字正文 + 话题」这类长输出的推理（实测慢模型 60s 不够）。
TEXT_COMPLETION_TIMEOUT = 300

#: `requests` 的超时类（ReadTimeout / ConnectTimeout / 连接池超时都继承 `Timeout`）。
#: ⚠️ 惰性构造：`requests` 是硬依赖，但这里只为了让 `isinstance` 判定可读。
import requests as _requests  # noqa: E402

_REQUESTS_TIMEOUT_ERRORS = (_requests.exceptions.Timeout,)


class OpenAICompatibleImageClient:
    def _validate(self, *, model_config: Any, api_key: str) -> None:
        if not model_config.base_url:
            raise ValueError("Image model base_url is required")
        if not model_config.model_name:
            raise ValueError("Image model_name is required")
        if not api_key:
            raise ValueError("Image model api_key is required")

    def generate_cover(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        size: str,
        style: str,
    ) -> dict[str, Any]:
        return self.generate_image(
            model_config=model_config, api_key=api_key, prompt=f"{prompt}\nStyle: {style or 'clean XHS cover'}",
        )

    def generate_image(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        self._validate(model_config=model_config, api_key=api_key)
        endpoint = f"{model_config.base_url.rstrip('/')}/images/generations"
        body: dict[str, Any] = {
            "model": model_config.model_name,
            "prompt": prompt,
            "response_format": "url",
        }
        if reference_images:
            resolved = [self._resolve_image_ref(url) for url in reference_images]
            if len(resolved) == 1:
                body["image"] = resolved[0]
            else:
                body["image"] = resolved
                body["sequential_image_generation"] = "disabled"
            body["watermark"] = False
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = ""
            code = ""
            try:
                error_payload = _load_json_response(exc.response) if exc.response else {}
                if isinstance(error_payload, dict):
                    err = error_payload.get("error")
                    if isinstance(err, dict):
                        detail = err.get("message", "")
                        code = err.get("code", "")
            except Exception:
                pass
            # ⚠️ **把上游的机器话翻成人话**（真机实测：千问额度耗尽时上游返回 403 +
            # `Free quota exhausted…`，但 SDK 的 HTTPError 文案是
            # `403 Client Error: Forbidden for url: …`，`detail` 一旦取不到
            # 用户就只能看到一串 URL —— 既不知道原因、也不知道该做什么）。
            # 只为**已知的、用户能自救的**情况给译文；其余原样透传（不臆造原因）。
            if detail and "quota" in detail.lower():
                detail = (
                    f"服务商额度不足（{code or 'insufficient_quota'}）："
                    f"{detail} —— 请到服务商控制台充值，或关闭「仅用免费额度」模式。"
                )
            elif not detail and exc.response is not None and exc.response.status_code in (401, 403):
                detail = (
                    f"服务商拒绝了这次请求（HTTP {exc.response.status_code}）："
                    "常见原因是额度耗尽 / 令牌无该模型权限 / 令牌已失效 —— "
                    "请到「大模型网关 → 服务商」点「测试」确认，并核对账户余额。"
                )
            raise ValueError(f"图片生成失败: {detail or exc}") from exc
        payload = _load_json_response(response)
        try:
            item = payload["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Image response missing data[0]") from exc
        image_ref = item.get("url") or item.get("b64_json")
        if not isinstance(image_ref, str) or not image_ref:
            raise ValueError("Image response missing url or b64_json")
        return {"url": image_ref, "raw": payload}

    def generate_images(
        self,
        *,
        model_config: Any,
        api_key: str,
        prompt: str,
        reference_images: list[str] | None = None,
        count: int = 1,
    ) -> list[dict[str, Any]]:
        """**一次请求要 N 张**（OpenAI 兼容的 `n` 参数）—— v2.29。

        用户口径（2026-10-03 原话）：
        > 「应该把商品信息参考图提示词一起全部打包给大模型进行出图，**为何是串行**」

        ## 为什么现在才敢用 `n`

        旧实现**逐张发请求**（`for index in range(count)`），代码注释写的理由是
        「不是所有服务商都认 `n` 参数」。**真机实测（2026-10-03）**：当前服务商
        （千问 compatible-mode）**认** —— `n=3` 一次返回 3 张、耗时 **57.8 秒**；
        而逐张串行需要 3 × ~40s ≈ 120 秒。**请求数从 3 降到 1**，既快又省限流额度。

        ⚠️ **回落**：`n > 1` 但响应只回 1 张（服务商忽略 `n`）⇒ 调用方按缺张补齐
        （见 `qianfan.generate_detail_image` 的补齐循环）。这里**只如实返回拿到几张**，
        不伪造、不重试（重试属于调用方的策略）。

        返回：`[{"url": ..., "raw": ...}, ...]`（至少 1 项；0 项直接抛错）。
        """
        self._validate(model_config=model_config, api_key=api_key)
        if count <= 1:
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
            ]

        endpoint = f"{model_config.base_url.rstrip('/')}/images/generations"
        body: dict[str, Any] = {
            "model": model_config.model_name,
            "prompt": prompt,
            "n": count,
            "response_format": "url",
        }
        if reference_images:
            resolved = [self._resolve_image_ref(url) for url in reference_images]
            body["image"] = resolved[0] if len(resolved) == 1 else resolved
            if len(resolved) > 1:
                body["sequential_image_generation"] = "disabled"
            body["watermark"] = False
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=300,  # N 张同批 ⇒ 上限比单张宽松（实测 n=3 约 58s）
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = ""
            code = ""
            try:
                error_payload = _load_json_response(exc.response) if exc.response else {}
                if isinstance(error_payload, dict):
                    err = error_payload.get("error")
                    if isinstance(err, dict):
                        detail = err.get("message", "")
                        code = err.get("code", "")
            except Exception:
                pass
            if detail and "quota" in detail.lower():
                detail = (
                    f"服务商额度不足（{code or 'insufficient_quota'}）："
                    f"{detail} —— 请到服务商控制台充值，或关闭「仅用免费额度」模式。"
                )
            elif not detail and exc.response is not None and exc.response.status_code in (401, 403):
                detail = (
                    f"服务商拒绝了这次请求（HTTP {exc.response.status_code}）："
                    "常见原因是额度耗尽 / 令牌无该模型权限 / 令牌已失效 —— "
                    "请到「大模型网关 → 服务商」点「测试」确认，并核对账户余额。"
                )
            raise ValueError(f"图片生成失败: {detail or exc}") from exc

        payload = _load_json_response(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ValueError("Image response missing data[]")
        results: list[dict[str, Any]] = []
        for item in data:
            ref = (item or {}).get("url") or (item or {}).get("b64_json")
            if isinstance(ref, str) and ref:
                results.append({"url": ref, "raw": payload})
        if not results:
            raise ValueError("Image response missing url or b64_json")
        return results

    @staticmethod
    def _resolve_image_ref(url: str) -> str:
        """参考图 → 上游能取的形态。

        ⚠️ **非 http(s) 的引用可能是本地文件路径**（用户口径 2026-10-16：
        *「参考图支持选本地或商品原图，本地素材均需要支持预览」*）——
        第三方模型拉不到我们带鉴权的端点，也拉不到磁盘路径 ⇒ 这类引用**读盘转
        base64 data URL**（就地内联，模型直接吃）。
        判据：**能读到的本地文件就内联**；读不到就原样返回（让上游自己报错，
        不在这一层假装成功）。
        """
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/api/files/media/"):
            import base64
            from pathlib import Path
            from backend.app.core.config import get_settings
            file_name = url.split("/")[-1]
            local = Path(get_settings().storage_dir) / "media" / file_name
            if local.is_file():
                raw = local.read_bytes()
                ext = local.suffix.lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
                return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        if url.startswith("/api/xhs/materials/files/"):
            #: 素材库引用（`/api/xhs/materials/files/{id}/file`）—— 同上，读盘内联。
            #: ⚠️ 走 `material_files.rel_path` 定位（**不拼 storage_dir/media**：
            #: 素材有自己的根目录，两者不同；拼错会静默取不到图）。
            import base64
            from pathlib import Path
            from backend.app.core.config import get_settings
            from backend.app.models.material import MaterialFile
            from backend.app.core.database import SessionLocal

            parts = url.split("/")
            try:
                file_id = int(parts[parts.index("files") + 1])
            except (ValueError, IndexError):
                return url
            from backend.app.models.material import MaterialFolder

            with SessionLocal() as db:
                item = db.get(MaterialFile, file_id)
                rel = str(getattr(item, "rel_path", "") or "") if item else ""
                #: ⚠️ **基准目录是 `material_folders.path`**（用户登记的那个文件夹），
                #: 不是 `storage_dir` —— 素材库支持任意本地目录（`/d/素材/xxx`）。
                #: 拼成 `storage_dir/{rel}` 会找不到文件、静默回落成「原样返回 URL」，
                #: 上游再拉那个带鉴权的端点必然失败（=「选了素材但生成不出来」）。
                #: 判据与 `api/platforms/xhs/materials.py` 一致：`Path(folder.path) / rel_path`。
                folder_path = ""
                if item is not None:
                    folder = db.get(MaterialFolder, getattr(item, "folder_id", None))
                    folder_path = str(getattr(folder, "path", "") or "") if folder else ""
            if rel and folder_path:
                local = Path(folder_path) / rel
                if local.is_file():
                    raw = local.read_bytes()
                    ext = local.suffix.lower().lstrip(".")
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4"}.get(ext, "image/png")
                    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        return url

    def describe_image(
        self,
        *,
        model_config: Any,
        api_key: str,
        image_url: str,
        instruction: str,
        platform: str = "",
        function: str = "",
    ) -> str:
        """图片理解链路（v1.7 记账的第二个汇聚点，设计 17 §4.3.1）。

        ⚠️ `generate_cover` / `generate_image` **v1.7 暂不记账** —— 它们的
        `/images/generations` 响应没有 `usage` 字段，token 用量无从取得；
        要记账得先有计价口径，不在此次范围内（不造假，见 §5 硬约束 5）。
        """
        self._validate(model_config=model_config, api_key=api_key)
        endpoint = f"{model_config.base_url.rstrip('/')}/chat/completions"
        started = time.perf_counter()
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_config.model_name,
                    "messages": [
                        {"role": "system", "content": "你是小红书图片分析助手。"},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction or "描述这张图片适合的小红书卖点。"},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        },
                    ],
                },
                timeout=120,
            )
            response.raise_for_status()
            payload = _load_json_response(response)
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("AI response missing choices[0].message.content") from exc
            if not isinstance(content, str) or not content.strip():
                raise ValueError("AI image description is empty")
        except Exception as exc:  # noqa: BLE001
            OpenAICompatibleTextClient._record_usage_quietly(
                model_config=model_config,
                platform=platform,
                function=function,
                payload=None,
                latency_ms=_elapsed_ms(started),
                status=STATUS_FAILED,
                error_message=_brief_error(exc),
            )
            raise

        OpenAICompatibleTextClient._record_usage_quietly(
            model_config=model_config,
            platform=platform,
            function=function,
            payload=payload,
            latency_ms=_elapsed_ms(started),
            status=STATUS_SUCCESS,
            error_message="",
        )
        return content.strip()

    def chat(
        self,
        *,
        model_config: Any,
        api_key: str,
        instruction: str,
        image_urls: list[str] | None = None,
        platform: str = "",
        function: str = "",
    ) -> str:
        """通用对话（可带图）—— **详情图提示词整理**用（设计 17 §13.3.7，v2.19）。

        与 `describe_image` 的区别：那个是「理解一张图」，本方法接受**多张**参考图 +
        一段长说明（商品名/参数/要求），产出自由文本（这里是出图提示词）。

        ⚠️ 走 `/chat/completions`（**不是** `/images/generations`）⇒ 需要**文本/多态**
        模型；调用方（`detail_image_prompt.refine_prompt`）负责「没配就回落」，
        本方法只管发请求与记账（v1.7 台账口径同 `describe_image`）。
        """
        self._validate(model_config=model_config, api_key=api_key)
        endpoint = f"{model_config.base_url.rstrip('/')}/chat/completions"
        content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for url in image_urls or []:
            ref = str(url or "").strip()
            if ref:
                content.append({"type": "image_url", "image_url": {"url": ref}})
        started = time.perf_counter()
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_config.model_name,
                    "messages": [
                        {"role": "system", "content": "你是电商视觉设计助手。"},
                        {"role": "user", "content": content},
                    ],
                },
                timeout=120,
            )
            response.raise_for_status()
            payload = _load_json_response(response)
            try:
                answer = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("AI response missing choices[0].message.content") from exc
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("AI chat response is empty")
        except Exception as exc:  # noqa: BLE001
            self._record_usage_quietly(
                model_config=model_config,
                platform=platform,
                function=function,
                payload=None,
                latency_ms=_elapsed_ms(started),
                status=STATUS_FAILED,
                error_message=_brief_error(exc),
            )
            raise

        self._record_usage_quietly(
            model_config=model_config,
            platform=platform,
            function=function,
            payload=payload,
            latency_ms=_elapsed_ms(started),
            status=STATUS_SUCCESS,
            error_message="",
        )
        return answer.strip()
