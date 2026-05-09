"""Workflow helpers for NASA Earthdata searches, exports, and downloads."""

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PRESET_SCHEMA_VERSION = 1
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


def presets_path(settings=None):
    """Return the JSON file path used for saved search presets."""
    return workflow_dir(settings) / "search_presets.json"


def manifests_dir(settings=None):
    """Return the directory used for download manifests."""
    return workflow_dir(settings) / "manifests"


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


def cog_links_from_links(links):
    """Return HTTPS TIFF/COG-looking links."""
    return [
        link
        for link in links
        if link.lower().startswith("http")
        and any(ext in link.lower() for ext in (".tif", ".tiff"))
    ]


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
