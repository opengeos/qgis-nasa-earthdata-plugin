"""
NASA Earthdata Plugin for QGIS

A plugin for searching, visualizing, and downloading NASA Earthdata products in QGIS.
Supports Cloud Optimized GeoTIFF (COG) visualization and data footprint display.
"""

from .nasa_earthdata import NASAEarthdata


def classFactory(iface):
    """Load NASAEarthdata class from file nasa_earthdata.

    Args:
        iface: A QGIS interface instance.

    Returns:
        NASAEarthdata: The plugin instance.
    """
    return NASAEarthdata(iface)
