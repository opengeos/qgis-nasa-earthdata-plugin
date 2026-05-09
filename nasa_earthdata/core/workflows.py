"""Workflow helpers for NASA Earthdata searches, exports, and downloads."""

import csv
import json
import os
from urllib.parse import quote, urlsplit
from datetime import datetime, timezone
from pathlib import Path

PRESET_SCHEMA_VERSION = 1
CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.json"
RESULT_EXPORT_FIELDS = [
    "result_idx",
    "native_id",
    "dataset_short_name",
    "dataset_concept_id",
    "dataset_provider",
    "dataset_version",
    "dataset_title",
    "temporal_start",
    "temporal_end",
    "size_bytes",
    "size_display",
    "cloud_cover",
    "day_night",
    "provider",
    "collection_concept_id",
    "granule_ur",
    "links",
    "cog_links",
]


def _jsonable(value):
    """Convert common Earthdata/QGIS-adjacent objects to JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "items"):
        try:
            return _jsonable(dict(value.items()))
        except Exception:
            pass  # nosec B110
    return str(value)


def utc_timestamp():
    """Return an ISO-8601 UTC timestamp with a trailing Z."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def workflow_dir(settings=None):
    """Return the directory used for saved searches and exported manifests."""
    configured = ""
    if settings is not None:
        configured = settings.value("NASAEarthdata/workflow_dir", "", type=str)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".qgis_nasa_earthdata" / "workflows"


def cmr_collection_url(dataset_item):
    """Return a CMR collection metadata URL for a catalog dataset item."""
    dataset_item = dataset_item or {}
    concept_id = (dataset_item.get("concept_id") or "").strip()
    short_name = (dataset_item.get("short_name") or "").strip()
    if concept_id:
        return f"{CMR_COLLECTIONS_URL}?concept_id={quote(concept_id)}"
    if short_name:
        return f"{CMR_COLLECTIONS_URL}?short_name={quote(short_name)}"
    return ""


def cmr_collection_summary(payload):
    """Extract compact collection details from a CMR collections response."""
    entries = (
        payload.get("feed", {}).get("entry", []) if isinstance(payload, dict) else []
    )
    if not entries:
        return {}
    entry = entries[0]
    polygons = entry.get("polygons") or []
    boxes = entry.get("boxes") or []
    links = [
        item.get("href")
        for item in entry.get("links", [])
        if isinstance(item, dict) and item.get("href")
    ]
    return {
        "concept_id": entry.get("id", ""),
        "short_name": entry.get("short_name", ""),
        "version_id": entry.get("version_id", ""),
        "title": entry.get("title", ""),
        "summary": entry.get("summary", ""),
        "provider": entry.get("data_center", ""),
        "time_start": entry.get("time_start", ""),
        "time_end": entry.get("time_end", ""),
        "updated": entry.get("updated", ""),
        "doi": entry.get("doi", ""),
        "cloud_hosted": bool(entry.get("cloud_hosted")),
        "archive_center": entry.get("archive_center", ""),
        "spatial_extent": polygons[:3] or boxes[:3],
        "links": links[:8],
    }


def presets_path(settings=None):
    """Return the JSON file path used for saved search presets."""
    return workflow_dir(settings) / "search_presets.json"


def manifests_dir(settings=None):
    """Return the directory used for download manifests."""
    return workflow_dir(settings) / "manifests"


def download_queue_state_path(settings=None):
    """Return the path used for the latest persistent download queue snapshot."""
    return workflow_dir(settings) / "download_queue_latest.json"


def load_search_presets(path):
    """Load search presets from disk."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list):
        return payload
    return payload.get("presets", [])


def save_search_presets(path, presets):
    """Write search presets to disk using a stable schema."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "updated_at": utc_timestamp(),
        "presets": presets,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def upsert_search_preset(path, preset):
    """Insert or replace a named search preset."""
    presets = [
        existing
        for existing in load_search_presets(path)
        if existing.get("name") != preset.get("name")
    ]
    presets.insert(0, preset)
    save_search_presets(path, presets)
    return presets


def delete_search_preset(path, name):
    """Delete saved search presets matching ``name``."""
    presets = [
        existing
        for existing in load_search_presets(path)
        if existing.get("name") != name
    ]
    save_search_presets(path, presets)
    return presets


def build_search_preset(
    name,
    dataset_item,
    bbox_text,
    start_date,
    end_date,
    max_items,
    advanced_options=None,
):
    """Build a serializable search preset from dock state."""
    dataset_item = dataset_item or {}
    return {
        "schema_version": PRESET_SCHEMA_VERSION,
        "name": name,
        "created_at": utc_timestamp(),
        "dataset": {
            "label": dataset_item.get("label", ""),
            "short_name": dataset_item.get("short_name", ""),
            "concept_id": dataset_item.get("concept_id", ""),
            "provider": dataset_item.get("provider", ""),
            "version": dataset_item.get("version", ""),
            "title": dataset_item.get("title", ""),
        },
        "bbox": bbox_text or "",
        "temporal": {"start": start_date or "", "end": end_date or ""},
        "max_items": int(max_items),
        "advanced": advanced_options or {},
    }


