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
import subprocess  # nosec B404
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

ASSISTANT_PACKAGES = [
    ("geoagent", "[providers]>=1.0.0"),
    ("openai", ">=1.0"),
    ("anthropic", ">=0.40"),
    ("google-genai", ">=1.0"),
    ("ollama", ">=0.3"),
    ("strands-agents", "[litellm]>=1.37"),
]

INSTALL_PACKAGES = REQUIRED_PACKAGES + ASSISTANT_PACKAGES


def _log(message, level=Qgis.MessageLevel.Info):
    """Log a message to the QGIS message log.

    Args:
        message: The message to log.
        level: The log level (Qgis.MessageLevel.Info, Qgis.MessageLevel.Warning, Qgis.MessageLevel.Critical).
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


def _is_python_executable_name(path: str) -> bool:
    """Return True when a path name looks like a Python interpreter."""
    name = os.path.basename(path).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("python", "python3"):
        return True
    if not name.startswith("python"):
        return False
    suffix = name[6:]
    if "-" in suffix:
        return False
    return suffix.isdigit() or (
        suffix.count(".") == 1 and all(part.isdigit() for part in suffix.split("."))
    )


def _is_macos_qgis_app_bundle_python(path: str) -> bool:
    """Return True for Python binaries inside a QGIS macOS .app bundle."""
    if not (platform.system() == "Darwin" or sys.platform == "darwin"):
        return False
    parts = os.path.abspath(path).split(os.sep)
    for idx, part in enumerate(parts):
        lower = part.lower()
        if not (lower.startswith("qgis") and lower.endswith(".app")):
            continue
        return idx + 1 < len(parts) and parts[idx + 1] == "Contents"
    return False


def _python_candidate_matches_runtime(path: str) -> bool:
    """Return True when a candidate is executable and matches QGIS Python."""
    if not path or not os.path.isfile(path) or not _is_python_executable_name(path):
        return False

    if _is_macos_qgis_app_bundle_python(path):
        return False
    try:
        result = subprocess.run(  # nosec B603
            [
                path,
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_get_clean_env_for_venv(),
            **_get_subprocess_kwargs(),
        )
    except Exception:
        return False
    runtime_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return result.returncode == 0 and result.stdout.strip() == runtime_version


def _contents_dir_from_path(path: str) -> Optional[str]:
    """Return the containing macOS app Contents directory for a path."""
    if not path:
        return None
    current = path if os.path.isdir(path) else os.path.dirname(path)
    for _ in range(8):
        if os.path.basename(current) == "Contents":
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _candidate_python_paths() -> List[str]:
    """Return possible Python interpreter paths for QGIS-bundled Python."""
    candidates = []
    exe_dir = os.path.dirname(sys.executable)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    names = (f"python{py_ver}", f"python{sys.version_info.major}", "python3", "python")

    for attr in ("_base_executable", "executable"):
        value = getattr(sys, attr, None)
        if value:
            candidates.append(value)

    for attr in ("_base_prefix", "base_prefix", "prefix", "exec_prefix"):
        prefix = getattr(sys, attr, None)
        if not prefix:
            continue
        candidates.extend([os.path.join(prefix, "python.exe")])
        candidates.extend(os.path.join(prefix, "bin", name) for name in names)
        candidates.extend(
            [
                os.path.join(prefix, "Versions", py_ver, "bin", "python3"),
                os.path.join(prefix, "Versions", "Current", "bin", "python3"),
            ]
        )

    candidates.extend(os.path.join(exe_dir, name) for name in names)
    candidates.extend(
        [os.path.join(exe_dir, "python.exe"), os.path.join(exe_dir, "python3.exe")]
    )

    apps_dir = os.path.join(os.path.dirname(exe_dir), "apps")
    if os.path.isdir(apps_dir):
        for entry in sorted(os.listdir(apps_dir), reverse=True):
            if entry.lower().startswith("python"):
                candidates.append(os.path.join(apps_dir, entry, "python.exe"))

    for root in [sys.executable, getattr(sys, "_base_executable", None), sys.prefix]:
        contents_dir = _contents_dir_from_path(root)
        if not contents_dir:
            continue
        candidates.extend(os.path.join(contents_dir, "MacOS", name) for name in names)
        candidates.extend(
            os.path.join(contents_dir, "MacOS", "bin", name) for name in names
        )
        candidates.extend(
            [
                os.path.join(
                    contents_dir,
                    "Frameworks",
                    "Python.framework",
                    "Versions",
                    py_ver,
                    "bin",
                    "python3",
                ),
                os.path.join(
                    contents_dir,
                    "Frameworks",
                    "Python.framework",
                    "Versions",
                    "Current",
                    "bin",
                    "python3",
                ),
                os.path.join(contents_dir, "Resources", "python", "bin", "python3"),
                os.path.join(
                    contents_dir,
                    "Resources",
                    "Python.app",
                    "Contents",
                    "MacOS",
                    "Python",
                ),
            ]
        )

    unique = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def _find_python_executable():
    """Find a real Python executable for venv creation."""
    candidates = _candidate_python_paths()
    for candidate in candidates:
        if _python_candidate_matches_runtime(candidate):
            return candidate

    candidates_text = "\n".join(f"  - {path}" for path in candidates)
    raise RuntimeError(
        "Could not find a Python executable matching the QGIS Python runtime.\n"
        f"QGIS sys.executable: {sys.executable}\n"
        f"Python version: {sys.version_info.major}.{sys.version_info.minor}\n"
        "Checked candidates:\n"
        f"{candidates_text or '  - none'}"
    )


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
            Qgis.MessageLevel.Warning,
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
            _log(
                f"Could not clean up partial venv: {venv_dir}",
                Qgis.MessageLevel.Warning,
            )


def create_venv(venv_dir=None, progress_callback=None):
    """Create a virtual environment using uv (preferred) or stdlib venv.

    When uv is available, uses ``uv venv`` which is faster and does not
    require pip to be bootstrapped inside the venv.  Falls back to
    ``python -m venv`` + ``ensurepip`` when uv is not available.

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

    system_python = None
    python_lookup_error = ""
    try:
        system_python = _get_system_python()
    except RuntimeError as exc:
        python_lookup_error = str(exc)
    if system_python:
        _log(f"Using Python: {system_python}")

    from .uv_manager import uv_exists, get_uv_path

    use_uv = uv_exists()

    if use_uv:
        uv_path = get_uv_path()
        uv_python = (
            system_python or f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        cmd = [uv_path, "venv"]
        if system_python is None:
            cmd.append("--managed-python")
        cmd += ["--python", uv_python, venv_dir]
        _log("Creating venv with uv")
    else:
        if system_python is None:
            return False, python_lookup_error
        cmd = [system_python, "-m", "venv", venv_dir]
        _log("Creating venv with stdlib venv")

    try:
        env = _get_clean_env_for_venv()
        kwargs = _get_subprocess_kwargs()

        result = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            **kwargs,
        )

        if result.returncode == 0:
            _log("Virtual environment created successfully", Qgis.MessageLevel.Success)

            # When using stdlib venv, ensure pip is available
            if not use_uv:
                pip_path = get_venv_pip_path(venv_dir)
                if not os.path.exists(pip_path):
                    _log("pip not found in venv, bootstrapping with ensurepip...")
                    python_in_venv = get_venv_python_path(venv_dir)
                    ensurepip_cmd = [
                        python_in_venv,
                        "-m",
                        "ensurepip",
                        "--upgrade",
                    ]
                    try:
                        ensurepip_result = subprocess.run(  # nosec B603
                            ensurepip_cmd,
                            capture_output=True,
                            text=True,
                            timeout=120,
                            env=env,
                            **kwargs,
                        )
                        if ensurepip_result.returncode == 0:
                            _log(
                                "pip bootstrapped via ensurepip",
                                Qgis.MessageLevel.Success,
                            )
                        else:
                            err = ensurepip_result.stderr or ensurepip_result.stdout
                            _log(
                                f"ensurepip failed: {err[:200]}",
                                Qgis.MessageLevel.Warning,
                            )
                            _cleanup_partial_venv(venv_dir)
                            return False, f"Failed to bootstrap pip: {err[:200]}"
                    except Exception as e:
                        _log(f"ensurepip exception: {e}", Qgis.MessageLevel.Warning)
                        _cleanup_partial_venv(venv_dir)
                        return False, f"Failed to bootstrap pip: {str(e)[:200]}"

            if progress_callback:
                progress_callback(20, "Virtual environment created")
            return True, "Virtual environment created"
        else:
            error_msg = (
                result.stderr or result.stdout or f"Return code {result.returncode}"
            )
            _log(f"Failed to create venv: {error_msg}", Qgis.MessageLevel.Critical)
            _cleanup_partial_venv(venv_dir)
            return False, f"Failed to create venv: {error_msg[:200]}"

    except subprocess.TimeoutExpired:
        _log("Virtual environment creation timed out", Qgis.MessageLevel.Critical)
        _cleanup_partial_venv(venv_dir)
        return False, "Virtual environment creation timed out"
    except FileNotFoundError:
        _log(
            f"Python executable not found: {system_python}", Qgis.MessageLevel.Critical
        )
        return False, f"Python not found: {system_python}"
    except Exception as e:
        _log(f"Exception during venv creation: {str(e)}", Qgis.MessageLevel.Critical)
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

    Uses uv when available for significantly faster installation,
    falling back to pip otherwise.

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

    from .uv_manager import uv_exists, get_uv_path

    use_uv = uv_exists()
    if use_uv:
        uv_path = get_uv_path()
        _log("Installing dependencies with uv")
    else:
        _log("Installing dependencies with pip")

    # Build the full list of package specs for batch installation
    pkg_specs = []
    pkg_names = []
    for package_name, version_spec in INSTALL_PACKAGES:
        pkg_spec = f"{package_name}{version_spec}" if version_spec else package_name
        pkg_specs.append(pkg_spec)
        pkg_names.append(package_name)

    if cancel_check and cancel_check():
        return False, "Installation cancelled."

    # Scale timeout with number of packages (600s per package)
    total = len(INSTALL_PACKAGES)
    timeout = 600 * total

    if progress_callback:
        progress_callback(20, f"Installing {', '.join(pkg_names)}...")

    if use_uv:
        cmd = [
            uv_path,
            "pip",
            "install",
            "--python",
            python_path,
            "--upgrade",
        ] + pkg_specs
        success, error_msg = _run_install(
            cmd,
            env,
            kwargs,
            timeout=timeout,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            installer="uv",
        )
    else:
        cmd = [
            python_path,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--prefer-binary",
            "--disable-pip-version-check",
            "--no-warn-script-location",
        ] + pkg_specs
        success, error_msg = _run_install(
            cmd,
            env,
            kwargs,
            timeout=timeout,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            installer="pip",
        )

    if not success:
        return False, error_msg

    _log(f"Installed {total} package(s)", Qgis.MessageLevel.Success)

    if progress_callback:
        progress_callback(90, "All packages installed")

    return True, f"Successfully installed {total} package(s)"


