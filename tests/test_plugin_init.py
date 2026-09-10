# -*- coding: utf-8 -*-
"""插件宿主装配回归（stub _PluginBase）：生命周期、实例身份、防重复、页面契约。"""

import threading

import pytest

import app.plugins.btdeckbridge as bridge_mod
from app.plugins.btdeckbridge import BtDeckBridge


def _configured(enabled=True, **overrides):
    plugin = BtDeckBridge()
    config = {
        "enabled": enabled,
        "btdeck_url": "http://btdeck:9090",
        "username": "mp-integration",
        "password": "secret",
        "instance_name": "测试实例",
        "sync_interval_minutes": 30,
        "full_rescan_hours": 24,
        "batch_size": 100,
    }
    config.update(overrides)
    plugin.init_plugin(config)
    return plugin


class TestLifecycle:
    def test_get_state_follows_enabled(self):
        assert _configured(enabled=True).get_state() is True
        assert _configured(enabled=False).get_state() is False

    def test_get_service_respects_enabled_and_interval_clamp(self):
        assert _configured(enabled=False).get_service() == []
        services = _configured(sync_interval_minutes=1).get_service()
        assert len(services) == 1
        assert services[0]["id"] == "BtDeckBridgeSync"
        assert services[0]["minutes"] == 5  # 下限钳制
        assert services[0]["trigger"] == "interval"
        assert callable(services[0]["func"])

    def test_stop_service_sets_stop_event(self):
        plugin = _configured()
        assert plugin._stop_event.is_set() is False
        plugin.stop_service()
        assert plugin._stop_event.is_set() is True

    def test_reinit_resets_stop_event(self):
        plugin = _configured()
        plugin.stop_service()
        plugin.init_plugin({"enabled": True})
        assert plugin._stop_event.is_set() is False


class TestInstanceId:
    def test_instance_id_generated_once_and_persisted(self):
        plugin = _configured()
        first = plugin._instance_id()
        assert len(first) == 32
        assert plugin.get_data("instance_id") == first
        assert plugin._instance_id() == first

    def test_instance_id_survives_reinit(self):
        plugin = _configured()
        first = plugin._instance_id()
        plugin.init_plugin(plugin.get_config())
        assert plugin._instance_id() == first


class TestSyncWiring:
    def test_run_without_url_returns_error(self):
        plugin = _configured(btdeck_url="")
        result = plugin.api_sync_now()
        assert result["ok"] is False

    def test_run_sync_full_wiring_with_fakes(self, monkeypatch):
        plugin = _configured()

        class FakeAdapter:
            def fetch_page(self, page, count):
                return 1, [{"id": 9, "title": "M", "src": "/d/9.mkv", "dest": "/m/9.mkv"}]

            def close(self):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def handshake(self, *a, **k):
                return {"status": "ok"}

            def sync_transfer_history(self, **kwargs):
                self.last_sync_kwargs = kwargs
                return {"inserted": 1, "updated": 0, "skipped": 0, "failed": 0, "errors": []}

            def close(self):
                pass

        monkeypatch.setattr(plugin, "_build_adapter", lambda: FakeAdapter())
        monkeypatch.setattr(bridge_mod, "BtDeckClient", FakeClient)
        result = plugin.api_sync_now(mode="full")

        assert result["ok"] is True
        assert result["finished"] is True
        assert result["inserted"] == 1
        # 状态页数据已持久化
        status = plugin.get_data("sync_status")
        assert status["finished"] is True
        # 实例身份随同步载荷下发
        checkpoint = plugin.get_data("sync_checkpoint")
        assert checkpoint["watermark"] == 9

    def test_concurrent_sync_second_call_skips(self):
        plugin = _configured()
        plugin._run_lock.acquire()  # 模拟一趟在途
        try:
            result = plugin.api_sync_now()
            assert result["ok"] is False
            assert "在途" in result["message"]
        finally:
            plugin._run_lock.release()

    def test_lock_released_after_failure(self, monkeypatch):
        plugin = _configured()

        def boom():
            raise bridge_mod.MoviePilotAdapterError("宿主配置不可用")

        monkeypatch.setattr(plugin, "_build_adapter", boom)
        result = plugin.api_sync_now()
        assert result["ok"] is False
        assert plugin._run_lock.locked() is False  # 失败也必须释放锁


class TestPages:
    def test_get_form_returns_vuetify_and_defaults(self):
        plugin = BtDeckBridge()
        form, defaults = plugin.get_form()
        assert isinstance(form, list) and form
        assert defaults["enabled"] is False
        assert "btdeck_url" in defaults
        models = set(defaults.keys())

        def collect_models(node):
            if isinstance(node, dict):
                props = node.get("props")
                if isinstance(props, dict) and "model" in props:
                    models.discard(props["model"])
                for child in node.get("content", []) or []:
                    collect_models(child)
            elif isinstance(node, list):
                for child in node:
                    collect_models(child)

        collect_models(form)
        assert models == set(), f"表单未覆盖的配置键: {models}"

    def test_get_page_renders_status(self):
        plugin = _configured()
        page = plugin.get_page()
        assert isinstance(page, list) and page
        plugin._save_status({"finished": True, "mode": "full", "inserted": 3, "updated": 0, "skipped": 0, "failed": 0, "watermark": 3})
        text = str(plugin.get_page())
        assert "实例 ID" in text
        assert "3" in text

    def test_get_api_registers_sync_endpoint(self):
        api = BtDeckBridge().get_api()
        assert len(api) == 1
        assert api[0]["path"] == "/sync"
        assert "GET" in api[0]["methods"]
        assert callable(api[0]["endpoint"])
