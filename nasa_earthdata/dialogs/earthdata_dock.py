"""
NASA Earthdata Search Dock Widget

This module provides a dockable panel for searching, visualizing,
and downloading NASA Earthdata products in QGIS.
"""

import os
import json
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSettings, QDate
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QTextEdit,
    QGroupBox,
    QComboBox,
    QSpinBox,
    QCheckBox,
    QFormLayout,
    QMessageBox,
    QFileDialog,
    QProgressBar,
    QDateEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QApplication,
    QScrollArea,
)
from qgis.PyQt.QtGui import QFont, QCursor
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
)

# NASA Earthdata TSV URL
NASA_DATA_URL = (
    "https://github.com/opengeos/NASA-Earth-Data/raw/main/nasa_earth_data.tsv"
)

# Cache settings
CACHE_DIR = Path(tempfile.gettempdir()) / "nasa_earthdata_cache"
CATALOG_CACHE_FILE = CACHE_DIR / "nasa_earth_data.tsv"
CATALOG_CACHE_MAX_AGE_DAYS = 7


class NumericTableWidgetItem(QTableWidgetItem):
    """Custom QTableWidgetItem that sorts numerically using UserRole data."""

    def __lt__(self, other):
        """Compare items using numeric data stored in UserRole."""
        try:
            self_data = self.data(Qt.UserRole)
            other_data = other.data(Qt.UserRole)
            # Handle None values
            if self_data is None:
                return True
            if other_data is None:
                return False
            return float(self_data) < float(other_data)
        except (ValueError, TypeError):
            # Fallback to string comparison if numeric comparison fails
            return super().__lt__(other)


class CatalogData:
    """Lightweight catalog data wrapper using stdlib only.

    Provides the interface the UI needs (name listing, keyword filtering,
    title lookup) without requiring pandas or the plugin venv.
    """

    def __init__(self, rows):
        """Initialize with a list of dicts (one per TSV row).

        Args:
            rows: List of dicts with at least 'ShortName' and 'EntryTitle' keys.
        """
        self._rows = rows

    def get_short_names(self):
        """Return a list of all ShortName values.

        Returns:
            List of ShortName strings.
        """
        return [r.get("ShortName", "") for r in self._rows]

    def filter_by_keyword(self, keyword):
        """Return ShortNames where keyword matches ShortName or EntryTitle.

        Args:
            keyword: Lowercase search string.

        Returns:
            List of matching ShortName strings.
        """
        result = []
        for r in self._rows:
            sn = r.get("ShortName", "")
            et = r.get("EntryTitle", "")
            if keyword in sn.lower() or keyword in et.lower():
                result.append(sn)
        return result

    def get_title(self, short_name):
        """Get the EntryTitle for a given ShortName.

        Args:
            short_name: The ShortName to look up.

        Returns:
            The EntryTitle string, or None if not found.
        """
        for r in self._rows:
            if r.get("ShortName") == short_name:
                return r.get("EntryTitle", "")
        return None


class CatalogLoadWorker(QThread):
    """Worker thread for loading the NASA Earthdata catalog."""

    finished = pyqtSignal(object, list)  # CatalogData, names list
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, force_refresh=False, parent=None):
        super().__init__(parent)
        self.force_refresh = force_refresh

    def run(self):
        """Load the catalog from cache or download."""
        try:
            import csv
            import urllib.request

            # Ensure cache directory exists
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

            # Check if cache exists and is fresh
            use_cache = False
            if not self.force_refresh and CATALOG_CACHE_FILE.exists():
                cache_age = (
                    datetime.now().timestamp() - CATALOG_CACHE_FILE.stat().st_mtime
                )
                if cache_age < CATALOG_CACHE_MAX_AGE_DAYS * 24 * 3600:
                    use_cache = True
                    self.progress.emit("Loading catalog from cache...")

            if use_cache:
                with open(CATALOG_CACHE_FILE, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f, delimiter="\t")
                    rows = list(reader)
            else:
                self.progress.emit("Downloading NASA Earthdata catalog...")
                with urllib.request.urlopen(NASA_DATA_URL, timeout=30) as resp:
                    text = resp.read().decode("utf-8")
                # Save raw TSV to cache
                with open(CATALOG_CACHE_FILE, "w", encoding="utf-8") as f:
                    f.write(text)
                reader = csv.DictReader(text.splitlines(), delimiter="\t")
                rows = list(reader)

            catalog = CatalogData(rows)
            names = catalog.get_short_names()
            self.finished.emit(catalog, names)

        except Exception as e:
            self.error.emit(str(e))


class DataSearchWorker(QThread):
    """Worker thread for searching NASA Earthdata."""

    finished = pyqtSignal(object, object)  # results, gdf
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        short_name,
        bbox,
        temporal,
        max_items,
        cloud_cover=None,
        day_night=None,
        provider=None,
        version=None,
        granule_id=None,
        orbit_number=None,
        parent=None,
    ):
        super().__init__(parent)
        self.short_name = short_name
        self.bbox = bbox
        self.temporal = temporal
        self.max_items = max_items
        self.cloud_cover = cloud_cover
        self.day_night = day_night
        self.provider = provider
        self.version = version
        self.granule_id = granule_id
        self.orbit_number = orbit_number

    def run(self):
        """Execute the search."""
        try:
            self.progress.emit("Importing earthaccess...")
            from ..core.venv_manager import ensure_venv_packages_available

            ensure_venv_packages_available()
            import earthaccess

            self.progress.emit("Searching NASA Earthdata...")

            kwargs = {"short_name": self.short_name}

            if self.bbox is not None:
                kwargs["bounding_box"] = self.bbox

            if self.temporal is not None:
                kwargs["temporal"] = self.temporal

            # Advanced search options
            if self.cloud_cover is not None:
                kwargs["cloud_cover"] = self.cloud_cover

            if self.day_night is not None:
                kwargs["day_night_flag"] = self.day_night

            if self.provider:
                kwargs["provider"] = self.provider

            if self.version:
                kwargs["version"] = self.version

            if self.granule_id:
                kwargs["granule_ur"] = self.granule_id

            if self.orbit_number is not None:
                kwargs["orbit_number"] = self.orbit_number

            granules = earthaccess.search_data(count=self.max_items, **kwargs)

            if len(granules) == 0:
                self.finished.emit([], None)
                return

            self.progress.emit("Converting to GeoDataFrame...")
            gdf = self._granules_to_gdf(granules)

            self.finished.emit(granules, gdf)

        except Exception as e:
            self.error.emit(str(e))

    def _granules_to_gdf(self, granules):
        """Convert granules to GeoDataFrame."""
        from ..core.venv_manager import ensure_venv_packages_available

        ensure_venv_packages_available()
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Polygon, box

        df = pd.json_normalize([dict(i.items()) for i in granules])
        df.columns = [col.split(".")[-1] for col in df.columns]
        df["result_idx"] = range(len(df))

        if "Version" in df.columns:
            df = df.drop("Version", axis=1)

        def get_bbox(rectangles):
            xmin = min(r["WestBoundingCoordinate"] for r in rectangles)
            ymin = min(r["SouthBoundingCoordinate"] for r in rectangles)
            xmax = max(r["EastBoundingCoordinate"] for r in rectangles)
            ymax = max(r["NorthBoundingCoordinate"] for r in rectangles)
            return (xmin, ymin, xmax, ymax)

        def get_polygon(coordinates):
            points = [
                (point["Longitude"], point["Latitude"])
                for point in coordinates[0]["Boundary"]["Points"]
            ]
            return Polygon(points)

        if "BoundingRectangles" in df.columns:
            df["bbox"] = df["BoundingRectangles"].apply(get_bbox)
            df["geometry"] = df["bbox"].apply(lambda x: box(*x))
        elif "GPolygons" in df.columns:
            df["geometry"] = df["GPolygons"].apply(get_polygon)

        # Build the GeoDataFrame first, then assign CRS. In some QGIS/venv setups
        # pyproj can import but fail to resolve EPSG codes if the PROJ database is
        # not discoverable ("no database context specified"). The geometries are
        # still lon/lat WGS84, so continue without CRS metadata and set it in QGIS.
        gdf = gpd.GeoDataFrame(df, geometry="geometry")
        try:
            gdf.set_crs("EPSG:4326", inplace=True, allow_override=True)
        except Exception as e:
            self.progress.emit(
                f"Warning: Could not assign CRS metadata (assuming WGS84): {e}"
            )
        return gdf