def _run_install_subprocess(
    cmd, env, kwargs, timeout, progress_callback=None, cancel_check=None
):
    """Run an install command with progress polling and cancellation support.

    Uses Popen to allow periodic progress updates and cancellation checks
    while the subprocess is running.

    Args:
        cmd: The command list to execute.
        env: Environment dict for the subprocess.
        kwargs: Additional subprocess kwargs.
        timeout: Timeout in seconds.
        progress_callback: Optional callback for progress updates (percent, msg).
        cancel_check: Optional function that returns True to cancel.

    Returns:
        A tuple of (returncode: int, stdout: str, stderr: str).
            returncode is -1 if cancelled, -2 if timed out.
    """
    proc = subprocess.Popen(  # nosec B603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        **kwargs,
    )
    start = time.time()
    poll_interval = 2  # seconds
    # Progress ticks from 25% to 85% over the timeout period
    while True:
        try:
            proc.wait(timeout=poll_interval)
            # Process finished
            break
        except subprocess.TimeoutExpired:
            pass

        # Check cancellation
        if cancel_check and cancel_check():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return -1, "", "Installation cancelled by user."

        # Check overall timeout
        elapsed = time.time() - start
        if elapsed >= timeout:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return -2, "", f"Timed out after {timeout // 60} minutes."

        # Emit intermediate progress (25-85% range based on elapsed time)
        if progress_callback:
            fraction = min(elapsed / timeout, 1.0)
            percent = int(25 + fraction * 60)
            progress_callback(percent, "Installing packages...")

    stdout = proc.stdout.read() if proc.stdout else ""
    stderr = proc.stderr.read() if proc.stderr else ""
    return proc.returncode, stdout, stderr


