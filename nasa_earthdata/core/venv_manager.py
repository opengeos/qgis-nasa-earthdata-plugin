"""
Virtual Environment Manager for NASA Earthdata Plugin.

Creates and manages an isolated virtual environment for installing
the plugin's Python dependencies (earthaccess, geopandas) without
modifying QGIS's built-in Python environment.
"""

import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Tuple, Optional, Callable, List

from qgis.core import QgsMessageLog, Qgis

CACHE_DIR = os.path.expanduser("~/.qgis_nasa_earthdata")
VENV_DIR = os.path.join(CACHE_DIR, "venv")

REQUIRED_PACKAGES = [
    ("earthaccess", ""),
    ("pandas", ""),
    ("geopandas", ""),
]


def _log(message, level=Qgis.Info):
    """Log a message to the QGIS message log.

    Args:
        message: The message to log.
        level: The log level (Qgis.Info, Qgis.Warning, Qgis.Critical).
    """
    QgsMessageLog.logMessage(str(message), "NASA Earthdata", level=level)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _get_clean_env_for_venv():
    """Create a clean environment dict for subprocess calls.

    Strips QGIS-specific variables that would interfere with the
    standalone Python or venv operations.

    Returns:
        A dict of environment variables.
    """
    env = os.environ.copy()

    vars_to_remove = [
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "QGIS_PREFIX_PATH",
        "QGIS_PLUGINPATH",
        "PROJ_DATA",
        "PROJ_LIB",
        "GDAL_DATA",
        "GDAL_DRIVER_PATH",
    ]
    for var in vars_to_remove:
        env.pop(var, None)

    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _get_subprocess_kwargs():
    """Get platform-specific subprocess kwargs.

    On Windows, suppresses the console window that would otherwise pop up
    for each subprocess invocation.

    Returns:
        A dict of keyword arguments for subprocess.run.
    """
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_venv_python_path(venv_dir=None):
    """Get the path to the Python executable inside the venv.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.

    Returns:
        The absolute path to the venv Python executable.
    """
    if venv_dir is None:
        venv_dir = VENV_DIR
    if platform.system() == "Windows":
        primary = os.path.join(venv_dir, "Scripts", "python.exe")
        if os.path.isfile(primary):
            return primary
        fallback = os.path.join(venv_dir, "Scripts", "python3.exe")
        if os.path.isfile(fallback):
            return fallback
        return primary  # Return expected path even if missing
    path = os.path.join(venv_dir, "bin", "python3")
    if os.path.isfile(path):
        return path
    return os.path.join(venv_dir, "bin", "python")


def get_venv_pip_path(venv_dir=None):
    """Get the path to pip inside the venv.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.

    Returns:
        The absolute path to the venv pip executable.
    """
    if venv_dir is None:
        venv_dir = VENV_DIR
    if platform.system() == "Windows":
        return os.path.join(venv_dir, "Scripts", "pip.exe")
    return os.path.join(venv_dir, "bin", "pip")


def get_venv_site_packages(venv_dir=None):
    """Get the path to the site-packages directory inside the venv.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.

    Returns:
        The path to the venv site-packages directory, or None if not found.
    """
    if venv_dir is None:
        venv_dir = VENV_DIR

    if platform.system() == "Windows":
        sp = os.path.join(venv_dir, "Lib", "site-packages")
        return sp if os.path.isdir(sp) else None

    # On Unix, detect the actual Python version directory in the venv
    lib_dir = os.path.join(venv_dir, "lib")
    if not os.path.isdir(lib_dir):
        return None
    for entry in sorted(os.listdir(lib_dir), reverse=True):
        if entry.startswith("python"):
            sp = os.path.join(lib_dir, entry, "site-packages")
            if os.path.isdir(sp):
                return sp
    return None


def venv_exists(venv_dir=None):
    """Check if the virtual environment exists.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.

    Returns:
        True if the venv Python executable exists.
    """
    return os.path.exists(get_venv_python_path(venv_dir))