def _earthdata_login():
    """Authenticate with NASA Earthdata using non-interactive strategies.

    Tries "environment" then "netrc" strategies, matching the approach used
    by the NASA OPERA plugin.

    Raises:
        RuntimeError: If authentication fails (credentials not configured).
    """
    import earthaccess

    for strategy in ("environment", "netrc"):
        try:
            auth = earthaccess.login(strategy=strategy)
            if auth:
                return
        except Exception:
            continue

    raise RuntimeError(
        "NASA Earthdata authentication failed.\n\n"
        "Please open the Settings tab and enter your Earthdata username "
        "and password, then click 'Test Credentials'."
    )


def setup_gdal_for_earthdata():
    """Configure GDAL for accessing NASA Earthdata via authenticated HTTPS.

    Authenticates with earthaccess, saves session cookies to a file, and
    sets GDAL config options so /vsicurl/ can stream COGs with authentication.

    Returns:
        Tuple of (success, error_message). error_message is None on success.
    """
    try:
        from ..core.venv_manager import ensure_venv_packages_available

        ensure_venv_packages_available()
        import earthaccess
        from osgeo import gdal

        _earthdata_login()

        # Save session cookies for GDAL's /vsicurl/ to use
        cookie_file = os.path.expanduser("~/.urs_cookies")
        try:
            session = earthaccess.get_requests_https_session()
            with open(cookie_file, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write(
                    "# https://curl.se/docs/http-cookies.html\n"
                    "# Generated by NASA Earthdata plugin\n\n"
                )
                for cookie in session.cookies:
                    secure = "TRUE" if cookie.secure else "FALSE"
                    expires = str(int(cookie.expires)) if cookie.expires else "0"
                    f.write(
                        f"{cookie.domain}\tTRUE\t{cookie.path}\t"
                        f"{secure}\t{expires}\t"
                        f"{cookie.name}\t{cookie.value}\n"
                    )
        except Exception:
            pass

        # Configure GDAL for authenticated HTTPS access
        gdal.SetConfigOption("GDAL_HTTP_COOKIEFILE", cookie_file)
        gdal.SetConfigOption("GDAL_HTTP_COOKIEJAR", cookie_file)
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        gdal.SetConfigOption(
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff,.TIFF"
        )
        gdal.SetConfigOption("GDAL_HTTP_UNSAFESSL", "YES")
        gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "3")
        gdal.SetConfigOption("GDAL_HTTP_RETRY_DELAY", "2")
        gdal.SetConfigOption("VSI_CACHE", "TRUE")
        gdal.SetConfigOption("VSI_CACHE_SIZE", "100000000")  # 100MB cache

        return True, None

    except Exception as e:
        return False, str(e)


class COGDisplayWorker(QThread):
    """Worker thread for displaying COG layers with authentication."""

    finished = pyqtSignal(list)  # list of (layer_name, vsi_path, url) tuples
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, granules, selected_cog_url=None, parent=None):
        """Initialize the COG display worker.

        Args:
            granules: List of earthaccess granule objects.
            selected_cog_url: Specific COG URL if provided.
            parent: Parent QObject.
        """
        super().__init__(parent)
        self.granules = granules
        self.selected_cog_url = selected_cog_url

    def run(self):
        """Authenticate, configure GDAL, and collect COG URLs."""
        try:
            self.progress.emit("Authenticating with NASA Earthdata...")
            success, error = setup_gdal_for_earthdata()
            if not success:
                self.error.emit(
                    f"NASA Earthdata authentication failed: {error}\n\n"
                    "Please check your credentials in Settings."
                )
                return

            self.progress.emit("Authentication successful")

            results = []

            # If a specific COG URL is provided, use that
            if self.selected_cog_url:
                link = self.selected_cog_url
                layer_name = os.path.basename(link).split("?")[0]
                vsi_path = f"/vsicurl/{link}"
                results.append((layer_name, vsi_path, link))
                self.progress.emit(f"Using selected: {layer_name}")
            else:
                # Get COGs from granules
                for granule in self.granules:
                    try:
                        # Get HTTPS data links
                        try:
                            links = granule.data_links(access="external")
                        except TypeError:
                            links = granule.data_links()

                        # Find COG/TIFF links (HTTPS only)
                        cog_links = [
                            link
                            for link in links
                            if any(ext in link.lower() for ext in [".tif", ".tiff"])
                            and link.startswith("http")
                        ]

                        if not cog_links:
                            continue

                        # Use the first TIFF link
                        for link in cog_links[:1]:
                            layer_name = os.path.basename(link).split("?")[0]
                            vsi_path = f"/vsicurl/{link}"
                            results.append((layer_name, vsi_path, link))
                            self.progress.emit(f"Found: {layer_name}")

                    except Exception as e:
                        self.progress.emit(f"Error processing granule: {e}")

            self.finished.emit(results)

        except Exception as e:
            self.error.emit(str(e))


