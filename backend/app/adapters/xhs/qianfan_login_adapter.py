from __future__ import annotations

from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env

#: 千帆（ark）在客户单点登录里的 **service 地址**。
#:
#: 2026-09-21 真机抓包（隔离 profile + CDP，用户本人扫码）验证：在
#: `https://customer.xiaohongshu.com/login?service=<本常量>` 扫码成功后，
#: 浏览器拿到 `access-token-ark.xiaohongshu.com` / `x-user-id-ark.xiaohongshu.com` /
#: `beaker.session.id` / `customer-sso-sid`，并跳转
#: `https://ark.xiaohongshu.com/app-system/home`。
#:
#: 与创作者扫码**同一套 CAS 流程**（`/api/cas/customer/web/qr-code`），
#: 差别只有 service 指向 —— 所以适配器直接复用创作者登录 SDK，
#: 不复制任何签名逻辑（同一语义不留两份实现）。
ARK_SERVICE_URL = "https://ark.xiaohongshu.com"

#: 邮箱密码登录的换票导航地址（2026-09-23 真机抓包：`GET /?ticket=<ST>&from=ark-login`，
#: 该导航的 Set-Cookie 与随后的 ssologin 一起种下 ark 域令牌）。
ARK_TICKET_LANDING = f"{ARK_SERVICE_URL}/?from=ark-login"


