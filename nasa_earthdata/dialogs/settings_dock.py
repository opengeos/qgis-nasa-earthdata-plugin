"""
Settings Dock Widget for NASA Earthdata Plugin

This module provides a settings panel for configuring NASA Earthdata
credentials and plugin preferences.
"""

import os
import platform
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QSettings, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QGroupBox,
    QComboBox,
    QSpinBox,
    QCheckBox,
    QFormLayout,
    QMessageBox,
    QFileDialog,
    QTabWidget,
    QProgressBar,
)
from qgis.PyQt.QtGui import QFont

from .chat_dock import DEFAULT_MODELS, PROVIDERS


def _enum_value(cls, enum_name, member_name):
    """Return an enum member from either scoped or legacy Qt APIs."""
    container = getattr(cls, enum_name, cls)
    return getattr(container, member_name)


class SettingsDockWidget(QDockWidget):
    """A settings panel for configuring NASA Earthdata plugin options."""

    # Emitted when dependencies are successfully installed
    deps_installed = pyqtSignal()

    # Settings keys
    SETTINGS_PREFIX = "NASAEarthdata/"

    def __init__(self, iface, parent=None):
        """Initialize the settings dock widget.

        Args:
            iface: QGIS interface instance.
            parent: Parent widget.
        """
        super().__init__("NASA Earthdata Settings", parent)
        self.iface = iface
        self.settings = QSettings()
        self._deps_worker = None

        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        """Set up the settings UI."""
        # Main widget
        main_widget = QWidget()
        self.setWidget(main_widget)

        # Main layout
        layout = QVBoxLayout(main_widget)
        layout.setSpacing(10)

        # Header
        header_label = QLabel("NASA Earthdata Settings")
        header_font = QFont()
        header_font.setPointSize(11)
        header_font.setBold(True)
        header_label.setFont(header_font)
        header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header_label)

        # Tab widget for organized settings
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # Dependencies tab (first tab)
        deps_tab = self._create_dependencies_tab()
        self.tab_widget.addTab(deps_tab, "Dependencies")

        # Credentials tab
        credentials_tab = self._create_credentials_tab()
        self.tab_widget.addTab(credentials_tab, "Credentials")

        # Model tab
        model_tab = self._create_model_tab()
        self.tab_widget.addTab(model_tab, "Model")

        # General settings tab
        general_tab = self._create_general_tab()
        self.tab_widget.addTab(general_tab, "General")

        # Advanced settings tab
        advanced_tab = self._create_advanced_tab()
        self.tab_widget.addTab(advanced_tab, "Advanced")

        # Buttons
        button_layout = QHBoxLayout()

        self.save_btn = QPushButton("Save Settings")
        self.save_btn.setStyleSheet("background-color: #0B3D91; color: white;")
        self.save_btn.clicked.connect(self._save_settings)
        button_layout.addWidget(self.save_btn)

        self.reset_btn = QPushButton("Reset Defaults")
        self.reset_btn.clicked.connect(self._reset_defaults)
        button_layout.addWidget(self.reset_btn)

        layout.addLayout(button_layout)

        # Stretch at the end
        layout.addStretch()

        # Status label
        self.status_label = QLabel("Settings loaded")
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.status_label)

    def _create_credentials_tab(self):
        """Create the credentials settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # NASA Earthdata credentials group
        creds_group = QGroupBox("NASA Earthdata Login")
        creds_layout = QFormLayout(creds_group)

        # Info label
        info_label = QLabel(
            "Enter your NASA Earthdata credentials.\n"
            "Register at: https://urs.earthdata.nasa.gov/"
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; font-size: 10px;")
        creds_layout.addRow(info_label)

        # Username
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("NASA Earthdata username")
        creds_layout.addRow("Username:", self.username_input)

        # Password
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("NASA Earthdata password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        creds_layout.addRow("Password:", self.password_input)

        # Test credentials button
        self.test_creds_btn = QPushButton("Test Credentials")
        self.test_creds_btn.clicked.connect(self._test_credentials)
        creds_layout.addRow("", self.test_creds_btn)

        # Credentials status
        self.creds_status_label = QLabel("")
        self.creds_status_label.setWordWrap(True)
        creds_layout.addRow("Status:", self.creds_status_label)

        layout.addWidget(creds_group)

        # Netrc file group
        netrc_group = QGroupBox(".netrc File")
        netrc_layout = QFormLayout(netrc_group)

        netrc_info = QLabel(
            "Alternatively, you can use a .netrc file for authentication.\n"
            "The file should be located at: ~/.netrc"
        )
        netrc_info.setWordWrap(True)
        netrc_info.setStyleSheet("color: #666; font-size: 10px;")
        netrc_layout.addRow(netrc_info)

        # Check netrc
        self.check_netrc_btn = QPushButton("Check .netrc File")
        self.check_netrc_btn.clicked.connect(self._check_netrc)
        netrc_layout.addRow("", self.check_netrc_btn)

        self.netrc_status_label = QLabel("")
        self.netrc_status_label.setWordWrap(True)
        netrc_layout.addRow("Status:", self.netrc_status_label)

        layout.addWidget(netrc_group)

        layout.addStretch()
        return widget

    def _create_model_tab(self):
        """Create the GeoAgent model provider and credential tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        model_group = QGroupBox("GeoAgent Provider")
        form = QFormLayout(model_group)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(PROVIDERS)
        self.provider_combo.setMinimumContentsLength(10)
        self.provider_combo.setSizeAdjustPolicy(
            _enum_value(
                QComboBox,
                "SizeAdjustPolicy",
                "AdjustToMinimumContentsLengthWithIcon",
            )
        )
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        form.addRow("Provider:", self.provider_combo)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("Provider default")
        form.addRow("Model:", self.model_input)

        self.fast_check = QCheckBox("Use fast GeoAgent prompt")
        form.addRow("", self.fast_check)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 32768)
        self.max_tokens_spin.setValue(4096)
        self.max_tokens_spin.setSingleStep(256)
        form.addRow("Max tokens:", self.max_tokens_spin)

        layout.addWidget(model_group)

        credentials_group = QGroupBox("Provider Credentials and Hosts")
        credentials_form = QFormLayout(credentials_group)

        password_mode = getattr(getattr(QLineEdit, "EchoMode", QLineEdit), "Password")

        self.openai_key_input = QLineEdit()
        self.openai_key_input.setEchoMode(password_mode)
        credentials_form.addRow("OpenAI API key:", self.openai_key_input)

        self.anthropic_key_input = QLineEdit()
        self.anthropic_key_input.setEchoMode(password_mode)
        credentials_form.addRow("Anthropic API key:", self.anthropic_key_input)

        self.gemini_key_input = QLineEdit()
        self.gemini_key_input.setEchoMode(password_mode)
        credentials_form.addRow("Gemini API key:", self.gemini_key_input)

        self.aws_region_input = QLineEdit()
        self.aws_region_input.setPlaceholderText("e.g. us-east-1")
        credentials_form.addRow("AWS region:", self.aws_region_input)

        self.ollama_host_input = QLineEdit()
        self.ollama_host_input.setPlaceholderText("http://127.0.0.1:11434")
        credentials_form.addRow("Ollama host:", self.ollama_host_input)

        self.litellm_key_input = QLineEdit()
        self.litellm_key_input.setEchoMode(password_mode)
        credentials_form.addRow("LiteLLM API key:", self.litellm_key_input)

        self.litellm_base_url_input = QLineEdit()
        self.litellm_base_url_input.setPlaceholderText("https://proxy.example.com")
        credentials_form.addRow("LiteLLM base URL:", self.litellm_base_url_input)

        layout.addWidget(credentials_group)

        note = QLabel(
            "These values are used by the NASA Earthdata AI Assistant and saved "
            "in QGIS settings."
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(note)
        layout.addStretch()
        return widget

    def _create_general_tab(self):
        """Create the general settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Download settings group
        download_group = QGroupBox("Download Settings")
        download_layout = QFormLayout(download_group)

        # Default download directory
        dir_layout = QHBoxLayout()
        self.download_dir_input = QLineEdit()
        self.download_dir_input.setPlaceholderText("Default download directory...")
        dir_layout.addWidget(self.download_dir_input)
        self.download_dir_btn = QPushButton("...")
        self.download_dir_btn.setMaximumWidth(30)
        self.download_dir_btn.clicked.connect(self._browse_download_dir)
        dir_layout.addWidget(self.download_dir_btn)
        download_layout.addRow("Download Directory:", dir_layout)

        # Download threads
        self.download_threads_spin = QSpinBox()
        self.download_threads_spin.setRange(1, 16)
        self.download_threads_spin.setValue(4)
        download_layout.addRow("Download Threads:", self.download_threads_spin)

        layout.addWidget(download_group)

        # Display settings group
        display_group = QGroupBox("Display Settings")
        display_layout = QFormLayout(display_group)

        # Default max items
        self.default_max_items_spin = QSpinBox()
        self.default_max_items_spin.setRange(10, 500)
        self.default_max_items_spin.setValue(50)
        display_layout.addRow("Default Max Items:", self.default_max_items_spin)

        # Auto-zoom to footprints
        self.auto_zoom_check = QCheckBox()
        self.auto_zoom_check.setChecked(True)
        display_layout.addRow("Auto-zoom to Results:", self.auto_zoom_check)

        # Show notifications
        self.notifications_check = QCheckBox()
        self.notifications_check.setChecked(True)
        display_layout.addRow("Show Notifications:", self.notifications_check)

        layout.addWidget(display_group)

        layout.addStretch()
        return widget

    def _create_advanced_tab(self):
        """Create the advanced settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Data source group
        source_group = QGroupBox("Data Source")
        source_layout = QFormLayout(source_group)

        # NASA data catalog URL
        self.catalog_url_input = QLineEdit()
        self.catalog_url_input.setText(
            "https://github.com/opengeos/NASA-Earth-Data/raw/main/nasa_earth_data.tsv"
        )
        source_layout.addRow("Catalog URL:", self.catalog_url_input)

        layout.addWidget(source_group)

        # Cache settings group
        cache_group = QGroupBox("Cache Settings")
        cache_layout = QFormLayout(cache_group)

        # Enable cache
        self.enable_cache_check = QCheckBox()
        self.enable_cache_check.setChecked(True)
        cache_layout.addRow("Enable Cache:", self.enable_cache_check)

        # Cache directory
        cache_layout_h = QHBoxLayout()
        self.cache_dir_input = QLineEdit()
        self.cache_dir_input.setPlaceholderText("Cache directory...")
        cache_layout_h.addWidget(self.cache_dir_input)
        self.cache_dir_btn = QPushButton("...")
        self.cache_dir_btn.setMaximumWidth(30)
        self.cache_dir_btn.clicked.connect(self._browse_cache_dir)
        cache_layout_h.addWidget(self.cache_dir_btn)
        cache_layout.addRow("Cache Directory:", cache_layout_h)

        # Clear cache button
        self.clear_cache_btn = QPushButton("Clear Cache")
        self.clear_cache_btn.clicked.connect(self._clear_cache)
        cache_layout.addRow("", self.clear_cache_btn)

        layout.addWidget(cache_group)

        # Debug group
        debug_group = QGroupBox("Debug")
        debug_layout = QFormLayout(debug_group)

        # Debug mode
        self.debug_check = QCheckBox()
        self.debug_check.setChecked(False)
        debug_layout.addRow("Debug Mode:", self.debug_check)

        layout.addWidget(debug_group)

        layout.addStretch()
        return widget

    def _create_dependencies_tab(self):
        """Create the dependencies management tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Info label
        info_label = QLabel(
            "This plugin requires additional Python packages.\n"
            "Click 'Install Dependencies' to install them in an\n"
            "isolated virtual environment."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("font-size: 10px;")
        layout.addWidget(info_label)

        # Package status group
        status_group = QGroupBox("Package Status")
        self._deps_status_layout = QFormLayout(status_group)

        # Create status labels for each package, grouped by required vs optional.
        self._deps_labels = {}
        from ..core.venv_manager import ASSISTANT_PACKAGES, REQUIRED_PACKAGES

        required_header = QLabel("Required (NASA Earthdata core):")
        required_header.setStyleSheet("font-weight: bold;")
        self._deps_status_layout.addRow(required_header)
        for package_name, _version_spec in REQUIRED_PACKAGES:
            label = QLabel("Checking...")
            label.setStyleSheet("color: gray;")
            self._deps_labels[package_name] = label
            self._deps_status_layout.addRow(f"{package_name}:", label)

        assistant_header = QLabel("AI Assistant (optional):")
        assistant_header.setStyleSheet("font-weight: bold; margin-top: 6px;")
        self._deps_status_layout.addRow(assistant_header)
        for package_name, _version_spec in ASSISTANT_PACKAGES:
            label = QLabel("Checking...")
            label.setStyleSheet("color: gray;")
            self._deps_labels[package_name] = label
            self._deps_status_layout.addRow(f"{package_name}:", label)

        layout.addWidget(status_group)

        # Install button
        self.install_deps_btn = QPushButton("Install Dependencies")
        self.install_deps_btn.setStyleSheet("background-color: #0B3D91; color: white;")
        self.install_deps_btn.clicked.connect(self._install_dependencies)
        layout.addWidget(self.install_deps_btn)

        # Progress bar (hidden by default)
        self.deps_progress_bar = QProgressBar()
        self.deps_progress_bar.setVisible(False)
        layout.addWidget(self.deps_progress_bar)

        # Progress/status label
        self.deps_progress_label = QLabel("")
        self.deps_progress_label.setWordWrap(True)
        self.deps_progress_label.setVisible(False)
        layout.addWidget(self.deps_progress_label)

        # Cancel button (hidden by default)
        self.cancel_deps_btn = QPushButton("Cancel")
        self.cancel_deps_btn.setStyleSheet("color: red;")
        self.cancel_deps_btn.setVisible(False)
        self.cancel_deps_btn.clicked.connect(self._cancel_deps_install)
        layout.addWidget(self.cancel_deps_btn)

        # Refresh button
        self.refresh_deps_btn = QPushButton("Refresh Status")
        self.refresh_deps_btn.clicked.connect(self._refresh_deps_status)
        layout.addWidget(self.refresh_deps_btn)

        layout.addStretch()

        # Initial status check
        self._refresh_deps_status()

        return widget

    def _refresh_deps_status(self):
        """Refresh the dependency status display."""
        from ..core.venv_manager import check_dependencies

        required_ok, _, _ = check_dependencies(include_assistant=False)
        all_ok, missing, installed = check_dependencies(include_assistant=True)

        for package_name, version in installed:
            if package_name in self._deps_labels:
                self._deps_labels[package_name].setText(f"v{version} (installed)")
                self._deps_labels[package_name].setStyleSheet(
                    "color: green; font-weight: bold;"
                )

        for package_name, _version_spec in missing:
            if package_name in self._deps_labels:
                self._deps_labels[package_name].setText("Not installed")
                self._deps_labels[package_name].setStyleSheet("color: red;")

        self.install_deps_btn.setEnabled(not all_ok)
        if all_ok:
            self.install_deps_btn.setText("All Dependencies Installed")
        elif required_ok:
            self.install_deps_btn.setText(
                f"Install AI Assistant Packages ({len(missing)} missing)"
            )
        else:
            self.install_deps_btn.setText(
                f"Install Dependencies ({len(missing)} missing)"
            )

    def _install_dependencies(self):
        """Start installing missing dependencies."""
        from .deps_manager import DepsInstallWorker

        # Guard against concurrent installs
        if self._deps_worker is not None and self._deps_worker.isRunning():
            return

        # Update UI for installation mode
        self.install_deps_btn.setEnabled(False)
        self.refresh_deps_btn.setEnabled(False)
        self.deps_progress_bar.setVisible(True)
        self.deps_progress_bar.setRange(0, 100)
        self.deps_progress_bar.setValue(0)
        self.deps_progress_label.setVisible(True)
        self.deps_progress_label.setText("Starting installation...")
        self.deps_progress_label.setStyleSheet("")
        self.cancel_deps_btn.setVisible(True)
        self.cancel_deps_btn.setEnabled(True)

        # Start worker
        self._deps_worker = DepsInstallWorker()
        self._deps_worker.progress.connect(self._on_deps_progress)
        self._deps_worker.finished.connect(self._on_deps_finished)
        self._deps_worker.start()

    def _on_deps_progress(self, percent, message):
        """Handle progress updates from the dependency install worker.

        Args:
            percent: Installation progress percentage (0-100).
            message: Status message describing current operation.
        """
        self.deps_progress_bar.setValue(percent)
        self.deps_progress_label.setText(message)

    def _on_deps_finished(self, success, message):
        """Handle completion of the dependency installation.

        Args:
            success: True if all packages installed successfully.
            message: Summary message.
        """
        # Reset UI
        self.deps_progress_bar.setVisible(False)
        self.deps_progress_label.setText(message)
        self.cancel_deps_btn.setVisible(False)
        self.refresh_deps_btn.setEnabled(True)

        if success:
            self.deps_progress_label.setStyleSheet("color: green;")
            self.iface.messageBar().pushSuccess(
                "NASA Earthdata", "Dependencies installed successfully!"
            )
            self.deps_installed.emit()
        else:
            self.deps_progress_label.setStyleSheet("color: red;")
            self.install_deps_btn.setEnabled(True)

        # Refresh status display
        self._refresh_deps_status()

    def _cancel_deps_install(self):
        """Cancel the ongoing dependency installation."""
        if self._deps_worker is not None and self._deps_worker.isRunning():
            self._deps_worker.cancel()
            self.cancel_deps_btn.setEnabled(False)
            self.deps_progress_label.setText("Cancelling...")

    def show_dependencies_tab(self):
        """Switch to the Dependencies tab and refresh status."""
        self.tab_widget.setCurrentIndex(0)
        self._refresh_deps_status()

    def _browse_download_dir(self):
        """Open directory browser for download directory."""
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self.download_dir_input.text() or ""
        )
        if dir_path:
            self.download_dir_input.setText(dir_path)

    def _browse_cache_dir(self):
        """Open directory browser for cache directory."""
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Cache Directory", self.cache_dir_input.text() or ""
        )
        if dir_path:
            self.cache_dir_input.setText(dir_path)

    def _test_credentials(self):
        """Test NASA Earthdata credentials."""
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()

        if not username or not password:
            self.creds_status_label.setText("Please enter username and password")
            self.creds_status_label.setStyleSheet("color: orange;")
            return

        self.creds_status_label.setText("Testing credentials...")
        self.creds_status_label.setStyleSheet("color: blue;")

        try:
            from ..core.venv_manager import import_earthaccess

            earthaccess = import_earthaccess()

            # Set environment variables for earthaccess
            os.environ["EARTHDATA_USERNAME"] = username
            os.environ["EARTHDATA_PASSWORD"] = password

            # Try to authenticate (persist=True writes to .netrc as well)
            auth = earthaccess.login(strategy="environment", persist=True)

            if auth.authenticated:
                # Save credentials to .netrc file for persistent authentication
                self._save_netrc(username, password)
                self.creds_status_label.setText("✓ Credentials valid! Saved to .netrc")
                self.creds_status_label.setStyleSheet(
                    "color: green; font-weight: bold;"
                )
                # Update netrc status
                self._check_netrc()
            else:
                self.creds_status_label.setText("✗ Authentication failed")
                self.creds_status_label.setStyleSheet("color: red;")

        except ImportError as e:
            self.creds_status_label.setText(str(e)[:200])
            self.creds_status_label.setStyleSheet("color: red;")
        except Exception as e:
            self.creds_status_label.setText(f"Error: {str(e)[:50]}")
            self.creds_status_label.setStyleSheet("color: red;")

    def _save_netrc(self, username, password):
        """Save NASA Earthdata credentials to .netrc file."""
        netrc_path = Path.home() / ".netrc"
        earthdata_host = "urs.earthdata.nasa.gov"

        # Read existing .netrc content if it exists
        existing_content = ""
        if netrc_path.exists():
            try:
                with open(netrc_path, "r") as f:
                    existing_content = f.read()
            except Exception:
                pass  # nosec B110

        # Parse existing entries, excluding any existing earthdata entry
        lines = existing_content.strip().split("\n") if existing_content.strip() else []
        new_lines = []
        skip_until_next_machine = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("machine"):
                # Check if this is the earthdata entry
                if earthdata_host in stripped:
                    skip_until_next_machine = True
                    continue
                else:
                    skip_until_next_machine = False

            if not skip_until_next_machine:
                new_lines.append(line)

        # Add new earthdata entry
        earthdata_entry = f"\nmachine {earthdata_host}\n    login {username}\n    password {password}\n"

        # Write the updated .netrc file
        try:
            with open(netrc_path, "w") as f:
                if new_lines:
                    f.write("\n".join(new_lines))
                f.write(earthdata_entry)

            # Set proper permissions (readable/writable only by owner)
            if platform.system() != "Windows":
                import stat

                os.chmod(netrc_path, stat.S_IRUSR | stat.S_IWUSR)

        except Exception as e:
            raise Exception(f"Failed to save .netrc: {e}")

    def _check_netrc(self):
        """Check if .netrc file exists and contains Earthdata credentials."""
        netrc_path = Path.home() / ".netrc"

        if not netrc_path.exists():
            self.netrc_status_label.setText("✗ .netrc file not found")
            self.netrc_status_label.setStyleSheet("color: orange;")
            return

        try:
            import netrc

            auths = netrc.netrc(str(netrc_path))
            earthdata_auth = auths.authenticators("urs.earthdata.nasa.gov")

            if earthdata_auth:
                username = earthdata_auth[0]
                self.netrc_status_label.setText(
                    f"✓ Found Earthdata credentials for: {username}"
                )
                self.netrc_status_label.setStyleSheet(
                    "color: green; font-weight: bold;"
                )
            else:
                self.netrc_status_label.setText("✗ No Earthdata credentials in .netrc")
                self.netrc_status_label.setStyleSheet("color: orange;")

        except Exception as e:
            self.netrc_status_label.setText(f"Error reading .netrc: {str(e)[:30]}")
            self.netrc_status_label.setStyleSheet("color: red;")

    def _get_netrc_earthdata_credentials(self):
        """Return Earthdata credentials from ~/.netrc if available."""
        netrc_path = Path.home() / ".netrc"
        if not netrc_path.exists():
            return None, None

        try:
            import netrc

            auths = netrc.netrc(str(netrc_path))
            earthdata_auth = auths.authenticators("urs.earthdata.nasa.gov")
            if not earthdata_auth:
                return None, None

            username = earthdata_auth[0] or ""
            password = earthdata_auth[2] or ""
            return username, password
        except Exception:
            return None, None

    def _clear_cache(self):
        """Clear the plugin cache."""
        cache_dir = self.cache_dir_input.text().strip()

        if not cache_dir:
            QMessageBox.information(
                self, "Clear Cache", "No cache directory configured."
            )
            return

        if not os.path.exists(cache_dir):
            QMessageBox.information(
                self, "Clear Cache", "Cache directory does not exist."
            )
            return

        reply = QMessageBox.question(
            self,
            "Clear Cache",
            f"Are you sure you want to clear the cache?\n\n{cache_dir}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                import shutil

                shutil.rmtree(cache_dir)
                os.makedirs(cache_dir)
                QMessageBox.information(
                    self, "Clear Cache", "Cache cleared successfully!"
                )
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clear cache:\n{e}")

    def _on_provider_changed(self, provider):
        """Update the model field when the provider changes."""
        self.model_input.setText(DEFAULT_MODELS.get(provider, ""))

    def _load_settings(self):
        """Load settings from QSettings."""
        # Credentials precedence: .netrc -> environment -> QSettings username
        username = ""
        password = ""  # nosec B105
        source = None

        netrc_username, netrc_password = self._get_netrc_earthdata_credentials()
        if netrc_username:
            username = netrc_username
            password = netrc_password or ""
            source = ".netrc"
        else:
            env_username = os.environ.get("EARTHDATA_USERNAME", "").strip()
            env_password = os.environ.get("EARTHDATA_PASSWORD", "").strip()
            if env_username:
                username = env_username
                password = env_password
                source = "environment"
            else:
                username = self.settings.value(
                    f"{self.SETTINGS_PREFIX}username", "", type=str
                )
                source = "saved settings"

        self.username_input.setText(username)
        # Only auto-fill password from .netrc/environment, not QSettings.
        self.password_input.setText(password)

        if source == ".netrc":
            self.creds_status_label.setText(
                "Using credentials from ~/.netrc (preferred)"
            )
            self.creds_status_label.setStyleSheet("color: green;")
        elif source == "environment":
            self.creds_status_label.setText(
                "Using credentials from EARTHDATA_USERNAME/EARTHDATA_PASSWORD"
            )
            self.creds_status_label.setStyleSheet("color: green;")

        # Model
        provider = self.settings.value(
            f"{self.SETTINGS_PREFIX}provider", "openai", type=str
        )
        index = self.provider_combo.findText(provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 1)
        model = self.settings.value(f"{self.SETTINGS_PREFIX}model", "", type=str)
        self.model_input.setText(model or DEFAULT_MODELS.get(provider, ""))
        self.fast_check.setChecked(
            self.settings.value(f"{self.SETTINGS_PREFIX}fast_mode", False, type=bool)
        )
        self.max_tokens_spin.setValue(
            self.settings.value(f"{self.SETTINGS_PREFIX}max_tokens", 4096, type=int)
        )
        self.openai_key_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}openai_api_key", "", type=str)
        )
        self.anthropic_key_input.setText(
            self.settings.value(
                f"{self.SETTINGS_PREFIX}anthropic_api_key", "", type=str
            )
        )
        self.gemini_key_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}gemini_api_key", "", type=str)
        )
        self.aws_region_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}aws_region", "", type=str)
        )
        self.ollama_host_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}ollama_host", "", type=str)
        )
        self.litellm_key_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}litellm_api_key", "", type=str)
        )
        self.litellm_base_url_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}litellm_base_url", "", type=str)
        )

        # General
        self.download_dir_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}download_dir", "", type=str)
        )
        self.download_threads_spin.setValue(
            self.settings.value(f"{self.SETTINGS_PREFIX}download_threads", 4, type=int)
        )
        self.default_max_items_spin.setValue(
            self.settings.value(
                f"{self.SETTINGS_PREFIX}default_max_items", 50, type=int
            )
        )
        self.auto_zoom_check.setChecked(
            self.settings.value(f"{self.SETTINGS_PREFIX}auto_zoom", True, type=bool)
        )
        self.notifications_check.setChecked(
            self.settings.value(f"{self.SETTINGS_PREFIX}notifications", True, type=bool)
        )

        # Advanced
        self.catalog_url_input.setText(
            self.settings.value(
                f"{self.SETTINGS_PREFIX}catalog_url",
                "https://github.com/opengeos/NASA-Earth-Data/raw/main/nasa_earth_data.tsv",
                type=str,
            )
        )
        self.enable_cache_check.setChecked(
            self.settings.value(f"{self.SETTINGS_PREFIX}enable_cache", True, type=bool)
        )
        self.cache_dir_input.setText(
            self.settings.value(f"{self.SETTINGS_PREFIX}cache_dir", "", type=str)
        )
        self.debug_check.setChecked(
            self.settings.value(f"{self.SETTINGS_PREFIX}debug", False, type=bool)
        )

        self.status_label.setText("Settings loaded")
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")

    def _save_settings(self):
        """Save settings to QSettings."""
        # Credentials
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()

        self.settings.setValue(f"{self.SETTINGS_PREFIX}username", username)

        # Set environment variables for earthaccess
        if username:
            os.environ["EARTHDATA_USERNAME"] = username
        if password:
            os.environ["EARTHDATA_PASSWORD"] = password
        if username and password:
            try:
                self._save_netrc(username, password)
                self.creds_status_label.setText(
                    "✓ Credentials saved to ~/.netrc and environment"
                )
                self.creds_status_label.setStyleSheet(
                    "color: green; font-weight: bold;"
                )
            except Exception as e:
                self.creds_status_label.setText(f"Failed to save .netrc: {str(e)[:50]}")
                self.creds_status_label.setStyleSheet("color: red;")
        elif username:
            self.creds_status_label.setText(
                "Username saved. Enter password and Save to persist to ~/.netrc"
            )
            self.creds_status_label.setStyleSheet("color: orange;")

        # Model
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}provider", self.provider_combo.currentText()
        )
        self.settings.setValue(f"{self.SETTINGS_PREFIX}model", self.model_input.text())
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}fast_mode", self.fast_check.isChecked()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}max_tokens", self.max_tokens_spin.value()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}openai_api_key", self.openai_key_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}anthropic_api_key",
            self.anthropic_key_input.text(),
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}gemini_api_key", self.gemini_key_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}aws_region", self.aws_region_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}ollama_host", self.ollama_host_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}litellm_api_key", self.litellm_key_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}litellm_base_url",
            self.litellm_base_url_input.text(),
        )

        # General
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}download_dir", self.download_dir_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}download_threads",
            self.download_threads_spin.value(),
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}default_max_items",
            self.default_max_items_spin.value(),
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}auto_zoom", self.auto_zoom_check.isChecked()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}notifications", self.notifications_check.isChecked()
        )

        # Advanced
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}catalog_url", self.catalog_url_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}enable_cache", self.enable_cache_check.isChecked()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}cache_dir", self.cache_dir_input.text()
        )
        self.settings.setValue(
            f"{self.SETTINGS_PREFIX}debug", self.debug_check.isChecked()
        )

        self.settings.sync()
        self._check_netrc()

        self.status_label.setText("Settings saved")
        self.status_label.setStyleSheet("color: green; font-size: 10px;")

        self.iface.messageBar().pushSuccess(
            "NASA Earthdata", "Settings saved successfully!"
        )

    def _reset_defaults(self):
        """Reset all settings to defaults."""
        reply = QMessageBox.question(
            self,
            "Reset Settings",
            "Are you sure you want to reset all settings to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # Credentials
        self.username_input.clear()
        self.password_input.clear()

        # Model
        self.provider_combo.setCurrentText("openai")
        self.model_input.setText(DEFAULT_MODELS["openai"])
        self.fast_check.setChecked(False)
        self.max_tokens_spin.setValue(4096)
        self.openai_key_input.clear()
        self.anthropic_key_input.clear()
        self.gemini_key_input.clear()
        self.aws_region_input.clear()
        self.ollama_host_input.clear()
        self.litellm_key_input.clear()
        self.litellm_base_url_input.clear()

        # General
        self.download_dir_input.clear()
        self.download_threads_spin.setValue(4)
        self.default_max_items_spin.setValue(50)
        self.auto_zoom_check.setChecked(True)
        self.notifications_check.setChecked(True)

        # Advanced
        self.catalog_url_input.setText(
            "https://github.com/opengeos/NASA-Earth-Data/raw/main/nasa_earth_data.tsv"
        )
        self.enable_cache_check.setChecked(True)
        self.cache_dir_input.clear()
        self.debug_check.setChecked(False)

        self.status_label.setText("Defaults restored (not saved)")
        self.status_label.setStyleSheet("color: orange; font-size: 10px;")
