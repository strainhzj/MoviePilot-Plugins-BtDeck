# -*- coding: utf-8 -*-
"""BtDeck REST 客户端（BtDeckBridge 插件）。

认证完全复用 BtDeck 现有用户令牌体系（不新建认证面）：

- 登录 ``POST /api/v1/auth/login`` → 访问令牌（60min）+ 刷新令牌（7 天，
  使用即轮换）；信封 ``data`` 为单元素数组；
- 401 时先用刷新令牌轮换一次，仍失败则回退用户名密码重登录；
- 令牌经注入的 ``token_store`` 持久化（宿主插件数据目录），进程内保存；
- 日志与异常文本**永不携带令牌/密码**（脱敏由本模块保证，调用方无感）。

本模块只依赖 httpx，可脱离 MoviePilot 宿主单测（transport 注入）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Protocol

import httpx


class BtDeckApiError(Exception):
    """BtDeck 接口错误（信封或 HTTP 层），message 面向用户、可安全展示。"""

    def __init__(self, message: str, http_status: int = 0, code: str = ""):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


class BtDeckAuthError(BtDeckApiError):
    """认证链失败（密码错误/令牌失效且无法续期/账号被禁）。"""


class TokenStore(Protocol):
    """令牌持久化边界（宿主注入；实现自行决定存哪）。"""

    def load(self) -> Optional[Dict[str, Any]]:
        ...

    def save(self, payload: Dict[str, Any]) -> None:
        ...


class _NullTokenStore:
    """默认空实现：令牌只留进程内（测试/无持久化场景）。"""

    def load(self) -> Optional[Dict[str, Any]]:
        return None

    def save(self, payload: Dict[str, Any]) -> None:
        return None


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    return value[:3] + "***" if len(value) > 3 else "***"


class BtDeckClient:
    """BtDeck /api/v1 集成面客户端（线程安全由调用方保证：同步任务单线程串行）。"""

    PROTOCOL_VERSION = 1

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        token_store: Optional[TokenStore] = None,
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.token_store: TokenStore = token_store if token_store is not None else _NullTokenStore()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._access_expire_at: float = 0.0
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._log = logger if logger is not None else (lambda message: None)
        self._load_tokens()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ 认证

    def ensure_authenticated(self) -> None:
        """确保持有可用访问令牌：内存有效 → 刷新 → 登录。"""
        if self._access_token and time.time() < self._access_expire_at - 60:
            return
        if self._refresh_token:
            try:
                self._refresh()
                return
            except BtDeckApiError as exc:
                self._log(f"刷新令牌不可用，回退登录（{exc.message}）")
        self._login()

    def _login(self) -> None:
        if not self.username or not self.password:
            raise BtDeckAuthError("未配置 BtDeck 集成账号的用户名或密码")
        try:
            payload = self._request_json(
                "POST",
                "/api/v1/auth/login",
                json_body={"username": self.username, "password": self.password},
                authenticated=False,
            )
        except BtDeckApiError as exc:
            # 登录失败语义化为认证错误（密码错/限流/账号禁用等，message 原样透传）
            raise BtDeckAuthError(exc.message, exc.http_status, exc.code) from exc
        token = self._first_data_item(payload, "登录失败：BtDeck 返回无令牌数据")
        self._apply_tokens(token)

    def _refresh(self) -> None:
        refresh_token = self._refresh_token or ""
        try:
            payload = self._request_json(
                "POST",
                "/api/v1/auth/refresh",
                json_body={"refresh_token": refresh_token},
                authenticated=False,
            )
        except BtDeckApiError as exc:
            raise BtDeckAuthError(exc.message, exc.http_status, exc.code) from exc
        token = self._first_data_item(payload, "刷新令牌失败")
        self._apply_tokens(token)

    def _apply_tokens(self, token: Dict[str, Any]) -> None:
        access = token.get("access_token")
        refresh = token.get("refresh_token")
        if not access or not refresh:
            raise BtDeckAuthError("BtDeck 返回的令牌数据不完整")
        self._access_token = str(access)
        # 使用即轮换：新刷新令牌立即替换旧值
        self._refresh_token = str(refresh)
        self._access_expire_at = time.time() + 55 * 60
        self.token_store.save({"access_token": self._access_token, "refresh_token": self._refresh_token})
        self._log(f"已获取 BtDeck 访问令牌（{_mask(self._access_token)}）")

    def _load_tokens(self) -> None:
        stored = self.token_store.load()
        if isinstance(stored, dict):
            access = stored.get("access_token")
            refresh = stored.get("refresh_token")
            if isinstance(access, str) and access:
                self._access_token = access
                self._access_expire_at = 0.0  # 存量令牌过期时间未知，首次使用前强制刷新
            if isinstance(refresh, str) and refresh:
                self._refresh_token = refresh

    # ------------------------------------------------------------ 集成面

    def handshake(
        self,
        instance_id: str,
        instance_name: str,
        plugin_version: str,
        moviepilot_version: str,
    ) -> Dict[str, Any]:
        self.ensure_authenticated()
        try:
            payload = self._request_json(
                "POST",
                "/api/v1/moviepilot/handshake",
                json_body={
                    "instanceId": instance_id,
                    "instanceName": instance_name,
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "pluginVersion": plugin_version,
                    "moviepilotVersion": moviepilot_version,
                },
            )
        except BtDeckApiError as exc:
            if exc.http_status == 403:
                raise BtDeckApiError(
                    f"握手被拒绝（403）：{exc.message or 'BtDeck 侧集成开关未开启或实例被禁用'}", 403, exc.code
                ) from exc
            raise
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def sync_transfer_history(
        self,
        instance_id: str,
        items: list,
        sync_mode: str,
        page_number: int,
        page_size: int,
        is_last_batch: bool,
    ) -> Dict[str, Any]:
        """推送一批整理历史；返回 {inserted, updated, skipped, failed, errors, ...}。"""
        self.ensure_authenticated()
        payload = self._request_json(
            "POST",
            "/api/v1/moviepilot/sync/transfer-history",
            json_body={
                "instanceId": instance_id,
                "protocolVersion": self.PROTOCOL_VERSION,
                "syncMode": sync_mode,
                "pageNumber": page_number,
                "pageSize": page_size,
                "isLastBatch": is_last_batch,
                "items": items,
            },
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 内部

    def _first_data_item(self, payload: Dict[str, Any], error_message: str) -> Dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        raise BtDeckAuthError(error_message)

    def _request_json(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        _retried_auth: bool = False,
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        if authenticated:
            if not self._access_token:
                self.ensure_authenticated()
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = self._http.request(method, self.base_url + path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise BtDeckApiError(f"无法连接 BtDeck（{self.base_url}）：{exc.__class__.__name__}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise BtDeckApiError(f"BtDeck 返回非 JSON 响应（HTTP {response.status_code}）", response.status_code) from exc
        if not isinstance(body, dict):
            raise BtDeckApiError("BtDeck 返回结构异常", response.status_code)

        code = str(body.get("code", ""))
        message = str(body.get("msg") or "请求失败")
        if response.status_code >= 400 or (code and code != "200"):
            # 401 → 一次令牌轮换/重登录后重试（防过期令牌；只重试一层防递归）
            if response.status_code == 401 and authenticated and not _retried_auth:
                self._log("访问令牌失效，尝试刷新后重试一次")
                self._access_token = None
                try:
                    self._refresh()
                except BtDeckApiError:
                    self._refresh_token = None
                    self._login()
                return self._request_json(method, path, json_body, authenticated=True, _retried_auth=True)
            raise BtDeckApiError(message, response.status_code, code)
        return body
