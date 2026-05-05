"""Tests for ``nasa_earthdata.core.venv_manager`` import helpers."""

import builtins
import sys
import types

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


def test_import_earthaccess_reports_missing_package(
    force_earthaccess_import_failure, monkeypatch
):
    """When metadata is missing, report that earthaccess is not installed."""

    def missing_version(name):
        raise venv_manager.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(venv_manager.importlib.metadata, "version", missing_version)

    with pytest.raises(ImportError) as exc_info:
        venv_manager.import_earthaccess()

    assert "not installed" in str(exc_info.value).lower()


def test_import_earthaccess_reports_broken_install(
    force_earthaccess_import_failure, monkeypatch
):
    """When metadata exists but import fails, report the import error."""
    monkeypatch.setattr(
        venv_manager.importlib.metadata, "version", lambda name: "0.14.0"
    )
    monkeypatch.setattr(venv_manager, "_log", lambda *args, **kwargs: None)

    with pytest.raises(ImportError) as exc_info:
        venv_manager.import_earthaccess()

    message = str(exc_info.value).lower()
    assert "0.14.0" in message
    assert "failed to import" in message
    assert "simulated: earthaccess fails to import" in message


def test_check_dependencies_returns_three_tuple(monkeypatch):
    """``check_dependencies`` returns (all_ok, missing, installed)."""
    monkeypatch.setattr(venv_manager, "ensure_venv_packages_available", lambda: True)

    def fake_version(name):
        if name == "earthaccess":
            return "0.14.0"
        if name == "pandas":
            return "2.2.0"
        raise venv_manager.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(venv_manager.importlib.metadata, "version", fake_version)

    result = venv_manager.check_dependencies()
    assert len(result) == 3
    all_ok, missing, installed = result

    assert all_ok is False
    assert ("geopandas", "") in missing
    assert ("earthaccess", "0.14.0") in installed
    assert ("pandas", "2.2.0") in installed


def test_python_executable_name_rejects_helper_binaries():
    """Only interpreter-like names should be probed."""
    assert venv_manager._is_python_executable_name("/tmp/python3")
    assert venv_manager._is_python_executable_name("/tmp/python3.11")
    assert venv_manager._is_python_executable_name("/tmp/python311")
    assert not venv_manager._is_python_executable_name("/tmp/python3-config")


def test_find_python_executable_selects_matching_bundle_python(monkeypatch, tmp_path):
    """macOS QGIS launchers should be skipped in favor of bundled Python."""
    contents = tmp_path / "QGIS.app" / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    launcher = macos / "QGIS"
    python_path = macos / f"python{sys.version_info.major}.{sys.version_info.minor}"
    launcher.write_text("", encoding="utf-8")
    python_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(venv_manager.sys, "executable", str(launcher))
    monkeypatch.setattr(venv_manager.sys, "prefix", str(contents))
    monkeypatch.setattr(venv_manager.sys, "base_prefix", str(contents))
    monkeypatch.setattr(venv_manager.sys, "exec_prefix", str(contents))
    monkeypatch.setattr(
        venv_manager.sys, "_base_executable", str(launcher), raising=False
    )
    monkeypatch.setattr(venv_manager.sys, "_base_prefix", str(contents), raising=False)

    def fake_run(cmd, **_kwargs):
        return types.SimpleNamespace(
            returncode=0, stdout=f"{sys.version_info.major}.{sys.version_info.minor}\n"
        )

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    assert venv_manager._find_python_executable() == str(python_path)


def test_find_python_executable_reports_checked_candidates(monkeypatch, tmp_path):
    """Resolver failures should be explicit and diagnosable."""
    launcher = tmp_path / "QGIS"
    launcher.write_text("", encoding="utf-8")

    monkeypatch.setattr(venv_manager.sys, "executable", str(launcher))
    monkeypatch.setattr(venv_manager.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(venv_manager.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(venv_manager.sys, "exec_prefix", str(tmp_path))
    monkeypatch.setattr(
        venv_manager.sys, "_base_executable", str(launcher), raising=False
    )
    monkeypatch.setattr(venv_manager.sys, "_base_prefix", str(tmp_path), raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        venv_manager._find_python_executable()

    message = str(exc_info.value)
    assert "Could not find a Python executable" in message
    assert "Checked candidates" in message
