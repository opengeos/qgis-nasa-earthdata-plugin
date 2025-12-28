# NASA Earthdata QGIS Plugin

A QGIS plugin for searching, visualizing, and downloading NASA Earthdata products. This plugin provides access to NASA's Earth science data catalog directly within QGIS, supporting Cloud Optimized GeoTIFF (COG) visualization and data footprint display.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![QGIS](https://img.shields.io/badge/QGIS-3.28+-green.svg)](https://qgis.org)

## Features

- **Search NASA Earthdata Catalog**: Browse and search thousands of NASA Earth science datasets using keywords, bounding boxes, and temporal filters
- **Visualize Data Footprints**: Display search result footprints directly on the QGIS map canvas
- **Cloud Optimized GeoTIFF (COG) Support**: Stream and visualize COG files directly without downloading
- **Data Download**: Download granules for local processing and visualization
- **Earthdata Login Integration**: Seamless authentication with NASA Earthdata Login credentials
- **Settings Panel**: Configure credentials, download preferences, and plugin options

## Screenshots

![](https://github.com/user-attachments/assets/6f243a41-cf97-4943-9918-33ad4886280b)

## Installation

### Prerequisites

1. **QGIS 3.28 or higher**
2. **NASA Earthdata Account**: Sign up at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov/)

### Install QGIS and Google Earth Engine

#### 1) Install Pixi

#### Linux/macOS (bash/zsh)

```bash
curl -fsSL https://pixi.sh/install.sh | sh
```

Close and re-open your terminal (or reload your shell) so `pixi` is on your `PATH`. Then confirm:

```bash
pixi --version
```

#### Windows (PowerShell)

Open **PowerShell** (preferably as a normal user, Admin not required), then run:

```powershell
powershell -ExecutionPolicy Bypass -c "irm -useb https://pixi.sh/install.ps1 | iex"
```

Close and re-open PowerShell, then confirm:

```powershell
pixi --version
```

---

#### 2) Create a Pixi project

Navigate to a directory where you want to create the project and run:

```bash
pixi init geo
cd geo
```

#### 3) Install the environment

From the `geo` folder:

```bash
pixi add qgis earthaccess geopandas
```

### Installing the Plugin

#### Method 1: From QGIS Plugin Manager (Recommended)

1. Open QGIS using `pixi run qgis`
2. Go to **Plugins** → **Manage and Install Plugins...**
3. Go to the **Settings** tab
4. Click **Add...** under "Plugin Repositories"
5. Give a name for the repository, e.g., "OpenGeos"
6. Enter the URL of the repository: <https://qgis.gishub.org/plugins.xml>
7. Click **OK**
8. Go to the **All** tab
9. Search for "NASA Earthdata"
10. Select "NASA Earthdata" from the list and click **Install Plugin**

#### Method 2: From ZIP File

1. Download the latest release ZIP from <https://qgis.gishub.org>
2. In QGIS, go to `Plugins` → `Manage and Install Plugins`
3. Click `Install from ZIP` and select the downloaded file
4. Enable the plugin in the `Installed` tab

#### Method 3: Manual Installation

