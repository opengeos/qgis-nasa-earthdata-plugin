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
import uuid
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSettings, QDate, QEvent, QTimer
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
    QSizePolicy,
    QListWidget,
    QListWidgetItem,
)
from qgis.PyQt.QtGui import QFont
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
)

from ..core.net import https_only_urlopen

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
            self_data = self.data(Qt.ItemDataRole.UserRole)
            other_data = other.data(Qt.ItemDataRole.UserRole)
            # Handle None values
            if self_data is None:
                return True
            if other_data is None:
                return False
            return float(self_data) < float(other_data)
        except (ValueError, TypeError):
            # Fallback to string comparison if numeric comparison fails
            return super().__lt__(other)


def _compact_result_id(value, prefix_chars=34, suffix_chars=18):
    """Shorten long result IDs while preserving useful start and end tokens."""
    text = str(value)
    max_chars = prefix_chars + suffix_chars + 3
    if len(text) <= max_chars:
        return text
    return f"{text[:prefix_chars]}...{text[-suffix_chars:]}"


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

    def _field_value(self, row, *names):
        """Return the first non-empty value from possible catalog field names."""
        for name in names:
            value = row.get(name)
            if value:
                return str(value).strip()
        return ""

    def _item_for_row(self, row, duplicate_short_name=False):
        short_name = self._field_value(row, "ShortName")
        concept_id = self._field_value(row, "concept-id", "ConceptID", "concept_id")
        provider = self._field_value(row, "provider-id", "Provider", "provider")
        version = self._field_value(row, "Version")
        title = self._field_value(row, "EntryTitle")

        label = short_name
        if duplicate_short_name:
            details = []
            if version:
                details.append(f"v{version}")
            if provider:
                details.append(provider)
            if concept_id:
                details.append(concept_id)
            if details:
                label = f"{short_name} ({', '.join(details)})"

        return {
            "label": label,
            "short_name": short_name,
            "concept_id": concept_id,
            "provider": provider,
            "version": version,
            "title": title,
            "row": row,
        }

    def get_dataset_items(self):
        """Return combo-box items that preserve collection identity.

        ShortName is not unique in CMR. Duplicate ShortNames are disambiguated
        in the visible label while the full row, including concept-id, is kept
        in the item data.
        """
        counts = {}
        for row in self._rows:
            short_name = self._field_value(row, "ShortName")
            counts[short_name] = counts.get(short_name, 0) + 1

        return [
            self._item_for_row(
                row,
                duplicate_short_name=counts.get(self._field_value(row, "ShortName"), 0)
                > 1,
            )
            for row in self._rows
        ]

    def get_short_names(self):
        """Return a list of all ShortName values.

        Returns:
            List of ShortName strings.
        """
        return [r.get("ShortName", "") for r in self._rows]

    def filter_by_keyword(self, keyword):
        """Return dataset items matching ShortName, title, or collection metadata.

        Args:
            keyword: Lowercase search string.

        Returns:
            List of matching dataset item dictionaries.
        """
        result = []
        for item in self.get_dataset_items():
            haystack = " ".join(
                [
                    item.get("short_name", ""),
                    item.get("title", ""),
                    item.get("concept_id", ""),
                    item.get("provider", ""),
                    item.get("version", ""),
                ]
            ).lower()
            if keyword in haystack:
                result.append(item)
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
                with https_only_urlopen(NASA_DATA_URL, timeout=30) as resp:
                    text = resp.read().decode("utf-8")
                # Save raw TSV to cache
                with open(CATALOG_CACHE_FILE, "w", encoding="utf-8") as f:
                    f.write(text)
                reader = csv.DictReader(text.splitlines(), delimiter="\t")
                rows = list(reader)

            catalog = CatalogData(rows)
            self.finished.emit(catalog, catalog.get_dataset_items())

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
        concept_id,
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
        self.concept_id = concept_id
        self.bbox = bbox
        self.temporal = temporal
        self.max_items = max_items
        self.cloud_cover = cloud_cover
        self.day_night = day_night
        self.provider = provider
        self.version = version
        self.granule_id = granule_id
        self.orbit_number = orbit_number

    def _build_search_kwargs(self):
        """Build earthaccess search kwargs for this request."""
        if self.concept_id:
            kwargs = {"concept_id": self.concept_id}
        else:
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

        return kwargs

    def run(self):
        """Execute the search."""
        try:
            self.progress.emit("Importing earthaccess...")
            from ..core.venv_manager import import_earthaccess

            earthaccess = import_earthaccess()

            self.progress.emit("Searching NASA Earthdata...")
            kwargs = self._build_search_kwargs()

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


