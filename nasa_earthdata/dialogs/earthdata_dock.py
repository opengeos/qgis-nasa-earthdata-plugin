"""
NASA Earthdata Search Dock Widget

This module provides a dockable panel for searching, visualizing,
and downloading NASA Earthdata products in QGIS.
"""

import os
import json
import html
import hashlib
import platform
import tempfile
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    QSettings,
    QDate,
    QEvent,
    QTimer,
    QItemSelectionModel,
)
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QTextEdit,
    QTextBrowser,
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
    QInputDialog,
    QDialog,
    QDialogButtonBox,
)
from qgis.PyQt.QtGui import QFont
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsContrastEnhancement,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
)

from ..core.net import https_only_urlopen
from ..core.workflows import (
    build_search_preset,
    cmr_collection_summary,
    cmr_collection_url,
    cog_links_from_links,
    delete_recent_search,
    delete_search_preset,
    download_manifest_path,
    download_queue_state_path,
    granule_export_row,
    granule_citation_links,
    granule_inaccessible_quicklook_links,
    granule_links,
    granule_native_id,
    granule_quicklook_links,
    granules_to_export_rows,
    granules_to_stac_item_collection,
    likely_existing_download_files,
    load_download_queue_state,
    load_recent_searches,
    load_search_presets,
    record_recent_search,
    upsert_search_preset,
    workflow_dir,
    write_download_manifest,
    write_download_queue_state,
    write_granules_json,
    write_results_stac,
    write_results_csv,
    write_results_geojson,
    write_workflow_bundle,
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

    def __init__(
        self,
        force_refresh=False,
        catalog_url=None,
        cache_dir=None,
        cache_enabled=True,
        parent=None,
    ):
        super().__init__(parent)
        self.force_refresh = force_refresh
        self.catalog_url = catalog_url or NASA_DATA_URL
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else CACHE_DIR
        self.cache_enabled = cache_enabled

    def run(self):
        """Load the catalog from cache or download."""
        try:
            import csv

            cache_file = self.cache_dir / CATALOG_CACHE_FILE.name
            if self.cache_enabled:
                self.cache_dir.mkdir(parents=True, exist_ok=True)

            # Check if cache exists and is fresh
            use_cache = False
            if self.cache_enabled and not self.force_refresh and cache_file.exists():
                cache_age = datetime.now().timestamp() - cache_file.stat().st_mtime
                if cache_age < CATALOG_CACHE_MAX_AGE_DAYS * 24 * 3600:
                    use_cache = True
                    self.progress.emit("Loading catalog from cache...")

            if use_cache:
                with open(cache_file, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f, delimiter="\t")
                    rows = list(reader)
            else:
                self.progress.emit("Downloading NASA Earthdata catalog...")
                with https_only_urlopen(self.catalog_url, timeout=30) as resp:
                    text = resp.read().decode("utf-8")
                # Save raw TSV to cache
                if self.cache_enabled:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        f.write(text)
                reader = csv.DictReader(text.splitlines(), delimiter="\t")
                rows = list(reader)

            catalog = CatalogData(rows)
            self.finished.emit(catalog, catalog.get_dataset_items())

        except Exception as e:
            self.error.emit(str(e))


class CollectionInfoWorker(QThread):
    """Worker thread for fetching live CMR collection metadata."""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, dataset_item, parent=None):
        super().__init__(parent)
        self.dataset_item = dataset_item or {}

    def run(self):
        """Fetch and summarize one CMR collection."""
        try:
            url = cmr_collection_url(self.dataset_item)
            if not url:
                self.error.emit("No dataset selected")
                return
            with https_only_urlopen(url, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            summary = cmr_collection_summary(payload)
            if not summary:
                self.error.emit("No CMR collection metadata found")
                return
            self.finished.emit(summary)
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

    finished = pyqtSignal(list, str, list)  # downloaded files, manifest, queue rows
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    queue_update = pyqtSignal(int, str, str, list)  # row, status, message, files

    def __init__(
        self,
        granules,
        output_dir,
        threads=1,
        skip_existing=True,
        parent=None,
    ):
        super().__init__(parent)
        self.granules = granules
        self.output_dir = output_dir
        self.threads = max(1, int(threads or 1))
        self.skip_existing = skip_existing
        self._cancelled = False

    def cancel(self):
        """Request cancellation after the current granule finishes."""
        self._cancelled = True

    def run(self):
        """Execute the download."""
        try:
            import inspect

            from ..core.venv_manager import import_earthaccess

            earthaccess = import_earthaccess()

            total = len(self.granules or [])
            if total == 0:
                self.finished.emit([], "", [])
                return

            self.progress.emit(5, "Preparing download queue...")
            downloaded_files = []
            queue_rows = []
            download_kwargs = {"local_path": self.output_dir}
            try:
                signature = inspect.signature(earthaccess.download)
                if "threads" in signature.parameters:
                    download_kwargs["threads"] = self.threads
                elif "n_threads" in signature.parameters:
                    download_kwargs["n_threads"] = self.threads
            except Exception:
                pass  # nosec B110

            for index, granule in enumerate(self.granules):
                native_id = granule_native_id(granule, f"Item {index + 1}")
                if self._cancelled:
                    row = {
                        "index": index,
                        "native_id": native_id,
                        "status": "cancelled",
                        "message": "Cancelled before download",
                        "files": [],
                    }
                    queue_rows.append(row)
                    self.queue_update.emit(index, "cancelled", row["message"], [])
                    continue

                existing = (
                    likely_existing_download_files(granule, self.output_dir)
                    if self.skip_existing
                    else []
                )
                if existing:
                    message = f"Skipped {len(existing)} existing file(s)"
                    row = {
                        "index": index,
                        "native_id": native_id,
                        "status": "skipped",
                        "message": message,
                        "files": existing,
                    }
                    queue_rows.append(row)
                    downloaded_files.extend(existing)
                    self.queue_update.emit(index, "skipped", message, existing)
                    continue

                percent = int((index / total) * 90) + 5
                self.progress.emit(
                    percent, f"Downloading {index + 1}/{total}: {native_id}"
                )
                self.queue_update.emit(index, "running", "Downloading...", [])
                try:
                    files = earthaccess.download([granule], **download_kwargs) or []
                    files = [str(file_path) for file_path in files]
                    downloaded_files.extend(files)
                    message = f"Downloaded {len(files)} file(s)"
                    row = {
                        "index": index,
                        "native_id": native_id,
                        "status": "done",
                        "message": message,
                        "files": files,
                    }
                    queue_rows.append(row)
                    self.queue_update.emit(index, "done", message, files)
                except Exception as e:
                    message = str(e)
                    row = {
                        "index": index,
                        "native_id": native_id,
                        "status": "failed",
                        "message": message,
                        "files": [],
                    }
                    queue_rows.append(row)
                    self.queue_update.emit(index, "failed", message, [])

            manifest = str(download_manifest_path(self.output_dir))
            write_download_manifest(manifest, queue_rows)
            self.progress.emit(100, "Download queue complete!")
            self.finished.emit(downloaded_files, manifest, queue_rows)

        except Exception as e:
            self.error.emit(str(e))


class IndexVrtWorker(QThread):
    """Worker thread for creating normalized-difference VRT files."""

    finished = pyqtSignal(str, str)  # index name, VRT path
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, positive, negative, index_name, output_path, parent=None):
        super().__init__(parent)
        self.positive = positive
        self.negative = negative
        self.index_name = index_name
        self.output_path = output_path

    def _source_path(self, value):
        return f"/vsicurl/{value}" if value.lower().startswith("http") else value

    def _escape_xml(self, text):
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _write_normalized_difference_vrt(self):
        """Write a VRT that computes (positive - negative) / (positive + negative)."""
        from osgeo import gdal

        config_overrides = {
            "GDAL_VRT_ENABLE_PYTHON": "YES",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": "tif,tiff,TIF,TIFF",
        }
        previous_options = {key: gdal.GetConfigOption(key) for key in config_overrides}
        for key, value in config_overrides.items():
            gdal.SetConfigOption(key, value)
        try:
            positive_path = self._source_path(self.positive)
            negative_path = self._source_path(self.negative)

            self.progress.emit("Opening positive band source...")
            source_ds = gdal.Open(positive_path)
            if source_ds is None:
                raise RuntimeError("Could not open positive band source")
            width = source_ds.RasterXSize
            height = source_ds.RasterYSize
            projection = source_ds.GetProjectionRef() or ""
            geotransform = source_ds.GetGeoTransform(can_return_null=True)
            source_ds = None

            self.progress.emit("Opening negative band source...")
            negative_ds = gdal.Open(negative_path)
            if negative_ds is None:
                raise RuntimeError("Could not open negative band source")
            if (
                negative_ds.RasterXSize != width
                or negative_ds.RasterYSize != height
                or (negative_ds.GetProjectionRef() or "") != projection
                or negative_ds.GetGeoTransform(can_return_null=True) != geotransform
            ):
                negative_ds = None
                raise RuntimeError(
                    "Positive and negative band sources must share the same "
                    "size, CRS, and geotransform; reproject/resample them to a "
                    "common grid first"
                )
            negative_ds = None
        finally:
            for key, value in previous_options.items():
                gdal.SetConfigOption(key, value)
        geotransform_text = (
            ", ".join(f"{value:.16g}" for value in geotransform) if geotransform else ""
        )

        code = """
import numpy as np

def normalized_difference(in_ar, out_ar, xoff, yoff, xsize, ysize,
                          raster_xsize, raster_ysize, buf_radius, gt, **kwargs):
    positive = in_ar[0].astype("float32")
    negative = in_ar[1].astype("float32")
    denominator = positive + negative
    out_ar[:] = np.where(denominator == 0, 0, (positive - negative) / denominator)
""".strip()
        lines = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">']
        if projection:
            lines.append(f"  <SRS>{self._escape_xml(projection)}</SRS>")
        if geotransform_text:
            lines.append(f"  <GeoTransform>{geotransform_text}</GeoTransform>")
        lines.extend(
            [
                '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTDerivedRasterBand">',
                f"    <Description>{self._escape_xml(self.index_name.upper())}</Description>",
                "    <PixelFunctionType>normalized_difference</PixelFunctionType>",
                "    <PixelFunctionLanguage>Python</PixelFunctionLanguage>",
                f"    <PixelFunctionCode><![CDATA[{code}]]></PixelFunctionCode>",
            ]
        )
        for path in (positive_path, negative_path):
            lines.extend(
                [
                    "    <SimpleSource>",
                    f'      <SourceFilename relativeToVRT="0">{self._escape_xml(path)}</SourceFilename>',
                    "      <SourceBand>1</SourceBand>",
                    '      <SrcRect xOff="0" yOff="0" xSize="{}" ySize="{}"/>'.format(
                        width, height
                    ),
                    '      <DstRect xOff="0" yOff="0" xSize="{}" ySize="{}"/>'.format(
                        width, height
                    ),
                    "    </SimpleSource>",
                ]
            )
        lines.extend(["  </VRTRasterBand>", "</VRTDataset>"])

        self.progress.emit("Writing normalized-difference VRT...")
        with open(self.output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def run(self):
        """Create the normalized-difference VRT in the background."""
        try:
            self._write_normalized_difference_vrt()
            self.finished.emit(self.index_name, self.output_path)
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
        self._adjusting_download_columns = False
        self._syncing_footprint_table_selection = False
        self._saved_presets = []
        self._recent_searches = []
        self._last_download_granules = []
        self._last_download_output_dir = ""
        self._last_download_rows = []
        self._last_download_manifest = ""
        self._alert_worker = None
        self._alert_baseline_ids = set()

        # Workers
        self._catalog_worker = None
        self._search_worker = None
        self._download_worker = None
        self._cog_worker = None
        self._collection_worker = None
        self._index_worker = None

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
        search_group = QGroupBox()
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
        self.max_items_spin.setValue(
            self.settings.value("NASAEarthdata/default_max_items", 50, type=int)
        )
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
        self.use_layer_aoi_btn = QPushButton("Use Layer AOI")
        self.use_layer_aoi_btn.setToolTip(
            "Use the active vector layer selection, or the active layer extent, as the search bounding box"
        )
        self.use_layer_aoi_btn.clicked.connect(self._use_active_layer_aoi)
        bbox_btn_layout.addWidget(self.use_layer_aoi_btn)
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

        # Saved and recent searches
        preset_layout = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Saved search presets")
        preset_layout.addWidget(self.preset_combo, 1)
        self.load_preset_btn = QPushButton("Load")
        self.load_preset_btn.clicked.connect(self._load_selected_preset)
        preset_layout.addWidget(self.load_preset_btn)
        self.save_preset_btn = QPushButton("Save")
        self.save_preset_btn.clicked.connect(self._save_current_preset)
        preset_layout.addWidget(self.save_preset_btn)
        self.delete_preset_btn = QPushButton("Delete")
        self.delete_preset_btn.clicked.connect(self._delete_selected_preset)
        preset_layout.addWidget(self.delete_preset_btn)
        search_layout.addRow("Preset:", preset_layout)

        recent_layout = QHBoxLayout()
        self.recent_combo = QComboBox()
        self.recent_combo.setToolTip("Recent search parameters")
        recent_layout.addWidget(self.recent_combo, 1)
        self.load_recent_btn = QPushButton("Load")
        self.load_recent_btn.clicked.connect(self._load_selected_recent)
        recent_layout.addWidget(self.load_recent_btn)
        self.delete_recent_btn = QPushButton("Delete")
        self.delete_recent_btn.clicked.connect(self._delete_selected_recent)
        recent_layout.addWidget(self.delete_recent_btn)
        search_layout.addRow("Recent:", recent_layout)

        collection_layout = QHBoxLayout()
        self.collection_info_btn = QPushButton("Collection Info")
        self.collection_info_btn.clicked.connect(self._show_collection_info)
        collection_layout.addWidget(self.collection_info_btn)
        self.check_new_btn = QPushButton("Check New")
        self.check_new_btn.setToolTip(
            "Run the selected saved/recent search and report granules not in the current results"
        )
        self.check_new_btn.clicked.connect(self._check_new_granules)
        collection_layout.addWidget(self.check_new_btn)
        collection_layout.addStretch()
        search_layout.addRow("Discovery:", collection_layout)

        self.search_section_check = self._add_collapsible_section(
            layout, "Search Parameters", search_group, checked=True
        )

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
        results_group = QGroupBox()
        results_layout = QVBoxLayout(results_group)

        # Results table
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter:"))
        self.result_filter_input = QLineEdit()
        self.result_filter_input.setPlaceholderText(
            "Filter results by ID, date, provider, cloud, day/night, or COG count..."
        )
        self.result_filter_input.textChanged.connect(self._filter_result_rows)
        filter_layout.addWidget(self.result_filter_input, 1)
        self.clear_result_filter_btn = QPushButton("Clear")
        self.clear_result_filter_btn.clicked.connect(self.result_filter_input.clear)
        filter_layout.addWidget(self.clear_result_filter_btn)
        results_layout.addLayout(filter_layout)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(7)
        self.results_table.setHorizontalHeaderLabels(
            ["ID", "Date", "Size", "Provider", "Cloud", "Day/Night", "COGs"]
        )
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

        # Selected granule detail inspector
        details_group = QGroupBox()
        details_layout = QVBoxLayout(details_group)
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setMinimumHeight(90)
        self.details_text.setMaximumHeight(160)
        self.details_text.setPlaceholderText("Select a result to inspect metadata...")
        details_layout.addWidget(self.details_text)
        self.details_section_check = self._add_collapsible_section(
            results_layout, "Granule Details", details_group, checked=False
        )

        preview_group = QGroupBox()
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = QTextBrowser()
        self.preview_text.setReadOnly(True)
        self.preview_text.setOpenExternalLinks(True)
        self.preview_text.setMinimumHeight(70)
        self.preview_text.setMaximumHeight(130)
        self.preview_text.setPlaceholderText(
            "Select a result to see quicklook and citation links..."
        )
        preview_layout.addWidget(self.preview_text)
        preview_btn_layout = QHBoxLayout()
        self.open_quicklook_btn = QPushButton("Open Quicklook")
        self.open_quicklook_btn.setEnabled(False)
        self.open_quicklook_btn.clicked.connect(self._open_selected_quicklook)
        preview_btn_layout.addWidget(self.open_quicklook_btn)
        self.open_gallery_btn = QPushButton("Gallery")
        self.open_gallery_btn.setEnabled(False)
        self.open_gallery_btn.clicked.connect(self._open_quicklook_gallery)
        preview_btn_layout.addWidget(self.open_gallery_btn)
        preview_btn_layout.addStretch()
        preview_layout.addLayout(preview_btn_layout)
        self.preview_section_check = self._add_collapsible_section(
            results_layout, "Quicklook and Citation", preview_group, checked=False
        )

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

        index_group = QGroupBox()
        index_layout = QFormLayout(index_group)
        self.index_type_combo = QComboBox()
        self.index_type_combo.addItem("NDVI", "ndvi")
        self.index_type_combo.addItem("NDWI", "ndwi")
        self.index_type_combo.addItem("MNDWI", "mndwi")
        self.index_type_combo.addItem("NDMI", "ndmi")
        self.index_type_combo.addItem("NBR", "nbr")
        self.index_type_combo.currentIndexChanged.connect(self._on_index_type_changed)
        index_layout.addRow("Index:", self.index_type_combo)
        self.index_positive_combo = QComboBox()
        self.index_negative_combo = QComboBox()
        for combo in (self.index_positive_combo, self.index_negative_combo):
            combo.setEnabled(False)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            combo.setMinimumContentsLength(18)
        index_layout.addRow("Positive band:", self.index_positive_combo)
        index_layout.addRow("Negative band:", self.index_negative_combo)
        self.create_index_btn = QPushButton("Create Index VRT")
        self.create_index_btn.setEnabled(False)
        self.create_index_btn.clicked.connect(self._create_index_vrt)
        index_layout.addRow("", self.create_index_btn)
        self.index_section_check = self._add_collapsible_section(
            results_layout, "Analysis-Ready Index", index_group, checked=False
        )

        # Zoom to footprints button and results info
        info_layout = QHBoxLayout()
        self.zoom_footprints_btn = QPushButton("Zoom to Footprints")
        self.zoom_footprints_btn.setEnabled(False)
        self.zoom_footprints_btn.clicked.connect(self._zoom_to_footprints)
        info_layout.addWidget(self.zoom_footprints_btn)
        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self._export_results_csv)
        info_layout.addWidget(self.export_csv_btn)
        self.export_geojson_btn = QPushButton("Export GeoJSON")
        self.export_geojson_btn.setEnabled(False)
        self.export_geojson_btn.clicked.connect(self._export_results_geojson)
        info_layout.addWidget(self.export_geojson_btn)
        self.export_json_btn = QPushButton("Export JSON")
        self.export_json_btn.setEnabled(False)
        self.export_json_btn.clicked.connect(self._export_results_json)
        info_layout.addWidget(self.export_json_btn)
        self.export_stac_btn = QPushButton("Export STAC")
        self.export_stac_btn.setEnabled(False)
        self.export_stac_btn.clicked.connect(self._export_results_stac)
        info_layout.addWidget(self.export_stac_btn)
        self.export_bundle_btn = QPushButton("Bundle")
        self.export_bundle_btn.setEnabled(False)
        self.export_bundle_btn.clicked.connect(self._export_workflow_bundle)
        info_layout.addWidget(self.export_bundle_btn)
        info_layout.addStretch()
        results_layout.addLayout(info_layout)

        # Results info
        self.results_label = QLabel("No search performed yet")
        self.results_label.setStyleSheet("color: gray;")
        results_layout.addWidget(self.results_label)

        self.results_section_check = self._add_collapsible_section(
            layout, "Search Results", results_group, checked=True
        )

        # Output section
        output_group = QGroupBox()
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

        self.output_section_check = self._add_collapsible_section(
            layout, "Output", output_group, checked=True, stretch=1
        )

        # Download queue section
        download_group = QGroupBox()
        download_layout = QVBoxLayout(download_group)
        self.download_queue_table = QTableWidget()
        self.download_queue_table.setColumnCount(4)
        self.download_queue_table.setHorizontalHeaderLabels(
            ["Granule", "Status", "Message", "Files"]
        )
        download_header = self.download_queue_table.horizontalHeader()
        download_header.setSectionsClickable(True)
        download_header.setSectionsMovable(False)
        download_header.setMinimumSectionSize(45)
        download_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        download_header.setStretchLastSection(False)
        download_header.sectionResized.connect(self._on_download_section_resized)
        self._set_default_download_column_widths()
        self.download_queue_table.installEventFilter(self)
        self.download_queue_table.viewport().installEventFilter(self)
        self.download_queue_table.setMinimumHeight(90)
        download_layout.addWidget(self.download_queue_table)
        download_btn_layout = QHBoxLayout()
        self.cancel_download_btn = QPushButton("Cancel Download")
        self.cancel_download_btn.setEnabled(False)
        self.cancel_download_btn.clicked.connect(self._cancel_download)
        download_btn_layout.addWidget(self.cancel_download_btn)
        self.retry_failed_btn = QPushButton("Retry Failed")
        self.retry_failed_btn.setEnabled(False)
        self.retry_failed_btn.clicked.connect(self._retry_failed_downloads)
        download_btn_layout.addWidget(self.retry_failed_btn)
        download_btn_layout.addStretch()
        download_layout.addLayout(download_btn_layout)
        self.download_section_check = self._add_collapsible_section(
            layout, "Download Queue", download_group, checked=False
        )

        # Status label
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.status_label)

        self._load_presets_into_combo()
        self._load_recent_into_combo()
        self._load_persistent_download_queue()

    def _add_collapsible_section(self, layout, title, widget, checked=True, stretch=0):
        """Add a checkbox-controlled section to a layout."""
        checkbox = QCheckBox(title)
        checkbox.setChecked(checked)
        checkbox.toggled.connect(widget.setVisible)
        layout.addWidget(checkbox)
        widget.setVisible(checked)
        if stretch:
            layout.addWidget(widget, stretch)
        else:
            layout.addWidget(widget)
        return checkbox

    def _load_datasets(self):
        """Load NASA datasets from cache or download."""
        self._log("Loading NASA Earthdata catalog...")
        self.refresh_catalog_btn.setEnabled(False)

        self._catalog_worker = CatalogLoadWorker(
            force_refresh=False,
            catalog_url=self.settings.value(
                "NASAEarthdata/catalog_url", NASA_DATA_URL, type=str
            ),
            cache_dir=self.settings.value("NASAEarthdata/cache_dir", "", type=str),
            cache_enabled=self.settings.value(
                "NASAEarthdata/enable_cache", True, type=bool
            ),
        )
        self._catalog_worker.finished.connect(self._on_catalog_loaded)
        self._catalog_worker.error.connect(self._on_catalog_error)
        self._catalog_worker.progress.connect(self._log)
        self._catalog_worker.start()

    def _refresh_catalog(self):
        """Force refresh the catalog from the server."""
        self._log("Refreshing catalog from server...")
        self.refresh_catalog_btn.setEnabled(False)

        self._catalog_worker = CatalogLoadWorker(
            force_refresh=True,
            catalog_url=self.settings.value(
                "NASAEarthdata/catalog_url", NASA_DATA_URL, type=str
            ),
            cache_dir=self.settings.value("NASAEarthdata/cache_dir", "", type=str),
            cache_enabled=self.settings.value(
                "NASAEarthdata/enable_cache", True, type=bool
            ),
        )
        self._catalog_worker.finished.connect(self._on_catalog_loaded)
        self._catalog_worker.error.connect(self._on_catalog_error)
        self._catalog_worker.progress.connect(self._log)
        self._catalog_worker.start()

    def reload_catalog(self):
        """Reload the catalog, e.g. after dependencies are installed."""
        self._load_datasets()

    def _presets_file(self):
        """Return saved-search preset storage path."""
        return workflow_dir(self.settings) / "search_presets.json"

    def _load_presets_into_combo(self):
        """Load saved search presets into the combo box."""
        try:
            self._saved_presets = load_search_presets(self._presets_file())
        except Exception as e:
            self._saved_presets = []
            self._log(f"Could not load saved searches: {e}", error=True)

        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self._saved_presets:
            self.preset_combo.addItem(preset.get("name", "Unnamed Search"), preset)
        self.preset_combo.blockSignals(False)
        self.load_preset_btn.setEnabled(bool(self._saved_presets))
        self.delete_preset_btn.setEnabled(bool(self._saved_presets))

    def _load_recent_into_combo(self):
        """Load recent searches into the combo box."""
        self._recent_searches = load_recent_searches(self.settings)
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        for preset in self._recent_searches:
            dataset = preset.get("dataset", {})
            temporal = preset.get("temporal", {})
            label = (
                f"{dataset.get('short_name') or dataset.get('label', 'Dataset')} "
                f"{temporal.get('start', '')} to {temporal.get('end', '')}"
            ).strip()
            self.recent_combo.addItem(label, preset)
        self.recent_combo.blockSignals(False)
        self.load_recent_btn.setEnabled(bool(self._recent_searches))
        self.delete_recent_btn.setEnabled(bool(self._recent_searches))

    def _current_advanced_options(self):
        """Return current advanced search UI options."""
        return {
            "enabled": self.advanced_check.isChecked(),
            "cloud_min": self.cloud_min_spin.value(),
            "cloud_max": self.cloud_max_spin.value(),
            "day_night": self.daynight_combo.currentData(),
            "provider": self.provider_input.text().strip(),
            "version": self.version_input.text().strip(),
            "granule_id": self.granule_id_input.text().strip(),
            "orbit_min": self.orbit_min_spin.value(),
            "orbit_max": self.orbit_max_spin.value(),
        }

    def _current_search_preset(self, name):
        """Build a search preset from current UI state."""
        return build_search_preset(
            name=name,
            dataset_item=self.dataset_combo.currentData() or {},
            bbox_text=self.bbox_input.text().strip(),
            start_date=self.start_date.date().toString("yyyy-MM-dd"),
            end_date=self.end_date.date().toString("yyyy-MM-dd"),
            max_items=self.max_items_spin.value(),
            advanced_options=self._current_advanced_options(),
        )

    def _save_current_preset(self):
        """Prompt for a name and save the current search as a preset."""
        default_name = self.dataset_combo.currentText() or "NASA Earthdata Search"
        name, ok = QInputDialog.getText(
            self,
            "Save Search Preset",
            "Preset name:",
            text=default_name,
        )
        name = name.strip()
        if not ok or not name:
            return

        try:
            upsert_search_preset(
                self._presets_file(), self._current_search_preset(name)
            )
            self._load_presets_into_combo()
            self._log(f"Saved search preset: {name}")
        except Exception as e:
            QMessageBox.critical(self, "Save Search Preset", f"Failed to save:\n{e}")

    def _delete_selected_preset(self):
        """Delete the selected saved search preset."""
        index = self.preset_combo.currentIndex()
        preset = self.preset_combo.currentData()
        if index < 0 or not preset:
            return

        name = preset.get("name", self.preset_combo.currentText())
        reply = QMessageBox.question(
            self,
            "Delete Search Preset",
            f"Delete saved search preset '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            delete_search_preset(self._presets_file(), name)
            self._load_presets_into_combo()
            self._log(f"Deleted search preset: {name}")
        except Exception as e:
            QMessageBox.critical(
                self, "Delete Search Preset", f"Failed to delete:\n{e}"
            )

    def _delete_selected_recent(self):
        """Delete the selected recent search."""
        index = self.recent_combo.currentIndex()
        if index < 0:
            return

        delete_recent_search(self.settings, index)
        self._load_recent_into_combo()
        self._log("Deleted recent search")

    def _select_dataset_from_preset(self, preset):
        """Select the dataset matching a preset."""
        dataset = preset.get("dataset", {})
        concept_id = dataset.get("concept_id", "")
        short_name = dataset.get("short_name", "")
        for index in range(self.dataset_combo.count()):
            item = self.dataset_combo.itemData(index) or {}
            if concept_id and item.get("concept_id") == concept_id:
                self.dataset_combo.setCurrentIndex(index)
                return
            if short_name and item.get("short_name") == short_name:
                self.dataset_combo.setCurrentIndex(index)
                return

    def _apply_search_preset(self, preset):
        """Apply a search preset to the dock controls."""
        if not preset:
            return
        self._select_dataset_from_preset(preset)
        self.bbox_input.setText(preset.get("bbox", ""))
        temporal = preset.get("temporal", {})
        start = QDate.fromString(temporal.get("start", ""), "yyyy-MM-dd")
        end = QDate.fromString(temporal.get("end", ""), "yyyy-MM-dd")
        if start.isValid():
            self.start_date.setDate(start)
        if end.isValid():
            self.end_date.setDate(end)
        self.max_items_spin.setValue(int(preset.get("max_items", 50) or 50))

        advanced = preset.get("advanced", {})
        self.advanced_check.setChecked(bool(advanced.get("enabled", False)))
        self.cloud_min_spin.setValue(int(advanced.get("cloud_min", 0) or 0))
        self.cloud_max_spin.setValue(int(advanced.get("cloud_max", 100) or 100))
        day_night = advanced.get("day_night")
        for index in range(self.daynight_combo.count()):
            if self.daynight_combo.itemData(index) == day_night:
                self.daynight_combo.setCurrentIndex(index)
                break
        self.provider_input.setText(advanced.get("provider", "") or "")
        self.version_input.setText(advanced.get("version", "") or "")
        self.granule_id_input.setText(advanced.get("granule_id", "") or "")
        self.orbit_min_spin.setValue(int(advanced.get("orbit_min", 0) or 0))
        self.orbit_max_spin.setValue(int(advanced.get("orbit_max", 0) or 0))
        self._log(f"Loaded search preset: {preset.get('name', 'recent search')}")

    def _load_selected_preset(self):
        """Load the selected saved search preset."""
        self._apply_search_preset(self.preset_combo.currentData())

    def _load_selected_recent(self):
        """Load the selected recent search."""
        self._apply_search_preset(self.recent_combo.currentData())

    def _record_recent_search(self):
        """Record current search parameters in the recent-search list."""
        try:
            record_recent_search(
                self.settings,
                self._current_search_preset("Recent Search"),
            )
            self._load_recent_into_combo()
        except Exception as e:
            self._log(f"Could not record recent search: {e}", error=True)

    def _populate_dataset_combo(self, items):
        """Populate dataset combo while preserving row metadata as item data."""
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        for item in items:
            self.dataset_combo.addItem(item.get("label", ""), item)
        self.dataset_combo.blockSignals(False)
        self._on_dataset_changed(self.dataset_combo.currentIndex())

    def _select_default_dataset(self):
        """Select the default HLSL30 collection by concept ID."""
        default_concept_id = "C2021957657-LPCLOUD"
        fallback_index = -1
        for index in range(self.dataset_combo.count()):
            item = self.dataset_combo.itemData(index) or {}
            if item.get("concept_id") == default_concept_id:
                self.dataset_combo.setCurrentIndex(index)
                return
            if fallback_index < 0 and item.get("short_name") == "HLSL30":
                fallback_index = index

        if fallback_index >= 0:
            self.dataset_combo.setCurrentIndex(fallback_index)

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
        if hasattr(self, "result_filter_input"):
            self.result_filter_input.clear()
        self.results_label.setText("No search performed yet")
        self.display_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.zoom_footprints_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.export_geojson_btn.setEnabled(False)
        self.export_json_btn.setEnabled(False)
        self.export_stac_btn.setEnabled(False)
        self.export_bundle_btn.setEnabled(False)
        self.open_quicklook_btn.setEnabled(False)
        self.open_gallery_btn.setEnabled(False)
        self.details_text.clear()
        self.preview_text.clear()
        self.cog_list.clear()
        self.cog_list.setEnabled(False)
        self.cog_mode_combo.setEnabled(False)
        self._clear_rgb_channel_combos()
        self._clear_index_combos()
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

    def _use_active_layer_aoi(self):
        """Set bbox from the active vector layer selection or layer extent."""
        try:
            layer = self.iface.activeLayer()
        except Exception:
            layer = None
        if layer is None:
            QMessageBox.information(
                self, "Use Layer AOI", "Select an active vector layer first."
            )
            return

        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(
                self, "Use Layer AOI", "The active layer must be a vector layer."
            )
            return

        try:
            selected_features = list(layer.selectedFeatures())
        except Exception:
            selected_features = []

        if selected_features:
            extent = selected_features[0].geometry().boundingBox()
            for feature in selected_features[1:]:
                extent.combineExtentWith(feature.geometry().boundingBox())
            source = f"{len(selected_features)} selected feature(s)"
        else:
            extent = layer.extent()
            source = "active layer extent"

        try:
            layer_crs = layer.crs()
            if layer_crs.authid() != "EPSG:4326":
                transform = QgsCoordinateTransform(
                    layer_crs,
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance(),
                )
                extent = transform.transformBoundingBox(extent)
            bbox_str = f"{extent.xMinimum():.4f}, {extent.yMinimum():.4f}, {extent.xMaximum():.4f}, {extent.yMaximum():.4f}"
            self.bbox_input.setText(bbox_str)
            self._log(f"Bounding box set from {source}: {layer.name()}")
        except Exception as e:
            QMessageBox.warning(
                self, "Use Layer AOI", f"Could not use active layer AOI:\n{e}"
            )

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
        self._record_recent_search()

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
        if self.settings.value("NASAEarthdata/debug", False, type=bool):
            self._log(f"Search kwargs: {self._search_worker._build_search_kwargs()}")
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
                row = granule_export_row(
                    granule, i, self.dataset_combo.currentData() or {}
                )
                native_id = row.get("native_id") or f"Item {i + 1}"
                time_start = (row.get("temporal_start") or "N/A")[:10]
                size_display = row.get("size_display") or "N/A"
                size_bytes = row.get("size_bytes") or 0
                provider = row.get("provider") or row.get("dataset_provider") or ""
                cloud_cover = row.get("cloud_cover")
                if cloud_cover in (None, ""):
                    cloud_display = ""
                    cloud_sort = -1
                else:
                    try:
                        cloud_sort = float(cloud_cover)
                        cloud_display = f"{cloud_sort:g}%"
                    except (TypeError, ValueError):
                        cloud_sort = -1
                        cloud_display = str(cloud_cover)
                day_night = row.get("day_night") or ""
                cog_count = len(
                    [link for link in row.get("cog_links", "").splitlines() if link]
                )

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

                provider_item = QTableWidgetItem(str(provider))
                provider_item.setToolTip(str(provider))
                self.results_table.setItem(i, 3, provider_item)

                cloud_item = NumericTableWidgetItem(str(cloud_display))
                cloud_item.setData(Qt.ItemDataRole.UserRole, cloud_sort)
                cloud_item.setToolTip(str(cloud_display))
                self.results_table.setItem(i, 4, cloud_item)

                daynight_item = QTableWidgetItem(str(day_night))
                daynight_item.setToolTip(str(day_night))
                self.results_table.setItem(i, 5, daynight_item)

                cogs_item = NumericTableWidgetItem(str(cog_count))
                cogs_item.setData(Qt.ItemDataRole.UserRole, cog_count)
                cogs_item.setToolTip(f"{cog_count} COG/TIFF link(s)")
                self.results_table.setItem(i, 6, cogs_item)
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
                for column in range(3, self.results_table.columnCount()):
                    self.results_table.setItem(i, column, QTableWidgetItem(""))

        # Re-enable sorting after population
        self.results_table.setSortingEnabled(True)
        self._set_default_results_column_widths()
        self._filter_result_rows()

        # Add footprints to map
        self._add_footprints(gdf)

        # Enable buttons
        self.display_btn.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.zoom_footprints_btn.setEnabled(True)
        self.export_csv_btn.setEnabled(True)
        self.export_geojson_btn.setEnabled(True)
        self.export_json_btn.setEnabled(True)
        self.export_stac_btn.setEnabled(True)
        self.export_bundle_btn.setEnabled(True)
        self._update_granule_details()

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
                self._connect_footprint_selection(layer)

                if self.settings.value("NASAEarthdata/auto_zoom", True, type=bool):
                    # Zoom to footprints with proper CRS handling
                    self._zoom_to_footprints()

                self._log("Footprints added to map")
            else:
                self._log("Failed to create footprints layer", error=True)

        except Exception as e:
            self._log(f"Error adding footprints: {e}", error=True)

    def _valid_layer_or_none(self, attr_name):
        """Return a live QgsMapLayer wrapper, clearing stale deleted wrappers."""
        layer = getattr(self, attr_name, None)
        if layer is None:
            return None
        try:
            if layer.isValid():
                return layer
        except RuntimeError:
            pass
        except Exception:
            pass  # nosec B110
        setattr(self, attr_name, None)
        return None

    def _connect_footprint_selection(self, layer):
        """Connect footprint feature selection changes to the result table."""
        try:
            layer.selectionChanged.connect(self._on_footprint_selection_changed)
        except Exception as e:
            self._log(f"Could not connect footprint selection sync: {e}", error=True)

    def _disconnect_footprint_selection(self, layer):
        """Disconnect footprint feature selection changes when removing the layer."""
        try:
            layer.selectionChanged.disconnect(self._on_footprint_selection_changed)
        except Exception:
            pass  # nosec B110

    def _zoom_to_footprints(self):
        """Zoom the map canvas to selected footprints or all if none selected."""
        footprints_layer = self._valid_layer_or_none("_footprints_layer")
        if footprints_layer is None:
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
                layer_extent = footprints_layer.extent()
                self._log("Zooming to all footprints")

            # Transform extent to map CRS if different
            layer_crs = footprints_layer.crs()
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
        footprints_layer = getattr(self, "_footprints_layer", None)
        if footprints_layer is not None:
            self._disconnect_footprint_selection(footprints_layer)
            try:
                # Remove layer from project
                QgsProject.instance().removeMapLayer(footprints_layer.id())
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
        selected_layer = getattr(self, "_selected_footprints_layer", None)
        if selected_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(selected_layer.id())
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
            footprints_layer = self._valid_layer_or_none("_footprints_layer")
            from qgis.core import (
                QgsFeature,
                QgsFillSymbol,
                QgsSingleSymbolRenderer,
                QgsWkbTypes,
            )

            geometry_name = (
                QgsWkbTypes.displayString(footprints_layer.wkbType())
                if footprints_layer is not None
                else "Polygon"
            )
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
            self._clear_index_combos()

        if not self._syncing_footprint_table_selection:
            self._sync_footprint_selection_from_table()
        self._update_granule_details()
        self._update_quicklook_preview()

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

    def _first_selected_result_index(self):
        """Return the first selected result index, if any."""
        selected = self._get_selected_result_indices()
        if selected:
            return selected[0]
        current_row = self.results_table.currentRow()
        if current_row >= 0:
            return self._get_result_index_for_table_row(current_row)
        return -1

    def _update_granule_details(self):
        """Update the selected-granule detail inspector."""
        if not self._search_results:
            self.details_text.clear()
            return

        result_index = self._first_selected_result_index()
        if result_index < 0 or result_index >= len(self._search_results):
            self.details_text.setPlainText("Select a result to inspect metadata.")
            return

        granule = self._search_results[result_index]
        dataset_item = self.dataset_combo.currentData() or {}
        row = granule_export_row(granule, result_index, dataset_item)
        links = granule_links(granule)
        cog_links = cog_links_from_links(links)
        lines = [
            f"Native ID: {row.get('native_id', '')}",
            f"Dataset: {row.get('dataset_short_name', '')}",
            f"Concept ID: {row.get('dataset_concept_id', '')}",
            f"Provider: {row.get('provider') or row.get('dataset_provider', '')}",
            f"Version: {row.get('dataset_version', '')}",
            f"Temporal Start: {row.get('temporal_start', '')}",
            f"Temporal End: {row.get('temporal_end', '')}",
            f"Size: {row.get('size_display', '')}",
            f"COG/TIFF Links: {len(cog_links)}",
            "",
            "Links:",
        ]
        lines.extend(links or ["No links available"])
        self.details_text.setPlainText("\n".join(str(line) for line in lines))

    def _update_quicklook_preview(self):
        """Update quicklook/citation links for selected granules."""
        if not self._search_results:
            self.preview_text.clear()
            self.open_quicklook_btn.setEnabled(False)
            self.open_gallery_btn.setEnabled(False)
            return

        selected_indices = self._get_selected_result_indices()
        if not selected_indices:
            first_index = self._first_selected_result_index()
            selected_indices = [first_index] if first_index >= 0 else []
        selected_indices = [
            index
            for index in selected_indices
            if 0 <= index < len(self._search_results)
        ]
        if not selected_indices:
            self.preview_text.setPlainText("Select a result to inspect preview links.")
            self.open_quicklook_btn.setEnabled(False)
            self.open_gallery_btn.setEnabled(False)
            return

        html_parts = ["<h4>Quicklook / browse gallery</h4>"]
        all_quicklooks = []
        all_citations = []
        for result_index in selected_indices[:12]:
            granule = self._search_results[result_index]
            native_id = granule_native_id(granule, f"Item {result_index + 1}")
            quicklooks = granule_quicklook_links(granule)
            inaccessible_quicklooks = granule_inaccessible_quicklook_links(granule)
            citations = granule_citation_links(granule)
            all_quicklooks.extend(quicklooks)
            all_citations.extend(citations)
            html_parts.append(
                f"<p><b>{html.escape(_compact_result_id(native_id))}</b></p>"
            )
            if quicklooks:
                html_parts.append("<div>")
                for url in quicklooks[:4]:
                    image_html = self._quicklook_img_html(url, 96)
                    if image_html:
                        html_parts.append(image_html)
                html_parts.append("</div>")
                html_parts.append(self._link_list_html(quicklooks, ""))
            elif inaccessible_quicklooks:
                html_parts.append(
                    "<p>Browse objects are available only as non-browser storage links "
                    "and cannot be previewed here.</p>"
                )
            else:
                html_parts.append("<p>No quicklook links found.</p>")
            if citations:
                html_parts.append("<p>Citation / documentation:</p>")
                html_parts.append(self._link_list_html(citations, ""))

        if len(selected_indices) > 12:
            html_parts.append(
                f"<p>Showing previews for 12 of {len(selected_indices)} selected granules.</p>"
            )
        unique_citations = list(dict.fromkeys(all_citations))
        if unique_citations:
            html_parts.append("<h4>All Citation / documentation links</h4>")
            html_parts.append(self._link_list_html(unique_citations, ""))
        self.preview_text.setHtml("".join(html_parts))
        self.open_quicklook_btn.setEnabled(bool(all_quicklooks))
        self.open_gallery_btn.setEnabled(bool(all_quicklooks))

    def _link_html(self, url, label=None):
        """Return an escaped external-link anchor for display widgets."""
        url = str(url or "").strip()
        if not url:
            return ""
        label = label or url
        return (
            f'<a href="{html.escape(url, quote=True)}">'
            f"{html.escape(str(label))}</a>"
        )

    def _link_list_html(self, links, empty_text):
        """Return a small HTML list of clickable links."""
        if not links:
            return f"<p>{html.escape(empty_text)}</p>"
        items = "".join(f"<li>{self._link_html(link)}</li>" for link in links)
        return f"<ul>{items}</ul>"

    def _cached_quicklook_image_src(self, url):
        """Download an HTTPS quicklook to cache and return a local image URI."""
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return ""

        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".gif"):
            suffix = ".jpg"

        cache_dir = Path(tempfile.gettempdir()) / "nasa_earthdata_quicklooks"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        image_path = cache_dir / f"{digest}{suffix}"
        if not image_path.exists():
            try:
                with https_only_urlopen(url, timeout=20) as resp:
                    image_path.write_bytes(resp.read())
            except Exception as e:
                self._log(f"Could not load quicklook thumbnail: {e}", error=True)
                return ""
        return image_path.as_uri()

    def _quicklook_img_html(self, url, height):
        """Return HTML for a cached quicklook image linked to its source URL."""
        src = self._cached_quicklook_image_src(url)
        if not src:
            return ""
        escaped_url = html.escape(url, quote=True)
        escaped_src = html.escape(src, quote=True)
        return (
            f'<a href="{escaped_url}"><img src="{escaped_src}" '
            f'height="{int(height)}" style="margin:4px;"/></a>'
        )

    def _open_selected_quicklook(self):
        """Open the first available quicklook link across selected granules."""
        if not self._search_results:
            return
        selected_indices = self._get_selected_result_indices()
        if not selected_indices:
            first_index = self._first_selected_result_index()
            selected_indices = [first_index] if first_index >= 0 else []
        for result_index in selected_indices:
            if 0 <= result_index < len(self._search_results):
                quicklooks = granule_quicklook_links(self._search_results[result_index])
                if quicklooks:
                    webbrowser.open(quicklooks[0])
                    return

    def _quicklook_gallery_html(self):
        """Return an HTML quicklook gallery for selected or current results."""
        if not self._search_results:
            return "<p>No search results available.</p>"
        selected_indices = self._get_selected_result_indices()
        if not selected_indices:
            selected_indices = list(range(min(24, len(self._search_results))))

        html_parts = [
            "<html><body>",
            "<h3>NASA Earthdata Quicklook Gallery</h3>",
            '<table width="100%" cellspacing="0" cellpadding="8">',
        ]
        for result_index in selected_indices[:48]:
            if result_index < 0 or result_index >= len(self._search_results):
                continue
            granule = self._search_results[result_index]
            native_id = granule_native_id(granule, f"Item {result_index + 1}")
            quicklooks = granule_quicklook_links(granule)
            if not quicklooks:
                continue
            image_parts = []
            for url in quicklooks[:6]:
                image_html = self._quicklook_img_html(url, 180)
                if image_html:
                    image_parts.append(image_html)
            if not image_parts:
                continue
            html_parts.append(
                "<tr>"
                '<td width="220" valign="top">'
                f"{''.join(image_parts)}"
                "</td>"
                '<td valign="top">'
                f"<b>{html.escape(str(native_id))}</b>"
                f"{self._link_list_html(quicklooks, '')}"
                "</td>"
                "</tr>"
            )
        html_parts.append("</table></body></html>")
        return "".join(html_parts)

    def _open_quicklook_gallery(self):
        """Open a scrollable dialog with selected quicklook images."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Quicklook Gallery")
        dialog.resize(820, 620)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setHtml(self._quicklook_gallery_html())
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        exec_dialog = getattr(dialog, "exec", None) or getattr(dialog, "exec_", None)
        if exec_dialog is not None:
            exec_dialog()

    def ai_context_summary(self):
        """Return a compact text summary of current NASA Earthdata context."""
        dataset = self.dataset_combo.currentData() or {}
        selected_indices = self._get_selected_result_indices()
        if not selected_indices and self._search_results:
            selected_indices = [0]
        lines = [
            "NASA Earthdata QGIS plugin context",
            f"Dataset: {dataset.get('label') or dataset.get('short_name', '')}",
            f"Concept ID: {dataset.get('concept_id', '')}",
            f"BBox: {self.bbox_input.text().strip()}",
            f"Date range: {self.start_date.date().toString('yyyy-MM-dd')} to {self.end_date.date().toString('yyyy-MM-dd')}",
            f"Result count: {len(self._search_results or [])}",
        ]
        if selected_indices:
            lines.append("Selected granules:")
        for index in selected_indices[:8]:
            if self._search_results and 0 <= index < len(self._search_results):
                granule = self._search_results[index]
                links = granule_links(granule)
                cogs = cog_links_from_links(links)
                lines.append(f"- {granule_native_id(granule, f'Item {index + 1}')}")
                if cogs:
                    lines.append(f"  COGs: {', '.join(cogs[:5])}")
        if len(selected_indices) > 8:
            lines.append(f"...and {len(selected_indices) - 8} more selected granules")
        return "\n".join(lines)

    def _export_rows(self):
        """Return export rows for all current results."""
        return granules_to_export_rows(
            self._search_results or [], self.dataset_combo.currentData() or {}
        )

    def _export_results_csv(self):
        """Export current result metadata to CSV."""
        if not self._search_results:
            QMessageBox.information(
                self, "Export Results", "No search results to export."
            )
            return

        default_path = str(workflow_dir(self.settings) / "earthdata_results.csv")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export NASA Earthdata Results CSV",
            default_path,
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        try:
            write_results_csv(file_path, self._export_rows())
            self._log(f"Exported result metadata: {file_path}")
            self._notify_success("NASA Earthdata", "Exported result metadata to CSV")
        except Exception as e:
            QMessageBox.critical(self, "Export CSV", f"Failed to export:\n{e}")

    def _export_results_geojson(self):
        """Export current result footprints and metadata to GeoJSON."""
        if not self._search_results:
            QMessageBox.information(
                self, "Export Results", "No search results to export."
            )
            return

        default_path = str(workflow_dir(self.settings) / "earthdata_results.geojson")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export NASA Earthdata Results GeoJSON",
            default_path,
            "GeoJSON Files (*.geojson *.json)",
        )
        if not file_path:
            return

        try:
            write_results_geojson(file_path, self._export_rows(), self._search_gdf)
            layer = QgsVectorLayer(file_path, "NASA Earthdata Exported Results", "ogr")
            if layer.isValid():
                layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
                QgsProject.instance().addMapLayer(layer)
            self._log(f"Exported result footprints: {file_path}")
            self._notify_success("NASA Earthdata", "Exported result footprints")
        except Exception as e:
            QMessageBox.critical(self, "Export GeoJSON", f"Failed to export:\n{e}")

    def _export_results_json(self):
        """Export raw current granule results to JSON."""
        if not self._search_results:
            QMessageBox.information(
                self, "Export Results", "No search results to export."
            )
            return

        default_path = str(workflow_dir(self.settings) / "earthdata_granules.json")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export NASA Earthdata Granules JSON",
            default_path,
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            write_granules_json(file_path, self._search_results)
            self._log(f"Exported raw granules: {file_path}")
            self._notify_success("NASA Earthdata", "Exported raw granules JSON")
        except Exception as e:
            QMessageBox.critical(self, "Export JSON", f"Failed to export:\n{e}")

    def _export_results_stac(self):
        """Export current results to a STAC ItemCollection."""
        if not self._search_results:
            QMessageBox.information(
                self, "Export Results", "No search results to export."
            )
            return

        default_path = str(workflow_dir(self.settings) / "earthdata_stac.json")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export NASA Earthdata STAC ItemCollection",
            default_path,
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            write_results_stac(
                file_path,
                self._search_results,
                self.dataset_combo.currentData() or {},
                self._search_gdf,
            )
            self._log(f"Exported STAC ItemCollection: {file_path}")
            self._notify_success("NASA Earthdata", "Exported STAC ItemCollection")
        except Exception as e:
            QMessageBox.critical(self, "Export STAC", f"Failed to export:\n{e}")

    def _export_workflow_bundle(self):
        """Export a reproducible bundle with search, results, granules, and STAC."""
        if not self._search_results:
            QMessageBox.information(
                self, "Export Workflow Bundle", "No search results to bundle."
            )
            return

        default_path = str(
            workflow_dir(self.settings) / "earthdata_workflow_bundle.json"
        )
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export NASA Earthdata Workflow Bundle",
            default_path,
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            dataset_item = self.dataset_combo.currentData() or {}
            stac = granules_to_stac_item_collection(
                self._search_results, dataset_item, self._search_gdf
            )
            write_workflow_bundle(
                file_path,
                self._current_search_preset("Workflow Bundle Search"),
                self._search_results,
                self._export_rows(),
                stac_item_collection=stac,
                manifest=self._last_download_manifest,
            )
            self._log(f"Exported workflow bundle: {file_path}")
            self._notify_success("NASA Earthdata", "Exported workflow bundle")
        except Exception as e:
            QMessageBox.critical(
                self, "Export Workflow Bundle", f"Failed to export:\n{e}"
            )

    def _show_collection_info(self):
        """Fetch and show live CMR collection metadata."""
        dataset_item = self.dataset_combo.currentData() or {}
        if not dataset_item:
            QMessageBox.information(self, "Collection Info", "Select a dataset first.")
            return
        self.collection_info_btn.setEnabled(False)
        self._log("Fetching live CMR collection metadata...")
        self._collection_worker = CollectionInfoWorker(dataset_item)
        self._collection_worker.finished.connect(self._on_collection_info_finished)
        self._collection_worker.error.connect(self._on_collection_info_error)
        self._collection_worker.start()

    def _on_collection_info_finished(self, summary):
        """Display fetched collection metadata."""
        self.collection_info_btn.setEnabled(True)
        self._show_collection_info_dialog(summary)
        self._log(f"Loaded collection info for {summary.get('short_name', '')}")

    def _show_collection_info_dialog(self, summary):
        """Show CMR collection details with clickable links."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Collection Info")
        dialog.resize(620, 460)

        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setHtml(self._collection_info_html(summary))
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        exec_dialog = getattr(dialog, "exec", None) or getattr(dialog, "exec_", None)
        if exec_dialog is not None:
            exec_dialog()

    def _collection_info_html(self, summary):
        """Build rich collection-info HTML with clickable anchors."""
        fields = [
            ("Title", summary.get("title", "")),
            ("Short Name", summary.get("short_name", "")),
            ("Concept ID", summary.get("concept_id", "")),
            ("Provider", summary.get("provider", "")),
            ("Version", summary.get("version_id", "")),
            ("Cloud Hosted", summary.get("cloud_hosted", False)),
            ("Temporal Start", summary.get("time_start", "")),
            ("Temporal End", summary.get("time_end", "")),
        ]
        doi = str(summary.get("doi", "") or "").strip()
        if doi:
            doi_url = (
                doi if doi.lower().startswith("http") else f"https://doi.org/{doi}"
            )
            fields.append(("DOI", self._link_html(doi_url, doi)))
        else:
            fields.append(("DOI", ""))

        rows = []
        for label, value in fields:
            value_text = str(value)
            if value_text.startswith("<a "):
                rendered = value_text
            else:
                rendered = html.escape(value_text)
            rows.append(
                "<tr>"
                f'<th align="left" valign="top">{html.escape(label)}</th>'
                f"<td>{rendered}</td>"
                "</tr>"
            )

        summary_text = html.escape(str(summary.get("summary", "") or ""))
        links_html = self._link_list_html(
            summary.get("links", []), "No links available."
        )
        return (
            "<html><body>"
            "<h3>NASA Earthdata Collection</h3>"
            f"<table cellspacing=\"6\">{''.join(rows)}</table>"
            "<h4>Summary</h4>"
            f"<p>{summary_text}</p>"
            "<h4>Links</h4>"
            f"{links_html}"
            "</body></html>"
        )

    def _on_collection_info_error(self, error_msg):
        """Handle CMR collection info errors."""
        self.collection_info_btn.setEnabled(True)
        self._log(f"Collection info error: {error_msg}", error=True)
        QMessageBox.warning(
            self, "Collection Info", f"Could not load collection metadata:\n{error_msg}"
        )

    def _check_new_granules(self):
        """Run the selected saved/recent search and report granules not in current results."""
        preset = self.preset_combo.currentData() or self.recent_combo.currentData()
        if not preset:
            QMessageBox.information(
                self, "Check New Granules", "Select a saved or recent search first."
            )
            return

        current_results = self._search_results or []
        self._alert_baseline_ids = {
            granule_native_id(granule, f"Item {index + 1}")
            for index, granule in enumerate(current_results)
        }
        self._apply_search_preset(preset)
        dataset_item = self.dataset_combo.currentData() or {}
        advanced = self._current_advanced_options()
        bbox = None
        bbox_text = self.bbox_input.text().strip()
        if bbox_text:
            try:
                parts = [float(x.strip()) for x in bbox_text.split(",")]
                if len(parts) != 4:
                    raise ValueError("Bounding box must have 4 values")
                bbox = tuple(parts)
            except Exception as e:
                self.check_new_btn.setEnabled(True)
                QMessageBox.warning(
                    self,
                    "Check New Granules",
                    f"Invalid bounding box in selected search:\n{e}",
                )
                return
        temporal = (
            self.start_date.date().toString("yyyy-MM-dd"),
            self.end_date.date().toString("yyyy-MM-dd"),
        )
        orbit_number = None
        if advanced.get("orbit_min") or advanced.get("orbit_max"):
            if advanced.get("orbit_min") and advanced.get("orbit_max"):
                orbit_number = (advanced["orbit_min"], advanced["orbit_max"])
            else:
                orbit_number = advanced.get("orbit_min") or advanced.get("orbit_max")

        self.check_new_btn.setEnabled(False)
        self._log(f"Checking for new granules in {preset.get('name', 'search')}...")
        self._alert_worker = DataSearchWorker(
            dataset_item.get("short_name", ""),
            dataset_item.get("concept_id", ""),
            bbox,
            temporal,
            self.max_items_spin.value(),
            cloud_cover=(
                (
                    advanced.get("cloud_min", 0),
                    advanced.get("cloud_max", 100),
                )
                if advanced.get("cloud_min", 0) > 0
                or advanced.get("cloud_max", 100) < 100
                else None
            ),
            day_night=advanced.get("day_night"),
            provider=advanced.get("provider") or None,
            version=advanced.get("version") or None,
            granule_id=advanced.get("granule_id") or None,
            orbit_number=orbit_number,
        )
        self._alert_worker.finished.connect(self._on_check_new_finished)
        self._alert_worker.error.connect(self._on_check_new_error)
        self._alert_worker.progress.connect(self._log)
        self._alert_worker.start()

    def _on_check_new_finished(self, results, _gdf):
        """Report saved-search delta results."""
        self.check_new_btn.setEnabled(True)
        new_ids = []
        for index, granule in enumerate(results or []):
            native_id = granule_native_id(granule, f"Item {index + 1}")
            if native_id not in self._alert_baseline_ids:
                new_ids.append(native_id)
        message = (
            f"Found {len(new_ids)} new granule(s) out of {len(results or [])} checked."
        )
        if new_ids:
            message += "\n\n" + "\n".join(new_ids[:25])
            if len(new_ids) > 25:
                message += f"\n...and {len(new_ids) - 25} more"
        self._log(message)
        QMessageBox.information(self, "Check New Granules", message)

    def _on_check_new_error(self, error_msg):
        """Handle saved-search delta check errors."""
        self.check_new_btn.setEnabled(True)
        self._log(f"Check new granules error: {error_msg}", error=True)
        QMessageBox.warning(
            self,
            "Check New Granules",
            f"Could not check for new granules:\n{error_msg}",
        )

    def _sync_footprint_selection_from_table(self):
        """Highlight footprint features matching selected table rows."""
        footprints_layer = self._valid_layer_or_none("_footprints_layer")
        if footprints_layer is None:
            return

        try:
            selected_indices = set(self._get_selected_result_indices())
            self._syncing_footprint_table_selection = True
            footprints_layer.removeSelection()
            self._remove_selected_footprints()

            if not selected_indices:
                self.iface.mapCanvas().refresh()
                return

            selected_features = []
            selected_feature_ids = []
            field_names = [f.name() for f in footprints_layer.fields()]
            has_result_idx_field = "result_idx" in field_names

            for feature_pos, feature in enumerate(footprints_layer.getFeatures()):
                if has_result_idx_field:
                    try:
                        feature_result_idx = int(feature["result_idx"])
                    except Exception:
                        feature_result_idx = feature_pos
                else:
                    feature_result_idx = feature_pos

                if feature_result_idx in selected_indices:
                    selected_features.append(feature)
                    selected_feature_ids.append(feature.id())

            if selected_feature_ids:
                footprints_layer.selectByIds(selected_feature_ids)
            self._add_selected_footprints_overlay(selected_features)
            self.iface.mapCanvas().refresh()
        except RuntimeError as e:
            if "wrapped C/C++ object" in str(e):
                self._footprints_layer = None
                self._remove_selected_footprints()
                return
            self._log(f"Error syncing footprint selection: {e}", error=True)
        except Exception as e:
            self._log(f"Error syncing footprint selection: {e}", error=True)
        finally:
            self._syncing_footprint_table_selection = False

    def _result_indices_for_selected_footprints(self, footprints_layer):
        """Return search result indices for selected footprint features."""
        selected_feature_ids = set(footprints_layer.selectedFeatureIds())
        if not selected_feature_ids:
            return []

        result_indices = []
        field_names = [field.name() for field in footprints_layer.fields()]
        has_result_idx_field = "result_idx" in field_names
        for feature_pos, feature in enumerate(footprints_layer.getFeatures()):
            if feature.id() not in selected_feature_ids:
                continue
            if has_result_idx_field:
                try:
                    result_idx = int(feature["result_idx"])
                except Exception:
                    result_idx = feature_pos
            else:
                result_idx = feature_pos
            result_indices.append(result_idx)
        return list(dict.fromkeys(result_indices))

    def _table_rows_for_result_indices(self, result_indices):
        """Return current table rows that correspond to stable result indices."""
        wanted = set(result_indices)
        rows = []
        for row in range(self.results_table.rowCount()):
            if self._get_result_index_for_table_row(row) in wanted:
                rows.append(row)
        return rows

    def _select_table_rows_for_result_indices(self, result_indices):
        """Select result table rows matching map-selected footprint features."""
        rows = self._table_rows_for_result_indices(result_indices)
        selection_model = self.results_table.selectionModel()
        if selection_model is None:
            return

        self._syncing_footprint_table_selection = True
        try:
            selection_model.clearSelection()
            if not rows:
                return
            flag_scope = getattr(
                QItemSelectionModel, "SelectionFlag", QItemSelectionModel
            )
            select_rows = getattr(flag_scope, "Select") | getattr(flag_scope, "Rows")
            for row in rows:
                index = self.results_table.model().index(row, 0)
                selection_model.select(index, select_rows)
            first_row = rows[0]
            self.results_table.setCurrentCell(first_row, 0)
            first_item = self.results_table.item(first_row, 0)
            if first_item is not None:
                self.results_table.scrollToItem(first_item)
        finally:
            self._syncing_footprint_table_selection = False

    def _on_footprint_selection_changed(self, *_args):
        """Select result table rows when footprint features are selected on the map."""
        if self._syncing_footprint_table_selection:
            return
        footprints_layer = self._valid_layer_or_none("_footprints_layer")
        if footprints_layer is None:
            return
        try:
            result_indices = self._result_indices_for_selected_footprints(
                footprints_layer
            )
            self._select_table_rows_for_result_indices(result_indices)
            self._remove_selected_footprints()
            if result_indices:
                selected_features = []
                selected = set(result_indices)
                for feature_pos, feature in enumerate(footprints_layer.getFeatures()):
                    try:
                        feature_result_idx = int(feature["result_idx"])
                    except Exception:
                        feature_result_idx = feature_pos
                    if feature_result_idx in selected:
                        selected_features.append(feature)
                self._add_selected_footprints_overlay(selected_features)
            self._update_granule_details()
            self._update_quicklook_preview()
        except RuntimeError as e:
            if "wrapped C/C++ object" in str(e):
                self._footprints_layer = None
                self._remove_selected_footprints()
                return
            self._log(f"Error syncing footprint selection to table: {e}", error=True)
        except Exception as e:
            self._log(f"Error syncing footprint selection to table: {e}", error=True)

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
        """Set initial detail widths and make ID fill the remaining space."""
        if self._adjusting_results_columns:
            return

        self._adjusting_results_columns = True
        try:
            self.results_table.setColumnWidth(1, 90)
            self.results_table.setColumnWidth(2, 80)
            self.results_table.setColumnWidth(3, 80)
            self.results_table.setColumnWidth(4, 70)
            self.results_table.setColumnWidth(5, 80)
            self.results_table.setColumnWidth(6, 55)
        finally:
            self._adjusting_results_columns = False

        self._fit_results_columns_to_width()

    def _fit_results_columns_to_width(self):
        """Make the ID column fill leftover width after detail columns."""
        if self._adjusting_results_columns:
            return

        viewport_width = self.results_table.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.results_table.width()
        if viewport_width <= 0:
            return

        header = self.results_table.horizontalHeader()
        min_width = max(45, header.minimumSectionSize())
        detail_width = 0
        for column, fallback in ((1, 90), (2, 80), (3, 80), (4, 70), (5, 80), (6, 55)):
            detail_width += max(
                min_width, self.results_table.columnWidth(column) or fallback
            )
        id_width = max(180, viewport_width - detail_width)

        self._adjusting_results_columns = True
        try:
            self.results_table.setColumnWidth(0, id_width)
        finally:
            self._adjusting_results_columns = False

    def _on_results_section_resized(self, logical_index, _old_size, _new_size):
        """Keep ID as the fill column when detail columns are resized."""
        if logical_index != 0 and not self._adjusting_results_columns:
            QTimer.singleShot(0, self._fit_results_columns_to_width)

    def _filter_result_rows(self):
        """Hide result rows that do not match the result filter text."""
        if not hasattr(self, "result_filter_input"):
            return
        text = self.result_filter_input.text().strip().lower()
        for row in range(self.results_table.rowCount()):
            if not text:
                self.results_table.setRowHidden(row, False)
                continue
            values = []
            for column in range(self.results_table.columnCount()):
                item = self.results_table.item(row, column)
                if item is None:
                    continue
                values.append(item.text())
                values.append(item.toolTip())
            haystack = " ".join(values).lower()
            self.results_table.setRowHidden(row, text not in haystack)

    def _set_default_download_column_widths(self):
        """Set initial queue column widths and make Granule fill remaining space."""
        if self._adjusting_download_columns:
            return

        self._adjusting_download_columns = True
        try:
            self.download_queue_table.setColumnWidth(1, 80)
            self.download_queue_table.setColumnWidth(2, 130)
            self.download_queue_table.setColumnWidth(3, 90)
        finally:
            self._adjusting_download_columns = False

        self._fit_download_columns_to_width()

    def _fit_download_columns_to_width(self):
        """Make the Granule column fill leftover queue table width."""
        if self._adjusting_download_columns:
            return

        viewport_width = self.download_queue_table.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.download_queue_table.width()
        if viewport_width <= 0:
            return

        header = self.download_queue_table.horizontalHeader()
        min_width = max(45, header.minimumSectionSize())
        status_width = max(min_width, self.download_queue_table.columnWidth(1) or 80)
        message_width = max(min_width, self.download_queue_table.columnWidth(2) or 130)
        files_width = max(min_width, self.download_queue_table.columnWidth(3) or 90)
        granule_width = max(
            180, viewport_width - status_width - message_width - files_width
        )

        self._adjusting_download_columns = True
        try:
            self.download_queue_table.setColumnWidth(0, granule_width)
        finally:
            self._adjusting_download_columns = False

    def _on_download_section_resized(self, logical_index, _old_size, _new_size):
        """Keep Granule as the fill column when queue detail columns resize."""
        if logical_index in (1, 2, 3) and not self._adjusting_download_columns:
            QTimer.singleShot(0, self._fit_download_columns_to_width)

    def eventFilter(self, obj, event):
        """Keep table columns fitted when viewports change size."""
        if (
            hasattr(self, "results_table")
            and obj in (self.results_table, self.results_table.viewport())
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._fit_results_columns_to_width)
        if (
            hasattr(self, "download_queue_table")
            and obj in (self.download_queue_table, self.download_queue_table.viewport())
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._fit_download_columns_to_width)
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

    def _clear_index_combos(self):
        """Clear and disable spectral index band selectors."""
        for combo in (self.index_positive_combo, self.index_negative_combo):
            combo.clear()
            combo.setEnabled(False)
        self.create_index_btn.setEnabled(False)

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
        self._populate_index_band_combos(cog_links)

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

    def _guess_index_channel_indices(self, cog_links, index_name):
        """Guess positive/negative bands for common normalized differences."""
        band_pairs = {
            "ndvi": (("b05", "b8", "nir"), ("b04", "b4", "red")),
            "ndwi": (("b03", "b3", "green"), ("b05", "b8", "nir")),
            "mndwi": (("b03", "b3", "green"), ("b06", "b11", "swir")),
            "ndmi": (("b05", "b8", "nir"), ("b06", "b11", "swir")),
            "nbr": (("b05", "b8", "nir"), ("b07", "b12", "swir")),
        }
        positive_tokens, negative_tokens = band_pairs.get(
            index_name, band_pairs["ndvi"]
        )

        def find_band(tokens):
            for idx, link in enumerate(cog_links):
                name = os.path.basename(link).split("?")[0].lower()
                normalized = name.replace("-", "_").replace(".", "_")
                for token in tokens:
                    token = token.lower()
                    if token in name or f"_{token}_" in f"_{normalized}_":
                        return idx
            return -1

        positive = find_band(positive_tokens)
        negative = find_band(negative_tokens)
        fallback = list(range(min(2, len(cog_links))))
        while len(fallback) < 2:
            fallback.append(-1)
        return (
            positive if positive >= 0 else fallback[0],
            negative if negative >= 0 else fallback[1],
        )

    def _populate_index_band_combos(self, cog_links):
        """Populate spectral index selectors."""
        for combo in (self.index_positive_combo, self.index_negative_combo):
            combo.clear()
            for link in cog_links:
                filename = os.path.basename(link).split("?")[0]
                combo.addItem(filename, link)
            combo.setEnabled(bool(cog_links))

        positive, negative = self._guess_index_channel_indices(
            cog_links, self.index_type_combo.currentData() or "ndvi"
        )
        for combo, index in (
            (self.index_positive_combo, positive),
            (self.index_negative_combo, negative),
        ):
            if 0 <= index < combo.count():
                combo.setCurrentIndex(index)
        self.create_index_btn.setEnabled(len(cog_links) >= 2)

    def _on_index_type_changed(self, _index):
        """Refresh guessed index bands when the index type changes."""
        links = [
            self.index_positive_combo.itemData(row)
            for row in range(self.index_positive_combo.count())
            if self.index_positive_combo.itemData(row)
        ]
        if links:
            self._populate_index_band_combos(links)

    def _create_index_vrt(self):
        """Start a background normalized-difference VRT creation job."""
        positive = self.index_positive_combo.currentData()
        negative = self.index_negative_combo.currentData()
        index_name = self.index_type_combo.currentData() or "ndvi"
        if self._index_worker is not None and self._index_worker.isRunning():
            return
        if not positive or not negative:
            QMessageBox.warning(self, "Create Index VRT", "Select two input bands.")
            return
        if positive == negative:
            QMessageBox.warning(
                self, "Create Index VRT", "Select two different input bands."
            )
            return

        output_path = os.path.join(
            tempfile.gettempdir(),
            f"nasa_earthdata_{index_name}_{uuid.uuid4().hex}.vrt",
        )
        self.create_index_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self._log(f"Creating {index_name.upper()} VRT...")

        self._index_worker = IndexVrtWorker(positive, negative, index_name, output_path)
        self._index_worker.progress.connect(self._log)
        self._index_worker.finished.connect(self._on_index_vrt_finished)
        self._index_worker.error.connect(self._on_index_vrt_error)
        self._index_worker.start()

    def _index_inputs_ready(self):
        """Return whether index controls contain enough bands to build a VRT."""
        return (
            self.index_positive_combo.count() >= 2
            and self.index_negative_combo.count() >= 2
        )

    def _on_index_vrt_finished(self, index_name, output_path):
        """Add a completed normalized-difference VRT to the project."""
        self.progress_bar.setVisible(False)
        self.create_index_btn.setEnabled(self._index_inputs_ready())
        self._index_worker = None
        try:
            layer = QgsRasterLayer(output_path, f"NASA Earthdata {index_name.upper()}")
            if layer.isValid():
                self._set_index_layer_visual_range(layer)
                QgsProject.instance().addMapLayer(layer)
                self._log(f"Added {index_name.upper()} VRT: {output_path}")
                self._notify_success(
                    "NASA Earthdata", f"Added {index_name.upper()} layer"
                )
            else:
                QMessageBox.warning(
                    self,
                    "Create Index VRT",
                    "The VRT was written but QGIS could not load it as a raster.",
                )
        except Exception as e:
            QMessageBox.critical(
                self, "Create Index VRT", f"Failed to create VRT:\n{e}"
            )

    def _on_index_vrt_error(self, error_msg):
        """Handle background normalized-difference VRT creation errors."""
        self.progress_bar.setVisible(False)
        self.create_index_btn.setEnabled(self._index_inputs_ready())
        self._index_worker = None
        self._log(f"Index VRT error: {error_msg}", error=True)
        QMessageBox.critical(
            self, "Create Index VRT", f"Failed to create VRT:\n{error_msg}"
        )

    def _set_index_layer_visual_range(self, layer, minimum=-1.0, maximum=1.0):
        """Set normalized-difference raster display range to [-1, 1]."""
        try:
            renderer = layer.renderer()
            if renderer is None:
                return

            if hasattr(renderer, "setClassificationMin"):
                renderer.setClassificationMin(float(minimum))
            if hasattr(renderer, "setClassificationMax"):
                renderer.setClassificationMax(float(maximum))

            provider = layer.dataProvider() if hasattr(layer, "dataProvider") else None
            data_type = provider.dataType(1) if provider is not None else None
            enhancement = QgsContrastEnhancement(data_type)
            enhancement.setMinimumValue(float(minimum))
            enhancement.setMaximumValue(float(maximum))
            algorithm = getattr(
                QgsContrastEnhancement,
                "StretchToMinimumMaximum",
                None,
            )
            if algorithm is not None:
                enhancement.setContrastEnhancementAlgorithm(algorithm, True)
            if hasattr(renderer, "setContrastEnhancement"):
                renderer.setContrastEnhancement(enhancement)
            if hasattr(layer, "triggerRepaint"):
                layer.triggerRepaint()
        except Exception as e:
            self._log(f"Could not set index visualization range: {e}", error=True)

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

    def _populate_download_queue(self, granules):
        """Populate the download queue table."""
        self.download_queue_table.setRowCount(len(granules or []))
        for index, granule in enumerate(granules or []):
            native_id = granule_native_id(granule, f"Item {index + 1}")
            values = [native_id, "queued", "", ""]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.download_queue_table.setItem(index, column, item)
        self._fit_download_columns_to_width()

    def _load_persistent_download_queue(self):
        """Restore the last download queue snapshot, if available."""
        try:
            state = load_download_queue_state(download_queue_state_path(self.settings))
        except Exception as e:
            self._log(f"Could not load previous download queue: {e}", error=True)
            return
        rows = state.get("rows") or []
        if not rows:
            return
        self.download_queue_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row.get("native_id", ""),
                row.get("status", ""),
                row.get("message", ""),
                "\n".join(str(path) for path in row.get("files", [])),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.download_queue_table.setItem(row_index, column, item)
        self._last_download_rows = rows
        self._last_download_manifest = state.get("manifest", "")
        self._last_download_output_dir = state.get("output_dir", "")
        self._fit_download_columns_to_width()

    def _on_download_queue_update(self, row, status, message, files):
        """Update one download queue row."""
        if row < 0 or row >= self.download_queue_table.rowCount():
            return
        values = {
            1: status,
            2: message,
            3: "\n".join(str(file_path) for file_path in files),
        }
        for column, value in values.items():
            item = self.download_queue_table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.download_queue_table.setItem(row, column, item)
            item.setText(str(value))
            item.setToolTip(str(value))

    def _cancel_download(self):
        """Cancel the active download queue."""
        if self._download_worker is not None and self._download_worker.isRunning():
            self._download_worker.cancel()
            self.cancel_download_btn.setEnabled(False)
            self._log("Cancelling download queue after the current item...")

    def _retry_failed_downloads(self):
        """Retry failed download queue items in the previous output folder."""
        if not self._last_download_output_dir:
            return

        failed_indices = [
            int(row.get("index"))
            for row in self._last_download_rows
            if row.get("status") == "failed"
        ]
        granules = [
            self._last_download_granules[index]
            for index in failed_indices
            if 0 <= index < len(self._last_download_granules)
        ]
        if not granules:
            self.retry_failed_btn.setEnabled(False)
            return

        self.download_btn.setEnabled(False)
        self.cancel_download_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self._populate_download_queue(granules)
        self._log(f"Retrying {len(granules)} failed download(s)...")

        self._download_worker = DataDownloadWorker(
            granules,
            self._last_download_output_dir,
            threads=self.settings.value("NASAEarthdata/download_threads", 1, type=int),
            skip_existing=True,
        )
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.error.connect(self._on_download_error)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.queue_update.connect(self._on_download_queue_update)
        self._download_worker.start()

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
            self._notify_success(
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
        self.cancel_download_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self._log(f"Downloading {len(granules)} granule(s) to {output_dir}...")
        self._populate_download_queue(granules)
        self._last_download_granules = list(granules)
        self._last_download_output_dir = output_dir

        # Start download worker
        self._download_worker = DataDownloadWorker(
            granules,
            output_dir,
            threads=self.settings.value("NASAEarthdata/download_threads", 1, type=int),
            skip_existing=True,
        )
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.error.connect(self._on_download_error)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.queue_update.connect(self._on_download_queue_update)
        self._download_worker.start()

    def _on_download_progress(self, percent, message):
        """Handle download progress."""
        self.progress_bar.setValue(percent)
        self._log(message)

    def _on_download_finished(self, files, manifest, queue_rows):
        """Handle download completion."""
        self.download_btn.setEnabled(True)
        self.cancel_download_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self._last_download_rows = queue_rows
        self._last_download_manifest = manifest
        try:
            write_download_queue_state(
                download_queue_state_path(self.settings),
                queue_rows,
                manifest=manifest,
                output_dir=self._last_download_output_dir,
            )
        except Exception as e:
            self._log(f"Could not persist download queue: {e}", error=True)

        failed_count = len([row for row in queue_rows if row.get("status") == "failed"])
        self.retry_failed_btn.setEnabled(failed_count > 0)
        self._log(
            f"Download complete! {len(files)} file(s) available. Manifest: {manifest}"
        )

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
        self._notify_success("NASA Earthdata", "Download queue complete")

    def _on_download_error(self, error_msg):
        """Handle download error."""
        self.download_btn.setEnabled(True)
        self.cancel_download_btn.setEnabled(False)
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

    def _notify_success(self, title, message):
        """Show a success notification when enabled."""
        if not self.settings.value("NASAEarthdata/notifications", True, type=bool):
            return
        try:
            self.iface.messageBar().pushSuccess(title, message)
        except Exception:
            pass  # nosec B110

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
