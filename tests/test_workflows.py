import csv
import json

from nasa_earthdata.core.workflows import (
    build_search_preset,
    cmr_collection_summary,
    cmr_collection_url,
    delete_recent_search,
    delete_search_preset,
    download_queue_state_path,
    granule_export_row,
    granule_quicklook_links,
    granule_inaccessible_quicklook_links,
    likely_existing_download_files,
    load_download_queue_state,
    load_search_presets,
    record_recent_search,
    granules_to_stac_item_collection,
    upsert_search_preset,
    write_download_queue_state,
    write_download_manifest,
    write_granules_json,
    write_results_stac,
    write_results_csv,
    write_results_geojson,
    write_workflow_bundle,
)


class FakeGranule(dict):
    def __init__(self, links=None):
        super().__init__(
            {
                "meta": {
                    "native-id": "HLS.L30.T10SEG.2025131T184540.v2.0",
                    "provider-id": "LPCLOUD",
                },
                "umm": {
                    "GranuleUR": "HLS_GRANULE",
                    "TemporalExtent": {
                        "RangeDateTime": {
                            "BeginningDateTime": "2025-05-11T18:45:40Z",
                            "EndingDateTime": "2025-05-11T18:45:45Z",
                        }
                    },
                    "DataGranule": {
                        "ArchiveAndDistributionInformation": [
                            {"SizeInBytes": 123456789}
                        ],
                    },
                    "CollectionReference": {"ConceptID": "C2021957657-LPCLOUD"},
                    "RelatedUrls": [
                        {
                            "URL": "https://example.test/preview.jpg",
                            "Type": "GET RELATED VISUALIZATION",
                            "Subtype": "BROWSE",
                        },
                        {
                            "URL": "s3://example-bucket/preview.jpg",
                            "Type": "GET RELATED VISUALIZATION",
                            "Subtype": "BROWSE",
                        },
                        {
                            "URL": "https://doi.org/10.1234/example",
                            "Description": "DOI landing page",
                        },
                    ],
                },
            }
        )
        self._links = links or []

    def data_links(self, access=None):
        return self._links


class FakeSettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default="", type=str):
        value = self.values.get(key, default)
        if type is str:
            return str(value)
        return value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


def test_search_preset_serialization_round_trip(tmp_path):
    preset = build_search_preset(
        name="Bay Area HLS",
        dataset_item={
            "label": "HLSL30 (2.0)",
            "short_name": "HLSL30",
            "concept_id": "C2021957657-LPCLOUD",
            "provider": "LPCLOUD",
            "version": "2.0",
            "title": "HLS Landsat",
        },
        bbox_text="-123,37,-122,38",
        start_date="2025-01-01",
        end_date="2025-01-31",
        max_items=25,
        advanced_options={"enabled": True, "cloud_max": 10},
    )

    path = tmp_path / "search_presets.json"
    upsert_search_preset(path, preset)
    loaded = load_search_presets(path)

    assert loaded[0]["name"] == "Bay Area HLS"
    assert loaded[0]["dataset"]["concept_id"] == "C2021957657-LPCLOUD"
    assert loaded[0]["advanced"]["cloud_max"] == 10


def test_delete_search_preset_removes_selected_name(tmp_path):
    path = tmp_path / "search_presets.json"
    first = build_search_preset("First", {}, "", "2025-01-01", "2025-01-02", 10)
    second = build_search_preset("Second", {}, "", "2025-02-01", "2025-02-02", 10)
    upsert_search_preset(path, first)
    upsert_search_preset(path, second)

    remaining = delete_search_preset(path, "First")

    assert [preset["name"] for preset in remaining] == ["Second"]
    assert [preset["name"] for preset in load_search_presets(path)] == ["Second"]


def test_delete_recent_search_removes_selected_index():
    settings = FakeSettings()
    first = build_search_preset("First", {}, "", "2025-01-01", "2025-01-02", 10)
    second = build_search_preset("Second", {}, "", "2025-02-01", "2025-02-02", 10)
    record_recent_search(settings, first)
    record_recent_search(settings, second)

    remaining = delete_recent_search(settings, 0)

    assert [preset["name"] for preset in remaining] == ["First"]


