# -*- coding: utf-8 -*-
"""插件宿主装配回归（stub _PluginBase）：生命周期、实例身份、防重复、页面契约。"""

import sys
import threading
from datetime import timedelta

import pytest
from apscheduler.triggers.interval import IntervalTrigger

import app.plugins.btdeckbridge as bridge_mod
from app.plugins.btdeckbridge import BtDeckBridge

# 宿主调度器（V2 app/scheduler.py 与 V3 app/scheduler/reconcile.py）注册插件
# 服务的真实消费方式：add_job(..., **(service.get("kwargs") or {}))。
HOST_SERVICE_CONTRACT_KEYS = {"id", "name", "trigger", "func", "kwargs", "func_kwargs"}


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

    def test_get_service_interval_goes_into_host_consumed_kwargs(self):
        """回归：宿主只展开 service["kwargs"]；顶层 minutes 会被静默忽略，
        interval 触发器空参数会被 APScheduler 钳制为每秒执行。"""
        services = _configured(sync_interval_minutes=30).get_service()
        assert len(services) == 1
        service = services[0]
        assert service["id"] == "BtDeckBridgeSync"
        assert service["trigger"] == "interval"
        assert callable(service["func"])
        assert set(service) <= HOST_SERVICE_CONTRACT_KEYS
        # 按宿主消费方式实例化真实触发器：30 分钟配置 → 30 分钟触发间隔
        trigger = IntervalTrigger(**(service.get("kwargs") or {}))
        assert trigger.interval == timedelta(minutes=30)

    def test_get_service_interval_clamped_to_minimum(self):
        services = _configured(sync_interval_minutes=1).get_service()
        trigger = IntervalTrigger(**(services[0].get("kwargs") or {}))
        assert trigger.interval == timedelta(minutes=5)

    def test_get_service_zero_interval_disables_periodic_job(self):
        """回归：0 关闭周期任务；手动同步 API 不受影响。"""
        plugin = _configured(sync_interval_minutes=0)
        assert plugin.get_state() is True
        assert plugin.get_service() == []

    @pytest.mark.parametrize("raw", [None, "", "  ", "abc", -3, "-5"])
    def test_get_service_invalid_interval_falls_back_to_default(self, raw):
        services = _configured(sync_interval_minutes=raw).get_service()
        assert len(services) == 1
        trigger = IntervalTrigger(**(services[0].get("kwargs") or {}))
        assert trigger.interval == timedelta(minutes=30)

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


class TestMoviePilotVersion:
    def test_prefers_root_version_app_version(self):
        # conftest 桩模拟宿主根目录 version.py（真实宿主: APP_VERSION='v2.15.6'）
        assert _configured()._moviepilot_version() == "v2.15.6"

    def test_falls_back_to_settings_version_flag_without_version_module(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "version", raising=False)
        assert _configured()._moviepilot_version() == "v2"

    def test_returns_empty_when_no_source_available(self, monkeypatch):
        import conftest

        monkeypatch.delitem(sys.modules, "version", raising=False)
        monkeypatch.delattr(conftest._StubSettings, "VERSION_FLAG")
        assert _configured()._moviepilot_version() == ""


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

    def test_partial_failure_reported_consistently_and_retried(self, monkeypatch):
        """回归：批次部分失败时 API 返回值/持久化状态/详情页一致表达失败，
        水位不推进；重试成功后恢复正常。"""
        plugin = _configured()

        class FakeAdapter:
            def fetch_page(self, page, count):
                # 2 条记录：id 3 成功、id 2 失败 → 部分失败
                return 2, [
                    {"id": 3, "title": "A", "date": "2026-09-01 10:00:00", "src": "/d/3", "dest": "/m/3"},
                    {"id": 2, "title": "B", "date": "2026-09-01 09:00:00", "src": "/d/2", "dest": "/m/2"},
                ]

            def close(self):
                pass

        client_outcomes = [
            {
                "inserted": 1, "updated": 0, "skipped": 0, "failed": 1,
                "errors": [{"historyId": 2, "error": "字段校验失败"}],
            }
        ]

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def handshake(self, *a, **k):
                return {"status": "ok"}

            def sync_transfer_history(self, **kwargs):
                self.last_sync_kwargs = kwargs
                if client_outcomes:
                    return client_outcomes.pop(0)
                # 重试整页：id 3 幂等吸收为 skipped，id 2 本次成功
                return {"inserted": 1, "updated": 0, "skipped": 1, "failed": 0, "errors": []}

            def close(self):
                pass

        monkeypatch.setattr(plugin, "_build_adapter", lambda: FakeAdapter())
        monkeypatch.setattr(bridge_mod, "BtDeckClient", FakeClient)

        result = plugin.api_sync_now(mode="full")
        assert result["ok"] is False
        assert result["finished"] is False
        assert result["failed"] == 1
        assert "部分记录推送失败" in result["message"]
        # 持久化状态与详情页一致表达失败原因
        status = plugin.get_data("sync_status")
        assert status["finished"] is False
        assert "部分记录推送失败" in status["stoppedReason"]
        page_text = str(plugin.get_page())
        assert "failed=1" in page_text and "部分记录推送失败" in page_text
        # 水位未推进，断点保留在失败页
        checkpoint = plugin.get_data("sync_checkpoint")
        assert checkpoint.get("watermark", 0) == 0
        assert checkpoint["pass"]["next_page"] == 1

        # 重试成功：整页重推，已成功条目计 skipped（无重复），水位推进
        result2 = plugin.api_sync_now(mode="full")
        assert result2["ok"] is True
        assert result2["inserted"] == 1 and result2["skipped"] == 1
        checkpoint2 = plugin.get_data("sync_checkpoint")
        assert checkpoint2["watermark"] == 3
        assert "pass" not in checkpoint2

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
