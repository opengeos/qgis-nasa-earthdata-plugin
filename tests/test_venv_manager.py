"""Tests for ``nasa_earthdata.core.venv_manager`` error classification.

These tests do NOT exercise a real install. They monkey-patch the
helpers used by ``import_earthaccess`` so we can assert the right
classification (genuinely missing vs. installed-but-broken) without
fabricating a working venv.
"""

import builtins
import sys

import pytest

from nasa_earthdata.core import venv_manager


@pytest.fixture
def force_earthaccess_import_failure(monkeypatch):
    """Make ``import earthaccess`` raise ImportError, even if installed.

    The dev environment may already have ``earthaccess`` available
    system-wide (e.g. in the ``geo`` conda env). This fixture intercepts
    Python's import machinery so ``import earthaccess`` always fails for
    the duration of the test, letting us verify the classification logic
    in isolation.
    """
    monkeypatch.delitem(sys.modules, "earthaccess", raising=False)
    monkeypatch.setattr(venv_manager, "ensure_venv_packages_available", lambda: True)

    real_import = builtins.__import__

    def raising_import(name, *args, **kwargs):
        if name == "earthaccess" or name.startswith("earthaccess."):
            raise ImportError("simulated: earthaccess fails to import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", raising_import)
    yield


def test_import_earthaccess_classifies_missing_dist_info(
    force_earthaccess_import_failure, monkeypatch
):
    """When no dist-info is present, raise EarthaccessNotInstalledError."""
    monkeypatch.setattr(venv_manager, "_earthaccess_dist_info_present", lambda: False)

    with pytest.raises(venv_manager.EarthaccessNotInstalledError) as exc_info:
        venv_manager.import_earthaccess()

    assert "not installed" in exc_info.value.user_message.lower()
    # Inherits from ImportError so generic handlers still catch it.
    assert isinstance(exc_info.value, ImportError)


def test_import_earthaccess_classifies_broken_install(
    force_earthaccess_import_failure, monkeypatch
):
    """When dist-info is present but import fails, raise EarthaccessImportError."""
    monkeypatch.setattr(venv_manager, "_earthaccess_dist_info_present", lambda: True)
    # Avoid touching subprocess/QGIS log during the test.
    monkeypatch.setattr(
        venv_manager, "_log_earthaccess_import_failure", lambda exc: None
    )

    with pytest.raises(venv_manager.EarthaccessImportError) as exc_info:
        venv_manager.import_earthaccess()

    assert "failed to import" in exc_info.value.user_message.lower()
    assert isinstance(exc_info.value, ImportError)
    assert exc_info.value.original is not None


def test_check_dependencies_returns_four_tuple(monkeypatch):
    """``check_dependencies`` must return (all_ok, missing, installed, broken)."""
    # Force the metadata lookups and probe to deterministic values.
    monkeypatch.setattr(venv_manager, "ensure_venv_packages_available", lambda: True)

    def fake_version(name):
        if name == "earthaccess":
            return "0.14.0"
        if name == "pandas":
            return "2.2.0"
        raise venv_manager.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(venv_manager.importlib.metadata, "version", fake_version)
    monkeypatch.setattr(
        venv_manager,
        "_probe_earthaccess_in_venv",
        lambda: (False, "ImportError: cannot import name foo"),
    )

    result = venv_manager.check_dependencies()
    assert len(result) == 4
    all_ok, missing, installed, broken = result

    assert all_ok is False
    assert ("geopandas", "") in missing
    assert ("pandas", "2.2.0") in installed
    assert any(name == "earthaccess" for name, _, _ in broken)
