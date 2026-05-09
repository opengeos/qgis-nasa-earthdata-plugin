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
    OUTPUT = "OUTPUT"
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
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Result footprints GeoJSON",
                fileFilter="GeoJSON files (*.geojson)",
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

        bbox = None
        if bbox_text:
            parts = [float(item.strip()) for item in bbox_text.split(",")]
            if len(parts) != 4:
                raise QgsProcessingException("Bounding box must have 4 values")
            bbox = tuple(parts)
        temporal = (start_date, end_date) if start_date and end_date else None

        worker = DataSearchWorker(short_name, concept_id, bbox, temporal, max_items)
        kwargs = worker._build_search_kwargs()
        if feedback:
            feedback.pushInfo(f"Searching NASA Earthdata with {kwargs}")

        from nasa_earthdata.core.venv_manager import import_earthaccess
        from nasa_earthdata.core.workflows import (
            granules_to_export_rows,
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
        return {self.OUTPUT: output, self.COUNT: len(granules)}


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
