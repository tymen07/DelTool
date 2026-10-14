"""Open field boundaries in a locally installed QGIS.

DelTool runs on the user's own machine, so it can start QGIS directly. QGIS
gets a small startup script (--code) that adds Esri World Imagery as an XYZ
tile layer, puts the fields on top with the same cyan style as in the
browser (a .qml file next to the GeoPackage), zooms to them and saves the
whole thing as a .qgz project next to the fields so it can be reopened.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

FIELD_STYLE_QML = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28" styleCategories="Symbology">
  <renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0">
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0">
          <Option type="Map">
            <Option type="QString" name="color" value="0,166,214,36"/>
            <Option type="QString" name="outline_color" value="0,166,214,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="0.5"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="solid"/>
          </Option>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
</qgis>
"""


ESRI_XYZ = ("type=xyz&zmin=0&zmax=19&crs=EPSG3857&url=https://server.arcgisonline.com/ArcGIS/rest/"
            "services/World_Imagery/MapServer/tile/%7Bz%7D/%7By%7D/%7Bx%7D")

# Runs inside QGIS. Placeholders are filled with JSON string literals.
STARTUP_SCRIPT = """# Written by DelTool: field boundaries on top of Esri World Imagery.
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
                       QgsRasterLayer, QgsVectorLayer)
from qgis.utils import iface

project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
basemap = QgsRasterLayer(__BASEMAP__, "Esri World Imagery", "wms")
fields = QgsVectorLayer(__FIELDS__, __TITLE__, "ogr")
if basemap.isValid():
    project.addMapLayer(basemap)
if fields.isValid():
    fields.loadNamedStyle(__STYLE__)
    project.addMapLayer(fields)
    extent = QgsCoordinateTransform(fields.crs(), project.crs(), project).transformBoundingBox(fields.extent())
    extent.scale(1.1)
    if iface:
        iface.mapCanvas().setExtent(extent)
        iface.mapCanvas().refresh()
project.write(__PROJECT__)
"""


def _version_key(path):
    return [int(n) for n in re.findall(r"\d+", path)]


@lru_cache(maxsize=1)
def find_qgis():
    """Path of the QGIS executable, or None. DELTOOL_QGIS overrides the search."""
    override = os.environ.get("DELTOOL_QGIS")
    if override and Path(override).exists():
        return override
    for name in ("qgis", "qgis-ltr", "qgis-bin"):
        if path := shutil.which(name):
            return path
    patterns = []
    if sys.platform == "win32":
        program_files = {os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432")} - {None}
        for root in program_files:
            patterns += [f"{root}/QGIS*/bin/qgis-bin.exe", f"{root}/QGIS*/bin/qgis-ltr-bin.exe"]
        patterns += ["C:/OSGeo4W*/bin/qgis-bin.exe", "C:/OSGeo4W*/bin/qgis-ltr-bin.exe"]
    elif sys.platform == "darwin":
        patterns += ["/Applications/QGIS*.app/Contents/MacOS/QGIS"]
    hits = sorted({h for p in patterns for h in glob.glob(p)}, key=_version_key)
    return hits[-1] if hits else None


def info():
    path = find_qgis()
    return {"available": bool(path), "path": path}


def write_field_style(vector_path: Path):
    Path(vector_path).with_suffix(".qml").write_text(FIELD_STYLE_QML, encoding="utf-8")


def startup_script(fields_path: Path, title: str) -> Path:
    """Write the QGIS startup script (and field style) next to the fields."""
    fields_path = Path(fields_path)
    write_field_style(fields_path)
    values = {"__BASEMAP__": ESRI_XYZ, "__FIELDS__": str(fields_path), "__TITLE__": title,
              "__STYLE__": str(fields_path.with_suffix(".qml")),
              "__PROJECT__": str(fields_path.with_suffix(".qgz"))}
    code = STARTUP_SCRIPT
    for key, value in values.items():
        code = code.replace(key, json.dumps(value))
    script = fields_path.with_name(fields_path.stem + "_open.py")
    script.write_text(code, encoding="utf-8")
    return script


def open_fields(fields_path: Path, title: str):
    """Start QGIS with the fields on top of Esri World Imagery."""
    return _launch(["--code", str(startup_script(fields_path, title))], Path(fields_path).parent)


def _launch(args, cwd):
    exe = find_qgis()
    if not exe:
        raise RuntimeError("QGIS was not found. Install QGIS, or set the DELTOOL_QGIS "
                           "environment variable to the QGIS executable.")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, *args], cwd=str(cwd), creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return exe