# ---------------------------------------------------------------------------
# System Python resolution
# ---------------------------------------------------------------------------


def _find_python_executable():
    """Find a working Python executable for venv creation.

    On QGIS Windows, sys.executable may point to qgis-bin.exe rather than
    a Python interpreter.  This function searches for the actual Python
    executable using multiple strategies.

    Returns:
        Path to a Python executable, or sys.executable as fallback.
    """
    if platform.system() != "Windows":
        return sys.executable

    # Strategy 1: Check if sys.executable is already Python
    exe_name = os.path.basename(sys.executable).lower()
    if exe_name in ("python.exe", "python3.exe"):
        return sys.executable

    # Strategy 2: Use sys._base_prefix to find the Python installation.
    # On QGIS Windows, sys._base_prefix typically points to
    # C:\Program Files\QGIS 3.x\apps\Python3x\
    base_prefix = getattr(sys, "_base_prefix", None) or sys.prefix
    python_in_prefix = os.path.join(base_prefix, "python.exe")
    if os.path.isfile(python_in_prefix):
        return python_in_prefix

    # Strategy 3: Look for python.exe next to sys.executable
    exe_dir = os.path.dirname(sys.executable)
    for name in ("python.exe", "python3.exe"):
        candidate = os.path.join(exe_dir, name)
        if os.path.isfile(candidate):
            return candidate

    # Strategy 4: Walk up from sys.executable to find apps/Python3x/python.exe
    # Typical QGIS layout: .../QGIS 3.x/bin/qgis-bin.exe
    #                       .../QGIS 3.x/apps/Python3x/python.exe
    parent = os.path.dirname(exe_dir)
    apps_dir = os.path.join(parent, "apps")
    if os.path.isdir(apps_dir):
        best_candidate = None
        best_version_num = -1
        for entry in os.listdir(apps_dir):
            lower_entry = entry.lower()
            if not lower_entry.startswith("python"):
                continue
            suffix = lower_entry.removeprefix("python")
            digits = "".join(ch for ch in suffix if ch.isdigit())
            if not digits:
                continue
            try:
                version_num = int(digits)
            except ValueError:
                continue
            candidate = os.path.join(apps_dir, entry, "python.exe")
            if os.path.isfile(candidate) and version_num > best_version_num:
                best_version_num = version_num
                best_candidate = candidate
        if best_candidate:
            return best_candidate

    # Strategy 5: Use shutil.which as last resort
    which_python = shutil.which("python")
    if which_python:
        return which_python

    # Fallback: return sys.executable (may fail, but preserves current behavior)
    return sys.executable


def _get_system_python():
    """Get the path to the Python executable for creating venvs.

    Uses the standalone Python downloaded by python_manager if available.
    On Windows, falls back to QGIS's bundled Python using multi-strategy
    detection (handles qgis-bin.exe, apps/Python3x/, etc.).

    Returns:
        The path to a usable Python executable.

    Raises:
        RuntimeError: If no usable Python is found.
    """
    from .python_manager import standalone_python_exists, get_standalone_python_path

    if standalone_python_exists():
        python_path = get_standalone_python_path()
        _log(f"Using standalone Python: {python_path}")
        return python_path

    # Fallback: find QGIS's bundled Python (critical on Windows where
    # sys.executable may be qgis-bin.exe)
    python_path = _find_python_executable()
    if python_path and os.path.isfile(python_path):
        _log(
            f"Standalone Python unavailable, using system Python: {python_path}",
            Qgis.Warning,
        )
        return python_path

    raise RuntimeError(
        "Python standalone not installed. "
        "Please click 'Install Dependencies' to download Python automatically."
    )


# ---------------------------------------------------------------------------
# Venv creation
# ---------------------------------------------------------------------------