def record_recent_search(settings, preset, limit=10):
    """Store a compact recent-search list in QSettings."""
    if settings is None:
        return []
    raw = settings.value("NASAEarthdata/recent_searches", "[]", type=str)
    try:
        recent = json.loads(raw) if raw else []
    except Exception:
        recent = []
    signature = _preset_signature(preset)
    recent = [item for item in recent if _preset_signature(item) != signature]
    recent.insert(0, preset)
    recent = recent[:limit]
    settings.setValue("NASAEarthdata/recent_searches", json.dumps(recent))
    settings.sync()
    return recent


def load_recent_searches(settings):
    """Load recent searches from QSettings."""
    if settings is None:
        return []
    raw = settings.value("NASAEarthdata/recent_searches", "[]", type=str)
    try:
        value = json.loads(raw) if raw else []
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_recent_searches(settings, recent):
    """Save recent searches to QSettings."""
    if settings is None:
        return []
    settings.setValue("NASAEarthdata/recent_searches", json.dumps(recent))
    settings.sync()
    return recent


def delete_recent_search(settings, index):
    """Delete one recent search by list index."""
    recent = load_recent_searches(settings)
    if 0 <= index < len(recent):
        del recent[index]
    return save_recent_searches(settings, recent)


def _preset_signature(preset):
    """Return a stable identity for deduplicating recent searches."""
    dataset = preset.get("dataset", {})
    temporal = preset.get("temporal", {})
    return (
        dataset.get("concept_id") or dataset.get("short_name"),
        preset.get("bbox", ""),
        temporal.get("start", ""),
        temporal.get("end", ""),
        json.dumps(preset.get("advanced", {}), sort_keys=True),
    )


def granule_get(granule, *path, default=None):
    """Read nested dict-like granule values."""
    current = granule
    for key in path:
        try:
            current = current.get(key, default)
        except AttributeError:
            return default
        if current is None:
            return default
    return current


def granule_native_id(granule, fallback=""):
    """Return the best available native granule ID."""
    return (
        granule_get(granule, "meta", "native-id")
        or granule_get(granule, "umm", "GranuleUR")
        or fallback
    )


def granule_temporal_range(granule):
    """Return beginning and ending timestamps from a granule."""
    range_dt = granule_get(
        granule,
        "umm",
        "TemporalExtent",
        "RangeDateTime",
        default={},
    )
    if not isinstance(range_dt, dict):
        return "", ""
    return range_dt.get("BeginningDateTime", ""), range_dt.get("EndingDateTime", "")