def _run_install(
    cmd,
    env,
    kwargs,
    timeout=600,
    progress_callback=None,
    cancel_check=None,
    installer="pip",
):
    """Run a pip/uv install command with retry logic.

    Args:
        cmd: The command list to execute.
        env: Environment dict for the subprocess.
        kwargs: Additional subprocess kwargs.
        timeout: Timeout in seconds.
        progress_callback: Optional callback for progress updates (percent, msg).
        cancel_check: Optional function that returns True to cancel.
        installer: "pip" or "uv", used for retry flags and logging.

    Returns:
        A tuple of (success: bool, error_message: str).
    """
    try:
        returncode, stdout, stderr = _run_install_subprocess(
            cmd,
            env,
            kwargs,
            timeout,
            progress_callback,
            cancel_check,
        )

        if returncode == -1:
            return False, "Installation cancelled."
        if returncode == -2:
            return False, (f"Installation timed out after {timeout // 60} minutes.")
        if returncode == 0:
            return True, ""

        stderr = stderr or stdout or ""

        # Retry on SSL errors
        if _is_ssl_error(stderr):
            if installer == "uv":
                ssl_flags = [
                    "--allow-insecure-host",
                    "pypi.org",
                    "--allow-insecure-host",
                    "files.pythonhosted.org",
                ]
            else:
                ssl_flags = [
                    "--trusted-host",
                    "pypi.org",
                    "--trusted-host",
                    "files.pythonhosted.org",
                ]
            _log(
                f"SSL error installing dependencies via {installer}, "
                f"retrying with trusted hosts",
                Qgis.MessageLevel.Warning,
            )
            retry_cmd = cmd + ssl_flags
            returncode, stdout, retry_stderr = _run_install_subprocess(
                retry_cmd,
                env,
                kwargs,
                timeout,
                progress_callback,
                cancel_check,
            )
            if returncode == -1:
                return False, "Installation cancelled."
            if returncode == 0:
                return True, ""
            stderr = retry_stderr or stderr

        # Retry on network errors with a delay
        if _is_network_error(stderr):
            _log(
                f"Network error installing dependencies via {installer}, "
                f"retrying in 5s...",
                Qgis.MessageLevel.Warning,
            )
            time.sleep(5)
            returncode, stdout, retry_stderr = _run_install_subprocess(
                cmd,
                env,
                kwargs,
                timeout,
                progress_callback,
                cancel_check,
            )
            if returncode == -1:
                return False, "Installation cancelled."
            if returncode == 0:
                return True, ""
            stderr = retry_stderr or stderr

        # Classify the error for a user-friendly message
        return False, _classify_pip_error(stderr)

    except FileNotFoundError:
        if installer == "uv":
            return False, "uv executable not found."
        return False, "Python executable not found in virtual environment."
    except Exception as e:
        return False, f"Unexpected error installing dependencies: {str(e)}"