def test_granule_export_row_contains_dataset_identity_and_links():
    granule = FakeGranule(
        [
            "https://example.test/HLS.B04.tif",
            "https://example.test/HLS.metadata.json",
        ]
    )

    row = granule_export_row(
        granule,
        0,
        {
            "short_name": "HLSL30",
            "concept_id": "C2021957657-LPCLOUD",
            "provider": "LPCLOUD",
            "version": "2.0",
            "title": "HLS Landsat",
        },
    )

    assert row["native_id"] == "HLS.L30.T10SEG.2025131T184540.v2.0"
    assert row["dataset_short_name"] == "HLSL30"
    assert row["dataset_concept_id"] == "C2021957657-LPCLOUD"
    assert row["size_display"] == "123.5 MB"
    assert row["cog_links"] == "https://example.test/HLS.B04.tif"


def test_result_exports_write_stable_csv_and_geojson(tmp_path):
    row = granule_export_row(FakeGranule(["https://example.test/HLS.B04.tif"]), 0)
    csv_path = tmp_path / "earthdata_results.csv"
    geojson_path = tmp_path / "earthdata_results.geojson"

    write_results_csv(csv_path, [row])
    write_results_geojson(geojson_path, [row], None)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["native_id"] == "HLS.L30.T10SEG.2025131T184540.v2.0"

    with open(geojson_path, encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["type"] == "FeatureCollection"
    assert payload["features"][0]["properties"]["result_idx"] == 0


def test_download_helpers_detect_existing_files_and_write_manifest(tmp_path):
    existing = tmp_path / "HLS.B04.tif"
    existing.write_text("placeholder", encoding="utf-8")
    granule = FakeGranule(["https://example.test/HLS.B04.tif?token=abc"])

    assert likely_existing_download_files(granule, tmp_path) == [str(existing)]

    manifest = tmp_path / "manifest.csv"
    write_download_manifest(
        manifest,
        [
            {
                "index": 0,
                "native_id": "HLS",
                "status": "skipped",
                "message": "Skipped existing",
                "files": [str(existing)],
            }
        ],
    )
    assert "skipped" in manifest.read_text(encoding="utf-8")


def test_granules_json_stac_and_workflow_bundle_exports(tmp_path):
    granule = FakeGranule(["https://example.test/HLS.B04.tif"])
    granules_json = tmp_path / "granules.json"
    stac_json = tmp_path / "stac.json"
    bundle_json = tmp_path / "bundle.json"

    write_granules_json(granules_json, [granule])
    write_results_stac(
        stac_json,
        [granule],
        {"short_name": "HLSL30", "concept_id": "C2021957657-LPCLOUD"},
    )
    stac = granules_to_stac_item_collection([granule], {"short_name": "HLSL30"})
    write_workflow_bundle(
        bundle_json,
        {"name": "test search"},
        [granule],
        [granule_export_row(granule, 0)],
        stac_item_collection=stac,
        manifest="manifest.csv",
    )

    assert (
        json.loads(granules_json.read_text(encoding="utf-8"))[0]["umm"]["GranuleUR"]
        == "HLS_GRANULE"
    )
    assert json.loads(stac_json.read_text(encoding="utf-8"))["stac_version"] == "1.0.0"
    payload = json.loads(bundle_json.read_text(encoding="utf-8"))
    assert payload["search"]["name"] == "test search"
    assert payload["download_manifest"] == "manifest.csv"


def test_quicklook_and_cmr_collection_helpers():
    granule = FakeGranule(["https://example.test/HLS.B04.tif"])
    assert granule_quicklook_links(granule) == ["https://example.test/preview.jpg"]
    assert granule_inaccessible_quicklook_links(granule) == [
        "s3://example-bucket/preview.jpg"
    ]
    assert (
        cmr_collection_url({"concept_id": "C2021957657-LPCLOUD"})
        == "https://cmr.earthdata.nasa.gov/search/collections.json?concept_id=C2021957657-LPCLOUD"
    )

    summary = cmr_collection_summary(
        {
            "feed": {
                "entry": [
                    {
                        "id": "C1-TEST",
                        "short_name": "TEST",
                        "title": "Test Collection",
                        "cloud_hosted": True,
                        "links": [{"href": "https://example.test/docs"}],
                    }
                ]
            }
        }
    )
    assert summary["concept_id"] == "C1-TEST"
    assert summary["cloud_hosted"] is True
    assert summary["links"] == ["https://example.test/docs"]


def test_download_queue_state_round_trip(tmp_path):
    path = tmp_path / "queue.json"
    rows = [{"native_id": "HLS", "status": "done", "files": ["HLS.B04.tif"]}]

    write_download_queue_state(path, rows, manifest="manifest.csv", output_dir="/tmp")
    loaded = load_download_queue_state(path)

    assert loaded["manifest"] == "manifest.csv"
    assert loaded["rows"][0]["native_id"] == "HLS"
    assert (
        download_queue_state_path(FakeSettings()).name == "download_queue_latest.json"
    )
