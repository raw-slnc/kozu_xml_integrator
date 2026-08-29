# -*- coding: utf-8 -*-
"""
Import Worker for KozuXmlIntegrator

Background worker that runs the XML/ZIP import process off the UI thread.
"""

from pathlib import Path
from typing import Optional, List, Dict, Tuple
import logging
import tempfile
import zipfile

from qgis.PyQt.QtCore import pyqtSignal, QObject
from qgis.core import (
    QgsVectorLayer,
    QgsPointXY,
    QgsGeometry,
)

from ..core import (
    DatabaseManager,
    XmlImporter,
    ImportProgress,
    ImportResult,
    SearchIndex,
    safe_extract_zip,
    UnsafeZipError,
)

logger = logging.getLogger(__name__)


class ImportWorker(QObject):
    """
    Worker thread for running import in background.

    Emits progress signals to update the UI without blocking.
    """

    progress = pyqtSignal(int, str)  # percent, status message
    finished = pyqtSignal(object)  # ImportResult
    error = pyqtSignal(str)

    def __init__(self, db_path: Path,
                 municipality_layer: Optional[QgsVectorLayer] = None,
                 municipality_name_field: str = 'N03_004',
                 oaza_layer: Optional[QgsVectorLayer] = None,
                 oaza_name_field: str = 'S_NAME',
                 zip_files: Optional[List[Path]] = None):
        super().__init__()
        self.zip_files = zip_files or []
        self.db_path = db_path
        self.municipality_layer = municipality_layer
        self.municipality_name_field = municipality_name_field
        self.oaza_layer = oaza_layer
        self.oaza_name_field = oaza_name_field
        self._cancelled = False

    def run(self):
        """Execute the import process.

        Note: Auto-positioning to Oaza centers has been removed.
        Positioning is now done in the Integration step.
        """
        try:
            # Create database
            db = DatabaseManager(self.db_path)
            db.create_database()
            db.migrate_database()

            # Create importer with Oaza layer for spatial join (oaza_name assignment)
            # Municipality layer is used for validation
            importer = XmlImporter(
                db,
                admin_layer=self.oaza_layer,
                admin_name_field=self.oaza_name_field,
                municipality_layer=self.municipality_layer,
                municipality_name_field=self.municipality_name_field
            )

            # Run import with progress callback
            def progress_callback(prog: ImportProgress):
                if self._cancelled:
                    return
                percent = int(prog.progress_percent * 0.95)  # Reserve 5% for index
                status = f"{prog.current_file} ({prog.completed_files}/{prog.total_files})"
                self.progress.emit(percent, status)

            result = self._import_multiple_zips(
                importer, self.zip_files, progress_callback
            )

            # Build search index
            if result.files_processed > 0:
                self.progress.emit(95, "検索インデックスを構築中...")
                try:
                    index = SearchIndex(db)
                    index.build()
                except Exception as e:
                    logger.warning(f"Could not build search index: {e}")

            self.progress.emit(100, "完了")
            self.finished.emit(result)

        except Exception as e:
            logger.error(f"Import error: {e}", exc_info=True)
            self.error.emit(str(e))

    def _position_arbitrary_to_oaza_centers(self, db: DatabaseManager) -> int:
        """
        Move arbitrary coordinate XMLs to the center of their corresponding Oaza.

        Args:
            db: Database manager

        Returns:
            Number of XMLs repositioned
        """
        if not self.oaza_layer:
            return 0

        # Get Oaza centers from the layer
        oaza_centers = self._get_oaza_centers()
        if not oaza_centers:
            logger.warning("No Oaza centers found in layer")
            return 0

        moved_count = 0

        try:
            with db.connection() as conn:
                # First, update t_xml_meta.oaza_name from t_fude_poly if not set
                # This handles cases where oaza_name comes from XML parsing, not spatial join
                conn.execute("""
                    UPDATE t_xml_meta
                    SET oaza_name = (
                        SELECT fp.oaza_name
                        FROM t_fude_poly fp
                        WHERE fp.xml_meta_id = t_xml_meta.id
                          AND fp.oaza_name IS NOT NULL
                          AND fp.oaza_name != ''
                        GROUP BY fp.oaza_name
                        ORDER BY COUNT(*) DESC
                        LIMIT 1
                    )
                    WHERE oaza_name IS NULL OR oaza_name = ''
                """)
                conn.commit()
                logger.info("Updated t_xml_meta.oaza_name from t_fude_poly")

                # Check if ShiftCoords is available (SpatiaLite function)
                has_shiftcoords = self._check_spatialite_function(conn, 'ShiftCoords')

                # Get all arbitrary coordinate XMLs grouped by oaza_name
                cursor = conn.execute("""
                    SELECT id, oaza_name
                    FROM t_xml_meta
                    WHERE crs_type = '任意座標系' AND oaza_name IS NOT NULL AND oaza_name != ''
                """)
                arbitrary_xmls = cursor.fetchall()

                for xml_id, oaza_name in arbitrary_xmls:
                    # Normalize the oaza name for matching
                    oaza_name_normalized = str(oaza_name).strip() if oaza_name else ""

                    if oaza_name_normalized not in oaza_centers:
                        logger.debug(f"No Oaza center found for: '{oaza_name_normalized}' (available: {list(oaza_centers.keys())[:5]}...)")
                        continue

                    target_center = oaza_centers[oaza_name_normalized]

                    # Get current center of all fude in this XML
                    cursor = conn.execute("""
                        SELECT AsText(Centroid(GUnion(geom)))
                        FROM t_fude_poly
                        WHERE xml_meta_id = ?
                    """, (xml_id,))
                    row = cursor.fetchone()
                    if not row or not row[0]:
                        continue

                    # Parse current center
                    current_center_wkt = row[0]
                    current_geom = QgsGeometry.fromWkt(current_center_wkt)
                    if current_geom.isNull():
                        continue

                    current_center = current_geom.asPoint()

                    # Calculate offset
                    dx = target_center.x() - current_center.x()
                    dy = target_center.y() - current_center.y()

                    if has_shiftcoords:
                        # Use SpatiaLite ShiftCoords function
                        conn.execute("""
                            UPDATE t_fude_poly
                            SET geom = ShiftCoords(geom, ?, ?)
                            WHERE xml_meta_id = ?
                        """, (dx, dy, xml_id))

                        conn.execute("""
                            UPDATE t_xml_meta
                            SET geom = ShiftCoords(geom, ?, ?)
                            WHERE id = ?
                        """, (dx, dy, xml_id))
                    else:
                        # Fallback: use QGIS geometry translation
                        self._translate_geometries_fallback(conn, xml_id, dx, dy)

                    moved_count += 1
                    logger.debug(f"Moved XML {xml_id} ({oaza_name}) by ({dx:.2f}, {dy:.2f})")

                conn.commit()

        except Exception as e:
            logger.error(f"Error positioning arbitrary data: {e}", exc_info=True)

        return moved_count

    def _check_spatialite_function(self, conn, func_name: str) -> bool:
        """Check if a SpatiaLite function is available."""
        try:
            conn.execute(f"SELECT {func_name}(GeomFromText('POINT(0 0)'), 1, 1)")
            return True
        except Exception:
            return False

    def _translate_geometries_fallback(self, conn, xml_id: int, dx: float, dy: float):
        """Fallback method to translate geometries using QGIS."""
        # Get and translate fude polygons
        cursor = conn.execute("""
            SELECT id, AsText(geom) FROM t_fude_poly WHERE xml_meta_id = ?
        """, (xml_id,))

        for fude_id, wkt in cursor.fetchall():
            if not wkt:
                continue
            geom = QgsGeometry.fromWkt(wkt)
            if geom.isNull():
                continue
            geom.translate(dx, dy)
            conn.execute("""
                UPDATE t_fude_poly SET geom = GeomFromText(?, 6676) WHERE id = ?
            """, (geom.asWkt(), fude_id))

        # Get and translate xml_meta geometry
        cursor = conn.execute("""
            SELECT AsText(geom) FROM t_xml_meta WHERE id = ?
        """, (xml_id,))
        row = cursor.fetchone()
        if row and row[0]:
            geom = QgsGeometry.fromWkt(row[0])
            if not geom.isNull():
                geom.translate(dx, dy)
                conn.execute("""
                    UPDATE t_xml_meta SET geom = GeomFromText(?, 6676) WHERE id = ?
                """, (geom.asWkt(), xml_id))

    def _get_oaza_centers(self) -> Dict[str, QgsPointXY]:
        """
        Get center points (centroids) from Oaza layer.

        If multiple features share the same oaza name, their geometries are
        unioned and the centroid of the combined geometry is used.

        Returns:
            Dictionary mapping oaza name to centroid point
        """
        centers = {}

        if not self.oaza_layer:
            return centers

        # First, collect all geometries for each oaza name
        oaza_geoms: Dict[str, QgsGeometry] = {}

        for feature in self.oaza_layer.getFeatures():
            name = feature[self.oaza_name_field]
            if not name or not feature.hasGeometry():
                continue

            # Normalize the name (strip whitespace)
            name_str = str(name).strip()
            if not name_str:
                continue

            geom = feature.geometry()
            if geom.isNull() or geom.isEmpty():
                continue

            if name_str in oaza_geoms:
                # Union with existing geometry
                oaza_geoms[name_str] = oaza_geoms[name_str].combine(geom)
            else:
                oaza_geoms[name_str] = QgsGeometry(geom)

        # Now calculate centroids from the combined geometries
        for name, geom in oaza_geoms.items():
            if not geom.isNull() and not geom.isEmpty():
                centroid = geom.centroid()
                if centroid and not centroid.isNull():
                    centers[name] = centroid.asPoint()

        logger.info(f"Loaded {len(centers)} Oaza centers from layer (field: {self.oaza_name_field})")
        logger.debug(f"Oaza names in layer: {list(centers.keys())[:10]}...")
        return centers

    def _import_multiple_zips(self, importer, zip_files: List[Path], progress_callback):
        """Extract all ZIPs into isolated directories and import with source labels."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            xml_sources = []
            bad_zips = []

            for index, zip_path in enumerate(zip_files):
                extract_root = tmp_path / f"zip_{index:03d}"
                extract_root.mkdir(parents=True, exist_ok=True)
                try:
                    xml_sources.extend(
                        self._extract_zip_sources(
                            zip_path,
                            extract_root,
                            source_prefix=zip_path.name
                        )
                    )
                except (zipfile.BadZipFile, UnsafeZipError) as e:
                    logger.warning(f"Skipping bad/unsafe ZIP: {zip_path.name} ({e})")
                    bad_zips.append(zip_path.name)

            if not xml_sources:
                return ImportResult(
                    success=False,
                    files_processed=0,
                    files_failed=0,
                    total_parcels=0,
                    elapsed_seconds=0,
                    errors=["ZIPファイル内にXMLが見つかりません"] + bad_zips,
                    oaza_assignments={}
                )

            result = importer.import_sources(
                xml_sources,
                progress_callback=progress_callback
            )
            if bad_zips:
                result.errors.extend([f"不正なZIPファイルです: {name}" for name in bad_zips])
                result.success = False
            return result

    def _extract_zip_sources(self, zip_path: Path, extract_root: Path,
                             source_prefix: str) -> List[Tuple[Path, str]]:
        """Recursively extract XML sources from ZIP files without collisions."""
        xml_sources = []

        with zipfile.ZipFile(zip_path, 'r') as zf:
            safe_extract_zip(zf, extract_root)

        for xml_path in extract_root.rglob('*.xml'):
            rel_path = xml_path.relative_to(extract_root).as_posix()
            xml_sources.append((xml_path, f"{source_prefix}/{rel_path}"))

        for inner_zip in sorted(extract_root.rglob('*.zip')):
            inner_prefix = f"{source_prefix}/{inner_zip.relative_to(extract_root).as_posix()}"
            inner_root = inner_zip.parent / f"__{inner_zip.stem}"
            inner_root.mkdir(parents=True, exist_ok=True)
            xml_sources.extend(
                self._extract_zip_sources(inner_zip, inner_root, inner_prefix)
            )
            inner_zip.unlink()

        return xml_sources

    def cancel(self):
        """Request cancellation of the import."""
        self._cancelled = True