class DataDownloadWorker(QThread):
    """Worker thread for downloading NASA Earthdata."""

    finished = pyqtSignal(list)  # downloaded files
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, granules, output_dir, parent=None):
        super().__init__(parent)
        self.granules = granules
        self.output_dir = output_dir

    def run(self):
        """Execute the download."""
        try:
            from ..core.venv_manager import ensure_venv_packages_available

            ensure_venv_packages_available()
            import earthaccess

            self.progress.emit(10, "Authenticating...")

            self.progress.emit(30, "Downloading files...")

            files = earthaccess.download(
                self.granules,
                local_path=self.output_dir,
            )

            self.progress.emit(100, "Download complete!")
            self.finished.emit(files)

        except Exception as e:
            self.error.emit(str(e))


class EarthdataDockWidget(QDockWidget):
    """A dockable panel for NASA Earthdata search and visualization."""

    def __init__(self, iface, parent=None):
        """Initialize the dock widget.

        Args:
            iface: QGIS interface instance.
            parent: Parent widget.
        """
        super().__init__("NASA Earthdata", parent)
        self.iface = iface
        self.settings = QSettings()

        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # Set minimum width but allow resizing
        self.setMinimumWidth(300)

        # Data storage
        self._nasa_data = None
        self._nasa_data_names = []
        self._search_results = None
        self._search_gdf = None
        self._footprints_layer = None
        self._temp_footprints_file = None

        # Workers
        self._catalog_worker = None
        self._search_worker = None
        self._download_worker = None
        self._cog_worker = None

        self._setup_ui()
        self._load_datasets()

    def _setup_ui(self):
        """Set up the dock widget UI."""
        # Create scroll area for the content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # Main widget inside scroll area
        main_widget = QWidget()
        scroll_area.setWidget(main_widget)
        self.setWidget(scroll_area)

        # Main layout
        layout = QVBoxLayout(main_widget)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        # Header with refresh button
        header_layout = QHBoxLayout()
        header_label = QLabel("NASA Earthdata Search")
        header_font = QFont()
        header_font.setPointSize(11)
        header_font.setBold(True)
        header_label.setFont(header_font)
        header_layout.addWidget(header_label)
        header_layout.addStretch()

        self.refresh_catalog_btn = QPushButton("↻")
        self.refresh_catalog_btn.setToolTip("Refresh dataset catalog")
        self.refresh_catalog_btn.setFixedWidth(30)
        self.refresh_catalog_btn.clicked.connect(self._refresh_catalog)
        header_layout.addWidget(self.refresh_catalog_btn)
        layout.addLayout(header_layout)

        # Search section
        search_group = QGroupBox("Search Parameters")
        search_layout = QFormLayout(search_group)
        search_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        # Keyword filter
        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("Filter datasets by keyword...")
        self.keyword_input.returnPressed.connect(self._filter_datasets)
        search_layout.addRow("Keyword:", self.keyword_input)

        # Dataset dropdown
        self.dataset_combo = QComboBox()
        self.dataset_combo.setMaxVisibleItems(20)
        self.dataset_combo.currentTextChanged.connect(self._on_dataset_changed)
        # Use AdjustToContents so dropdown shows full text
        self.dataset_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.dataset_combo.setMinimumContentsLength(20)
        search_layout.addRow("Dataset:", self.dataset_combo)

        # Dataset title (read-only) - use disabled state for proper theming
        self.title_label = QLineEdit()
        self.title_label.setReadOnly(True)
        self.title_label.setEnabled(False)  # Disabled state respects dark/light theme
        search_layout.addRow("Title:", self.title_label)

        # Max items
        self.max_items_spin = QSpinBox()
        self.max_items_spin.setRange(1, 500)
        self.max_items_spin.setValue(50)
        search_layout.addRow("Max Items:", self.max_items_spin)

        # Bounding box
        self.bbox_input = QLineEdit()
        self.bbox_input.setPlaceholderText("xmin, ymin, xmax, ymax (or leave empty)")
        search_layout.addRow("Bounding Box:", self.bbox_input)

        # Use map extent button
        bbox_btn_layout = QHBoxLayout()
        self.use_extent_btn = QPushButton("Use Map Extent")
        self.use_extent_btn.clicked.connect(self._use_map_extent)
        bbox_btn_layout.addWidget(self.use_extent_btn)
        self.clear_bbox_btn = QPushButton("Clear")
        self.clear_bbox_btn.clicked.connect(lambda: self.bbox_input.clear())
        bbox_btn_layout.addWidget(self.clear_bbox_btn)
        search_layout.addRow("", bbox_btn_layout)

        # Date range
        date_layout = QHBoxLayout()
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate().addYears(-1))
        self.start_date.setSpecialValueText(" ")
        date_layout.addWidget(QLabel("From:"))
        date_layout.addWidget(self.start_date)

        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate.currentDate())
        self.end_date.setSpecialValueText(" ")
        date_layout.addWidget(QLabel("To:"))
        date_layout.addWidget(self.end_date)
        search_layout.addRow("Date Range:", date_layout)

        layout.addWidget(search_group)

        # Advanced Options (collapsible checkbox)
        self.advanced_check = QCheckBox("Advanced Options")
        self.advanced_check.setChecked(False)
        self.advanced_check.toggled.connect(self._toggle_advanced_options)
        layout.addWidget(self.advanced_check)

        # Advanced options container widget (hidden by default)
        self.advanced_widget = QWidget()
        advanced_layout = QFormLayout(self.advanced_widget)
        advanced_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        advanced_layout.setContentsMargins(10, 5, 0, 5)

        # Cloud cover range
        cloud_layout = QHBoxLayout()
        self.cloud_min_spin = QSpinBox()
        self.cloud_min_spin.setRange(0, 100)
        self.cloud_min_spin.setValue(0)
        self.cloud_min_spin.setSuffix("%")
        self.cloud_min_spin.setToolTip("Minimum cloud cover percentage")
        cloud_layout.addWidget(QLabel("Min:"))
        cloud_layout.addWidget(self.cloud_min_spin)

        self.cloud_max_spin = QSpinBox()
        self.cloud_max_spin.setRange(0, 100)
        self.cloud_max_spin.setValue(100)
        self.cloud_max_spin.setSuffix("%")
        self.cloud_max_spin.setToolTip("Maximum cloud cover percentage")
        cloud_layout.addWidget(QLabel("Max:"))
        cloud_layout.addWidget(self.cloud_max_spin)
        advanced_layout.addRow("Cloud Cover:", cloud_layout)

        # Day/Night flag
        self.daynight_combo = QComboBox()
        self.daynight_combo.addItem("Any", None)
        self.daynight_combo.addItem("Day only", "day")
        self.daynight_combo.addItem("Night only", "night")
        self.daynight_combo.addItem("Both (unspecified)", "unspecified")
        self.daynight_combo.setToolTip("Filter by day or night acquisition")
        advanced_layout.addRow("Day/Night:", self.daynight_combo)

        # Provider
        self.provider_input = QLineEdit()
        self.provider_input.setPlaceholderText("e.g., LPCLOUD, PODAAC, NSIDC_ECS")
        self.provider_input.setToolTip("Data provider (leave empty for any)")
        advanced_layout.addRow("Provider:", self.provider_input)

        # Version
        self.version_input = QLineEdit()
        self.version_input.setPlaceholderText("e.g., 2.0, 061")
        self.version_input.setToolTip("Dataset version (leave empty for latest)")
        advanced_layout.addRow("Version:", self.version_input)

        # Granule ID pattern
        self.granule_id_input = QLineEdit()
        self.granule_id_input.setPlaceholderText("e.g., *T11* or HLS.L30.*")
        self.granule_id_input.setToolTip("Filter by granule ID pattern (wildcards: *)")
        advanced_layout.addRow("Granule ID:", self.granule_id_input)

        # Orbit number
        orbit_layout = QHBoxLayout()
        self.orbit_min_spin = QSpinBox()
        self.orbit_min_spin.setRange(0, 999999)
        self.orbit_min_spin.setValue(0)
        self.orbit_min_spin.setSpecialValueText("Any")
        self.orbit_min_spin.setToolTip("Minimum orbit number")
        orbit_layout.addWidget(QLabel("Min:"))
        orbit_layout.addWidget(self.orbit_min_spin)

        self.orbit_max_spin = QSpinBox()
        self.orbit_max_spin.setRange(0, 999999)
        self.orbit_max_spin.setValue(0)
        self.orbit_max_spin.setSpecialValueText("Any")
        self.orbit_max_spin.setToolTip("Maximum orbit number")
        orbit_layout.addWidget(QLabel("Max:"))
        orbit_layout.addWidget(self.orbit_max_spin)
        advanced_layout.addRow("Orbit Number:", orbit_layout)

        # Initially hidden
        self.advanced_widget.setVisible(False)
        layout.addWidget(self.advanced_widget)

        # Action buttons
        action_layout = QHBoxLayout()

        self.search_btn = QPushButton("Search")
        self.search_btn.setStyleSheet(
            "background-color: #0B3D91; color: white; font-weight: bold;"
        )
        self.search_btn.clicked.connect(self._search_data)
        action_layout.addWidget(self.search_btn)

        self.display_btn = QPushButton("Display COG")
        self.display_btn.setEnabled(False)
        self.display_btn.clicked.connect(self._display_cog)
        action_layout.addWidget(self.display_btn)

        self.download_btn = QPushButton("Download")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._download_data)
        action_layout.addWidget(self.download_btn)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.clicked.connect(self._reset)
        action_layout.addWidget(self.reset_btn)

        self.clear_results_btn = QPushButton("Clear Results")
        self.clear_results_btn.clicked.connect(self._clear_results)
        action_layout.addWidget(self.clear_results_btn)

        layout.addLayout(action_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # Results section
        results_group = QGroupBox("Search Results")
        results_layout = QVBoxLayout(results_group)

        # Results table
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(3)
        self.results_table.setHorizontalHeaderLabels(["ID", "Date", "Size"])
        # ID column stretches to fill space, Date and Size are fixed width
        self.results_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.results_table.setColumnWidth(1, 90)  # Date
        self.results_table.setColumnWidth(2, 70)  # Size
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_table.setMinimumHeight(120)
        # Enable tooltips for truncated text
        self.results_table.setMouseTracking(True)
        self.results_table.itemSelectionChanged.connect(self._on_selection_changed)
        # Enable sorting
        self.results_table.setSortingEnabled(True)
        # Connect double-click on header to sort
        self.results_table.horizontalHeader().sectionDoubleClicked.connect(
            self._on_header_double_clicked
        )
        results_layout.addWidget(self.results_table)

        # COG file selection dropdown
        cog_layout = QHBoxLayout()
        cog_layout.addWidget(QLabel("COG File:"))
        self.cog_combo = QComboBox()
        self.cog_combo.setEnabled(False)
        self.cog_combo.setToolTip(
            "Select a COG file to display from the selected granule"
        )
        self.cog_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.cog_combo.setMinimumContentsLength(15)
        cog_layout.addWidget(self.cog_combo, 1)  # Stretch to fill
        results_layout.addLayout(cog_layout)

        # Zoom to footprints button and results info
        info_layout = QHBoxLayout()
        self.zoom_footprints_btn = QPushButton("Zoom to Footprints")
        self.zoom_footprints_btn.setEnabled(False)
        self.zoom_footprints_btn.clicked.connect(self._zoom_to_footprints)
        info_layout.addWidget(self.zoom_footprints_btn)
        info_layout.addStretch()
        results_layout.addLayout(info_layout)

        # Results info
        self.results_label = QLabel("No search performed yet")
        self.results_label.setStyleSheet("color: gray;")
        results_layout.addWidget(self.results_label)

        layout.addWidget(results_group)

        # Output section
        output_group = QGroupBox("Output")
        output_layout = QVBoxLayout(output_group)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setMaximumHeight(80)
        self.output_text.setPlaceholderText("Status messages will appear here...")
        output_layout.addWidget(self.output_text)

        layout.addWidget(output_group)

        # Status label
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.status_label)

        # Add stretch at end
        layout.addStretch()

    def _load_datasets(self):
        """Load NASA datasets from cache or download."""
        self._log("Loading NASA Earthdata catalog...")
        self.refresh_catalog_btn.setEnabled(False)

        self._catalog_worker = CatalogLoadWorker(force_refresh=False)
        self._catalog_worker.finished.connect(self._on_catalog_loaded)
        self._catalog_worker.error.connect(self._on_catalog_error)
        self._catalog_worker.progress.connect(self._log)
        self._catalog_worker.start()

    def _refresh_catalog(self):
        """Force refresh the catalog from the server."""
        self._log("Refreshing catalog from server...")
        self.refresh_catalog_btn.setEnabled(False)

        self._catalog_worker = CatalogLoadWorker(force_refresh=True)
        self._catalog_worker.finished.connect(self._on_catalog_loaded)
        self._catalog_worker.error.connect(self._on_catalog_error)
        self._catalog_worker.progress.connect(self._log)
        self._catalog_worker.start()

    def reload_catalog(self):
        """Reload the catalog, e.g. after dependencies are installed."""
        self._load_datasets()

    def _on_catalog_loaded(self, df, names):
        """Handle catalog loaded."""
        self.refresh_catalog_btn.setEnabled(True)
        self._nasa_data = df
        self._nasa_data_names = names

        self.dataset_combo.clear()
        self.dataset_combo.addItems(names)

        # Set default dataset
        default_dataset = "HLSL30"
        if default_dataset in names:
            self.dataset_combo.setCurrentText(default_dataset)

        self._log(f"Loaded {len(names)} datasets")
        self.status_label.setText(f"{len(names)} datasets available")

    def _on_catalog_error(self, error_msg):
        """Handle catalog load error."""
        self.refresh_catalog_btn.setEnabled(True)
        self._log(f"Error loading catalog: {error_msg}", error=True)
        error_lower = str(error_msg).lower()

        if "no module named" in error_lower:
            QMessageBox.warning(
                self,
                "Dependencies Missing",
                f"Failed to load NASA Earthdata catalog:\n{error_msg}\n\n"
                "This is a plugin dependency issue, not a network issue.\n"
                "Open the plugin Settings and run 'Install Dependencies', "
                "then restart QGIS.",
            )
            return

        QMessageBox.warning(
            self,
            "Warning",
            f"Failed to load NASA Earthdata catalog:\n{error_msg}\n\n"
            "Please check your internet connection and try again.",
        )

    def _filter_datasets(self):
        """Filter datasets based on keyword in ShortName and EntryTitle only."""
        keyword = self.keyword_input.text().strip().lower()

        if not keyword:
            self.dataset_combo.clear()
            self.dataset_combo.addItems(self._nasa_data_names)
            return

        if self._nasa_data is None:
            return

        filtered = self._nasa_data.filter_by_keyword(keyword)

        self.dataset_combo.clear()
        self.dataset_combo.addItems(filtered)

        self._log(f"Found {len(filtered)} datasets matching '{keyword}'")

    def _on_dataset_changed(self, short_name):
        """Handle dataset selection change."""
        if self._nasa_data is None or not short_name:
            return

        try:
            title = self._nasa_data.get_title(short_name)
            if title:
                self.title_label.setText(title)
            else:
                self.title_label.clear()
        except Exception:
            self.title_label.clear()

        # Clear previous search results when dataset changes
        self._clear_results()

    def _clear_results(self):
        """Clear search results without resetting other settings."""
        self.results_table.setRowCount(0)
        self.results_label.setText("No search performed yet")
        self.display_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.zoom_footprints_btn.setEnabled(False)
        self.cog_combo.clear()
        self.cog_combo.setEnabled(False)
        self._search_results = None
        self._search_gdf = None
        self._remove_footprints()
        self.iface.mapCanvas().refresh()

    def _use_map_extent(self):
        """Set bounding box from current map extent."""
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()

        # Transform to WGS84 if needed
        crs = canvas.mapSettings().destinationCrs()
        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(
                crs,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            )
            extent = transform.transformBoundingBox(extent)

        bbox_str = f"{extent.xMinimum():.4f}, {extent.yMinimum():.4f}, {extent.xMaximum():.4f}, {extent.yMaximum():.4f}"
        self.bbox_input.setText(bbox_str)

    def _toggle_advanced_options(self, checked):
        """Toggle visibility of advanced options."""
        self.advanced_widget.setVisible(checked)

    def _search_data(self):
        """Search NASA Earthdata."""
        short_name = self.dataset_combo.currentText()
        if not short_name:
            QMessageBox.warning(self, "Warning", "Please select a dataset.")
            return

        # Parse bounding box
        bbox = None
        bbox_text = self.bbox_input.text().strip()
        if bbox_text:
            try:
                parts = [float(x.strip()) for x in bbox_text.split(",")]
                if len(parts) != 4:
                    raise ValueError("Bounding box must have 4 values")
                bbox = tuple(parts)
            except Exception as e:
                QMessageBox.warning(
                    self, "Warning", f"Invalid bounding box format:\n{e}"
                )
                return
        else:
            # Use map extent
            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            crs = canvas.mapSettings().destinationCrs()
            if crs.authid() != "EPSG:4326":
                transform = QgsCoordinateTransform(
                    crs,
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance(),
                )
                extent = transform.transformBoundingBox(extent)
            bbox = (
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
            )

        # Parse temporal
        temporal = None
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        if start and end:
            temporal = (start, end)

        max_items = self.max_items_spin.value()

        # Collect advanced options if enabled
        cloud_cover = None
        day_night = None
        provider = None
        version = None
        granule_id = None
        orbit_number = None

        if self.advanced_check.isChecked():
            # Cloud cover
            cloud_min = self.cloud_min_spin.value()
            cloud_max = self.cloud_max_spin.value()
            if cloud_min > 0 or cloud_max < 100:
                cloud_cover = (cloud_min, cloud_max)

            # Day/Night flag
            day_night = self.daynight_combo.currentData()

            # Provider
            provider = self.provider_input.text().strip() or None

            # Version
            version = self.version_input.text().strip() or None

            # Granule ID pattern
            granule_id = self.granule_id_input.text().strip() or None

            # Orbit number
            orbit_min = self.orbit_min_spin.value()
            orbit_max = self.orbit_max_spin.value()
            if orbit_min > 0 or orbit_max > 0:
                if orbit_min > 0 and orbit_max > 0:
                    orbit_number = (orbit_min, orbit_max)
                elif orbit_min > 0:
                    orbit_number = orbit_min
                else:
                    orbit_number = orbit_max

        # Disable UI during search
        self.search_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self._log(f"Searching {short_name}...")

        # Start search worker
        self._search_worker = DataSearchWorker(
            short_name,
            bbox,
            temporal,
            max_items,
            cloud_cover=cloud_cover,
            day_night=day_night,
            provider=provider,
            version=version,
            granule_id=granule_id,
            orbit_number=orbit_number,
        )
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_worker.error.connect(self._on_search_error)
        self._search_worker.progress.connect(self._log)
        self._search_worker.start()

    def _on_search_finished(self, results, gdf):
        """Handle search completion."""
        self.search_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        self._search_results = results
        self._search_gdf = gdf

        if not results:
            self._log("No results found")
            self.results_label.setText("No results found")
            self.display_btn.setEnabled(False)
            self.download_btn.setEnabled(False)
            self.zoom_footprints_btn.setEnabled(False)
            return

        self._log(f"Found {len(results)} results")
        self.results_label.setText(f"Found {len(results)} results")

        # Populate results table
        # Disable sorting temporarily for performance during population
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(len(results))
        for i, granule in enumerate(results):
            try:
                native_id = granule.get("meta", {}).get("native-id", f"Item {i+1}")
                # Get date from time range
                time_start = (
                    granule.get("umm", {})
                    .get("TemporalExtent", {})
                    .get("RangeDateTime", {})
                    .get("BeginningDateTime", "N/A")
                )
                if time_start != "N/A":
                    time_start = time_start[:10]  # Just the date part

                # Estimate size
                size_display = "N/A"
                size_bytes = 0  # For sorting
                data_granule = granule.get("umm", {}).get("DataGranule", {})
                if "ArchiveAndDistributionInformation" in data_granule:
                    for info in data_granule["ArchiveAndDistributionInformation"]:
                        if "SizeInBytes" in info:
                            size_bytes = info["SizeInBytes"]
                            if size_bytes > 1e9:
                                size_display = f"{size_bytes / 1e9:.1f} GB"
                            elif size_bytes > 1e6:
                                size_display = f"{size_bytes / 1e6:.1f} MB"
                            else:
                                size_display = f"{size_bytes / 1e3:.1f} KB"
                            break

                # Create items with tooltips for full text
                id_item = QTableWidgetItem(str(native_id))
                id_item.setToolTip(str(native_id))  # Show full ID on hover
                id_item.setData(
                    Qt.UserRole, i
                )  # Stable index into _search_results/_search_gdf
                self.results_table.setItem(i, 0, id_item)

                date_item = QTableWidgetItem(str(time_start))
                self.results_table.setItem(i, 1, date_item)

                # Store raw bytes for proper numeric sorting
                size_item = NumericTableWidgetItem(str(size_display))
                size_item.setData(
                    Qt.UserRole, size_bytes
                )  # Store raw value for sorting
                self.results_table.setItem(i, 2, size_item)
            except Exception:
                id_item = QTableWidgetItem(f"Item {i+1}")
                id_item.setToolTip(f"Item {i+1}")
                id_item.setData(Qt.UserRole, i)
                self.results_table.setItem(i, 0, id_item)
                self.results_table.setItem(i, 1, QTableWidgetItem("N/A"))

                size_item = NumericTableWidgetItem("N/A")
                size_item.setData(Qt.UserRole, 0)  # Store 0 for N/A
                self.results_table.setItem(i, 2, size_item)

        # Re-enable sorting after population
        self.results_table.setSortingEnabled(True)

        # Add footprints to map
        self._add_footprints(gdf)

        # Enable buttons
        self.display_btn.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.zoom_footprints_btn.setEnabled(True)

    def _on_search_error(self, error_msg):
        """Handle search error."""
        self.search_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._log(f"Search error: {error_msg}", error=True)

        if "login" in error_msg.lower() or "auth" in error_msg.lower():
            QMessageBox.critical(
                self,
                "Authentication Error",
                f"NASA Earthdata authentication failed:\n{error_msg}\n\n"
                "Please configure your credentials in Settings or run:\n"
                "  earthaccess.login()\n"
                "in the Python console.",
            )
        else:
            QMessageBox.critical(self, "Search Error", f"Search failed:\n{error_msg}")

    def _write_footprints_geojson_fallback(self, gdf, output_path):
        """Write footprints as GeoJSON without pyogrio/fiona."""
        features = []

        for i in range(len(gdf)):
            geom = None
            try:
                geom_obj = gdf.geometry.iloc[i]
                if geom_obj is not None and not geom_obj.is_empty:
                    geom = geom_obj.__geo_interface__
            except Exception:
                geom = None

            # Minimal properties are enough for display and selection handling.
            features.append(
                {
                    "type": "Feature",
                    "properties": {"result_idx": int(i)},
                    "geometry": geom,
                }
            )

        geojson = {"type": "FeatureCollection", "features": features}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f)

    def _add_footprints(self, gdf):
        """Add search result footprints to the map."""
        if gdf is None:
            return

        try:
            # Remove existing footprints layer
            self._remove_footprints()

            # Create temporary GeoJSON file
            temp_file = os.path.join(
                tempfile.gettempdir(), "nasa_earthdata_footprints.geojson"
            )
            try:
                gdf.to_file(temp_file, driver="GeoJSON")
            except Exception as e:
                self._log(
                    f"GeoPandas export failed, using fallback writer: {e}",
                    error=False,
                )
                self._write_footprints_geojson_fallback(gdf, temp_file)
            self._temp_footprints_file = temp_file

            # Add layer to QGIS
            layer = QgsVectorLayer(temp_file, "NASA Earthdata Footprints", "ogr")
            if layer.isValid():
                # GeoJSON footprints are WGS84 lon/lat. Set CRS explicitly so map
                # transforms still work when GeoPandas could not attach CRS metadata.
                layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

                # Style the layer
                from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer

                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": "51,136,255,25",
                        "outline_color": "#3388ff",
                        "outline_width": "0.5",
                    }
                )
                layer.setRenderer(QgsSingleSymbolRenderer(symbol))

                QgsProject.instance().addMapLayer(layer)
                self._footprints_layer = layer

                # Zoom to footprints with proper CRS handling
                self._zoom_to_footprints()

                self._log("Footprints added to map")
            else:
                self._log("Failed to create footprints layer", error=True)

        except Exception as e:
            self._log(f"Error adding footprints: {e}", error=True)

    def _zoom_to_footprints(self):
        """Zoom the map canvas to selected footprints or all if none selected."""
        if self._footprints_layer is None or not self._footprints_layer.isValid():
            return

        if self._search_gdf is None:
            return

        try:
            # Check if rows are selected in the table
            selected_rows = self.results_table.selectionModel().selectedRows()

            if selected_rows:
                # Zoom to selected features only
                indices = set(self._get_selected_result_indices())
                # Get bounding box of selected features from the GeoDataFrame
                selected_gdf = self._search_gdf.iloc[list(indices)]
                bounds = selected_gdf.total_bounds  # [minx, miny, maxx, maxy]
                layer_extent = QgsRectangle(bounds[0], bounds[1], bounds[2], bounds[3])

                # Highlight selected features in the layer
                self._sync_footprint_selection_from_table()

                self._log(f"Zooming to {len(indices)} selected footprint(s)")
            else:
                # Zoom to all footprints
                layer_extent = self._footprints_layer.extent()
                self._log("Zooming to all footprints")

            # Transform extent to map CRS if different
            layer_crs = self._footprints_layer.crs()
            canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()

            if layer_crs != canvas_crs:
                transform = QgsCoordinateTransform(
                    layer_crs,
                    canvas_crs,
                    QgsProject.instance(),
                )
                layer_extent = transform.transformBoundingBox(layer_extent)

            # Add some buffer (10%)
            buffer_x = layer_extent.width() * 0.1 if layer_extent.width() > 0 else 0.01
            buffer_y = (
                layer_extent.height() * 0.1 if layer_extent.height() > 0 else 0.01
            )
            buffered_extent = QgsRectangle(
                layer_extent.xMinimum() - buffer_x,
                layer_extent.yMinimum() - buffer_y,
                layer_extent.xMaximum() + buffer_x,
                layer_extent.yMaximum() + buffer_y,
            )

            # Set extent and refresh
            self.iface.mapCanvas().setExtent(buffered_extent)
            self.iface.mapCanvas().refresh()

        except Exception as e:
            self._log(f"Error zooming to footprints: {e}", error=True)

    def _remove_footprints(self):
        """Remove footprints layer from map and clean up temporary file."""
        if self._footprints_layer is not None:
            try:
                # Remove layer from project
                QgsProject.instance().removeMapLayer(self._footprints_layer.id())
            except Exception:
                pass

            # Delete the layer object to release file handles (important on Windows)
            try:
                del self._footprints_layer
            except Exception:
                pass
            self._footprints_layer = None

            # Force garbage collection to ensure file handles are released on Windows
            import gc

            gc.collect()

            # On Windows, give the OS a moment to release file handles
            if platform.system() == "Windows":
                time.sleep(0.1)

        # Delete temporary file if it exists
        if self._temp_footprints_file is not None:
            if os.path.exists(self._temp_footprints_file):
                # On Windows, retry a few times if file is locked
                max_retries = 3 if platform.system() == "Windows" else 1
                for attempt in range(max_retries):
                    try:
                        os.remove(self._temp_footprints_file)
                        break  # Success
                    except (PermissionError, OSError) as e:
                        if attempt < max_retries - 1:
                            # Wait and retry on Windows
                            time.sleep(0.1)
                        else:
                            # Final attempt failed - log but don't raise
                            self._log(
                                f"Could not delete temp file (will be reused): {e}",
                                error=False,
                            )
            self._temp_footprints_file = None

    def _on_selection_changed(self):
        """Handle table selection change."""
        selected_rows = self.results_table.selectionModel().selectedRows()
        if selected_rows:
            self.display_btn.setEnabled(True)
            self.download_btn.setEnabled(True)
            # Populate COG dropdown for the first selected granule
            self._populate_cog_dropdown(selected_rows[0].row())
        else:
            # Still enable if we have results
            has_results = (
                self._search_results is not None and len(self._search_results) > 0
            )
            self.display_btn.setEnabled(has_results)
            self.download_btn.setEnabled(has_results)
            # Clear COG dropdown
            self.cog_combo.clear()
            self.cog_combo.setEnabled(False)

        self._sync_footprint_selection_from_table()

    def _get_result_index_for_table_row(self, table_row):
        """Map current table row (after sorting) to original search result index."""
        item = self.results_table.item(table_row, 0)
        if item is None:
            return table_row

        result_idx = item.data(Qt.UserRole)
        if result_idx is None:
            return table_row

        try:
            return int(result_idx)
        except (TypeError, ValueError):
            return table_row

    def _get_selected_result_indices(self):
        """Get stable result indices for the current table selection."""
        selection_model = self.results_table.selectionModel()
        if selection_model is None:
            return []

        indices = []
        for row in selection_model.selectedRows():
            result_idx = self._get_result_index_for_table_row(row.row())
            if self._search_results is None or 0 <= result_idx < len(
                self._search_results
            ):
                indices.append(result_idx)

        # Preserve order while removing duplicates.
        return list(dict.fromkeys(indices))

    def _sync_footprint_selection_from_table(self):
        """Highlight footprint features matching selected table rows."""
        if self._footprints_layer is None or not self._footprints_layer.isValid():
            return

        try:
            selected_indices = set(self._get_selected_result_indices())
            self._footprints_layer.removeSelection()

            if not selected_indices:
                return

            feature_ids = []
            field_names = [f.name() for f in self._footprints_layer.fields()]
            has_result_idx_field = "result_idx" in field_names

            for feature_pos, feature in enumerate(self._footprints_layer.getFeatures()):
                if has_result_idx_field:
                    try:
                        feature_result_idx = int(feature["result_idx"])
                    except Exception:
                        feature_result_idx = feature_pos
                else:
                    feature_result_idx = feature_pos

                if feature_result_idx in selected_indices:
                    feature_ids.append(feature.id())

            if feature_ids:
                self._footprints_layer.selectByIds(feature_ids)
        except Exception as e:
            self._log(f"Error syncing footprint selection: {e}", error=True)

    def _on_header_double_clicked(self, logical_index):
        """Handle double-click on table header to toggle sort order."""
        # Get current sort order for this column
        header = self.results_table.horizontalHeader()
        current_order = header.sortIndicatorOrder()

        # Toggle sort order: if already sorted by this column, reverse it
        if header.sortIndicatorSection() == logical_index:
            new_order = (
                Qt.DescendingOrder
                if current_order == Qt.AscendingOrder
                else Qt.AscendingOrder
            )
        else:
            # Default to ascending for new column
            new_order = Qt.AscendingOrder

        # Apply the sort
        self.results_table.sortItems(logical_index, new_order)
        self._log(
            f"Sorted by column {logical_index} ({'descending' if new_order == Qt.DescendingOrder else 'ascending'})"
        )

    def _populate_cog_dropdown(self, row_index):
        """Populate the COG dropdown with available files for the selected granule."""
        self.cog_combo.clear()
        self.cog_combo.setEnabled(False)

        result_index = self._get_result_index_for_table_row(row_index)
        if self._search_results is None or result_index >= len(self._search_results):
            return

        granule = self._search_results[result_index]

        try:
            # Get data links
            try:
                links = granule.data_links(access="external")
            except TypeError:
                links = granule.data_links()

            # Find COG/TIFF links (HTTPS only)
            cog_links = [
                link
                for link in links
                if any(ext in link.lower() for ext in [".tif", ".tiff"])
                and link.startswith("http")
            ]

            if cog_links:
                # Add COG files to dropdown (show just filenames)
                for link in cog_links:
                    filename = os.path.basename(link).split("?")[0]
                    self.cog_combo.addItem(filename, link)  # Store full URL as data

                self.cog_combo.setEnabled(True)
                self._log(f"Found {len(cog_links)} COG file(s) for selected granule")
            else:
                self.cog_combo.addItem("No COG files found")

        except Exception as e:
            self.cog_combo.addItem(f"Error: {str(e)[:30]}")

    def _get_selected_granules(self):
        """Get the selected granules from the table."""
        selected_rows = self.results_table.selectionModel().selectedRows()
        if selected_rows and self._search_results:
            indices = self._get_selected_result_indices()
            return [
                self._search_results[i]
                for i in indices
                if i < len(self._search_results)
            ]
        return self._search_results  # Return all if none selected

    def _display_cog(self):
        """Display selected COG layers using a worker thread."""
        # Check if a specific COG is selected in the dropdown
        selected_cog_url = None
        if self.cog_combo.isEnabled() and self.cog_combo.currentIndex() >= 0:
            selected_cog_url = (
                self.cog_combo.currentData()
            )  # Get the full URL stored as data

        if selected_cog_url:
            # Display the specific COG selected in dropdown
            self._log(f"Displaying: {self.cog_combo.currentText()}")
        else:
            # No specific COG selected, will display first COG from each selected granule
            granules = self._get_selected_granules()
            if not granules:
                QMessageBox.warning(self, "Warning", "No data to display.")
                return
            self._log("Looking for COG/TIFF files...")

        # Show progress
        self.display_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        # Set wait cursor
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))

        # Start COG worker
        if selected_cog_url:
            self._cog_worker = COGDisplayWorker([], selected_cog_url=selected_cog_url)
        else:
            self._cog_worker = COGDisplayWorker(self._get_selected_granules())
        self._cog_worker.finished.connect(self._on_cog_finished)
        self._cog_worker.error.connect(self._on_cog_error)
        self._cog_worker.progress.connect(self._log)
        self._cog_worker.start()

    def _on_cog_finished(self, results):
        """Handle COG display completion.

        Args:
            results: List of (layer_name, vsi_path, url) tuples.
        """
        # Keep wait cursor while loading layers
        self.progress_bar.setVisible(False)

        if not results:
            # Restore cursor and UI before showing dialog
            QApplication.restoreOverrideCursor()
            self.display_btn.setEnabled(True)
            self._log("No COG files found in selection")
            QMessageBox.information(
                self,
                "No COG Files",
                "No Cloud Optimized GeoTIFF files found in the selected data.\n\n"
                "Try using the Download button to download the data first.",
            )
            return

        # GDAL config was already set by setup_gdal_for_earthdata() in the worker
        added_count = 0
        for item in results:
            layer_name, vsi_path, original_url = (
                item[0],
                item[1],
                item[2] if len(item) > 2 else item[1],
            )
            try:
                self._log(f"Loading: {layer_name}")
                # Process events to update UI while loading
                QApplication.processEvents()
                layer = QgsRasterLayer(vsi_path, layer_name)
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    added_count += 1
                    self._log(f"Added layer: {layer_name}")
                else:
                    self._log(f"Could not load: {layer_name}", error=True)
            except Exception as e:
                self._log(f"Error adding layer {layer_name}: {e}", error=True)

        # Restore cursor and UI after all layers are loaded
        QApplication.restoreOverrideCursor()
        self.display_btn.setEnabled(True)

        if added_count > 0:
            self._log(f"Added {added_count} COG layer(s)")
            self.iface.messageBar().pushSuccess(
                "NASA Earthdata", f"Added {added_count} COG layer(s) to the map"
            )
        else:
            self._log("No COG files could be displayed")
            QMessageBox.information(
                self,
                "COG Display",
                "Could not display COG files from the selected data.\n\n"
                "NASA Earthdata COGs require authentication for streaming.\n"
                "Try using the Download button to download the data first.",
            )

    def _on_cog_error(self, error_msg):
        """Handle COG display error."""
        QApplication.restoreOverrideCursor()
        self.display_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._log(f"COG display error: {error_msg}", error=True)
        QMessageBox.critical(self, "Error", f"Failed to display COG:\n{error_msg}")

    def _download_data(self):
        """Download selected data (selected rows only, or all if none selected)."""
        # Check if specific rows are selected
        selected_rows = self.results_table.selectionModel().selectedRows()
        if selected_rows and self._search_results:
            indices = [row.row() for row in selected_rows]
            granules = [
                self._search_results[i]
                for i in indices
                if i < len(self._search_results)
            ]
            selection_msg = f"{len(granules)} selected"
        else:
            granules = self._search_results
            selection_msg = f"all {len(granules)}" if granules else "0"

        if not granules:
            QMessageBox.warning(self, "Warning", "No data to download.")
            return

        # Confirm download
        reply = QMessageBox.question(
            self,
            "Confirm Download",
            f"Download {selection_msg} granule(s)?\n\n"
            f"Tip: Select specific rows in the table to download only those items.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        # Get output directory
        default_dir = self.settings.value("NASAEarthdata/download_dir", "")
        output_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Download Directory",
            default_dir,
        )

        if not output_dir:
            return

        # Save directory preference
        self.settings.setValue("NASAEarthdata/download_dir", output_dir)

        # Disable UI during download
        self.download_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self._log(f"Downloading {len(granules)} granule(s) to {output_dir}...")

        # Start download worker
        self._download_worker = DataDownloadWorker(granules, output_dir)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.error.connect(self._on_download_error)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.start()

    def _on_download_progress(self, percent, message):
        """Handle download progress."""
        self.progress_bar.setValue(percent)
        self._log(message)

    def _on_download_finished(self, files):
        """Handle download completion."""
        self.download_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        self._log(f"Download complete! {len(files)} file(s) downloaded.")

        # Offer to add downloaded files to map
        if files:
            reply = QMessageBox.question(
                self,
                "Download Complete",
                f"Downloaded {len(files)} file(s).\n\nAdd raster files to the map?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )

            if reply == QMessageBox.Yes:
                for file_path in files:
                    if any(
                        str(file_path).lower().endswith(ext)
                        for ext in [".tif", ".tiff", ".nc", ".hdf"]
                    ):
                        layer_name = os.path.basename(str(file_path))
                        layer = QgsRasterLayer(str(file_path), layer_name)
                        if layer.isValid():
                            QgsProject.instance().addMapLayer(layer)
                            self._log(f"Added: {layer_name}")

    def _on_download_error(self, error_msg):
        """Handle download error."""
        self.download_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._log(f"Download error: {error_msg}", error=True)

        if "login" in error_msg.lower() or "auth" in error_msg.lower():
            QMessageBox.critical(
                self,
                "Authentication Error",
                f"NASA Earthdata authentication failed:\n{error_msg}\n\n"
                "Please configure your credentials in Settings.",
            )
        else:
            QMessageBox.critical(
                self, "Download Error", f"Download failed:\n{error_msg}"
            )

    def _reset(self):
        """Reset the search panel."""
        self.keyword_input.clear()
        self.bbox_input.clear()
        self.output_text.clear()
        self.status_label.setText("Ready")

        # Clear search results
        self._clear_results()

        # Reset advanced options
        self.advanced_check.setChecked(False)
        self.cloud_min_spin.setValue(0)
        self.cloud_max_spin.setValue(100)
        self.daynight_combo.setCurrentIndex(0)
        self.provider_input.clear()
        self.version_input.clear()
        self.granule_id_input.clear()
        self.orbit_min_spin.setValue(0)
        self.orbit_max_spin.setValue(0)

        # Reset dataset list
        if self._nasa_data_names:
            self.dataset_combo.clear()
            self.dataset_combo.addItems(self._nasa_data_names)
            self.dataset_combo.setCurrentText("HLSL30")

    def _log(self, message, error=False):
        """Log a message to the output text area."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = "ERROR: " if error else ""
        self.output_text.append(f"[{timestamp}] {prefix}{message}")

        # Scroll to bottom
        scrollbar = self.output_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Update status
        if error:
            self.status_label.setText(f"Error: {message[:50]}...")
            self.status_label.setStyleSheet("color: red; font-size: 10px;")
        else:
            self.status_label.setText(message[:50])
            self.status_label.setStyleSheet("color: gray; font-size: 10px;")

    def closeEvent(self, event):
        """Handle dock widget close event."""
        # Stop workers
        for worker in [
            self._catalog_worker,
            self._search_worker,
            self._download_worker,
            self._cog_worker,
        ]:
            if worker and worker.isRunning():
                worker.terminate()
                worker.wait()

        event.accept()