def _cleanup_partial_venv(venv_dir):
    """Remove a partially-created venv directory.

    Args:
        venv_dir: The venv directory to remove.
    """
    if os.path.exists(venv_dir):
        try:
            shutil.rmtree(venv_dir, ignore_errors=True)
            _log(f"Cleaned up partial venv: {venv_dir}")
        except Exception:
            _log(f"Could not clean up partial venv: {venv_dir}", Qgis.Warning)


def create_venv(venv_dir=None, progress_callback=None):
    """Create a virtual environment using the standalone Python.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.
        progress_callback: Function called with (percent, message).

    Returns:
        A tuple of (success: bool, message: str).
    """
    if venv_dir is None:
        venv_dir = VENV_DIR

    _log(f"Creating virtual environment at: {venv_dir}")

    if progress_callback:
        progress_callback(10, "Creating virtual environment...")

    system_python = _get_system_python()
    _log(f"Using Python: {system_python}")

    cmd = [system_python, "-m", "venv", venv_dir]

    try:
        env = _get_clean_env_for_venv()
        kwargs = _get_subprocess_kwargs()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            **kwargs,
        )

        if result.returncode == 0:
            _log("Virtual environment created successfully", Qgis.Success)

            # Ensure pip is available
            pip_path = get_venv_pip_path(venv_dir)
            if not os.path.exists(pip_path):
                _log("pip not found in venv, bootstrapping with ensurepip...")
                python_in_venv = get_venv_python_path(venv_dir)
                ensurepip_cmd = [python_in_venv, "-m", "ensurepip", "--upgrade"]
                try:
                    ensurepip_result = subprocess.run(
                        ensurepip_cmd,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=env,
                        **kwargs,
                    )
                    if ensurepip_result.returncode == 0:
                        _log("pip bootstrapped via ensurepip", Qgis.Success)
                    else:
                        err = ensurepip_result.stderr or ensurepip_result.stdout
                        _log(f"ensurepip failed: {err[:200]}", Qgis.Warning)
                        _cleanup_partial_venv(venv_dir)
                        return False, f"Failed to bootstrap pip: {err[:200]}"
                except Exception as e:
                    _log(f"ensurepip exception: {e}", Qgis.Warning)
                    _cleanup_partial_venv(venv_dir)
                    return False, f"Failed to bootstrap pip: {str(e)[:200]}"

            if progress_callback:
                progress_callback(20, "Virtual environment created")
            return True, "Virtual environment created"
        else:
            error_msg = (
                result.stderr or result.stdout or f"Return code {result.returncode}"
            )
            _log(f"Failed to create venv: {error_msg}", Qgis.Critical)
            _cleanup_partial_venv(venv_dir)
            return False, f"Failed to create venv: {error_msg[:200]}"

    except subprocess.TimeoutExpired:
        _log("Virtual environment creation timed out", Qgis.Critical)
        _cleanup_partial_venv(venv_dir)
        return False, "Virtual environment creation timed out"
    except FileNotFoundError:
        _log(f"Python executable not found: {system_python}", Qgis.Critical)
        return False, f"Python not found: {system_python}"
    except Exception as e:
        _log(f"Exception during venv creation: {str(e)}", Qgis.Critical)
        _cleanup_partial_venv(venv_dir)
        return False, f"Error: {str(e)[:200]}"


# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------


def _is_ssl_error(stderr):
    """Check if a pip error is SSL-related.

    Args:
        stderr: The stderr output from pip.

    Returns:
        True if the error is SSL-related.
    """
    ssl_markers = ["ssl", "certificate", "CERTIFICATE_VERIFY_FAILED"]
    lower = stderr.lower()
    return any(m.lower() in lower for m in ssl_markers)


def _is_network_error(stderr):
    """Check if a pip error is network-related.

    Args:
        stderr: The stderr output from pip.

    Returns:
        True if the error is network-related.
    """
    network_markers = [
        "ConnectionError",
        "connection refused",
        "connection reset",
        "timed out",
        "RemoteDisconnected",
        "NewConnectionError",
    ]
    return any(m.lower() in stderr.lower() for m in network_markers)