1. Clone or download this repository
2. Copy the `nasa_earthdata` folder to your QGIS plugins directory:
   - **Linux**: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - **Windows**: `C:\Users\<username>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\`
   - **macOS**: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
3. Restart QGIS and enable the plugin

### Uninstalling

```bash
python install.py --remove
# or
./install.sh --remove
```

## Usage

### Authentication

Before using the plugin, you need to configure your NASA Earthdata credentials:

1. Click on **NASA Earthdata** → **Settings** in the menu
2. Enter your Earthdata username and password
3. Click **Test Credentials** to verify
4. Click **Save Settings**

Alternatively, you can configure credentials via:
- Environment variables: `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`
- `.netrc` file with entry for `urs.earthdata.nasa.gov`

### Searching Data

1. Click the **NASA Earthdata Search** button in the toolbar
2. Filter datasets by keyword (optional)
3. Select a dataset from the dropdown
4. Set the bounding box (or use current map extent)
5. Set the date range
6. Click **Search**

   ![](https://github.com/user-attachments/assets/5f41e258-662f-4441-84e5-d752971de573)

### Visualizing Data

- **Footprints**: Search results are automatically displayed as footprints on the map
- **COG Layers**: Select results and click **Display COG** to stream Cloud Optimized GeoTIFFs
- **Downloaded Data**: After downloading, you can add raster files directly to the map

### Downloading Data

1. Select the granules you want to download from the results table
2. Click **Download**
3. Choose a destination folder
4. Wait for the download to complete
5. Optionally add downloaded files to the map

## Supported Datasets

The plugin provides access to thousands of NASA datasets including:

- **GEDI**: Global Ecosystem Dynamics Investigation (forest structure, biomass)
- **MODIS**: Moderate Resolution Imaging Spectroradiometer
- **Landsat**: Landsat 8 and 9 imagery
- **VIIRS**: Visible Infrared Imaging Radiometer Suite
- **SMAP**: Soil Moisture Active Passive
- **ICESat-2**: Ice, Cloud, and land Elevation Satellite
- **OPERA**: Observational Products for End-Users from Remote Sensing Analysis
- And many more...

## Development

### Project Structure

```
qgis-nasa-earthdata-plugin/
├── nasa_earthdata/          # Plugin source code
│   ├── __init__.py
│   ├── nasa_earthdata.py    # Main plugin class
│   ├── metadata.txt         # Plugin metadata
│   ├── LICENSE
│   ├── dialogs/             # UI components
│   │   ├── __init__.py
│   │   ├── earthdata_dock.py    # Main search panel
│   │   ├── settings_dock.py     # Settings panel
│   │   └── update_checker.py    # Update checker dialog
│   └── icons/               # Plugin icons
│       ├── icon.svg
│       ├── settings.svg
│       ├── about.svg
│       ├── search.svg
│       └── download.svg
├── install.py               # Python installation script
├── install.sh               # Bash installation script
├── package_plugin.py        # Python packaging script
├── package_plugin.sh        # Bash packaging script
├── README.md
└── LICENSE
```

### Building a Release

To create a ZIP file for distribution:

```bash
python package_plugin.py
```

Or:

```bash
./package_plugin.sh
```

### Uninstalling

To remove the plugin:

```bash
./install.sh --remove
```

Or:

```bash
python install.py --remove
```

## Troubleshooting

### Authentication Issues

If you encounter authentication errors:

1. Verify your credentials at https://urs.earthdata.nasa.gov/
2. Check that you've accepted the EULA for the datasets you're accessing
3. Try running `earthaccess.login()` in the QGIS Python console

### Missing Dependencies

If the plugin fails to load due to missing packages:

```bash
# Using pip
pip install earthaccess geopandas

# Or using the QGIS Python environment
# On Linux:
~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/nasa_earthdata/
python3 -m pip install earthaccess geopandas
```

### COG Visualization Issues

- Ensure GDAL has network support enabled
- Check that the data URL is accessible
- Some datasets may require authentication for streaming

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [NASA Earthdata](https://earthdata.nasa.gov/) for providing access to Earth science data
- [earthaccess](https://github.com/nsidc/earthaccess) library for NASA Earthdata API access
- [leafmap](https://github.com/opengeos/leafmap) for inspiration on the GUI design
- [QGIS](https://qgis.org/) for the amazing open-source GIS platform

## Links

- [GitHub Repository](https://github.com/opengeos/qgis-nasa-earthdata-plugin)
- [Issue Tracker](https://github.com/opengeos/qgis-nasa-earthdata-plugin/issues)
- [NASA Earthdata](https://earthdata.nasa.gov/)
- [NASA Earthdata Login](https://urs.earthdata.nasa.gov/)
