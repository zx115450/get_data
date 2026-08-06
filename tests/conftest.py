"""pytest 公共夹具。

包导入依赖 pyproject.toml 的 [tool.pytest.ini_options].pythonpath，
或 `pip install -e .`；此处不再改 sys.path。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每个用例后清 Settings 缓存，避免 env 污染。"""
    yield
    try:
        from config.settings import get_settings

        get_settings.cache_clear()
    except Exception:
        pass
