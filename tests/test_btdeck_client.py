# -*- coding: utf-8 -*-
"""BtDeck 客户端回归：登录/刷新轮换/401 重试/信封解析/载荷形状。

令牌脱敏纪律：断言日志与异常文本不出现完整令牌。
"""

import json

import httpx
import pytest

from app.plugins.btdeckbridge.btdeck_client import (
    BtDeckApiError,
    BtDeckAuthError,
    BtDeckClient,
)


class MemoryTokenStore:
    def __init__(self):
        self.payload = None

    def load(self):
        return self.payload

    def save(self, payload):
        self.payload = dict(payload)


def _ok(data, code="200"):
    return {"status": "success", "msg": "ok", "code": code, "data": data}


def _err(msg, code, http_status=None):
    return {"status": "error", "msg": msg, "code": code, "data": None}


class Router:
    """按 (method, path) 路由的 MockTransport，记录请求供断言。"""

    def __init__(self):
        self.requests = []
        self.routes = {}

    def add(self, method, path, handler):
        self.routes[(method, path)] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, json=_err("未注册路由", "404"))
        return handler(request)


def _client(router, store=None, url="http://btdeck:9090"):
    logs = []
    client = BtDeckClient(
        base_url=url,
        username="mp-integration",
        password="secret-pass",
        token_store=store if store is not None else MemoryTokenStore(),
        transport=httpx.MockTransport(router.handler),
        logger=logs.append,
    )
    client._test_logs = logs
    return client


class TestAuth:
    def test_login_stores_tokens_and_uses_bearer(self):
        router = Router()
        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "bearer"}])))
        router.add("GET", "/api/v1/moviepilot/settings", lambda r: httpx.Response(200, json=_ok({"x": 1})))
        store = MemoryTokenStore()
        client = _client(router, store)
        try:
            client.ensure_authenticated()
            client._request_json("GET", "/api/v1/moviepilot/settings")
        finally:
            client.close()
        assert store.payload == {"access_token": "acc-1", "refresh_token": "ref-1"}
        sync_request = router.requests[-1]
        assert sync_request.headers["Authorization"] == "Bearer acc-1"
        # 日志脱敏：只出现前缀
        assert any("acc***" in line for line in client._test_logs)
        assert not any("acc-1" in line or "ref-1" in line for line in client._test_logs)

    def test_login_rejected_credentials_raise_auth_error(self):
        router = Router()
        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_err("用户名或密码错误", "401")))
        client = _client(router)
        try:
            with pytest.raises(BtDeckAuthError, match="用户名或密码错误"):
                client.ensure_authenticated()
        finally:
            client.close()

    def test_missing_credentials_rejected(self):
        client = BtDeckClient(base_url="http://bt", username="", password="", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        try:
            with pytest.raises(BtDeckAuthError, match="用户名或密码"):
                client.ensure_authenticated()
        finally:
            client.close()

    def test_expired_access_token_rotates_via_refresh_once(self):
        router = Router()
        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-1", "refresh_token": "ref-1"}])))
        router.add("POST", "/api/v1/moviepilot/handshake", lambda r: httpx.Response(401, json=_err("访问令牌无效或已过期", "401")) if r.headers.get("Authorization") == "Bearer acc-1" else httpx.Response(200, json=_ok({"status": "ok"})))
        router.add("POST", "/api/v1/auth/refresh", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-2", "refresh_token": "ref-2"}])))
        client = _client(router)
        try:
            client.ensure_authenticated()
            result = client.handshake("inst-1", "测试", "1.0.0", "2.9.9")
        finally:
            client.close()
        assert result["status"] == "ok"
        paths = [r.url.path for r in router.requests]
        assert paths.count("/api/v1/moviepilot/handshake") == 2
        assert paths.count("/api/v1/auth/refresh") == 1


class TestIntegrationCalls:
    def test_handshake_payload_shape(self):
        router = Router()
        captured = {}

        def handshake_handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_ok({"status": "ok", "created": True, "protocolVersion": 1}))

        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-1", "refresh_token": "ref-1"}])))
        router.add("POST", "/api/v1/moviepilot/handshake", handshake_handler)
        client = _client(router)
        try:
            result = client.handshake("inst-1", "测试实例", "1.0.0", "2.9.9")
        finally:
            client.close()
        assert result["created"] is True
        assert captured["instanceId"] == "inst-1"
        assert captured["instanceName"] == "测试实例"
        assert captured["protocolVersion"] == 1
        assert captured["pluginVersion"] == "1.0.0"
        assert captured["moviepilotVersion"] == "2.9.9"

    def test_sync_payload_shape_and_result(self):
        router = Router()
        captured = {}

        def sync_handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_ok({"inserted": 2, "updated": 0, "skipped": 1, "failed": 0, "errors": []}))

        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-1", "refresh_token": "ref-1"}])))
        router.add("POST", "/api/v1/moviepilot/sync/transfer-history", sync_handler)
        client = _client(router)
        try:
            result = client.sync_transfer_history(
                instance_id="inst-1",
                items=[{"historyId": 1}, {"historyId": 2}, {"historyId": 3}],
                sync_mode="incremental",
                page_number=2,
                page_size=100,
                is_last_batch=True,
            )
        finally:
            client.close()
        assert result["inserted"] == 2
        assert captured["instanceId"] == "inst-1"
        assert captured["syncMode"] == "incremental"
        assert captured["pageNumber"] == 2
        assert captured["isLastBatch"] is True
        assert len(captured["items"]) == 3

    def test_handshake_403_wrapped_with_hint(self):
        router = Router()
        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "acc-1", "refresh_token": "ref-1"}])))
        router.add(
            "POST",
            "/api/v1/moviepilot/handshake",
            lambda r: httpx.Response(403, json=_err("MoviePilot 集成未启用（请在 BtDeck 设置中开启）", "403")),
        )
        client = _client(router)
        try:
            with pytest.raises(BtDeckApiError, match="握手被拒绝（403）") as excinfo:
                client.handshake("inst-1", "n", "1.0.0", "2.9.9")
        finally:
            client.close()
        assert excinfo.value.http_status == 403

    def test_network_unreachable_raises_clean_message(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = BtDeckClient(
            base_url="http://btdeck-unreachable:9090",
            username="u",
            password="p",
            transport=httpx.MockTransport(boom),
        )
        try:
            with pytest.raises(BtDeckApiError, match="无法连接 BtDeck"):
                client.ensure_authenticated()
        finally:
            client.close()

    def test_tokens_never_in_error_text(self):
        router = Router()
        router.add("POST", "/api/v1/auth/login", lambda r: httpx.Response(200, json=_ok([{"access_token": "supersecretaccesstoken", "refresh_token": "supersecretrefreshtoken"}])))
        router.add("POST", "/api/v1/moviepilot/handshake", lambda r: httpx.Response(500, json=_err("内部错误", "500")))
        client = _client(router)
        try:
            client.ensure_authenticated()
            with pytest.raises(BtDeckApiError) as excinfo:
                client.handshake("inst-1", "n", "1.0.0", "2.9.9")
        finally:
            client.close()
        assert "supersecret" not in str(excinfo.value)
        assert all("supersecret" not in line for line in client._test_logs)
