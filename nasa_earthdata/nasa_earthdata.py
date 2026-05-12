"""
NASA Earthdata Plugin - Main Plugin Class

This module contains the main plugin class that manages the QGIS interface
integration, menu items, toolbar buttons, and dockable panels.
"""

import os
import sys

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolBar, QMessageBox

OPEN_GEOAGENT_PLUGIN_CANDIDATES = ("open_geoagent",)
TOOLBAR_OBJECT_NAME = "NASAEarthdataToolbar"
MENU_TITLE = "&NASA Earthdata"


class NASAEarthdata:
    """NASA Earthdata Plugin implementation class for QGIS."""

    def __init__(self, iface):
        """Constructor.

        Args:
            iface: An interface instance that provides the hook to QGIS.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = None
        self.toolbar = None

        # Dock widgets (lazy loaded)
        self._earthdata_dock = None
        self._settings_dock = None
        self._deps_signal_connected = False
        self._processing_provider = None
        try:
            setattr(self.iface, "_nasa_earthdata_plugin", self)
        except Exception:
            pass  # nosec B110

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        checkable=False,
        parent=None,
    ):
        """Add a toolbar icon to the toolbar.

        Args:
            icon_path: Path to the icon for this action.
            text: Text that appears in the menu for this action.
            callback: Function to be called when the action is triggered.
            enabled_flag: A flag indicating if the action should be enabled.
            add_to_menu: Flag indicating whether action should be added to menu.
            add_to_toolbar: Flag indicating whether action should be added to toolbar.
            status_tip: Optional text to show in status bar when mouse hovers over action.
            checkable: Whether the action is checkable (toggle).
            parent: Parent widget for the new action.

        Returns:
            The action that was created.
        """
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setCheckable(checkable)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if add_to_toolbar:
            self.toolbar.addAction(action)

        if add_to_menu:
            self.menu.addAction(action)

        self.actions.append(action)

        return action

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        self._remove_toolbars_by_object_name()
        self._remove_menus_by_title()

        # Create menu
        self.menu = QMenu(MENU_TITLE)
        self.iface.mainWindow().menuBar().addMenu(self.menu)

        # Create toolbar
        self.toolbar = QToolBar("NASA Earthdata Toolbar")
        self.toolbar.setObjectName(TOOLBAR_OBJECT_NAME)
        self.iface.addToolBar(self.toolbar)

        # Get icon paths
        icon_base = os.path.join(self.plugin_dir, "icons")

        # Main panel icon
        main_icon = os.path.join(icon_base, "icon.svg")
        if not os.path.exists(main_icon):
            main_icon = ":/images/themes/default/mActionAddRasterLayer.svg"

        settings_icon = os.path.join(icon_base, "settings.svg")
        if not os.path.exists(settings_icon):
            settings_icon = ":/images/themes/default/mActionOptions.svg"

        about_icon = os.path.join(icon_base, "about.svg")
        if not os.path.exists(about_icon):
            about_icon = ":/images/themes/default/mActionHelpContents.svg"

        # Add NASA Earthdata Panel action (checkable for dock toggle)
        self.earthdata_action = self.add_action(
            main_icon,
            "NASA Earthdata Search",
            self.toggle_earthdata_dock,
            status_tip="Search and visualize NASA Earthdata",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        ai_icon = os.path.join(icon_base, "ai_chat.svg")
        if not os.path.exists(ai_icon):
            ai_icon = ":/images/themes/default/mActionHelpContents.svg"

        self.ai_chat_action = self.add_action(
            ai_icon,
            "AI Assistant",
            self.open_ai_assistant,
            add_to_toolbar=False,
            status_tip="Open the OpenGeoAgent chat panel",
            parent=self.iface.mainWindow(),
        )

        # Add Settings Panel action (checkable for dock toggle)
        self.settings_action = self.add_action(
            settings_icon,
            "Settings",
            self.toggle_settings_dock,
            status_tip="Configure NASA Earthdata settings",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        # Add separator to menu
        self.menu.addSeparator()

        # Update icon - use QGIS default download/update icon
        update_icon = ":/images/themes/default/mActionRefresh.svg"

        # Add Check for Updates action (menu only)
        self.add_action(
            update_icon,
            "Check for Updates...",
            self.show_update_checker,
            add_to_toolbar=False,
            status_tip="Check for plugin updates from GitHub",
            parent=self.iface.mainWindow(),
        )

        # Add About action (menu only)
        self.add_action(
            about_icon,
            "About NASA Earthdata Plugin",
            self.show_about,
            add_to_toolbar=False,
            status_tip="About NASA Earthdata Plugin",
            parent=self.iface.mainWindow(),
        )

        self._register_processing_provider()

    def _remove_toolbar(self, toolbar):
        """Detach and schedule deletion of a plugin toolbar widget."""
        if toolbar is None:
            return

        main_window = self.iface.mainWindow()
        actions = []
        try:
            actions = list(toolbar.actions())
        except Exception:
            pass  # nosec B110
        try:
            toolbar.clear()
        except Exception:
            pass  # nosec B110
        for action in actions:
            try:
                action.deleteLater()
            except Exception:
                pass  # nosec B110
        try:
            main_window.removeToolBar(toolbar)
        except Exception:
            pass  # nosec B110
        try:
            toolbar.hide()
        except Exception:
            pass  # nosec B110
        try:
            toolbar.setParent(None)
        except Exception:
            pass  # nosec B110
        try:
            toolbar.deleteLater()
        except Exception:
            pass  # nosec B110

    def _remove_toolbars_by_object_name(self):
        """Remove current or stale NASA Earthdata toolbars from QGIS."""
        main_window = self.iface.mainWindow()
        for toolbar in main_window.findChildren(QToolBar, TOOLBAR_OBJECT_NAME):
            self._remove_toolbar(toolbar)

    def _remove_menu(self, menu):
        """Detach and schedule deletion of a plugin menu."""
        if menu is None:
            return

        main_window = self.iface.mainWindow()
        try:
            menu.clear()
        except Exception:
            pass  # nosec B110
        try:
            main_window.menuBar().removeAction(menu.menuAction())
        except Exception:
            pass  # nosec B110
        try:
            menu.setParent(None)
        except Exception:
            pass  # nosec B110
        try:
            menu.deleteLater()
        except Exception:
            pass  # nosec B110

    def _remove_menus_by_title(self):
        """Remove current or stale NASA Earthdata menus from QGIS."""
        menu_bar = self.iface.mainWindow().menuBar()
        for action in menu_bar.actions():
            menu = action.menu()
            if menu is not None and menu.title() == MENU_TITLE:
                self._remove_menu(menu)

    def unload(self):
        """Remove the plugin menu item and icon from QGIS GUI."""
        # Remove dock widgets
        if self._earthdata_dock:
            self.iface.removeDockWidget(self._earthdata_dock)
            self._earthdata_dock.deleteLater()
            self._earthdata_dock = None

        if self._settings_dock:
            self.iface.removeDockWidget(self._settings_dock)
            self._settings_dock.deleteLater()
            self._settings_dock = None

        # Remove actions from plugin UI containers.
        for action in self.actions:
            if self.toolbar:
                self.toolbar.removeAction(action)
            if self.menu:
                self.menu.removeAction(action)
            action.deleteLater()
        self.actions = []

        # Remove toolbar
        if self.toolbar:
            self._remove_toolbar(self.toolbar)
            self.toolbar = None
        self._remove_toolbars_by_object_name()

        self._unregister_processing_provider()

        # Remove menu
        if self.menu:
            self._remove_menu(self.menu)
            self.menu = None
        self._remove_menus_by_title()
        try:
            if getattr(self.iface, "_nasa_earthdata_plugin", None) is self:
                delattr(self.iface, "_nasa_earthdata_plugin")
        except Exception:
            pass  # nosec B110

    def _register_processing_provider(self):
        """Register QGIS Processing provider when Processing is available."""
        if self._processing_provider is not None:
            return
        try:
            from qgis.core import QgsApplication

            from .processing.provider import NASAEarthdataProcessingProvider

            registry = QgsApplication.processingRegistry()
            if registry is None:
                return
            self._processing_provider = NASAEarthdataProcessingProvider()
            registry.addProvider(self._processing_provider)
        except Exception as exc:
            print(
                f"NASA Earthdata: could not register Processing provider: {exc}",
                file=sys.stderr,
            )
            self._processing_provider = None

    def _unregister_processing_provider(self):
        """Unregister QGIS Processing provider."""
        if self._processing_provider is None:
            return
        try:
            from qgis.core import QgsApplication

            registry = QgsApplication.processingRegistry()
            if registry is not None:
                registry.removeProvider(self._processing_provider)
        except Exception as exc:
            print(
                f"NASA Earthdata: could not unregister Processing provider: {exc}",
                file=sys.stderr,
            )
        self._processing_provider = None

    def toggle_earthdata_dock(self):
        """Toggle the NASA Earthdata dock widget visibility."""
        if self._earthdata_dock is None:
            try:
                from .dialogs.earthdata_dock import EarthdataDockWidget

                self._earthdata_dock = EarthdataDockWidget(
                    self.iface, self.iface.mainWindow()
                )
                self._earthdata_dock.setObjectName("NASAEarthdataDock")
                self._earthdata_dock.visibilityChanged.connect(
                    self._on_earthdata_visibility_changed
                )
                self.iface.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea, self._earthdata_dock
                )
                self._earthdata_dock.show()
                self._earthdata_dock.raise_()

                # Check dependencies on first open
                self._check_dependencies_on_open()
                self._connect_deps_signal()
                return

            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create NASA Earthdata panel:\n{str(e)}",
                )
                self.earthdata_action.setChecked(False)
                return

        # Toggle visibility
        if self._earthdata_dock.isVisible():
            self._earthdata_dock.hide()
        else:
            self._earthdata_dock.show()
            self._earthdata_dock.raise_()

    def _on_earthdata_visibility_changed(self, visible):
        """Handle NASA Earthdata dock visibility change."""
        self.earthdata_action.setChecked(visible)

    def open_ai_assistant(self, context=None):
        """Open the OpenGeoAgent chat panel, or prompt for plugin installation."""
        plugin = self._get_open_geoagent_plugin()
        if plugin is None:
            self._prompt_open_geoagent_install()
            return

        if not hasattr(plugin, "toggle_chat_dock"):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "OpenGeoAgent Required",
                "OpenGeoAgent is installed, but this version does not expose "
                "the chat panel launcher expected by NASA Earthdata.\n\n"
                "Please update OpenGeoAgent and try again.",
            )
            return

        try:
            chat_dock = getattr(plugin, "_chat_dock", None)
            if chat_dock is not None and chat_dock.isVisible():
                chat_dock.show()
                chat_dock.raise_()
                self._deliver_ai_context(plugin, context)
                return

            plugin.toggle_chat_dock()
            self._deliver_ai_context(plugin, context)
        except Exception as exc:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "OpenGeoAgent",
                f"Failed to open the OpenGeoAgent chat panel:\n{exc}",
            )

    def _deliver_ai_context(self, plugin, context=None):
        """Best-effort handoff of NASA Earthdata state to OpenGeoAgent."""
        if not context:
            context = self._current_earthdata_context()
        if not context:
            return

        targets = [plugin, getattr(plugin, "_chat_dock", None)]
        method_names = (
            "set_external_context",
            "set_context",
            "append_context",
            "receive_context",
        )
        for target in targets:
            if target is None:
                continue
            for method_name in method_names:
                method = getattr(target, method_name, None)
                if callable(method):
                    try:
                        method("NASA Earthdata", context)
                    except TypeError:
                        method(context)
                    return
            try:
                setattr(target, "_nasa_earthdata_context", context)
            except Exception:
                pass  # nosec B110

    def _current_earthdata_context(self):
        """Return current dock context for the AI assistant, when available."""
        dock = self._earthdata_dock
        if dock is not None and hasattr(dock, "ai_context_summary"):
            try:
                return dock.ai_context_summary()
            except Exception:
                return ""
        return ""

    def _get_open_geoagent_plugin(self):
        """Return the loaded OpenGeoAgent plugin instance, loading it if possible."""
        try:
            import qgis.utils as qgis_utils
        except Exception as exc:
            print(
                f"NASA Earthdata: could not import qgis.utils: {exc}",
                file=sys.stderr,
            )
            return None

        plugins = getattr(qgis_utils, "plugins", {}) or {}
        for package_name in OPEN_GEOAGENT_PLUGIN_CANDIDATES:
            plugin = plugins.get(package_name)
            if plugin is not None:
                return plugin

        available = set(getattr(qgis_utils, "available_plugins", []) or [])
        for package_name in OPEN_GEOAGENT_PLUGIN_CANDIDATES:
            if package_name not in available:
                continue

            try:
                load_plugin = getattr(qgis_utils, "loadPlugin", None)
                if callable(load_plugin) and package_name not in plugins:
                    load_plugin(package_name)

                start_plugin = getattr(qgis_utils, "startPlugin", None)
                active_plugins = getattr(qgis_utils, "active_plugins", []) or []
                if callable(start_plugin) and package_name not in active_plugins:
                    start_plugin(package_name)

                plugins = getattr(qgis_utils, "plugins", {}) or {}
                plugin = plugins.get(package_name)
                if plugin is not None:
                    return plugin
            except Exception as exc:
                print(
                    f"NASA Earthdata: failed to load OpenGeoAgent plugin "
                    f"'{package_name}': {exc}",
                    file=sys.stderr,
                )

        return None

    def _prompt_open_geoagent_install(self):
        """Tell the user how to install OpenGeoAgent from the QGIS Plugin Manager."""
        message = (
            "The AI Assistant is provided by the OpenGeoAgent QGIS plugin.\n\n"
            "Install it from the QGIS Plugin Manager:\n"
            "  Plugins > Manage and Install Plugins... > All\n"
            "  Search for 'OpenGeoAgent' and click Install Plugin.\n\n"
            "After installing (or enabling) OpenGeoAgent, click the AI "
            "Assistant button again."
        )
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Install OpenGeoAgent")
        box.setText(message)
        manager_button = box.addButton(
            "Open Plugin Manager", QMessageBox.ButtonRole.ActionRole
        )
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()

        if box.clickedButton() == manager_button:
            self._open_qgis_plugin_manager()

    def _open_qgis_plugin_manager(self):
        """Open the QGIS Plugin Manager dialog."""
        try:
            action = self.iface.actionManagePlugins()
            if action is not None:
                action.trigger()
                return
        except Exception as exc:
            print(
                f"NASA Earthdata: could not open QGIS Plugin Manager: {exc}",
                file=sys.stderr,
            )

        QMessageBox.information(
            self.iface.mainWindow(),
            "Open Plugin Manager",
            "Open the QGIS Plugin Manager from the menu:\n"
            "Plugins > Manage and Install Plugins...",
        )

    def toggle_settings_dock(self):
        """Toggle the Settings dock widget visibility."""
        if self._settings_dock is None:
            try:
                from .dialogs.settings_dock import SettingsDockWidget

                self._settings_dock = SettingsDockWidget(
                    self.iface, self.iface.mainWindow()
                )
                self._settings_dock.setObjectName("NASAEarthdataSettingsDock")
                self._settings_dock.visibilityChanged.connect(
                    self._on_settings_visibility_changed
                )
                self.iface.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea, self._settings_dock
                )
                self._settings_dock.show()
                self._settings_dock.raise_()
                self._connect_deps_signal()
                return

            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Settings panel:\n{str(e)}",
                )
                self.settings_action.setChecked(False)
                return

        # Toggle visibility
        if self._settings_dock.isVisible():
            self._settings_dock.hide()
        else:
            self._settings_dock.show()
            self._settings_dock.raise_()

    def _on_settings_visibility_changed(self, visible):
        """Handle Settings dock visibility change."""
        self.settings_action.setChecked(visible)

    def _connect_deps_signal(self):
        """Connect settings dock deps_installed signal to earthdata dock reload."""
        if (
            not self._deps_signal_connected
            and self._settings_dock is not None
            and self._earthdata_dock is not None
        ):
            self._settings_dock.deps_installed.connect(
                self._earthdata_dock.reload_catalog
            )
            self._deps_signal_connected = True

    def _check_dependencies_on_open(self):
        """Check if required dependencies are installed and prompt if missing."""
        try:
            from .core.venv_manager import check_dependencies

            all_ok, missing, _installed = check_dependencies()
            if all_ok:
                return

            missing_names = ", ".join(name for name, _ in missing)
            reply = QMessageBox.warning(
                self.iface.mainWindow(),
                "Missing Dependencies",
                f"The following required packages are not installed:\n\n"
                f"  {missing_names}\n\n"
                f"The plugin needs these packages to search and download data.\n\n"
                f"Would you like to open Settings to install them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )

            if reply == QMessageBox.StandardButton.Yes:
                self._open_settings_deps_tab()

        except Exception:
            # Don't let dependency check errors prevent the dock from opening
            pass  # nosec B110

    def _open_settings_deps_tab(self):
        """Open the Settings dock and switch to the Dependencies tab."""
        if self._settings_dock is None:
            try:
                from .dialogs.settings_dock import SettingsDockWidget

                self._settings_dock = SettingsDockWidget(
                    self.iface, self.iface.mainWindow()
                )
                self._settings_dock.setObjectName("NASAEarthdataSettingsDock")
                self._settings_dock.visibilityChanged.connect(
                    self._on_settings_visibility_changed
                )
                self.iface.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea, self._settings_dock
                )
                self._connect_deps_signal()
            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Settings panel:\n{str(e)}",
                )
                return

        self._settings_dock.show()
        self._settings_dock.raise_()
        self.settings_action.setChecked(True)

        # Switch to Dependencies tab
        self._settings_dock.show_dependencies_tab()

    def show_about(self):
        """Display the about dialog."""
        # Read version from metadata.txt
        version = "Unknown"
        try:
            metadata_path = os.path.join(self.plugin_dir, "metadata.txt")
            with open(metadata_path, "r", encoding="utf-8") as f:
                import re

                content = f.read()
                version_match = re.search(r"^version=(.+)$", content, re.MULTILINE)
                if version_match:
                    version = version_match.group(1).strip()
        except Exception as e:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "NASA Earthdata",
                f"Could not read version from metadata.txt:\n{str(e)}",
            )

        about_text = f"""