class COGDisplayWorker(QThread):
    """Worker thread for preparing streamed COG layers.

    Authenticates with earthaccess, primes a requests session for NASA's
    redirect flow, writes a GDAL-readable cookie jar, and emits /vsicurl/
    sources for QGIS to stream directly.
    """

    finished = pyqtSignal(list, object)  # results, cookie_file
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        granules,
        selected_cog_urls=None,
        display_mode="single",
        username=None,
        password=None,
        parent=None,
    ):
        """Initialize the COG display worker.

        Args:
            granules: List of earthaccess granule objects.
            selected_cog_urls: Specific COG URLs if provided.
            display_mode: "single" for individual layers, "rgb" for a VRT
                composite built from three selected single-band COGs.
            username: Earthdata username from Settings input box.
            password: Earthdata password from Settings input box.
            parent: Parent QObject.
        """
        super().__init__(parent)
        self.granules = granules
        self.selected_cog_urls = selected_cog_urls or []
        self.display_mode = display_mode
        self.username = (username or "").strip()
        self.password = (password or "").strip()

    def _login(self, earthaccess):
        """Authenticate with earthaccess using available credentials.

        Tries .netrc, environment variables, and Settings input boxes in order.

        Args:
            earthaccess: The earthaccess module.

        Returns:
            True if authenticated, False otherwise.
        """
        # Prefer .netrc for persistent credentials, then environment vars.
        for strategy in ("netrc", "environment"):
            try:
                auth = earthaccess.login(strategy=strategy, persist=True)
                if getattr(auth, "authenticated", False):
                    self.progress.emit(f"Authenticated via {strategy}")
                    return True
            except Exception:
                continue  # nosec B112

        # Try credentials from Settings input boxes
        if self.username and self.password:
            try:
                os.environ["EARTHDATA_USERNAME"] = self.username
                os.environ["EARTHDATA_PASSWORD"] = self.password
                auth = earthaccess.login(strategy="environment", persist=True)
                if getattr(auth, "authenticated", False):
                    self.progress.emit("Authenticated via settings input")
                    return True
            except Exception:
                pass  # nosec B110

        return False

    def _collect_cog_urls(self):
        """Collect COG/TIFF URLs from explicit selection or granule links."""
        if self.selected_cog_urls:
            return list(dict.fromkeys(self.selected_cog_urls))

        cog_urls = []
        for granule in self.granules:
            try:
                try:
                    links = granule.data_links(access="external")
                except TypeError:
                    links = granule.data_links()

                for link in links:
                    if any(ext in link.lower() for ext in [".tif", ".tiff"]) and (
                        link.startswith("http")
                    ):
                        cog_urls.append(link)
                        name = os.path.basename(link).split("?")[0]
                        self.progress.emit(f"Found: {name}")
                        break  # first TIFF per granule for the default path
            except Exception as e:
                self.progress.emit(f"Error processing granule: {e}")

        return cog_urls

    def _prime_session(self, session, url):
        """Follow auth redirects without downloading the COG body."""
        try:
            with session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
        except Exception as e:
            self.progress.emit(f"Warning: could not preflight COG URL: {e}")

    def _write_cookie_file(self, session):
        """Write requests cookies in Netscape format for GDAL /vsicurl/."""
        cookie_file = os.path.join(
            tempfile.gettempdir(), f"nasa_earthdata_gdal_{uuid.uuid4().hex}.cookies"
        )
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for cookie in session.cookies:
                domain = cookie.domain or ""
                include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
                path = cookie.path or "/"
                secure = "TRUE" if cookie.secure else "FALSE"
                expires = str(cookie.expires or 0)
                f.write(
                    "\t".join(
                        [
                            domain,
                            include_subdomains,
                            path,
                            secure,
                            expires,
                            str(cookie.name),
                            str(cookie.value),
                        ]
                    )
                    + "\n"
                )
        return cookie_file

    def _configure_gdal_streaming(self, gdal, cookie_file):
        """Configure GDAL for authenticated /vsicurl/ COG reads."""
        if cookie_file:
            gdal.SetConfigOption("GDAL_HTTP_COOKIEFILE", cookie_file)
            gdal.SetConfigOption("GDAL_HTTP_COOKIEJAR", cookie_file)
        netrc_path = os.path.expanduser("~/.netrc")
        if os.path.exists(netrc_path):
            gdal.SetConfigOption("GDAL_HTTP_NETRC", "YES")
            gdal.SetConfigOption("GDAL_HTTP_NETRC_FILE", netrc_path)
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", "tif,tiff,TIF,TIFF")
        gdal.SetConfigOption("GDAL_HTTP_UNSAFESSL", "YES")
        gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "3")
        gdal.SetConfigOption("VSI_CACHE", "TRUE")
        gdal.SetConfigOption("VSI_CACHE_SIZE", "100000000")  # 100MB cache

    def _create_rgb_vrt(self, layer_name, sources, gdal):
        """Create a small local VRT that references three streamed COG sources."""
        if len(sources) != 3:
            return None

        vrt_id = uuid.uuid4().hex
        vrt_path = os.path.join(
            tempfile.gettempdir(),
            f"nasa_earthdata_{vrt_id}_rgb.vrt",
        )
        vsimem_vrt_path = f"/vsimem/nasa_earthdata_{vrt_id}_rgb.vrt"
        try:
            vrt = gdal.BuildVRT(
                vsimem_vrt_path,
                sources,
                options=gdal.BuildVRTOptions(separate=True),
            )
            if vrt is None:
                return None

            for band_index, color_interp in enumerate(
                (gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand),
                start=1,
            ):
                band = vrt.GetRasterBand(band_index)
                if band is not None:
                    band.SetColorInterpretation(color_interp)

            vrt.FlushCache()
            vrt_xml = vrt.GetMetadata("xml:VRT")
            if vrt_xml:
                with open(vrt_path, "w", encoding="utf-8") as f:
                    f.write(vrt_xml[0])
            else:
                translated = gdal.Translate(vrt_path, vrt, format="VRT")
                if translated is not None:
                    translated = None
            vrt = None

            if not os.path.exists(vrt_path):
                self.progress.emit("RGB VRT was not written to disk")
                return None

            self.progress.emit(f"Built RGB VRT: {vrt_path}")
            return vrt_path
        finally:
            try:
                gdal.Unlink(vsimem_vrt_path)
            except Exception:
                pass  # nosec B110

    def run(self):
        """Authenticate and emit streamed COG paths."""
        try:
            from ..core.venv_manager import import_earthaccess

            earthaccess = import_earthaccess()

            self.progress.emit("Authenticating with NASA Earthdata...")
            if not self._login(earthaccess):
                self.error.emit(
                    "NASA Earthdata authentication failed.\n"
                    "Please check your credentials in Settings."
                )
                return

            cog_urls = self._collect_cog_urls()
            if not cog_urls:
                self.finished.emit([], None)
                return

            session = earthaccess.get_requests_https_session()
            self._prime_session(session, cog_urls[0])
            cookie_file = self._write_cookie_file(session)

            if self.display_mode == "rgb":
                rgb_urls = cog_urls[:3]
                layer_name = "NASA Earthdata RGB Composite"
                self.progress.emit("Preparing RGB composite stream")
                from osgeo import gdal

                self._configure_gdal_streaming(gdal, cookie_file)
                vrt_path = self._create_rgb_vrt(
                    layer_name, [f"/vsicurl/{url}" for url in rgb_urls], gdal
                )
                results = [(layer_name, vrt_path)] if vrt_path else []
            else:
                results = []
                for url in cog_urls:
                    layer_name = os.path.basename(url).split("?")[0]
                    results.append((layer_name, f"/vsicurl/{url}", url))
                    self.progress.emit(f"Prepared stream: {layer_name}")

            self.finished.emit(results, cookie_file)

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
            from ..core.venv_manager import import_earthaccess

            earthaccess = import_earthaccess()

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

        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        # Set minimum width but allow resizing
        self.setMinimumWidth(300)

        # Data storage
        self._nasa_data = None
        self._nasa_data_names = []
        self._search_results = None
        self._search_gdf = None
        self._footprints_layer = None
        self._selected_footprints_layer = None
        self._temp_footprints_file = None
        self._bbox_map_tool = None
        self._previous_map_tool = None
        self._adjusting_results_columns = False

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
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

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
        search_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        # Keyword filter
        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("Filter datasets by keyword...")
        self.keyword_input.returnPressed.connect(self._filter_datasets)
        search_layout.addRow("Keyword:", self.keyword_input)

        # Dataset dropdown
        self.dataset_combo = QComboBox()
        self.dataset_combo.setMaxVisibleItems(20)
        self.dataset_combo.currentIndexChanged.connect(self._on_dataset_changed)
        # Use AdjustToContents so dropdown shows full text
        self.dataset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.dataset_combo.setMinimumContentsLength(20)
        search_layout.addRow("Dataset:", self.dataset_combo)

        # Dataset title (read-only), wrapped so long collection titles stay visible.
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.title_label.setMinimumHeight(40)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.title_label.setStyleSheet("QLabel { color: palette(text); padding: 2px; }")
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
        self.draw_bbox_btn = QPushButton("Draw Bbox")
        self.draw_bbox_btn.setCheckable(True)
        self.draw_bbox_btn.toggled.connect(self._toggle_draw_bbox)
        bbox_btn_layout.addWidget(self.draw_bbox_btn)
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
        advanced_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
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
        # Start with practical widths, but keep all columns user-resizable.
        results_header = self.results_table.horizontalHeader()
        results_header.setSectionsClickable(True)
        results_header.setSectionsMovable(False)
        results_header.setMinimumSectionSize(45)
        results_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        results_header.setStretchLastSection(False)
        results_header.sectionResized.connect(self._on_results_section_resized)
        self._set_default_results_column_widths()
        self.results_table.installEventFilter(self)
        self.results_table.viewport().installEventFilter(self)
        self.results_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
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

        # COG file selection controls
        cog_mode_layout = QHBoxLayout()
        cog_mode_layout.addWidget(QLabel("COG Mode:"))
        self.cog_mode_combo = QComboBox()
        self.cog_mode_combo.addItem("Single band", "single")
        self.cog_mode_combo.addItem("RGB composite", "rgb")
        self.cog_mode_combo.setEnabled(False)
        self.cog_mode_combo.currentIndexChanged.connect(self._on_cog_mode_changed)
        cog_mode_layout.addWidget(self.cog_mode_combo, 1)
        results_layout.addLayout(cog_mode_layout)

        cog_layout = QVBoxLayout()
        self.cog_files_label = QLabel("COG Files:")
        cog_layout.addWidget(self.cog_files_label)
        self.cog_list = QListWidget()
        self.cog_list.setEnabled(False)
        self.cog_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.cog_list.setMaximumHeight(86)
        self.cog_list.setToolTip(
            "Select one or more COG files. Use three selected files for RGB."
        )
        results_layout.addLayout(cog_layout)
        cog_layout.addWidget(self.cog_list)

        self.rgb_channel_widget = QWidget()
        rgb_channel_layout = QFormLayout(self.rgb_channel_widget)
        rgb_channel_layout.setContentsMargins(0, 0, 0, 0)
        self.rgb_red_combo = QComboBox()
        self.rgb_green_combo = QComboBox()
        self.rgb_blue_combo = QComboBox()
        for combo in (
            self.rgb_red_combo,
            self.rgb_green_combo,
            self.rgb_blue_combo,
        ):
            combo.setEnabled(False)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            combo.setMinimumContentsLength(18)
        rgb_channel_layout.addRow("Red:", self.rgb_red_combo)
        rgb_channel_layout.addRow("Green:", self.rgb_green_combo)
        rgb_channel_layout.addRow("Blue:", self.rgb_blue_combo)
        self.rgb_channel_widget.setVisible(False)
        results_layout.addWidget(self.rgb_channel_widget)

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
        output_layout.setContentsMargins(6, 6, 6, 6)
        output_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setMinimumHeight(100)
        self.output_text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.output_text.setPlaceholderText("Status messages will appear here...")
        output_layout.addWidget(self.output_text)

        layout.addWidget(output_group, 1)

        # Status label
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.status_label)

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

    def _populate_dataset_combo(self, items):
        """Populate dataset combo while preserving row metadata as item data."""
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        for item in items:
            self.dataset_combo.addItem(item.get("label", ""), item)
        self.dataset_combo.blockSignals(False)
        self._on_dataset_changed(self.dataset_combo.currentIndex())

    def _select_default_dataset(self):
        """Select the default HLSL30 collection, preferring the newer duplicate."""
        hlsl30_indices = [
            index
            for index in range(self.dataset_combo.count())
            if (self.dataset_combo.itemData(index) or {}).get("short_name") == "HLSL30"
        ]
        if hlsl30_indices:
            default_index = (
                hlsl30_indices[1] if len(hlsl30_indices) > 1 else hlsl30_indices[0]
            )
            self.dataset_combo.setCurrentIndex(default_index)

    def _on_catalog_loaded(self, df, items):
        """Handle catalog loaded."""
        self.refresh_catalog_btn.setEnabled(True)
        self._nasa_data = df
        self._nasa_data_names = items

        self._populate_dataset_combo(items)
        self._select_default_dataset()

        self._log(f"Loaded {len(items)} datasets")
        self.status_label.setText(f"{len(items)} datasets available")

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
        """Filter datasets based on ShortName, title, and collection metadata."""
        keyword = self.keyword_input.text().strip().lower()

        if not keyword:
            self._populate_dataset_combo(self._nasa_data_names)
            return

        if self._nasa_data is None:
            return

        filtered = self._nasa_data.filter_by_keyword(keyword)

        self._populate_dataset_combo(filtered)

        self._log(f"Found {len(filtered)} datasets matching '{keyword}'")

    def _on_dataset_changed(self, _index):
        """Handle dataset selection change."""
        item = self.dataset_combo.currentData()
        if self._nasa_data is None or not item:
            self.title_label.clear()
            self.title_label.setToolTip("")
            self._clear_results()
            return

        try:
            title = item.get("title")
            if title:
                self.title_label.setText(title)
                self.title_label.setToolTip(title)
            else:
                self.title_label.clear()
                self.title_label.setToolTip("")
        except Exception:
            self.title_label.clear()
            self.title_label.setToolTip("")

        # Clear previous search results when dataset changes
        self._clear_results()

    def _clear_results(self):
        """Clear search results without resetting other settings."""
        self.results_table.setRowCount(0)
        self.results_label.setText("No search performed yet")
        self.display_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.zoom_footprints_btn.setEnabled(False)
        self.cog_list.clear()
        self.cog_list.setEnabled(False)
        self.cog_mode_combo.setEnabled(False)
        self._clear_rgb_channel_combos()
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

    def _extent_to_wgs84_bbox_text(self, extent):
        """Convert a map-canvas extent to formatted WGS84 bbox text."""
        canvas = self.iface.mapCanvas()
        crs = canvas.mapSettings().destinationCrs()
        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(
                crs,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            )
            extent = transform.transformBoundingBox(extent)

        xmin = min(extent.xMinimum(), extent.xMaximum())
        ymin = min(extent.yMinimum(), extent.yMaximum())
        xmax = max(extent.xMinimum(), extent.xMaximum())
        ymax = max(extent.yMinimum(), extent.yMaximum())
        return f"{xmin:.4f}, {ymin:.4f}, {xmax:.4f}, {ymax:.4f}"

    def _set_bbox_from_drawn_extent(self, extent):
        """Set bbox input from a drawn map extent and restore previous map tool."""
        self.bbox_input.setText(self._extent_to_wgs84_bbox_text(extent))
        self._log("Bounding box set from drawn rectangle")
        self._finish_draw_bbox()

    def _finish_draw_bbox(self):
        """Restore the previous map tool after bbox drawing."""
        canvas = self.iface.mapCanvas()
        self.draw_bbox_btn.blockSignals(True)
        self.draw_bbox_btn.setChecked(False)
        self.draw_bbox_btn.blockSignals(False)
        self.draw_bbox_btn.setEnabled(True)

        if self._previous_map_tool is not None:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except Exception:
                pass  # nosec B110

        self._previous_map_tool = None
        self._bbox_map_tool = None

    def _toggle_draw_bbox(self, checked):
        """Start or cancel bbox drawing from the toggle button."""
        if checked:
            self._start_draw_bbox()
        else:
            self._finish_draw_bbox()

    def _start_draw_bbox(self):
        """Activate a one-shot map tool for drawing a bounding box."""
        try:
            from qgis.PyQt.QtGui import QColor
            from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
            from qgis.gui import QgsMapTool, QgsRubberBand
        except Exception as e:
            self.draw_bbox_btn.blockSignals(True)
            self.draw_bbox_btn.setChecked(False)
            self.draw_bbox_btn.blockSignals(False)
            QMessageBox.warning(
                self,
                "Draw Bbox",
                f"Could not activate the bounding box drawing tool:\n{e}",
            )
            return

        dock = self
        canvas = self.iface.mapCanvas()

        class BboxMapTool(QgsMapTool):
            def __init__(self, map_canvas):
                super().__init__(map_canvas)
                self.canvas = map_canvas
                self.start_point = None
                geometry_type = getattr(
                    getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes),
                    "PolygonGeometry",
                    2,
                )
                self.rubber_band = QgsRubberBand(map_canvas, geometry_type)
                self.rubber_band.setColor(QColor(255, 235, 59, 80))
                if hasattr(self.rubber_band, "setStrokeColor"):
                    self.rubber_band.setStrokeColor(QColor(255, 235, 59, 220))
                self.rubber_band.setWidth(2)

            def canvasPressEvent(self, event):
                self.start_point = self.toMapCoordinates(event.pos())
                self._update_rubber_band(self.start_point)

            def canvasMoveEvent(self, event):
                if self.start_point is None:
                    return
                self._update_rubber_band(self.toMapCoordinates(event.pos()))

            def canvasReleaseEvent(self, event):
                if self.start_point is None:
                    dock._finish_draw_bbox()
                    return

                end_point = self.toMapCoordinates(event.pos())
                rect = QgsRectangle(self.start_point, end_point)
                self.rubber_band.reset()
                if rect.width() == 0 or rect.height() == 0:
                    dock._finish_draw_bbox()
                    return

                dock._set_bbox_from_drawn_extent(rect)

            def deactivate(self):
                try:
                    self.rubber_band.reset()
                except Exception:
                    pass  # nosec B110
                super().deactivate()

            def _update_rubber_band(self, end_point):
                rect = QgsRectangle(self.start_point, end_point)
                points = [
                    QgsPointXY(rect.xMinimum(), rect.yMinimum()),
                    QgsPointXY(rect.xMinimum(), rect.yMaximum()),
                    QgsPointXY(rect.xMaximum(), rect.yMaximum()),
                    QgsPointXY(rect.xMaximum(), rect.yMinimum()),
                    QgsPointXY(rect.xMinimum(), rect.yMinimum()),
                ]
                self.rubber_band.setToGeometry(QgsGeometry.fromPolygonXY([points]))

        self._previous_map_tool = canvas.mapTool()
        self._bbox_map_tool = BboxMapTool(canvas)
        canvas.setMapTool(self._bbox_map_tool)
        self._log("Draw a rectangle on the map to set the bounding box")

    def _toggle_advanced_options(self, checked):
        """Toggle visibility of advanced options."""
        self.advanced_widget.setVisible(checked)

    def _search_data(self):
        """Search NASA Earthdata."""
        dataset_item = self.dataset_combo.currentData()
        if dataset_item:
            short_name = dataset_item.get("short_name", "")
            concept_id = dataset_item.get("concept_id", "")
            dataset_label = dataset_item.get("label", short_name)
        else:
            short_name = self.dataset_combo.currentText()
            concept_id = ""
            dataset_label = short_name
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
        if concept_id:
            self._log(f"Searching {dataset_label} by concept-id...")
        else:
            self._log(f"Searching {short_name}...")

        # Start search worker
        self._search_worker = DataSearchWorker(
            short_name,
            concept_id,
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
                id_item = QTableWidgetItem(_compact_result_id(native_id))
                id_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                id_item.setToolTip(str(native_id))  # Show full ID on hover
                id_item.setData(
                    Qt.ItemDataRole.UserRole, i
                )  # Stable index into _search_results/_search_gdf
                self.results_table.setItem(i, 0, id_item)

                date_item = QTableWidgetItem(str(time_start))
                date_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                self.results_table.setItem(i, 1, date_item)

                # Store raw bytes for proper numeric sorting
                size_item = NumericTableWidgetItem(str(size_display))
                size_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                size_item.setData(
                    Qt.ItemDataRole.UserRole, size_bytes
                )  # Store raw value for sorting
                self.results_table.setItem(i, 2, size_item)
            except Exception:
                id_item = QTableWidgetItem(f"Item {i+1}")
                id_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                id_item.setToolTip(f"Item {i+1}")
                id_item.setData(Qt.ItemDataRole.UserRole, i)
                self.results_table.setItem(i, 0, id_item)
                date_item = QTableWidgetItem("N/A")
                date_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                self.results_table.setItem(i, 1, date_item)

                size_item = NumericTableWidgetItem("N/A")
                size_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                size_item.setData(Qt.ItemDataRole.UserRole, 0)  # Store 0 for N/A
                self.results_table.setItem(i, 2, size_item)

        # Re-enable sorting after population
        self.results_table.setSortingEnabled(True)
        self._set_default_results_column_widths()

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
        self._remove_selected_footprints()
        if self._footprints_layer is not None:
            try:
                # Remove layer from project
                QgsProject.instance().removeMapLayer(self._footprints_layer.id())
            except Exception:
                pass  # nosec B110

            # Delete the layer object to release file handles (important on Windows)
            try:
                del self._footprints_layer
            except Exception:
                pass  # nosec B110
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

    def _remove_selected_footprints(self):
        """Remove the outline-only selected footprint overlay."""
        if self._selected_footprints_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(
                    self._selected_footprints_layer.id()
                )
            except Exception:
                pass  # nosec B110
            try:
                del self._selected_footprints_layer
            except Exception:
                pass  # nosec B110
            self._selected_footprints_layer = None

    def _add_selected_footprints_overlay(self, features):
        """Draw selected footprints as a yellow outline layer above results."""
        if not features:
            return

        try:
            from qgis.core import (
                QgsFeature,
                QgsFillSymbol,
                QgsSingleSymbolRenderer,
                QgsWkbTypes,
            )

            geometry_name = QgsWkbTypes.displayString(self._footprints_layer.wkbType())
            if "Polygon" not in geometry_name:
                geometry_name = "Polygon"
            selected_layer = QgsVectorLayer(
                f"{geometry_name}?crs=EPSG:4326",
                "Selected NASA Earthdata Footprints",
                "memory",
            )
            provider = selected_layer.dataProvider()
            overlay_features = []
            for source_feature in features:
                feature = QgsFeature()
                feature.setGeometry(source_feature.geometry())
                overlay_features.append(feature)

            provider.addFeatures(overlay_features)
            selected_layer.updateExtents()

            symbol = QgsFillSymbol.createSimple(
                {
                    "color": "255,255,0,0",
                    "outline_color": "#ffeb3b",
                    "outline_width": "1.2",
                }
            )
            selected_layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            QgsProject.instance().addMapLayer(selected_layer)
            self._selected_footprints_layer = selected_layer
        except Exception as e:
            self._log(f"Error drawing selected footprint outline: {e}", error=True)

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
            self.cog_list.clear()
            self.cog_list.setEnabled(False)
            self.cog_mode_combo.setEnabled(False)
            self._clear_rgb_channel_combos()

        self._sync_footprint_selection_from_table()

    def _get_result_index_for_table_row(self, table_row):
        """Map current table row (after sorting) to original search result index."""
        item = self.results_table.item(table_row, 0)
        if item is None:
            return table_row

        result_idx = item.data(Qt.ItemDataRole.UserRole)
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
            self._remove_selected_footprints()

            if not selected_indices:
                self.iface.mapCanvas().refresh()
                return

            selected_features = []
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
                    selected_features.append(feature)

            self._add_selected_footprints_overlay(selected_features)
            self.iface.mapCanvas().refresh()
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
                Qt.SortOrder.DescendingOrder
                if current_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            # Default to ascending for new column
            new_order = Qt.SortOrder.AscendingOrder

        # Apply the sort
        self.results_table.sortItems(logical_index, new_order)
        self._log(
            f"Sorted by column {logical_index} ({'descending' if new_order == Qt.SortOrder.DescendingOrder else 'ascending'})"
        )

    def _set_default_results_column_widths(self):
        """Set initial Date/Size widths and make ID fill the remaining space."""
        if self._adjusting_results_columns:
            return

        self._adjusting_results_columns = True
        try:
            self.results_table.setColumnWidth(1, 90)
            self.results_table.setColumnWidth(2, 80)
        finally:
            self._adjusting_results_columns = False

        self._fit_results_columns_to_width()

    def _fit_results_columns_to_width(self):
        """Make the ID column fill leftover width after Date and Size columns."""
        if self._adjusting_results_columns:
            return

        viewport_width = self.results_table.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.results_table.width()
        if viewport_width <= 0:
            return

        header = self.results_table.horizontalHeader()
        min_width = max(45, header.minimumSectionSize())
        date_width = max(min_width, self.results_table.columnWidth(1) or 90)
        size_width = max(min_width, self.results_table.columnWidth(2) or 80)
        id_width = max(180, viewport_width - date_width - size_width)

        self._adjusting_results_columns = True
        try:
            self.results_table.setColumnWidth(0, id_width)
        finally:
            self._adjusting_results_columns = False

    def _on_results_section_resized(self, logical_index, _old_size, _new_size):
        """Keep ID as the fill column when Date or Size is resized."""
        if logical_index in (1, 2) and not self._adjusting_results_columns:
            QTimer.singleShot(0, self._fit_results_columns_to_width)

    def eventFilter(self, obj, event):
        """Keep results columns fitted when the table viewport changes size."""
        if (
            hasattr(self, "results_table")
            and obj in (self.results_table, self.results_table.viewport())
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._fit_results_columns_to_width)
        return super().eventFilter(obj, event)

    def _sort_cog_links(self, cog_links):
        """Return COG links sorted by displayed filename."""
        return sorted(
            cog_links, key=lambda link: os.path.basename(link).split("?")[0].lower()
        )

    def _populate_cog_dropdown(self, row_index):
        """Populate the COG list with available files for the selected granule."""
        self.cog_list.clear()
        self.cog_list.setEnabled(False)
        self.cog_mode_combo.setEnabled(False)
        self._clear_rgb_channel_combos()

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
            cog_links = self._sort_cog_links(cog_links)

            if cog_links:
                # Add COG files to list (show just filenames)
                for link in cog_links:
                    filename = os.path.basename(link).split("?")[0]
                    item = QListWidgetItem(filename)
                    item.setToolTip(link)
                    item.setData(Qt.ItemDataRole.UserRole, link)
                    self.cog_list.addItem(item)

                self.cog_list.setEnabled(True)
                self.cog_mode_combo.setEnabled(True)
                if self.cog_list.count() > 0:
                    self.cog_list.setCurrentRow(0)
                self._populate_rgb_channel_combos(cog_links)
                self._log(f"Found {len(cog_links)} COG file(s) for selected granule")
            else:
                self.cog_list.addItem("No COG files found")

        except Exception as e:
            self.cog_list.addItem(f"Error: {str(e)[:30]}")

    def _on_cog_mode_changed(self, _index):
        """Toggle controls for the selected COG display mode."""
        is_rgb = self.cog_mode_combo.currentData() == "rgb"
        self.cog_files_label.setVisible(not is_rgb)
        self.cog_list.setVisible(not is_rgb)
        self.rgb_channel_widget.setVisible(is_rgb)

    def _clear_rgb_channel_combos(self):
        """Clear and disable RGB channel selectors."""
        for combo in (
            self.rgb_red_combo,
            self.rgb_green_combo,
            self.rgb_blue_combo,
        ):
            combo.clear()
            combo.setEnabled(False)

    def _populate_rgb_channel_combos(self, cog_links):
        """Populate RGB channel selectors and choose common natural-color bands."""
        channel_combos = (
            self.rgb_red_combo,
            self.rgb_green_combo,
            self.rgb_blue_combo,
        )
        for combo in channel_combos:
            combo.clear()
            for link in cog_links:
                filename = os.path.basename(link).split("?")[0]
                combo.addItem(filename, link)
            combo.setEnabled(bool(cog_links))

        defaults = self._guess_rgb_channel_indices(cog_links)
        for combo, index in zip(channel_combos, defaults):
            if 0 <= index < combo.count():
                combo.setCurrentIndex(index)

    def _guess_rgb_channel_indices(self, cog_links):
        """Guess Red/Green/Blue defaults from common COG filename band tokens."""

        def find_band(candidates):
            for idx, link in enumerate(cog_links):
                name = os.path.basename(link).split("?")[0].lower()
                for token in candidates:
                    if token in name:
                        return idx
            return -1

        red = find_band((".b04.", "_b04_", "-b04-", ".b4.", "_b4_", "-b4-", "red"))
        green = find_band((".b03.", "_b03_", "-b03-", ".b3.", "_b3_", "-b3-", "green"))
        blue = find_band((".b02.", "_b02_", "-b02-", ".b2.", "_b2_", "-b2-", "blue"))

        fallback = list(range(min(3, len(cog_links))))
        while len(fallback) < 3:
            fallback.append(-1)
        return (
            red if red >= 0 else fallback[0],
            green if green >= 0 else fallback[1],
            blue if blue >= 0 else fallback[2],
        )

    def _get_selected_cog_urls(self):
        """Return selected COG URLs from the list in visual row order."""
        if not self.cog_list.isEnabled():
            return []

        selected_items = self.cog_list.selectedItems()
        if not selected_items and self.cog_list.currentItem() is not None:
            selected_items = [self.cog_list.currentItem()]

        selected_rows = sorted(self.cog_list.row(item) for item in selected_items)
        urls = []
        for row in selected_rows:
            item = self.cog_list.item(row)
            if item is None:
                continue
            url = item.data(Qt.ItemDataRole.UserRole)
            if url:
                urls.append(url)
        return urls

    def _get_rgb_channel_urls(self):
        """Return selected RGB URLs in Red, Green, Blue order."""
        urls = []
        labels = []
        for label, combo in (
            ("Red", self.rgb_red_combo),
            ("Green", self.rgb_green_combo),
            ("Blue", self.rgb_blue_combo),
        ):
            url = combo.currentData()
            if not url:
                return [], []
            urls.append(url)
            labels.append(f"{label}={combo.currentText()}")
        return urls, labels

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
        display_mode = self.cog_mode_combo.currentData() or "single"
        selected_cog_urls = self._get_selected_cog_urls()
        rgb_labels = []

        if display_mode == "rgb":
            selected_cog_urls, rgb_labels = self._get_rgb_channel_urls()
            if len(selected_cog_urls) != 3:
                QMessageBox.warning(
                    self,
                    "RGB Composite",
                    "Select a COG file for each RGB channel.",
                )
                return
            if len(set(selected_cog_urls)) != 3:
                QMessageBox.warning(
                    self,
                    "RGB Composite",
                    "Select three different COG files for RGB display.",
                )
                return

        if selected_cog_urls:
            if display_mode == "rgb":
                self._log("Displaying RGB composite: " + ", ".join(rgb_labels))
            else:
                self._log(f"Displaying {len(selected_cog_urls)} selected COG stream(s)")
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

        # Read credentials from Settings input boxes as fallback
        username, password = self._get_settings_input_credentials()

        # Start COG worker
        if selected_cog_urls:
            self._cog_worker = COGDisplayWorker(
                [],
                selected_cog_urls=selected_cog_urls,
                display_mode=display_mode,
                username=username,
                password=password,
            )
        else:
            self._cog_worker = COGDisplayWorker(
                self._get_selected_granules(),
                display_mode="single",
                username=username,
                password=password,
            )
        self._cog_worker.finished.connect(self._on_cog_finished)
        self._cog_worker.error.connect(self._on_cog_error)
        self._cog_worker.progress.connect(self._log)
        self._cog_worker.start()

    def _get_settings_input_credentials(self):
        """Read Earthdata credentials from the Settings dock input boxes.

        Returns:
            Tuple of (username, password) strings. Empty strings if unavailable.
        """
        username = ""
        password = ""  # nosec B105
        try:
            settings_dock = self.iface.mainWindow().findChild(
                QDockWidget, "NASAEarthdataSettingsDock"
            )
            if settings_dock is not None:
                if hasattr(settings_dock, "username_input"):
                    username = settings_dock.username_input.text().strip()
                if hasattr(settings_dock, "password_input"):
                    password = settings_dock.password_input.text().strip()
        except Exception:
            pass  # nosec B110
        return username, password

    def _on_cog_finished(self, results, cookie_file=None):
        """Handle COG display completion.

        Args:
            results: List of (layer_name, raster_path) tuples. Single-band COGs
                use /vsicurl/ paths; RGB entries use worker-created local VRTs.
            cookie_file: Optional cookie file for /vsicurl loading.
        """
        from osgeo import gdal

        # Keep wait cursor while loading layers
        self.progress_bar.setVisible(False)

        if not results:
            self.display_btn.setEnabled(True)
            self._log("No COG files found in selection")
            QMessageBox.information(
                self,
                "No COG Files",
                "No Cloud Optimized GeoTIFF files found in the selected data.\n\n"
                "Try using the Download button to download the data first.",
            )
            return

        using_vsicurl = any(
            len(item) > 1
            and isinstance(item[1], str)
            and item[1].startswith("/vsicurl/")
            for item in results
        )
        if using_vsicurl:
            # Configure GDAL auth and conservative network behavior for streamed COGs.
            if cookie_file:
                gdal.SetConfigOption("GDAL_HTTP_COOKIEFILE", cookie_file)
                gdal.SetConfigOption("GDAL_HTTP_COOKIEJAR", cookie_file)
            netrc_path = os.path.expanduser("~/.netrc")
            if os.path.exists(netrc_path):
                gdal.SetConfigOption("GDAL_HTTP_NETRC", "YES")
                gdal.SetConfigOption("GDAL_HTTP_NETRC_FILE", netrc_path)
            gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
            gdal.SetConfigOption(
                "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", "tif,tiff,TIF,TIFF"
            )
            gdal.SetConfigOption("GDAL_HTTP_UNSAFESSL", "YES")
            gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "3")
            gdal.SetConfigOption("VSI_CACHE", "TRUE")
            gdal.SetConfigOption("VSI_CACHE_SIZE", "100000000")  # 100MB cache

        added_count = 0
        for item in results:
            layer_name = item[0]
            raster_path = item[1]
            try:
                self._log(f"Loading: {layer_name}")
                # Process events to update UI while loading
                QApplication.processEvents()

                layer = QgsRasterLayer(raster_path, layer_name)

                if layer is not None and layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    added_count += 1
                    self._log(f"Added layer: {layer_name}")
                else:
                    self._log(f"Could not load: {layer_name}", error=True)
            except Exception as e:
                self._log(f"Error adding layer {layer_name}: {e}", error=True)

        # Restore UI after all layers are loaded
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
                "The streamed COG request did not return a valid GeoTIFF.\n"
                "Please verify NASA Earthdata credentials in Settings.",
            )

    def _on_cog_error(self, error_msg):
        """Handle COG display error."""
        self.display_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._log(f"COG display error: {error_msg}", error=True)
        QMessageBox.critical(self, "Error", f"Failed to display COG:\n{error_msg}")

    def _download_data(self):
        """Download selected data (selected rows only, or all if none selected)."""
        # Check if specific rows are selected
        selected_rows = self.results_table.selectionModel().selectedRows()
        if selected_rows and self._search_results:
            indices = self._get_selected_result_indices()
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
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
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )

            if reply == QMessageBox.StandardButton.Yes:
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
            self._populate_dataset_combo(self._nasa_data_names)
            self._select_default_dataset()

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
        self._finish_draw_bbox()

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
