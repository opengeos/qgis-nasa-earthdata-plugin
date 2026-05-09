from nasa_earthdata.dialogs.earthdata_dock import (
    CatalogData,
    CatalogLoadWorker,
    DataSearchWorker,
    EarthdataDockWidget,
    IndexVrtWorker,
    _compact_result_id,
)


def test_catalog_data_disambiguates_duplicate_short_names():
    catalog = CatalogData(
        [
            {
                "ShortName": "HLSL30",
                "EntryTitle": "HLS Landsat v1.5",
                "concept-id": "C3322682485-LPCLOUD",
                "provider-id": "LPCLOUD",
                "Version": "015",
            },
            {
                "ShortName": "HLSL30",
                "EntryTitle": "HLS Landsat v2.0",
                "concept-id": "C2021957657-LPCLOUD",
                "provider-id": "LPCLOUD",
                "Version": "2.0",
            },
        ]
    )

    items = catalog.get_dataset_items()

    assert items[0]["short_name"] == "HLSL30"
    assert items[0]["concept_id"] == "C3322682485-LPCLOUD"
    assert "C3322682485-LPCLOUD" in items[0]["label"]
    assert items[1]["title"] == "HLS Landsat v2.0"
    assert "C2021957657-LPCLOUD" in items[1]["label"]


def test_catalog_keyword_filter_can_match_concept_id():
    catalog = CatalogData(
        [
            {
                "ShortName": "HLSL30",
                "EntryTitle": "HLS Landsat v1.5",
                "concept-id": "C3322682485-LPCLOUD",
            },
            {
                "ShortName": "HLSS30",
                "EntryTitle": "HLS Sentinel v2.0",
                "concept-id": "C2021957295-LPCLOUD",
            },
        ]
    )

    assert [item["short_name"] for item in catalog.filter_by_keyword("7295")] == [
        "HLSS30"
    ]


def test_data_search_worker_prefers_concept_id_over_short_name():
    worker = DataSearchWorker(
        short_name="HLSL30",
        concept_id="C2021957657-LPCLOUD",
        bbox=(-123.0, 37.0, -122.0, 38.0),
        temporal=("2025-01-01", "2025-01-31"),
        max_items=10,
        provider="LPCLOUD",
    )

    kwargs = worker._build_search_kwargs()

    assert kwargs["concept_id"] == "C2021957657-LPCLOUD"
    assert "short_name" not in kwargs
    assert kwargs["bounding_box"] == (-123.0, 37.0, -122.0, 38.0)
    assert kwargs["temporal"] == ("2025-01-01", "2025-01-31")
    assert kwargs["provider"] == "LPCLOUD"


def test_catalog_load_worker_accepts_custom_catalog_and_cache_settings(tmp_path):
    worker = CatalogLoadWorker(
        force_refresh=True,
        catalog_url="https://example.test/catalog.tsv",
        cache_dir=str(tmp_path),
        cache_enabled=False,
    )

    assert worker.catalog_url == "https://example.test/catalog.tsv"
    assert worker.cache_dir == tmp_path
    assert worker.cache_enabled is False


def test_default_dataset_prefers_hlsl30_concept_id():
    class FakeCombo:
        def __init__(self):
            self.items = [
                {"short_name": "HLSL30", "concept_id": "C3322682485-LPCLOUD"},
                {"short_name": "HLSS30", "concept_id": "C2021957295-LPCLOUD"},
                {"short_name": "HLSL30", "concept_id": "C2021957657-LPCLOUD"},
            ]
            self.selected = None

        def count(self):
            return len(self.items)

        def itemData(self, index):
            return self.items[index]

        def setCurrentIndex(self, index):
            self.selected = index

    dock = type("Dock", (), {"dataset_combo": FakeCombo()})()

    EarthdataDockWidget._select_default_dataset(dock)

    assert dock.dataset_combo.selected == 2


def test_rgb_channel_guess_uses_common_band_tokens():
    links = [
        "https://example.test/HLS.L30.T10SEG.2025131T184540.v2.0.B02.tif",
        "https://example.test/HLS.L30.T10SEG.2025131T184540.v2.0.B04.tif",
        "https://example.test/HLS.L30.T10SEG.2025131T184540.v2.0.B03.tif",
    ]

    assert EarthdataDockWidget._guess_rgb_channel_indices(None, links) == (1, 2, 0)


def test_cog_links_sort_by_displayed_filename():
    links = [
        "https://example.test/path/HLS.B10.tif?token=z",
        "https://example.test/path/HLS.B02.tif",
        "https://example.test/path/HLS.B01.tif?token=a",
    ]

    assert EarthdataDockWidget._sort_cog_links(None, links) == [
        "https://example.test/path/HLS.B01.tif?token=a",
        "https://example.test/path/HLS.B02.tif",
        "https://example.test/path/HLS.B10.tif?token=z",
    ]


def test_index_vrt_worker_is_lazy_and_configured(tmp_path):
    output = tmp_path / "ndvi.vrt"
    worker = IndexVrtWorker(
        "https://example.test/HLS.B05.tif",
        "https://example.test/HLS.B04.tif",
        "ndvi",
        str(output),
    )

    assert worker.index_name == "ndvi"
    assert worker.output_path == str(output)
    assert worker._source_path("https://example.test/HLS.B05.tif").startswith(
        "/vsicurl/"
    )


def test_index_layer_visual_range_sets_normalized_bounds():
    class FakeRenderer:
        def __init__(self):
            self.minimum = None
            self.maximum = None

        def setClassificationMin(self, value):
            self.minimum = value

        def setClassificationMax(self, value):
            self.maximum = value

    class FakeLayer:
        def __init__(self):
            self.fake_renderer = FakeRenderer()

        def renderer(self):
            return self.fake_renderer

    dock = type("Dock", (), {"_log": lambda *args, **kwargs: None})()
    layer = FakeLayer()

    EarthdataDockWidget._set_index_layer_visual_range(dock, layer)

    assert layer.fake_renderer.minimum == -1.0
    assert layer.fake_renderer.maximum == 1.0


def test_compact_result_id_preserves_start_and_end():
    result_id = "OPERA_L3_DSWx-HLS_T10SEG_20250510T184540Z_20250512T010101Z_L8_30_v1.0"

    compacted = _compact_result_id(result_id, prefix_chars=20, suffix_chars=12)

    assert compacted == "OPERA_L3_DSWx-HLS_T1...Z_L8_30_v1.0"