<h2>NASA Earthdata Plugin for QGIS</h2>
<p>Version: {version}</p>
<p>Author: Qiusheng Wu</p>

<h3>Features:</h3>
<ul>
<li><b>Search:</b> Search NASA Earthdata catalog with keywords, bounding box, and temporal filters</li>
<li><b>Visualize:</b> Display Cloud Optimized GeoTIFFs (COG) directly in QGIS</li>
<li><b>Footprints:</b> Show data footprints on the map</li>
<li><b>Download:</b> Download data products for local use</li>
</ul>

<h3>Requirements:</h3>
<ul>
<li>NASA Earthdata account - <a href="https://urs.earthdata.nasa.gov/">Register here</a></li>
<li>Python packages: earthaccess, geopandas</li>
</ul>

<h3>Links:</h3>
<ul>
<li><a href="https://github.com/opengeos/qgis-nasa-earthdata-plugin">GitHub Repository</a></li>
<li><a href="https://github.com/opengeos/qgis-nasa-earthdata-plugin/issues">Report Issues</a></li>
</ul>

<p>Licensed under MIT License</p>
"""
        QMessageBox.about(
            self.iface.mainWindow(),
            "About NASA Earthdata Plugin",
            about_text,
        )

    def show_update_checker(self):
        """Display the update checker dialog."""
        try:
            from .dialogs.update_checker import UpdateCheckerDialog
        except ImportError as e:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Error",
                f"Failed to import update checker dialog:\n{str(e)}",
            )
            return

        try:
            dialog = UpdateCheckerDialog(self.plugin_dir, self.iface.mainWindow())
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Error",
                f"Failed to open update checker:\n{str(e)}",
            )