class XhsQianfanLoginAdapter:
    """千帆扫码登录（用户 2026-09-21 要求「千帆也能扫码登录」）。

    背景：拆分前三平台令牌后，千帆（ark 平台）只能靠粘贴浏览器 Cookie 获得
    （`ssologin` 那一步带 `X-s`/`X-t` 签名，服务端复现不可交付）。本次真机
    抓包证明**平台自己的登录页就支持扫码拿千帆令牌**，且与创作者扫码共用
    同一套 customer 单点登录接口 —— 于是扫码成为千帆续期的正规入口。

    只读边界：扫码由**用户本人**在 App 里完成；适配器只生成二维码、轮询状态、
    校验令牌（`get_seller_info` 是只读 GET）。不代用户确认、不碰写接口。
    """

    def _api(self) -> Any:
        from apis.xhs_creator_login_apis import XHSCreatorLoginApi

        return XHSCreatorLoginApi(service_url=ARK_SERVICE_URL)

    def create_qrcode(self) -> dict[str, Any]:
        with direct_xhs_request_env():
            api = self._api()
            cookies = api.generate_init_cookies()
            success, message, payload = api.generate_qrcode(cookies)
        if not success or not payload:
            raise RuntimeError(message)
        return {
            "cookies": payload["cookies"],
            "qr_id": payload["qr_id"],
            "qr_url": payload["qr_url"],
        }

    def check_qrcode_status(self, qr_id: str, cookies: dict[str, Any]) -> dict[str, Any]:
        """轮询扫码状态；**确认后补做 ark 域换票**（2026-09-26 修复）。

        ## 为什么必须换票（用户实测：「扫码成功，但读取千帆店铺信息失败」）

        `check_qrcode_status()` 只种下 **customer 域**的会话 —— 它等价于浏览器在
        扫码页轮询的那几次请求。浏览器在确认之后还会**跳转**
        `ark/?ticket=…&from=ark-login` 并调 `ssologin`，千帆域令牌
        （`access-token-ark.*`）是那一跳种下的。

        只做轮询不做换票，手里的 jar 打千帆一律 `-13000 缺失登录凭证` ——
        于是「登录成功」与「读不到店铺」同时成立，用户看到的是
        「登录成功，但读取千帆店铺信息失败……本次没有保存任何令牌」。
        这与邮箱密码登录**本来就是同一条链**（同一套 CAS），所以换票逻辑
        抽成 `_exchange_ticket_for_ark_session()` 供两条路复用。
        """
        with direct_xhs_request_env():
            api = self._api()
            # 用 `query_qrcode_status`（而不是 `check_qrcode_status`）：前者保留
            # `ticket`，那正是换票必需的输入；后者把它丢掉了。
            detail = api.query_qrcode_status(qr_id, cookies)
            status = self._status_from_detail(detail)
            if status == "confirmed":
                ticket = str(detail.get("ticket") or "")
                if ticket:
                    self._exchange_ticket_for_ark_session(api, ticket)
            # 返回**整个 profile**：换票往同一个 CookieProfile 里种 ark 域令牌，
            # `detail['cookies']` 是换票前那一版的快照，用它会把新令牌丢掉。
            cookies_out = dict(api.profile.cookie_map)
        return {"status": status, "cookies": cookies_out}

    @staticmethod
    def _status_from_detail(detail: dict[str, Any]) -> str:
        """CAS 轮询结果 → 我们的四态（`pending` / `scanned` / `confirmed` / `expired`）。"""
        message = str(detail.get("message") or "")
        status = "confirmed" if detail.get("success") else "pending"
        if "过期" in message or "expired" in message.lower():
            status = "expired"
        if "确认" in message or "confirm" in message.lower():
            status = "scanned"
        return status

    # ------------------------------------------------------------------ #
    # 邮箱密码登录（2026-09-23，设计 13 §18.15.3）
    # ------------------------------------------------------------------ #

    def login_by_email_pwd(self, email: str, pwd: str) -> dict[str, Any]:
        """千帆邮箱密码登录：**一次调用跑完浏览器里的整条链**，返回 ark 令牌。

        真机抓包（2026-09-23，真实 Chrome + 隔离 profile + CDP，用户本人登录）
        + 服务端全流程实证（本机复现，每一步 200，末尾只读校验店铺信息通过）：

        1. `POST customer/api/cas/customer/web/service-ticket`
           body `{service, email, pwd, source:"", type:"emailPwd"}`（0201/nop 签名档）
           ⇒ 响应**直接带回服务票据 ST**（`data.ticket`，`type:"st"`）；
        2. `POST ark/api/edith/seller/ticket/route` `{system:"ark", url, ticket:ST}`（签名）；
        3. `GET ark/?ticket=ST&from=ark-login`（导航，无签名）；
        4. `POST ark/api/edith/open/ssologin` `{system, ticket:ST}`（签名）
           ⇒ `seller_infos[]`（seller_id / userId）；
        5. `POST ark/api/edith/open/seller/login` `{seller_id, user_id, is_vendor:false}`（签名）。

        ⚠️ 第 2–5 步已抽成 `_exchange_ticket_for_ark_session()` 并被**扫码路径复用**
        （2026-09-26）—— 两条链拿到的是同一种 ST，换票动作逐字相同。

        ⚠️ **§18.14.3「服务端复现 ssologin 不可交付」的边界被本条推翻**：那条针对
        「从创作者 jar 自动派生」（用户无感知）；本条是**用户本人提交邮箱密码**、
        平台自己发令牌，与扫码同性质。签名机制复用创作者登录 SDK 的 `_signed()`
        （host 无关：签的是 api path + body + cookie），**不新增签名维护面**。

        ⚠️ **邮箱与密码不落库**：它们只是本方法换令牌的输入，方法出口只有 Cookie。
        `LoginSession` 行、响应体、日志都不允许出现它们（调用方同理）。

        只读边界：除「用户本人登录」外全是只读 GET / 既有的只读查询 POST；
        不代替用户确认任何东西。
        """
        with direct_xhs_request_env():
            # 懒加载（与 _api() 同模式：SDK 导入有副作用时不进模块级）
            from apis.xhs_creator_login_apis import CREATOR_USER_AGENT, REQUEST_TIMEOUT

            api = self._api()
            # 1. 安全引导 + 邮箱密码登录（SDK 内部完成 0201/nop 签名）
            cookies = api.generate_init_cookies()
            success, message, result = api.login_by_email_pwd(email, pwd, cookies)
            if not success:
                raise RuntimeError(message or "邮箱密码登录失败")
            data = (result.get("res_json") or {}).get("data") or {}
            ticket = str(data.get("ticket") or "")
            if not ticket:
                raise RuntimeError("登录成功但未拿到服务票据（ST），请重试")
            # 2-5. 换票（与扫码路径**同一份实现**，见下）
            self._exchange_ticket_for_ark_session(api, ticket)
            return {"status": "confirmed", "cookies": api.profile.cookie_map}

    def _exchange_ticket_for_ark_session(self, api: Any, ticket: str) -> None:
        """CAS 服务票据（ST）→ **ark 域令牌**（浏览器跳转那一跳的等价物）。

        四步，缺任何一步千帆域令牌都种不下来：

        1. `POST /api/edith/seller/ticket/route` —— 把 ST 注册到 ark 系统；
        2. `GET ark/?ticket=…&from=ark-login` —— 导航换票（`Set-Cookie`）；
        3. `POST /api/edith/open/ssologin` —— ST → 店铺会话；
        4. `POST /api/edith/open/seller/login` —— 选定店铺（个人店单选，多店取主店）。

        ⚠️ **扫码与邮箱密码共用这一份**（2026-09-26）：两条路拿到的是同一种 ST，
        换票动作逐字相同。原先只有邮箱密码做了这四步，扫码只做轮询 —— 于是
        「账号密码登录正常、扫码登录异常」（用户实测）。同一语义不留两份实现：
        邮箱密码那条链现在也调用本函数，任何一侧的修正都会同时生效。
        """
        from apis.xhs_creator_login_apis import CREATOR_USER_AGENT, REQUEST_TIMEOUT

        landing_url = f"{ARK_SERVICE_URL}/?ticket={ticket}&from=ark-login"
        # 1. ticket/route：把 ST 注册到 ark 系统（浏览器在跳转前从登录页调用）
        self._ark_signed_post(
            api,
            "/api/edith/seller/ticket/route",
            {"system": "ark", "url": ARK_SERVICE_URL, "ticket": ticket},
            referer=f"{ARK_SERVICE_URL}/",
        )
        # 2. 导航换票：该请求的 Set-Cookie 与后续 ssologin 一起种下 ark 域令牌
        landing = api.http.get(
            landing_url,
            headers={
                "cookie": self._cookie_header(api, ARK_SERVICE_URL),
                "user-agent": CREATOR_USER_AGENT,
                "referer": f"{ARK_SERVICE_URL}/",
            },
            proxies=api.proxies,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        api._merge_response_cookies(landing)
        # 3. ssologin：ST → 店铺会话（拿 seller_id / userId）
        sso = self._ark_signed_post(
            api,
            "/api/edith/open/ssologin",
            {"system": ARK_TICKET_LANDING, "ticket": ticket},
            referer=landing_url,
        )
        seller = self._first_seller(sso)
        # 4. seller/login：选定店铺（个人店单选；多店时取主店）
        if seller.get("seller_id"):
            self._ark_signed_post(
                api,
                "/api/edith/open/seller/login",
                {
                    "seller_id": seller["seller_id"],
                    "user_id": seller.get("user_id", ""),
                    "is_vendor": False,
                },
                referer=landing_url,
            )

    def _ark_signed_post(self, api: Any, path: str, body: dict, *, referer: str) -> dict:
        """带 `X-s`/`X-t`/`X-S-Common` 签名的 ark POST（签名逻辑全部在 SDK 内）。"""
        from apis.xhs_creator_login_apis import REQUEST_TIMEOUT

        headers, _cookie_map, signed_body = api._signed(
            path,
            body,
            "POST",
            origin=ARK_SERVICE_URL,
            referer=referer,
            tier="0101",
            mns_profile="login_ready",
            b1_profile="login",
        )
        headers["content-type"] = "application/json"
        response = api.http.post(
            ARK_SERVICE_URL + path,
            headers=headers,
            data=signed_body.encode("utf-8"),
            proxies=api.proxies,
            timeout=REQUEST_TIMEOUT,
        )
        api._merge_response_cookies(response)
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _cookie_header(api: Any, url: str) -> str:
        cookie_map = api._cookies_for_url(url, api.profile.cookie_map)
        return "; ".join(f"{key}={value}" for key, value in cookie_map.items())

    @staticmethod
    def _first_seller(sso: dict) -> dict:
        infos = (sso.get("data") or {}).get("seller_infos") or []
        if not infos:
            return {}
        first = infos[0]
        return {
            "seller_id": str(first.get("seller_id") or ""),
            "user_id": str(first.get("userId") or ""),
        }

    def get_shop_info(self, cookies: dict[str, Any]) -> dict[str, str]:
        """用扫码得到的 jar 打一次**只读**的千帆店铺信息，返回身份锚点。

        千帆侧没有「小红书用户」概念，能对账的只有**店铺**（`shop_user_id`）。
        续期时用它和目标账号已有的绑定对账 —— 对不上就拒绝写入（§18.8 ③A 同型：
        宁可失败，不可把别人店的令牌写进这个账号）。
        """
        from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter

        header = "; ".join(f"{key}={value}" for key, value in (cookies or {}).items())
        adapter = XhsQianfanApiAdapter(header)
        success, message, payload = adapter.get_seller_info()
        if not success:
            raise RuntimeError(message or "千帆登录态校验失败")
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        return {
            "shop_user_id": str(data.get("user_id") or data.get("seller_id") or ""),
            "shop_name": str(data.get("seller_name") or data.get("user_name") or ""),
        }