def _classify_pip_error(stderr):
    """Classify a pip/uv error into a user-friendly message.

    Args:
        stderr: The stderr output from pip/uv.

    Returns:
        A user-friendly error message string.
    """
    stderr_lower = stderr.lower()

    if "no matching distribution" in stderr_lower:
        return (
            "A required package was not found. "
            "Check your internet connection and try again."
        )
    if "permission" in stderr_lower or "denied" in stderr_lower:
        return (
            "Permission denied installing dependencies. "
            "Try running QGIS as administrator."
        )
    if "no space left" in stderr_lower:
        return "Not enough disk space to install dependencies."

    return f"Failed to install dependencies: {stderr[:300]}"


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
            result = subprocess.run(  # nosec B603
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
                    Qgis.MessageLevel.Warning,
                )
                return False, (
                    f"Package {package_name} is broken: {error_detail[:200]}"
                )

        except subprocess.TimeoutExpired:
            _log(f"Verification of {package_name} timed out", Qgis.MessageLevel.Warning)
            return False, f"Verification of {package_name} timed out"
        except Exception as e:
            _log(
                f"Failed to verify {package_name}: {str(e)}", Qgis.MessageLevel.Warning
            )
            return False, f"Verification error: {package_name}"

    if progress_callback:
        progress_callback(100, "Verification complete")

    _log("Virtual environment verified successfully", Qgis.MessageLevel.Success)
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
        pass  # nosec B110

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
        pass  # nosec B110

    # Strategy 4: Search common system locations
    for candidate in (
        "/usr/share/proj",
        "/usr/local/share/proj",
    ):
        if os.path.isdir(candidate):
            _set_proj_data(candidate)
            return

    _log("Could not find PROJ data directory", Qgis.MessageLevel.Warning)


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
            Qgis.MessageLevel.Warning,
        )
        return False

    site_packages = get_venv_site_packages()
    if site_packages is None:
        _log(f"Venv site-packages not found in: {VENV_DIR}", Qgis.MessageLevel.Warning)
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