def granule_size_bytes(granule):
    """Return the first declared archive size in bytes."""
    infos = granule_get(
        granule,
        "umm",
        "DataGranule",
        "ArchiveAndDistributionInformation",
        default=[],
    )
    if not isinstance(infos, list):
        return 0
    for info in infos:
        try:
            return int(float(info.get("SizeInBytes", 0) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def format_size(size_bytes):
    """Format a byte count for display."""
    if size_bytes <= 0:
        return "N/A"
    if size_bytes > 1e9:
        return f"{size_bytes / 1e9:.1f} GB"
    if size_bytes > 1e6:
        return f"{size_bytes / 1e6:.1f} MB"
    return f"{size_bytes / 1e3:.1f} KB"


def granule_links(granule):
    """Return data links from an earthaccess granule or UMM RelatedUrls."""
    links = []
    try:
        try:
            links = list(granule.data_links(access="external"))
        except TypeError:
            links = list(granule.data_links())
    except Exception:
        links = []
    if links:
        return list(dict.fromkeys(str(link) for link in links if link))

    related_urls = granule_get(granule, "umm", "RelatedUrls", default=[])
    if isinstance(related_urls, list):
        for item in related_urls:
            if isinstance(item, dict) and item.get("URL"):
                links.append(str(item["URL"]))
    return list(dict.fromkeys(links))


def granule_related_urls(granule):
    """Return UMM RelatedUrls entries as dictionaries."""
    related_urls = granule_get(granule, "umm", "RelatedUrls", default=[])
    if not isinstance(related_urls, list):
        return []
    return [item for item in related_urls if isinstance(item, dict)]


def granule_quicklook_links(granule):
    """Return directly displayable browse/quicklook image URLs for a granule.

    CMR entries can include matching ``s3://`` browse objects. Those are useful
    for same-region cloud access but are not browser-displayable in QGIS, so do
    not return them here.
    """
    links = []
    for item in granule_related_urls(granule):
        url = str(item.get("URL", "")).strip()
        if not url:
            continue
        url_lower = url.lower()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            continue
        path_lower = parsed.path.lower()
        type_text = " ".join(
            str(item.get(key, ""))
            for key in ("Type", "Subtype", "Description", "Format")
        ).lower()
        if (
            "browse" in type_text
            or "quicklook" in type_text
            or "thumbnail" in type_text
            or any(
                path_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif")
            )
        ):
            links.append(url)
    return list(dict.fromkeys(links))


def granule_inaccessible_quicklook_links(granule):
    """Return browse/quicklook URLs that are not directly displayable in QGIS."""
    links = []
    for item in granule_related_urls(granule):
        url = str(item.get("URL", "")).strip()
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https"):
            continue
        path_lower = parsed.path.lower()
        type_text = " ".join(
            str(item.get(key, ""))
            for key in ("Type", "Subtype", "Description", "Format")
        ).lower()
        if (
            "browse" in type_text
            or "quicklook" in type_text
            or "thumbnail" in type_text
            or any(
                path_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif")
            )
        ):
            links.append(url)
    return list(dict.fromkeys(links))


def granule_citation_links(granule):
    """Return likely DOI/citation/documentation URLs for a granule."""
    links = []
    for item in granule_related_urls(granule):
        url = str(item.get("URL", "")).strip()
        if not url:
            continue
        text = " ".join(
            str(item.get(key, ""))
            for key in ("Type", "Subtype", "Description", "Format")
        ).lower()
        if "doi" in url.lower() or "citation" in text or "documentation" in text:
            links.append(url)
    return list(dict.fromkeys(links))


def cog_links_from_links(links):
    """Return HTTPS TIFF/COG-looking links."""
    return [
        link
        for link in links
        if link.lower().startswith("http")
        and any(ext in link.lower() for ext in (".tif", ".tiff"))
    ]


def granules_to_raw_jsonable(granules):
    """Convert earthaccess granules to a stable JSON-serializable list."""
    return [_jsonable(granule) for granule in (granules or [])]


def write_granules_json(path, granules):
    """Write raw earthaccess/CMR granules to JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(granules_to_raw_jsonable(granules), f, indent=2, sort_keys=True)


def granule_export_row(granule, result_idx, dataset_item=None):
    """Convert a granule into a stable flat export row."""
    dataset_item = dataset_item or {}
    links = granule_links(granule)
    cog_links = cog_links_from_links(links)
    temporal_start, temporal_end = granule_temporal_range(granule)
    size_bytes = granule_size_bytes(granule)
    return {
        "result_idx": result_idx,
        "native_id": granule_native_id(granule, f"Item {result_idx + 1}"),
        "dataset_short_name": dataset_item.get("short_name", ""),
        "dataset_concept_id": dataset_item.get("concept_id", ""),
        "dataset_provider": dataset_item.get("provider", ""),
        "dataset_version": dataset_item.get("version", ""),
        "dataset_title": dataset_item.get("title", ""),
        "temporal_start": temporal_start,
        "temporal_end": temporal_end,
        "size_bytes": size_bytes,
        "size_display": format_size(size_bytes),
        "cloud_cover": granule_get(granule, "umm", "CloudCover", default=""),
        "day_night": granule_get(
            granule, "umm", "DataGranule", "DayNightFlag", default=""
        ),
        "provider": granule_get(granule, "meta", "provider-id", default=""),
        "collection_concept_id": granule_get(
            granule, "umm", "CollectionReference", "ConceptID", default=""
        ),
        "granule_ur": granule_get(granule, "umm", "GranuleUR", default=""),
        "links": "\n".join(links),
        "cog_links": "\n".join(cog_links),
    }


def granules_to_export_rows(granules, dataset_item=None):
    """Convert granules to export rows."""
    return [
        granule_export_row(granule, index, dataset_item)
        for index, granule in enumerate(granules or [])
    ]


def _geometry_for_index(gdf, index):
    if gdf is None:
        return None
    try:
        geom = gdf.geometry.iloc[index]
        if geom is not None and not geom.is_empty:
            return geom.__geo_interface__
    except Exception:
        return None
    return None


def _bbox_for_geometry(geometry):
    if not geometry:
        return None

    coords = []

    def collect(value):
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(
                isinstance(item, (int, float)) for item in value[:2]
            ):
                coords.append((float(value[0]), float(value[1])))
            else:
                for item in value:
                    collect(item)

    collect(geometry.get("coordinates"))
    if not coords:
        return None
    xs = [item[0] for item in coords]
    ys = [item[1] for item in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def granules_to_stac_item_collection(granules, dataset_item=None, gdf=None):
    """Convert granules to a lightweight STAC ItemCollection."""
    dataset_item = dataset_item or {}
    rows = granules_to_export_rows(granules, dataset_item)
    collection_href = cmr_collection_url(dataset_item)
    features = []
    for index, row in enumerate(rows):
        geometry = _geometry_for_index(gdf, index)
        bbox = _bbox_for_geometry(geometry)
        links = [link for link in row.get("links", "").splitlines() if link]
        cog_links = [link for link in row.get("cog_links", "").splitlines() if link]
        assets = {}
        for asset_index, link in enumerate(links, start=1):
            filename = os.path.basename(link.split("?")[0]) or f"asset-{asset_index}"
            role = "data" if link in cog_links else "metadata"
            assets[f"asset_{asset_index}"] = {
                "href": link,
                "title": filename,
                "roles": [role],
            }

        cloud_cover_raw = row.get("cloud_cover")
        try:
            cloud_cover = (
                float(cloud_cover_raw) if cloud_cover_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            cloud_cover = None

        features.append(
            {
                "type": "Feature",
                "stac_version": "1.0.0",
                "id": row.get("native_id") or f"granule-{index + 1}",
                "bbox": bbox,
                "geometry": geometry,
                "properties": {
                    "datetime": row.get("temporal_start") or None,
                    "start_datetime": row.get("temporal_start") or None,
                    "end_datetime": row.get("temporal_end") or None,
                    "platform": dataset_item.get("short_name", ""),
                    "earthdata:concept_id": row.get("collection_concept_id")
                    or dataset_item.get("concept_id", ""),
                    "earthdata:provider": row.get("provider")
                    or dataset_item.get("provider", ""),
                    "earthdata:granule_ur": row.get("granule_ur", ""),
                    "eo:cloud_cover": cloud_cover,
                },
                "collection": dataset_item.get("concept_id")
                or dataset_item.get("short_name", ""),
                "assets": assets,
                "links": (
                    [{"rel": "collection", "href": collection_href}]
                    if collection_href
                    else []
                ),
            }
        )

    return {
        "type": "FeatureCollection",
        "stac_version": "1.0.0",
        "features": features,
    }


def write_results_stac(path, granules, dataset_item=None, gdf=None):
    """Write current results as a STAC ItemCollection JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = granules_to_stac_item_collection(granules, dataset_item, gdf)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def write_workflow_bundle(
    path,
    preset,
    granules,
    rows,
    stac_item_collection=None,
    manifest=None,
):
    """Write a reproducible search/download workflow bundle."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "created_at": utc_timestamp(),
        "search": preset or {},
        "results": rows or [],
        "granules": granules_to_raw_jsonable(granules),
        "stac": stac_item_collection or {},
        "download_manifest": manifest or "",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def write_results_csv(path, rows):
    """Write result metadata rows to CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: row.get(field, "") for field in RESULT_EXPORT_FIELDS}
            )


