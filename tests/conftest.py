# -*- coding: utf-8 -*-
"""BtDeckBridge 插件单测基建：宿主桩 + 插件包导入。

宿主真实机制：MoviePilot 把市场仓库的 ``plugins.v2`` 目录并入
``app.plugins`` 包的 ``__path__``，再以 ``app.plugins.<插件id>`` 导入
（PluginManager 的 sys.modules 清理也按该命名）。本桩还原同一机制：

- ``app.plugins``：__path__ 指向本仓库 ``plugins.v2/``，附带内存版
  ``_PluginBase``（save_data/get_data/get_config/update_config）；
- ``app.core.config.settings`` / ``app.log.logger``：懒加载导入的宿主符号；
- 测试内统一 ``from app.plugins.btdeckbridge import ...`` 导入插件。
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class _StubPluginBase:
    """宿主 _PluginBase 桩：配置与插件数据的内存存储。"""

    def __init__(self):
        self._data_store = {}
        self._config_store = {}

    # ---- 宿主配置（systemconfig，键 plugin.{类名}） ----
    def get_config(self, plugin_id=None):
        return dict(self._config_store)

    def update_config(self, config, plugin_id=None):
        self._config_store = dict(config or {})
        return True

    # ---- 插件数据（plugindata） ----
    def save_data(self, key, value, plugin_id=None):
        self._data_store[key] = value

    def get_data(self, key=None, plugin_id=None):
        if key is None:
            return dict(self._data_store)
        return self._data_store.get(key)

    def del_data(self, key, plugin_id=None):
        self._data_store.pop(key, None)


class _StubSettings:
    PORT = 3001
    API_TOKEN = "mp-stub-token"
    VERSION = "2.9.9-stub"


class _StubLogger:
    def __init__(self):
        self.records = []

    def info(self, message):
        self.records.append(("info", message))

    def warning(self, message):
        self.records.append(("warning", message))

    def error(self, message):
        self.records.append(("error", message))


def _install_host_stubs():
    app_mod = types.ModuleType("app")
    app_mod.__path__ = []
    plugins_mod = types.ModuleType("app.plugins")
    # 宿主同款：plugins.v2 目录并入 app.plugins.__path__
    plugins_mod.__path__ = [str(REPO_ROOT / "plugins.v2")]
    plugins_mod._PluginBase = _StubPluginBase
    core_mod = types.ModuleType("app.core")
    core_mod.__path__ = []
    config_mod = types.ModuleType("app.core.config")
    config_mod.settings = _StubSettings()
    log_mod = types.ModuleType("app.log")
    log_mod.logger = _StubLogger()
    sys.modules.setdefault("app", app_mod)
    sys.modules.setdefault("app.plugins", plugins_mod)
    sys.modules.setdefault("app.core", core_mod)
    sys.modules.setdefault("app.core.config", config_mod)
    sys.modules.setdefault("app.log", log_mod)


_install_host_stubs()

