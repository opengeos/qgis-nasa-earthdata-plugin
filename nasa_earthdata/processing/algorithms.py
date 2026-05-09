"""Processing algorithms for NASA Earthdata workflows.

The classes in this module are intentionally conservative wrappers around the
same earthaccess/GDAL behavior used by the dock. They also import cleanly in
the repository's PyQt smoke-test stubs, where real QGIS Processing base classes
are not available.
"""

import os

from qgis.PyQt.QtCore import QCoreApplication

try:
    from qgis.core import (
        QgsProcessing,
        QgsProcessingAlgorithm,
        QgsProcessingException,
        QgsProcessingOutputNumber,
        QgsProcessingParameterBoolean,
        QgsProcessingParameterEnum,
        QgsProcessingParameterExtent,
        QgsProcessingParameterFile,
        QgsProcessingParameterFileDestination,
        QgsProcessingParameterFolderDestination,
        QgsProcessingParameterNumber,
        QgsProcessingParameterString,
    )
except Exception:  # pragma: no cover - exercised by import smoke only
    QgsProcessing = None
    QgsProcessingAlgorithm = object
    QgsProcessingException = RuntimeError
    QgsProcessingOutputNumber = None
    QgsProcessingParameterBoolean = None
    QgsProcessingParameterEnum = None
    QgsProcessingParameterExtent = None
    QgsProcessingParameterFile = None
    QgsProcessingParameterFileDestination = None
    QgsProcessingParameterFolderDestination = None
    QgsProcessingParameterNumber = None
    QgsProcessingParameterString = None


def _is_real_processing_base():
    return isinstance(QgsProcessingAlgorithm, type)


if not _is_real_processing_base():
    QgsProcessingAlgorithm = object


class _BaseAlgorithm(QgsProcessingAlgorithm):
    """Common helpers for Processing algorithms."""

    def tr(self, string):
        return QCoreApplication.translate("NASAEarthdataProcessing", string)

    def group(self):
        return self.tr("Tools")

    def groupId(self):
        return "tools"

    def shortHelpString(self):
        return self.tr("Runs a NASA Earthdata workflow from QGIS Processing.")

    def _add_parameter(self, parameter):
        if parameter is not None:
            self.addParameter(parameter)

    def _add_output(self, output):
        if output is not None and hasattr(self, "addOutput"):
            self.addOutput(output)

    def _parameter_as_string(self, parameters, name, context):
        if hasattr(self, "parameterAsString"):
            return self.parameterAsString(parameters, name, context)
        return parameters.get(name, "")

    def _parameter_as_int(self, parameters, name, context):
        if hasattr(self, "parameterAsInt"):
            return self.parameterAsInt(parameters, name, context)
        return int(parameters.get(name, 0) or 0)

    def _parameter_as_bool(self, parameters, name, context):
        if hasattr(self, "parameterAsBool"):
            return self.parameterAsBool(parameters, name, context)
        return bool(parameters.get(name, False))

    def _parameter_as_file_output(self, parameters, name, context):
        if hasattr(self, "parameterAsFileOutput"):
            return self.parameterAsFileOutput(parameters, name, context)
        return parameters.get(name, "")