def install_dependencies(venv_dir=None, progress_callback=None, cancel_check=None):
    """Install required packages into the virtual environment.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.
        progress_callback: Function called with (percent, message).
        cancel_check: Function that returns True if operation should be cancelled.

    Returns:
        A tuple of (success: bool, message: str).
    """
    if venv_dir is None:
        venv_dir = VENV_DIR

    python_path = get_venv_python_path(venv_dir)
    if not os.path.exists(python_path):
        return False, "Virtual environment Python not found"

    env = _get_clean_env_for_venv()
    kwargs = _get_subprocess_kwargs()

    total = len(REQUIRED_PACKAGES)
    installed_count = 0

    for i, (package_name, version_spec) in enumerate(REQUIRED_PACKAGES):
        if cancel_check and cancel_check():
            return False, (
                f"Installation cancelled. "
                f"{installed_count}/{total} packages installed."
            )

        pkg_spec = f"{package_name}{version_spec}" if version_spec else package_name
        base_percent = int(20 + (i / total) * 70)  # 20-90% range

        if progress_callback:
            progress_callback(base_percent, f"Installing {package_name}...")

        cmd = [
            python_path,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--prefer-binary",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            pkg_spec,
        ]

        success, error_msg = _run_pip_install(
            cmd, env, kwargs, package_name, timeout=600
        )
        if not success:
            return False, error_msg

        installed_count += 1
        _log(f"Installed {package_name} ({installed_count}/{total})", Qgis.Success)

    if progress_callback:
        progress_callback(90, "All packages installed")

    return True, f"Successfully installed {installed_count} package(s)"