def import_earthaccess():
    """Import ``earthaccess`` with a clear message when import fails.

    Returns:
        The imported ``earthaccess`` module.

    Raises:
        ImportError: If ``earthaccess`` is missing or installed but broken.
    """
    ensure_venv_packages_available()

    try:
        import earthaccess  # noqa: WPS433 - intentional runtime import

        return earthaccess
    except ImportError as exc:
        try:
            version = importlib.metadata.version("earthaccess")
        except importlib.metadata.PackageNotFoundError:
            raise ImportError(
                "earthaccess package not installed - "
                "please run Install Dependencies in Settings."
            ) from exc

        message = (
            f"earthaccess {version} is installed but failed to import: {exc}. "
            "Try Install Dependencies in Settings."
        )
        _log(
            f"{message} QGIS Python: {sys.version}",
            Qgis.MessageLevel.Critical,
        )
        raise ImportError(message) from exc


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


def check_dependencies(include_assistant=False):
    """Check if all required packages are installed.

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

    packages = INSTALL_PACKAGES if include_assistant else REQUIRED_PACKAGES
    for package_name, version_spec in packages:
        try:
            version = importlib.metadata.version(package_name)
            installed.append((package_name, version))
        except importlib.metadata.PackageNotFoundError:
            missing.append((package_name, version_spec))

    all_ok = len(missing) == 0
    return all_ok, missing, installed


def assistant_dependencies_met():
    """Return True when GeoAgent and provider packages are installed."""
    all_ok, _missing, _installed = check_dependencies(include_assistant=True)
    return all_ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def create_venv_and_install(progress_callback=None, cancel_check=None):
    """Complete installation: download Python + download uv + create venv + install.

    Progress breakdown:
        0-35%: Download Python standalone
        35-40%: Download uv package installer
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
    from .uv_manager import uv_exists, download_uv

    start_time = time.time()

    # Step 1: Download Python standalone if needed (0-35%)
    if not standalone_python_exists():
        _log("Downloading Python standalone...")

        def python_progress(percent, msg):
            if progress_callback:
                progress_callback(int(percent * 0.35), msg)

        success, msg = download_python_standalone(
            progress_callback=python_progress,
            cancel_check=cancel_check,
        )

        if not success:
            # Fallback: use QGIS's bundled Python (critical on Windows
            # where sys.executable may be qgis-bin.exe)
            try:
                fallback = _find_python_executable()
            except RuntimeError as exc:
                _log(str(exc), Qgis.MessageLevel.Warning)
                fallback = None
            if fallback and os.path.isfile(fallback):
                _log(
                    f"Standalone download failed, using system Python: {fallback}",
                    Qgis.MessageLevel.Warning,
                )
            else:
                return False, f"Failed to download Python: {msg}"

        if cancel_check and cancel_check():
            return False, "Installation cancelled"
    else:
        _log("Python standalone already installed")
        if progress_callback:
            progress_callback(35, "Python standalone ready")

    # Step 1b: Download uv package installer if needed (35-40%)
    if not uv_exists():
        _log("Downloading uv package installer...")

        def uv_progress(percent, msg):
            if progress_callback:
                progress_callback(35 + int(percent * 0.05), msg)

        success, msg = download_uv(
            progress_callback=uv_progress,
            cancel_check=cancel_check,
        )

        if not success:
            # Non-fatal: fall back to pip for venv creation and installation
            _log(
                f"uv download failed ({msg}), will use pip instead",
                Qgis.MessageLevel.Warning,
            )
        else:
            _log("uv package installer ready")

        if cancel_check and cancel_check():
            return False, "Installation cancelled"
    else:
        _log("uv already installed")
        if progress_callback:
            progress_callback(40, "uv ready")

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

    elapsed = time.time() - start_time
    if elapsed >= 60:
        minutes, seconds = divmod(int(elapsed), 60)
        elapsed_str = f"{minutes}:{seconds:02d}"
    else:
        elapsed_str = f"{elapsed:.1f}s"

    if progress_callback:
        progress_callback(100, f"All dependencies installed in {elapsed_str}")

    _log(
        f"All dependencies installed and verified in {elapsed_str}",
        Qgis.MessageLevel.Success,
    )
    return True, f"All dependencies installed successfully in {elapsed_str}"


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
                            Qgis.MessageLevel.Warning,
                        )
    except Exception as e:
        _log(f"Error scanning for old venvs: {e}", Qgis.MessageLevel.Warning)

    return removed