def write_results_geojson(path, rows, gdf=None):
    """Write result metadata and optional geometries to GeoJSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    features = []
    for index, row in enumerate(rows):
        geometry = None
        if gdf is not None:
            try:
                geom = gdf.geometry.iloc[index]
                if geom is not None and not geom.is_empty:
                    geometry = geom.__geo_interface__
            except Exception:
                geometry = None
        features.append(
            {
                "type": "Feature",
                "properties": {
                    key: value for key, value in row.items() if key != "links"
                },
                "geometry": geometry,
            }
        )
    payload = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def likely_existing_download_files(granule, output_dir):
    """Return files in ``output_dir`` matching link basenames for a granule."""
    output_dir = Path(output_dir)
    existing = []
    for link in granule_links(granule):
        filename = os.path.basename(str(link).split("?")[0])
        if not filename:
            continue
        path = output_dir / filename
        if path.exists():
            existing.append(str(path))
    return existing


def download_manifest_path(output_dir):
    """Return a timestamped manifest path for a download queue."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(output_dir) / f"nasa_earthdata_download_manifest_{timestamp}.csv"


def write_download_manifest(path, rows):
    """Write download queue results to a manifest CSV."""
    fieldnames = ["index", "native_id", "status", "message", "files"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "index": row.get("index", ""),
                    "native_id": row.get("native_id", ""),
                    "status": row.get("status", ""),
                    "message": row.get("message", ""),
                    "files": "\n".join(str(item) for item in row.get("files", [])),
                }
            )


def write_download_queue_state(path, rows, manifest="", output_dir=""):
    """Persist the latest download queue state for restart recovery."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "updated_at": utc_timestamp(),
        "manifest": manifest or "",
        "output_dir": output_dir or "",
        "rows": rows or [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def load_download_queue_state(path):
    """Load the latest persistent download queue state."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}