def _run_pip_install(cmd, env, kwargs, package_name, timeout=600):
    """Run a pip install command with retry logic.

    Args:
        cmd: The command list to execute.
        env: Environment dict for the subprocess.
        kwargs: Additional subprocess kwargs.
        package_name: Name of the package being installed (for logging).
        timeout: Timeout in seconds.

    Returns:
        A tuple of (success: bool, error_message: str).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            **kwargs,
        )

        if result.returncode == 0:
            return True, ""

        stderr = result.stderr or result.stdout or ""

        # Retry on SSL errors with --trusted-host
        if _is_ssl_error(stderr):
            _log(
                f"SSL error installing {package_name}, retrying with trusted hosts",
                Qgis.Warning,
            )
            retry_cmd = cmd + [
                "--trusted-host",
                "pypi.org",
                "--trusted-host",
                "files.pythonhosted.org",
            ]
            retry_result = subprocess.run(
                retry_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                **kwargs,
            )
            if retry_result.returncode == 0:
                return True, ""
            stderr = retry_result.stderr or retry_result.stdout or stderr

        # Retry on network errors with a delay
        if _is_network_error(stderr):
            _log(
                f"Network error installing {package_name}, retrying in 5s...",
                Qgis.Warning,
            )
            time.sleep(5)
            retry_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                **kwargs,
            )
            if retry_result.returncode == 0:
                return True, ""
            stderr = retry_result.stderr or retry_result.stdout or stderr

        # Classify the error for a user-friendly message
        return False, _classify_pip_error(package_name, stderr)

    except subprocess.TimeoutExpired:
        return False, (
            f"Installation of '{package_name}' timed out after "
            f"{timeout // 60} minutes."
        )
    except FileNotFoundError:
        return False, "Python executable not found in virtual environment."
    except Exception as e:
        return False, f"Unexpected error installing '{package_name}': {str(e)}"


def _classify_pip_error(package_name, stderr):
    """Classify a pip error into a user-friendly message.

    Args:
        package_name: Name of the package that failed.
        stderr: The stderr output from pip.

    Returns:
        A user-friendly error message string.
    """
    stderr_lower = stderr.lower()

    if "no matching distribution" in stderr_lower:
        return (
            f"Package '{package_name}' not found. "
            "Check your internet connection and try again."
        )
    if "permission" in stderr_lower or "denied" in stderr_lower:
        return (
            f"Permission denied installing '{package_name}'. "
            "Try running QGIS as administrator."
        )
    if "no space left" in stderr_lower:
        return f"Not enough disk space to install '{package_name}'."

    return f"Failed to install '{package_name}': {stderr[:300]}"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _get_verification_code(package_name):
    """Get functional test code for a package.

    Args:
        package_name: The package to generate test code for.

    Returns:
        A Python code string that tests the package.
    """
    if package_name == "earthaccess":
        return "import earthaccess; print(earthaccess.__version__)"
    elif package_name == "geopandas":
        return "import geopandas as gpd; " "print(gpd.__version__)"
    else:
        import_name = package_name.replace("-", "_")
        return f"import {import_name}"


def verify_venv(venv_dir=None, progress_callback=None):
    """Verify that all required packages work in the venv.

    Runs functional test code for each package in a subprocess to
    verify the venv is properly set up.

    Args:
        venv_dir: Optional venv directory path. Defaults to VENV_DIR.
        progress_callback: Function called with (percent, message).

    Returns:
        A tuple of (success: bool, message: str).
    """
    if venv_dir is None:
        venv_dir = VENV_DIR

    if not venv_exists(venv_dir):
        return False, "Virtual environment not found"

    python_path = get_venv_python_path(venv_dir)
    env = _get_clean_env_for_venv()
    kwargs = _get_subprocess_kwargs()

    total = len(REQUIRED_PACKAGES)
    for i, (package_name, _) in enumerate(REQUIRED_PACKAGES):
        if progress_callback:
            percent = int((i / total) * 100)
            progress_callback(percent, f"Verifying {package_name}... ({i + 1}/{total})")

        verify_code = _get_verification_code(package_name)
        cmd = [python_path, "-c", verify_code]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
                **kwargs,
            )

            if result.returncode != 0:
                error_detail = (
                    result.stderr[:300] if result.stderr else result.stdout[:300]
                )
                _log(
                    f"Package {package_name} verification failed: {error_detail}",
                    Qgis.Warning,
                )
                return False, (
                    f"Package {package_name} is broken: {error_detail[:200]}"
                )

        except subprocess.TimeoutExpired:
            _log(f"Verification of {package_name} timed out", Qgis.Warning)
            return False, f"Verification of {package_name} timed out"
        except Exception as e:
            _log(f"Failed to verify {package_name}: {str(e)}", Qgis.Warning)
            return False, f"Verification error: {package_name}"

    if progress_callback:
        progress_callback(100, "Verification complete")

    _log("Virtual environment verified successfully", Qgis.Success)
    return True, "Virtual environment ready"


# ---------------------------------------------------------------------------
# Runtime integration
# ---------------------------------------------------------------------------


def _set_proj_data(proj_dir):
    """Set PROJ_DATA and PROJ_LIB environment variables.

    Args:
        proj_dir: Path to the PROJ data directory.
    """
    os.environ["PROJ_DATA"] = proj_dir
    os.environ["PROJ_LIB"] = proj_dir
    _log(f"Set PROJ_DATA={proj_dir}")


def _ensure_proj_data():
    """Ensure PROJ_DATA / PROJ_LIB env vars point to a valid PROJ data dir.

    Venv packages like pyogrio need access to PROJ data files.  QGIS
    knows where these live, so we detect and propagate the path.
    Called BEFORE venv site-packages are on sys.path.
    """
    # If already set AND valid, nothing to do
    for var in ("PROJ_DATA", "PROJ_LIB"):
        val = os.environ.get(var)
        if val and os.path.isdir(val) and os.path.isfile(os.path.join(val, "proj.db")):
            return

    # Strategy 1: QGIS's pyproj (should be importable before venv is on path)
    try:
        import pyproj

        proj_dir = pyproj.datadir.get_data_dir()
        if proj_dir and os.path.isdir(proj_dir):
            _set_proj_data(proj_dir)
            return
    except Exception:
        pass

    # Strategy 2: sys.prefix/share/proj (conda / pixi / OSGeo4W)
    candidate = os.path.join(sys.prefix, "share", "proj")
    if os.path.isdir(candidate):
        _set_proj_data(candidate)
        return

    # Strategy 3: QgsApplication paths
    try:
        from qgis.core import QgsApplication

        for base in (QgsApplication.pkgDataPath(), QgsApplication.prefixPath()):
            for subdir in ("share/proj", "resources/proj", "proj"):
                candidate = os.path.join(base, subdir)
                if os.path.isdir(candidate):
                    _set_proj_data(candidate)
                    return
    except Exception:
        pass

    # Strategy 4: Search common system locations
    for candidate in (
        "/usr/share/proj",
        "/usr/local/share/proj",
    ):
        if os.path.isdir(candidate):
            _set_proj_data(candidate)
            return

    _log("Could not find PROJ data directory", Qgis.Warning)


def ensure_venv_packages_available():
    """Make venv packages importable by adding site-packages to sys.path.

    This should be called before importing any venv-installed packages
    (earthaccess, geopandas, etc.). Safe to call multiple times.

    Returns:
        True if venv packages are available, False otherwise.
    """
    if not venv_exists():
        python_path = get_venv_python_path()
        _log(
            f"Venv does not exist: expected Python at {python_path}",
            Qgis.Warning,
        )
        return False

    site_packages = get_venv_site_packages()
    if site_packages is None:
        _log(f"Venv site-packages not found in: {VENV_DIR}", Qgis.Warning)
        return False

    if site_packages not in sys.path:
        # Append (not insert at 0) so QGIS's built-in packages (pyproj,
        # numpy, etc.) keep priority.  Venv-only packages (earthaccess,
        # geopandas) are still found because QGIS doesn't ship them.
        sys.path.append(site_packages)
        _log(f"Added venv site-packages to sys.path: {site_packages}")

    # Ensure PROJ data is findable by venv packages (pyogrio, etc.)
    _ensure_proj_data()

    return True


# ---------------------------------------------------------------------------
# Status checking
# ---------------------------------------------------------------------------


def get_venv_status():
    """Get the status of the virtual environment installation.

    Returns:
        A tuple of (is_ready: bool, message: str).
    """
    from .python_manager import standalone_python_exists

    if not standalone_python_exists():
        return False, "Dependencies not installed"

    if not venv_exists():
        return False, "Virtual environment not configured"

    # Quick filesystem check for packages
    site_packages = get_venv_site_packages()
    if site_packages is None:
        return False, "Virtual environment incomplete"

    for package_name, _ in REQUIRED_PACKAGES:
        pkg_dir = os.path.join(site_packages, package_name)
        dist_info_pattern = package_name.replace("-", "_")
        has_pkg = os.path.exists(pkg_dir)
        has_dist = any(
            entry.startswith(dist_info_pattern) and entry.endswith(".dist-info")
            for entry in os.listdir(site_packages)
        )

        if not has_pkg and not has_dist:
            return False, f"Package {package_name} not found in venv"

    return True, "Virtual environment ready"


def check_dependencies():
    """Check if all required packages are installed and importable.

    Attempts to use importlib.metadata after ensuring venv packages
    are on sys.path. This is a lightweight check suitable for UI display.

    Returns:
        A tuple of (all_ok, missing, installed) where:
            all_ok: True if all required packages are installed.
            missing: List of (package_name, version_spec) for missing packages.
            installed: List of (package_name, version_string) for installed packages.
    """
    ensure_venv_packages_available()

    missing = []
    installed = []

    for package_name, version_spec in REQUIRED_PACKAGES:
        try:
            version = importlib.metadata.version(package_name)
            installed.append((package_name, version))
        except importlib.metadata.PackageNotFoundError:
            missing.append((package_name, version_spec))

    all_ok = len(missing) == 0
    return all_ok, missing, installed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def create_venv_and_install(progress_callback=None, cancel_check=None):
    """Complete installation: download Python + create venv + install packages.

    Progress breakdown:
        0-40%: Download Python standalone
        40-50%: Create virtual environment
        50-90%: Install packages
        90-100%: Verify installation

    Args:
        progress_callback: Function called with (percent, message).
        cancel_check: Function that returns True if operation should be cancelled.

    Returns:
        A tuple of (success: bool, message: str).
    """
    from .python_manager import (
        standalone_python_exists,
        download_python_standalone,
    )

    # Step 1: Download Python standalone if needed (0-40%)
    if not standalone_python_exists():
        _log("Downloading Python standalone...")

        def python_progress(percent, msg):
            if progress_callback:
                progress_callback(int(percent * 0.40), msg)

        success, msg = download_python_standalone(
            progress_callback=python_progress,
            cancel_check=cancel_check,
        )

        if not success:
            # Fallback: use QGIS's bundled Python (critical on Windows
            # where sys.executable may be qgis-bin.exe)
            fallback = _find_python_executable()
            if fallback and os.path.isfile(fallback):
                _log(
                    f"Standalone download failed, using system Python: {fallback}",
                    Qgis.Warning,
                )
            else:
                return False, f"Failed to download Python: {msg}"

        if cancel_check and cancel_check():
            return False, "Installation cancelled"
    else:
        _log("Python standalone already installed")
        if progress_callback:
            progress_callback(40, "Python standalone ready")

    # Step 2: Create venv if needed (40-50%)
    if venv_exists():
        _log("Virtual environment already exists")
        if progress_callback:
            progress_callback(50, "Virtual environment ready")
    else:

        def venv_progress(percent, msg):
            if progress_callback:
                progress_callback(40 + int(percent * 0.10), msg)

        success, msg = create_venv(progress_callback=venv_progress)
        if not success:
            return False, msg

        if cancel_check and cancel_check():
            return False, "Installation cancelled"

    # Step 3: Install dependencies (50-90%)
    def deps_progress(percent, msg):
        if progress_callback:
            # Map 20-90 range from install_dependencies to 50-90
            mapped = 50 + int((percent - 20) * (40.0 / 70.0))
            progress_callback(min(mapped, 90), msg)

    success, msg = install_dependencies(
        progress_callback=deps_progress,
        cancel_check=cancel_check,
    )

    if not success:
        return False, msg

    # Step 4: Verify installation (90-100%)
    def verify_progress(percent, msg):
        if progress_callback:
            mapped = 90 + int(percent * 0.10)
            progress_callback(min(mapped, 99), msg)

    is_valid, verify_msg = verify_venv(progress_callback=verify_progress)

    if not is_valid:
        return False, f"Verification failed: {verify_msg}"

    if progress_callback:
        progress_callback(100, "All dependencies installed")

    _log("All dependencies installed and verified", Qgis.Success)
    return True, "All dependencies installed successfully"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_old_venv_directories():
    """Remove old versioned venv directories (venv_py3.x) from previous layout.

    The plugin now uses a single ``venv/`` directory.  This helper removes
    leftover ``venv_py*`` directories created by earlier versions.

    Returns:
        A list of removed directory paths.
    """
    removed = []

    if not os.path.exists(CACHE_DIR):
        return removed

    try:
        for entry in os.listdir(CACHE_DIR):
            if entry.lower().startswith("venv_py"):
                old_path = os.path.join(CACHE_DIR, entry)
                if os.path.isdir(old_path):
                    try:
                        shutil.rmtree(old_path)
                        _log(f"Cleaned up old venv: {old_path}")
                        removed.append(old_path)
                    except Exception as e:
                        _log(
                            f"Failed to remove old venv {old_path}: {e}",
                            Qgis.Warning,
                        )
    except Exception as e:
        _log(f"Error scanning for old venvs: {e}", Qgis.Warning)

    return removed
