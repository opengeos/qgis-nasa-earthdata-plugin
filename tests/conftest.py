"""Shared pytest fixtures.

When running outside of QGIS, stubs the ``qgis`` package onto PyQt6 so the
plugin's modules can be imported. The stub reproduces the real
``qgis.PyQt`` shim behavior on Qt6: it re-exports ``QAction``,
``QActionGroup`` and ``QShortcut`` from ``PyQt6.QtGui`` under
``qgis.PyQt.QtWidgets`` (they moved out of ``QtWidgets`` in Qt6).

Pytest skips this file (and the modules that depend on it) when PyQt6 is
not installed, and leaves a real ``qgis`` package alone if it's already
importable, so this conftest never masks an integration environment.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

PyQt6_QtCore = pytest.importorskip("PyQt6.QtCore")
PyQt6_QtGui = pytest.importorskip("PyQt6.QtGui")
PyQt6_QtNetwork = pytest.importorskip("PyQt6.QtNetwork")
PyQt6_QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


def _install_qgis_stub() -> None:
    qgis = types.ModuleType("qgis")
    qgis.__path__ = []
    sys.modules["qgis"] = qgis

    qgis_pyqt = types.ModuleType("qgis.PyQt")
    qgis_pyqt.__path__ = []
    sys.modules["qgis.PyQt"] = qgis_pyqt
    qgis.PyQt = qgis_pyqt

    pyqt_submodules = {
        "QtCore": PyQt6_QtCore,
        "QtGui": PyQt6_QtGui,
        "QtNetwork": PyQt6_QtNetwork,
        "QtWidgets": PyQt6_QtWidgets,
    }
    for name, real in pyqt_submodules.items():
        alias = types.ModuleType(f"qgis.PyQt.{name}")
        for attr in dir(real):
            if not attr.startswith("_"):
                setattr(alias, attr, getattr(real, attr))
        sys.modules[f"qgis.PyQt.{name}"] = alias
        setattr(qgis_pyqt, name, alias)

    # Qt6: QAction, QActionGroup, and QShortcut live in QtGui. The real
    # qgis.PyQt.QtWidgets shim re-exports them, so mirror that here.
    qtwidgets_alias = sys.modules["qgis.PyQt.QtWidgets"]
    for attr in ("QAction", "QActionGroup", "QShortcut"):
        setattr(qtwidgets_alias, attr, getattr(PyQt6_QtGui, attr))

    for submodule in ("QtSvg", "QtWebEngineWidgets"):
        alias = MagicMock()
        sys.modules[f"qgis.PyQt.{submodule}"] = alias
        setattr(qgis_pyqt, submodule, alias)

    for name in ("core", "gui", "utils"):
        stub = MagicMock()
        stub.__spec__ = None
        sys.modules[f"qgis.{name}"] = stub
        setattr(qgis, name, stub)


# Only stub when a real qgis isn't already importable. This keeps the test
# suite usable both standalone (CI / dev box) and inside a real QGIS
# Python environment, where masking the real package would hide
# integration problems.
try:
    import qgis  # noqa: F401
except ImportError:
    _install_qgis_stub()