class SearchEarthdataAlgorithm(_BaseAlgorithm):
    SHORT_NAME = "SHORT_NAME"
    CONCEPT_ID = "CONCEPT_ID"
    BBOX = "BBOX"
    START_DATE = "START_DATE"
    END_DATE = "END_DATE"
    MAX_ITEMS = "MAX_ITEMS"
    CLOUD_MIN = "CLOUD_MIN"
    CLOUD_MAX = "CLOUD_MAX"
    DAY_NIGHT = "DAY_NIGHT"
    PROVIDER = "PROVIDER"
    VERSION = "VERSION"
    GRANULE_ID = "GRANULE_ID"
    ORBIT_MIN = "ORBIT_MIN"
    ORBIT_MAX = "ORBIT_MAX"
    OUTPUT = "OUTPUT"
    OUTPUT_JSON = "OUTPUT_JSON"
    COUNT = "COUNT"

    def name(self):
        return "search_nasa_earthdata"

    def displayName(self):
        return self.tr("Search NASA Earthdata")

    def createInstance(self):
        return SearchEarthdataAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(QgsProcessingParameterString(self.SHORT_NAME, "Short name"))
        self._add_parameter(
            QgsProcessingParameterString(self.CONCEPT_ID, "Concept ID", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterString(
                self.BBOX, "Bounding box xmin,ymin,xmax,ymax", optional=True
            )
        )
        self._add_parameter(QgsProcessingParameterString(self.START_DATE, "Start date"))
        self._add_parameter(QgsProcessingParameterString(self.END_DATE, "End date"))
        self._add_parameter(
            QgsProcessingParameterNumber(
                self.MAX_ITEMS,
                "Maximum items",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=50,
                minValue=1,
            )
        )
        self._add_parameter(
            QgsProcessingParameterNumber(
                self.CLOUD_MIN,
                "Minimum cloud cover",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
                maxValue=100,
            )
        )
        self._add_parameter(
            QgsProcessingParameterNumber(
                self.CLOUD_MAX,
                "Maximum cloud cover",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=100,
                minValue=0,
                maxValue=100,
            )
        )
        self._add_parameter(
            QgsProcessingParameterEnum(
                self.DAY_NIGHT,
                "Day/night",
                options=["Any", "Day only", "Night only", "Both/unspecified"],
                defaultValue=0,
            )
        )
        self._add_parameter(
            QgsProcessingParameterString(self.PROVIDER, "Provider", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterString(self.VERSION, "Version", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterString(
                self.GRANULE_ID, "Granule ID pattern", optional=True
            )
        )
        self._add_parameter(
            QgsProcessingParameterNumber(
                self.ORBIT_MIN,
                "Minimum orbit number",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )
        self._add_parameter(
            QgsProcessingParameterNumber(
                self.ORBIT_MAX,
                "Maximum orbit number",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Result footprints GeoJSON",
                fileFilter="GeoJSON files (*.geojson)",
                optional=True,
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_JSON,
                "Raw granules JSON",
                fileFilter="JSON files (*.json)",
                optional=True,
            )
        )
        if QgsProcessingOutputNumber is not None:
            self._add_output(
                QgsProcessingOutputNumber(self.COUNT, "Number of granules")
            )

    def processAlgorithm(self, parameters, context, feedback):
        from nasa_earthdata.dialogs.earthdata_dock import DataSearchWorker

        short_name = self._parameter_as_string(parameters, self.SHORT_NAME, context)
        concept_id = self._parameter_as_string(parameters, self.CONCEPT_ID, context)
        bbox_text = self._parameter_as_string(parameters, self.BBOX, context)
        start_date = self._parameter_as_string(parameters, self.START_DATE, context)
        end_date = self._parameter_as_string(parameters, self.END_DATE, context)
        max_items = self._parameter_as_int(parameters, self.MAX_ITEMS, context)
        cloud_min = self._parameter_as_int(parameters, self.CLOUD_MIN, context)
        cloud_max = self._parameter_as_int(parameters, self.CLOUD_MAX, context)
        day_night_index = self._parameter_as_int(parameters, self.DAY_NIGHT, context)
        provider = self._parameter_as_string(parameters, self.PROVIDER, context)
        version = self._parameter_as_string(parameters, self.VERSION, context)
        granule_id = self._parameter_as_string(parameters, self.GRANULE_ID, context)
        orbit_min = self._parameter_as_int(parameters, self.ORBIT_MIN, context)
        orbit_max = self._parameter_as_int(parameters, self.ORBIT_MAX, context)

        bbox = None
        if bbox_text:
            parts = [float(item.strip()) for item in bbox_text.split(",")]
            if len(parts) != 4:
                raise QgsProcessingException("Bounding box must have 4 values")
            bbox = tuple(parts)
        temporal = (start_date, end_date) if start_date and end_date else None

        day_night = [None, "day", "night", "unspecified"][day_night_index]
        orbit_number = None
        if orbit_min or orbit_max:
            orbit_number = (
                (orbit_min, orbit_max)
                if orbit_min and orbit_max
                else orbit_min or orbit_max
            )

        worker = DataSearchWorker(
            short_name,
            concept_id,
            bbox,
            temporal,
            max_items,
            cloud_cover=(
                (cloud_min, cloud_max) if cloud_min > 0 or cloud_max < 100 else None
            ),
            day_night=day_night,
            provider=provider or None,
            version=version or None,
            granule_id=granule_id or None,
            orbit_number=orbit_number,
        )
        kwargs = worker._build_search_kwargs()
        if feedback:
            feedback.pushInfo(f"Searching NASA Earthdata with {kwargs}")

        from nasa_earthdata.core.venv_manager import import_earthaccess
        from nasa_earthdata.core.workflows import (
            granules_to_export_rows,
            write_granules_json,
            write_results_geojson,
        )

        earthaccess = import_earthaccess()
        granules = earthaccess.search_data(count=max_items, **kwargs)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        if output:
            rows = granules_to_export_rows(
                granules,
                {"short_name": short_name, "concept_id": concept_id},
            )
            write_results_geojson(output, rows, None)
        output_json = self._parameter_as_file_output(
            parameters, self.OUTPUT_JSON, context
        )
        if output_json:
            write_granules_json(output_json, granules)
        return {
            self.OUTPUT: output,
            self.OUTPUT_JSON: output_json,
            self.COUNT: len(granules),
        }


class DownloadGranulesAlgorithm(_BaseAlgorithm):
    GRANULES_JSON = "GRANULES_JSON"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    SKIP_EXISTING = "SKIP_EXISTING"

    def name(self):
        return "download_nasa_earthdata_granules"

    def displayName(self):
        return self.tr("Download NASA Earthdata Granules")

    def createInstance(self):
        return DownloadGranulesAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterFile(
                self.GRANULES_JSON,
                "Granules JSON exported from earthaccess",
                behavior=QgsProcessingParameterFile.File,
                extension="json",
            )
        )
        self._add_parameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER, "Download folder"
            )
        )
        self._add_parameter(
            QgsProcessingParameterBoolean(
                self.SKIP_EXISTING, "Skip existing files", defaultValue=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import json

        from nasa_earthdata.core.venv_manager import import_earthaccess
        from nasa_earthdata.core.workflows import likely_existing_download_files

        granules_json = self._parameter_as_string(
            parameters, self.GRANULES_JSON, context
        )
        output_folder = self._parameter_as_string(
            parameters, self.OUTPUT_FOLDER, context
        )
        skip_existing = self._parameter_as_bool(parameters, self.SKIP_EXISTING, context)
        with open(granules_json, "r", encoding="utf-8") as f:
            granules = json.load(f)

        skipped_files = []
        if skip_existing:
            pending = []
            for granule in granules:
                existing = likely_existing_download_files(granule, output_folder)
                if existing:
                    skipped_files.extend(existing)
                else:
                    pending.append(granule)
            if feedback and skipped_files:
                feedback.pushInfo(
                    f"Skipping {len(granules) - len(pending)} granule(s) with "
                    f"existing files in {output_folder}"
                )
            granules = pending

        files = []
        if granules:
            earthaccess = import_earthaccess()
            files = earthaccess.download(granules, local_path=output_folder) or []
        return {
            "FILES": [str(path) for path in files] + skipped_files,
            self.OUTPUT_FOLDER: output_folder,
        }


class AddFootprintsAlgorithm(_BaseAlgorithm):
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"

    def name(self):
        return "add_nasa_earthdata_footprints"

    def displayName(self):
        return self.tr("Add Earthdata Footprints")

    def createInstance(self):
        return AddFootprintsAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterFile(
                self.INPUT,
                "Earthdata results GeoJSON",
                behavior=QgsProcessingParameterFile.File,
                extension="geojson",
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Footprints GeoJSON",
                fileFilter="GeoJSON files (*.geojson)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self._parameter_as_string(parameters, self.INPUT, context)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        if output and source != output:
            import shutil

            shutil.copyfile(source, output)
        return {self.OUTPUT: output or source}


class ExportStacAlgorithm(_BaseAlgorithm):
    GRANULES_JSON = "GRANULES_JSON"
    SHORT_NAME = "SHORT_NAME"
    CONCEPT_ID = "CONCEPT_ID"
    OUTPUT = "OUTPUT"

    def name(self):
        return "export_nasa_earthdata_stac"

    def displayName(self):
        return self.tr("Export NASA Earthdata STAC")

    def createInstance(self):
        return ExportStacAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterFile(
                self.GRANULES_JSON,
                "Granules JSON exported from earthaccess",
                behavior=QgsProcessingParameterFile.File,
                extension="json",
            )
        )
        self._add_parameter(
            QgsProcessingParameterString(self.SHORT_NAME, "Short name", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterString(self.CONCEPT_ID, "Concept ID", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "STAC ItemCollection JSON",
                fileFilter="JSON files (*.json)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import json

        from nasa_earthdata.core.workflows import write_results_stac

        granules_json = self._parameter_as_string(
            parameters, self.GRANULES_JSON, context
        )
        short_name = self._parameter_as_string(parameters, self.SHORT_NAME, context)
        concept_id = self._parameter_as_string(parameters, self.CONCEPT_ID, context)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        with open(granules_json, "r", encoding="utf-8") as f:
            granules = json.load(f)
        write_results_stac(
            output,
            granules,
            {"short_name": short_name, "concept_id": concept_id},
        )
        if feedback:
            feedback.pushInfo(f"Wrote STAC ItemCollection: {output}")
        return {self.OUTPUT: output}


class ExportWorkflowBundleAlgorithm(_BaseAlgorithm):
    GRANULES_JSON = "GRANULES_JSON"
    SEARCH_JSON = "SEARCH_JSON"
    MANIFEST = "MANIFEST"
    OUTPUT = "OUTPUT"

    def name(self):
        return "export_nasa_earthdata_workflow_bundle"

    def displayName(self):
        return self.tr("Export NASA Earthdata Workflow Bundle")

    def createInstance(self):
        return ExportWorkflowBundleAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterFile(
                self.GRANULES_JSON,
                "Granules JSON exported from earthaccess",
                behavior=QgsProcessingParameterFile.File,
                extension="json",
            )
        )
        self._add_parameter(
            QgsProcessingParameterFile(
                self.SEARCH_JSON,
                "Optional search preset/workflow JSON",
                behavior=QgsProcessingParameterFile.File,
                extension="json",
                optional=True,
            )
        )
        self._add_parameter(
            QgsProcessingParameterString(
                self.MANIFEST, "Optional download manifest path", optional=True
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Workflow bundle JSON",
                fileFilter="JSON files (*.json)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import json

        from nasa_earthdata.core.workflows import (
            granules_to_export_rows,
            granules_to_stac_item_collection,
            write_workflow_bundle,
        )

        granules_json = self._parameter_as_string(
            parameters, self.GRANULES_JSON, context
        )
        search_json = self._parameter_as_string(parameters, self.SEARCH_JSON, context)
        manifest = self._parameter_as_string(parameters, self.MANIFEST, context)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        with open(granules_json, "r", encoding="utf-8") as f:
            granules = json.load(f)

        search = {}
        if search_json:
            with open(search_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
            search = payload.get("search", payload) if isinstance(payload, dict) else {}

        dataset = search.get("dataset", {}) if isinstance(search, dict) else {}
        rows = granules_to_export_rows(granules, dataset)
        stac = granules_to_stac_item_collection(granules, dataset)
        write_workflow_bundle(
            output,
            search,
            granules,
            rows,
            stac_item_collection=stac,
            manifest=manifest,
        )
        if feedback:
            feedback.pushInfo(f"Wrote workflow bundle: {output}")
        return {self.OUTPUT: output}


class CollectionInfoAlgorithm(_BaseAlgorithm):
    SHORT_NAME = "SHORT_NAME"
    CONCEPT_ID = "CONCEPT_ID"
    OUTPUT = "OUTPUT"

    def name(self):
        return "nasa_earthdata_collection_info"

    def displayName(self):
        return self.tr("NASA Earthdata Collection Info")

    def createInstance(self):
        return CollectionInfoAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterString(self.SHORT_NAME, "Short name", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterString(self.CONCEPT_ID, "Concept ID", optional=True)
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Collection metadata summary JSON",
                fileFilter="JSON files (*.json)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import json
        from pathlib import Path

        from nasa_earthdata.core.net import https_only_urlopen
        from nasa_earthdata.core.workflows import (
            cmr_collection_summary,
            cmr_collection_url,
        )

        short_name = self._parameter_as_string(parameters, self.SHORT_NAME, context)
        concept_id = self._parameter_as_string(parameters, self.CONCEPT_ID, context)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        url = cmr_collection_url({"short_name": short_name, "concept_id": concept_id})
        if not url:
            raise QgsProcessingException("Short name or concept ID is required")
        with https_only_urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        summary = cmr_collection_summary(payload)
        if not summary:
            raise QgsProcessingException("No CMR collection metadata found")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
        if feedback:
            feedback.pushInfo(f"Wrote collection metadata summary: {output}")
        return {self.OUTPUT: output}


class CheckNewGranulesAlgorithm(SearchEarthdataAlgorithm):
    BASELINE_JSON = "BASELINE_JSON"
    NEW_GRANULES_JSON = "NEW_GRANULES_JSON"
    NEW_COUNT = "NEW_COUNT"

    def name(self):
        return "check_new_nasa_earthdata_granules"

    def displayName(self):
        return self.tr("Check New NASA Earthdata Granules")

    def createInstance(self):
        return CheckNewGranulesAlgorithm()

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self._add_parameter(
            QgsProcessingParameterFile(
                self.BASELINE_JSON,
                "Baseline granules JSON",
                behavior=QgsProcessingParameterFile.File,
                extension="json",
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.NEW_GRANULES_JSON,
                "New granules JSON",
                fileFilter="JSON files (*.json)",
                optional=True,
            )
        )
        if QgsProcessingOutputNumber is not None:
            self._add_output(
                QgsProcessingOutputNumber(self.NEW_COUNT, "Number of new granules")
            )

    def processAlgorithm(self, parameters, context, feedback):
        import json

        from nasa_earthdata.core.venv_manager import import_earthaccess
        from nasa_earthdata.core.workflows import (
            granule_native_id,
            granules_to_export_rows,
            write_granules_json,
            write_results_geojson,
        )

        short_name = self._parameter_as_string(parameters, self.SHORT_NAME, context)
        concept_id = self._parameter_as_string(parameters, self.CONCEPT_ID, context)
        bbox_text = self._parameter_as_string(parameters, self.BBOX, context)
        start_date = self._parameter_as_string(parameters, self.START_DATE, context)
        end_date = self._parameter_as_string(parameters, self.END_DATE, context)
        max_items = self._parameter_as_int(parameters, self.MAX_ITEMS, context)
        cloud_min = self._parameter_as_int(parameters, self.CLOUD_MIN, context)
        cloud_max = self._parameter_as_int(parameters, self.CLOUD_MAX, context)
        day_night_index = self._parameter_as_int(parameters, self.DAY_NIGHT, context)
        provider = self._parameter_as_string(parameters, self.PROVIDER, context)
        version = self._parameter_as_string(parameters, self.VERSION, context)
        granule_id = self._parameter_as_string(parameters, self.GRANULE_ID, context)
        orbit_min = self._parameter_as_int(parameters, self.ORBIT_MIN, context)
        orbit_max = self._parameter_as_int(parameters, self.ORBIT_MAX, context)
        baseline_json = self._parameter_as_string(
            parameters, self.BASELINE_JSON, context
        )

        bbox = None
        if bbox_text:
            parts = [float(item.strip()) for item in bbox_text.split(",")]
            if len(parts) != 4:
                raise QgsProcessingException("Bounding box must have 4 values")
            bbox = tuple(parts)

        if not concept_id and not short_name:
            raise QgsProcessingException("Short name or concept ID is required")
        kwargs = (
            {"concept_id": concept_id} if concept_id else {"short_name": short_name}
        )
        if bbox is not None:
            kwargs["bounding_box"] = bbox
        if start_date and end_date:
            kwargs["temporal"] = (start_date, end_date)
        if cloud_min > 0 or cloud_max < 100:
            kwargs["cloud_cover"] = (cloud_min, cloud_max)
        day_night = [None, "day", "night", "unspecified"][day_night_index]
        if day_night is not None:
            kwargs["day_night_flag"] = day_night
        if provider:
            kwargs["provider"] = provider
        if version:
            kwargs["version"] = version
        if granule_id:
            kwargs["granule_ur"] = granule_id
        if orbit_min or orbit_max:
            kwargs["orbit_number"] = (
                (orbit_min, orbit_max)
                if orbit_min and orbit_max
                else orbit_min or orbit_max
            )

        with open(baseline_json, "r", encoding="utf-8") as f:
            baseline = json.load(f)
        baseline_ids = {
            granule_native_id(granule, f"Item {index + 1}")
            for index, granule in enumerate(baseline or [])
        }

        earthaccess = import_earthaccess()
        granules = earthaccess.search_data(count=max_items, **kwargs)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        if output:
            rows = granules_to_export_rows(
                granules,
                {"short_name": short_name, "concept_id": concept_id},
            )
            write_results_geojson(output, rows, None)
        output_all_json = self._parameter_as_file_output(
            parameters, self.OUTPUT_JSON, context
        )
        if output_all_json:
            write_granules_json(output_all_json, granules)

        new_granules = []
        for index, granule in enumerate(granules or []):
            native_id = granule_native_id(granule, f"Item {index + 1}")
            if native_id not in baseline_ids:
                new_granules.append(granule)

        output_json = self._parameter_as_file_output(
            parameters, self.NEW_GRANULES_JSON, context
        )
        if output_json:
            write_granules_json(output_json, new_granules)
        if feedback:
            feedback.pushInfo(
                f"Found {len(new_granules)} new granule(s) out of {len(granules or [])}"
            )
        return {
            self.OUTPUT: output,
            self.OUTPUT_JSON: output_all_json,
            self.NEW_GRANULES_JSON: output_json,
            self.NEW_COUNT: len(new_granules),
            self.COUNT: len(granules or []),
        }


class CreateRgbCogLayerAlgorithm(_BaseAlgorithm):
    RED = "RED"
    GREEN = "GREEN"
    BLUE = "BLUE"
    OUTPUT = "OUTPUT"

    def name(self):
        return "create_rgb_cog_layer"

    def displayName(self):
        return self.tr("Create RGB COG Layer")

    def createInstance(self):
        return CreateRgbCogLayerAlgorithm()

    def initAlgorithm(self, config=None):
        for name, label in (
            (self.RED, "Red COG URL/path"),
            (self.GREEN, "Green COG URL/path"),
            (self.BLUE, "Blue COG URL/path"),
        ):
            self._add_parameter(QgsProcessingParameterString(name, label))
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT, "RGB VRT", fileFilter="VRT files (*.vrt)"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        from osgeo import gdal

        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        sources = [
            self._parameter_as_string(parameters, self.RED, context),
            self._parameter_as_string(parameters, self.GREEN, context),
            self._parameter_as_string(parameters, self.BLUE, context),
        ]
        vrt_sources = [
            f"/vsicurl/{source}" if source.lower().startswith("http") else source
            for source in sources
        ]
        vrt = gdal.BuildVRT(
            output,
            vrt_sources,
            options=gdal.BuildVRTOptions(separate=True),
        )
        if vrt is None:
            raise QgsProcessingException("Could not build RGB VRT")
        for band_index, color_interp in enumerate(
            (gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand),
            start=1,
        ):
            band = vrt.GetRasterBand(band_index)
            if band is not None:
                band.SetColorInterpretation(color_interp)
        vrt.FlushCache()
        vrt = None
        if not os.path.exists(output):
            raise QgsProcessingException("RGB VRT was not written")
        return {self.OUTPUT: output}


class CreateNormalizedDifferenceVrtAlgorithm(_BaseAlgorithm):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    INDEX_NAME = "INDEX_NAME"
    OUTPUT = "OUTPUT"

    def name(self):
        return "create_normalized_difference_vrt"

    def displayName(self):
        return self.tr("Create Normalized Difference VRT")

    def createInstance(self):
        return CreateNormalizedDifferenceVrtAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_parameter(
            QgsProcessingParameterString(
                self.POSITIVE, "Positive band COG URL/path (e.g. NIR or Green)"
            )
        )
        self._add_parameter(
            QgsProcessingParameterString(
                self.NEGATIVE, "Negative band COG URL/path (e.g. Red, NIR, or SWIR)"
            )
        )
        self._add_parameter(
            QgsProcessingParameterEnum(
                self.INDEX_NAME,
                "Index",
                options=["NDVI", "NDWI", "MNDWI", "NDMI", "NBR", "Custom"],
                defaultValue=0,
            )
        )
        self._add_parameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT, "Normalized difference VRT", fileFilter="VRT files (*.vrt)"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        from osgeo import gdal

        positive = self._parameter_as_string(parameters, self.POSITIVE, context)
        negative = self._parameter_as_string(parameters, self.NEGATIVE, context)
        output = self._parameter_as_file_output(parameters, self.OUTPUT, context)
        index_options = ["NDVI", "NDWI", "MNDWI", "NDMI", "NBR", "Custom"]
        index_name = index_options[
            self._parameter_as_int(parameters, self.INDEX_NAME, context)
        ]

        if positive == negative:
            raise QgsProcessingException("Positive and negative bands must differ")

        def source_path(value):
            return f"/vsicurl/{value}" if value.lower().startswith("http") else value

        positive_path = source_path(positive)
        negative_path = source_path(negative)
        config_overrides = {
            "GDAL_VRT_ENABLE_PYTHON": "YES",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": "tif,tiff,TIF,TIFF",
        }
        previous_options = {key: gdal.GetConfigOption(key) for key in config_overrides}
        for key, value in config_overrides.items():
            gdal.SetConfigOption(key, value)
        try:
            source_ds = gdal.Open(positive_path)
            if source_ds is None:
                raise QgsProcessingException("Could not open positive band source")
            width = source_ds.RasterXSize
            height = source_ds.RasterYSize
            projection = source_ds.GetProjectionRef() or ""
            geotransform = source_ds.GetGeoTransform(can_return_null=True)
            source_ds = None

            negative_ds = gdal.Open(negative_path)
            if negative_ds is None:
                raise QgsProcessingException("Could not open negative band source")
            if (
                negative_ds.RasterXSize != width
                or negative_ds.RasterYSize != height
                or (negative_ds.GetProjectionRef() or "") != projection
                or negative_ds.GetGeoTransform(can_return_null=True) != geotransform
            ):
                negative_ds = None
                raise QgsProcessingException(
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

        def esc(text):
            return (
                str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
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
            lines.append(f"  <SRS>{esc(projection)}</SRS>")
        if geotransform_text:
            lines.append(f"  <GeoTransform>{geotransform_text}</GeoTransform>")
        lines.extend(
            [
                '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTDerivedRasterBand">',
                f"    <Description>{esc(index_name)}</Description>",
                "    <PixelFunctionType>normalized_difference</PixelFunctionType>",
                "    <PixelFunctionLanguage>Python</PixelFunctionLanguage>",
                f"    <PixelFunctionCode><![CDATA[{code}]]></PixelFunctionCode>",
            ]
        )
        for path in (positive_path, negative_path):
            lines.extend(
                [
                    "    <SimpleSource>",
                    f'      <SourceFilename relativeToVRT="0">{esc(path)}</SourceFilename>',
                    "      <SourceBand>1</SourceBand>",
                    f'      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>',
                    f'      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>',
                    "    </SimpleSource>",
                ]
            )
        lines.extend(["  </VRTRasterBand>", "</VRTDataset>"])
        with open(output, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        if feedback:
            feedback.pushInfo(f"Wrote {index_name} VRT: {output}")
        if not os.path.exists(output):
            raise QgsProcessingException("Normalized difference VRT was not written")
        return {self.OUTPUT: output}
