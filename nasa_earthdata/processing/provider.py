"""QGIS Processing provider for NASA Earthdata."""

from qgis.PyQt.QtGui import QIcon

try:
    from qgis.core import QgsProcessingProvider
except Exception:  # pragma: no cover - import smoke fallback
    QgsProcessingProvider = object

if not isinstance(QgsProcessingProvider, type):
    QgsProcessingProvider = object

from .algorithms import (
    AddFootprintsAlgorithm,
    CreateRgbCogLayerAlgorithm,
    DownloadGranulesAlgorithm,
    SearchEarthdataAlgorithm,
)


class NASAEarthdataProcessingProvider(QgsProcessingProvider):
    """Expose NASA Earthdata workflows in QGIS Processing."""

    def __init__(self):
        super().__init__()
        self._fallback_algorithms = []

    def id(self):
        return "nasa_earthdata"

    def name(self):
        return "NASA Earthdata"

    def longName(self):
        return "NASA Earthdata"

    def icon(self):
        return QIcon()

    def loadAlgorithms(self):
        for algorithm in (
            SearchEarthdataAlgorithm(),
            DownloadGranulesAlgorithm(),
            AddFootprintsAlgorithm(),
            CreateRgbCogLayerAlgorithm(),
        ):
            if hasattr(self, "addAlgorithm"):
                self.addAlgorithm(algorithm)
            else:
                self._fallback_algorithms.append(algorithm)
