from types import SimpleNamespace
from unittest.mock import MagicMock

import qgis.utils as qgis_utils

from nasa_earthdata.nasa_earthdata import NASAEarthdata


def test_ai_assistant_button_opens_loaded_open_geoagent(monkeypatch):
    plugin = SimpleNamespace(toggle_chat_dock=MagicMock(), _chat_dock=None)
    monkeypatch.setattr(qgis_utils, "plugins", {"open_geoagent": plugin}, raising=False)
    monkeypatch.setattr(qgis_utils, "available_plugins", [], raising=False)

    NASAEarthdata(MagicMock()).open_ai_assistant()

    plugin.toggle_chat_dock.assert_called_once_with()


def test_ai_assistant_hands_context_to_open_geoagent(monkeypatch):
    plugin = SimpleNamespace(
        toggle_chat_dock=MagicMock(),
        _chat_dock=None,
        set_external_context=MagicMock(),
    )
    monkeypatch.setattr(qgis_utils, "plugins", {"open_geoagent": plugin}, raising=False)
    monkeypatch.setattr(qgis_utils, "available_plugins", [], raising=False)

    NASAEarthdata(MagicMock()).open_ai_assistant(context="current NASA context")

    plugin.toggle_chat_dock.assert_called_once_with()
    plugin.set_external_context.assert_called_once_with(
        "NASA Earthdata", "current NASA context"
    )


def test_ai_assistant_button_raises_visible_open_geoagent_chat(monkeypatch):
    dock = MagicMock()
    dock.isVisible.return_value = True
    plugin = SimpleNamespace(toggle_chat_dock=MagicMock(), _chat_dock=dock)
    monkeypatch.setattr(qgis_utils, "plugins", {"open_geoagent": plugin}, raising=False)
    monkeypatch.setattr(qgis_utils, "available_plugins", [], raising=False)

    NASAEarthdata(MagicMock()).open_ai_assistant()

    dock.show.assert_called_once_with()
    dock.raise_.assert_called_once_with()
    plugin.toggle_chat_dock.assert_not_called()


def test_ai_assistant_button_loads_available_open_geoagent(monkeypatch):
    plugin = SimpleNamespace(toggle_chat_dock=MagicMock(), _chat_dock=None)
    load_plugin = MagicMock()
    start_plugin = MagicMock(
        side_effect=lambda package_name: setattr(
            qgis_utils, "plugins", {package_name: plugin}
        )
    )
    monkeypatch.setattr(qgis_utils, "plugins", {}, raising=False)
    monkeypatch.setattr(
        qgis_utils, "available_plugins", ["open_geoagent"], raising=False
    )
    monkeypatch.setattr(qgis_utils, "active_plugins", [], raising=False)
    monkeypatch.setattr(qgis_utils, "loadPlugin", load_plugin, raising=False)
    monkeypatch.setattr(qgis_utils, "startPlugin", start_plugin, raising=False)

    NASAEarthdata(MagicMock()).open_ai_assistant()

    load_plugin.assert_called_once_with("open_geoagent")
    start_plugin.assert_called_once_with("open_geoagent")
    plugin.toggle_chat_dock.assert_called_once_with()


def test_ai_assistant_button_prompts_when_open_geoagent_missing(monkeypatch):
    iface = MagicMock()
    prompt = MagicMock()
    monkeypatch.setattr(qgis_utils, "plugins", {}, raising=False)
    monkeypatch.setattr(qgis_utils, "available_plugins", [], raising=False)
    monkeypatch.setattr(NASAEarthdata, "_prompt_open_geoagent_install", prompt)

    NASAEarthdata(iface).open_ai_assistant()

    prompt.assert_called_once_with()
