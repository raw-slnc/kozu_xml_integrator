# -*- coding: utf-8 -*-
"""
Import Panel Controller for KozuXmlIntegrator

Handles the import tab UI logic:
- Folder/file selection dialogs
- Import process execution with progress tracking
- Layer loading after import
"""

from pathlib import Path
from typing import Optional, Callable, List
import logging

from qgis.PyQt.QtWidgets import (
    QWidget, QFileDialog, QMessageBox, QInputDialog
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsTextFormat,
    QgsTextBufferSettings,
    QgsPointXY,
    QgsGeometry,
)
from qgis.PyQt.QtGui import QColor, QFont

from ..core import (
    DatabaseManager,
    XmlImporter,
    ImportProgress,
    ImportResult,
    SpatialJoiner,
    SearchIndex,
    load_admin_layer,
)
from typing import Dict

logger = logging.getLogger(__name__)


class ImportWorker(QObject):
    """
    Worker thread for running import in background.

    Emits progress signals to update the UI without blocking.
    """

    progress = pyqtSignal(int, str)  # percent, status message
    finished = pyqtSignal(object)  # ImportResult
    error = pyqtSignal(str)

    def __init__(self, xml_dir: Path, db_path: Path,
                 municipality_layer: Optional[QgsVectorLayer] = None,
                 municipality_name_field: str = 'N03_004',
                 oaza_layer: Optional[QgsVectorLayer] = None,
                 oaza_name_field: str = 'S_NAME',
                 include_subdirs: bool = True):
        super().__init__()
        self.xml_dir = xml_dir
        self.db_path = db_path
        self.municipality_layer = municipality_layer
        self.municipality_name_field = municipality_name_field
        self.oaza_layer = oaza_layer
        self.oaza_name_field = oaza_name_field
        self.include_subdirs = include_subdirs
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

            result = importer.import_directory(
                self.xml_dir,
                include_subdirs=self.include_subdirs,
                progress_callback=progress_callback
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

    def cancel(self):
        """Request cancellation of the import."""
        self._cancelled = True


class ImportPanelController:
    """
    Controller for the Import tab in the dock widget.

    Manages:
    - File/folder selection dialogs
    - Validation of inputs
    - Starting/monitoring import process
    - Loading results to QGIS
    """

    def __init__(self, dock_widget):
        """
        Initialize controller.

        Args:
            dock_widget: The main dock widget instance
        """
        self.dock = dock_widget
        self.xml_folder: Optional[Path] = None
        self.municipality_layer_path: Optional[Path] = None
        self.oaza_layer_path: Optional[Path] = None
        self.output_db_path: Optional[Path] = None
        self.municipality_layer: Optional[QgsVectorLayer] = None
        self.oaza_layer: Optional[QgsVectorLayer] = None

        self._worker: Optional[ImportWorker] = None
        self._thread: Optional[QThread] = None

        self._connect_signals()

    def _connect_signals(self):
        """Connect UI signals to handlers."""
        self.dock.btnSelectXmlFolder.clicked.connect(self._on_select_xml_folder)
        self.dock.btnSelectMunicipalityLayer.clicked.connect(self._on_select_municipality_layer)
        self.dock.btnSelectOazaLayer.clicked.connect(self._on_select_oaza_layer)
        self.dock.btnSelectOutputDb.clicked.connect(self._on_select_output_db)
        self.dock.btnOpenExistingDb.clicked.connect(self._on_open_existing_db)
        self.dock.btnStartImport.clicked.connect(self._on_start_import)

    def _on_select_xml_folder(self):
        """Handle XML folder selection."""
        folder = QFileDialog.getExistingDirectory(
            self.dock,
            "XMLフォルダを選択",
            str(Path.home())
        )

        if folder:
            self.xml_folder = Path(folder)
            self.dock.lineEditXmlFolder.setText(str(self.xml_folder))
            self._update_import_button_state()

    def _on_select_municipality_layer(self):
        """Handle municipality layer file selection."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "市町村レイヤーを選択",
            str(Path.home()),
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;All Files (*.*)"
        )

        if file_path:
            self.municipality_layer_path = Path(file_path)

            # For GeoPackage, let user select layer if multiple exist
            layer_name = None
            if self.municipality_layer_path.suffix.lower() == '.gpkg':
                layer_name = self._select_gpkg_layer(self.municipality_layer_path)
                if layer_name is None:
                    return  # User cancelled

            self.dock.lineEditMunicipalityLayer.setText(str(self.municipality_layer_path))

            # Load layer and populate field combo
            try:
                self.municipality_layer = load_admin_layer(self.municipality_layer_path, layer_name)
                self._populate_municipality_fields()
            except Exception as e:
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    f"市町村レイヤーの読み込みに失敗しました:\n{e}"
                )
                self.municipality_layer = None

    def _populate_municipality_fields(self):
        """Populate municipality field combo with layer fields."""
        self.dock.comboMunicipalityField.clear()
        self.dock.comboMunicipalityField.setEnabled(False)

        if not self.municipality_layer:
            return

        fields = self.municipality_layer.fields()
        field_names = [field.name() for field in fields]

        self.dock.comboMunicipalityField.addItems(field_names)
        self.dock.comboMunicipalityField.setEnabled(True)

        # Try to select common municipality name fields by default
        default_fields = ['N03_004', 'CITY_NAME', '市町村名', '名称', 'name']
        for default_field in default_fields:
            for i, name in enumerate(field_names):
                if name == default_field:
                    self.dock.comboMunicipalityField.setCurrentIndex(i)
                    return

    def _on_select_oaza_layer(self):
        """Handle Oaza layer file selection."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "大字レイヤーを選択",
            str(Path.home()),
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;All Files (*.*)"
        )

        if file_path:
            self.oaza_layer_path = Path(file_path)

            # For GeoPackage, let user select layer if multiple exist
            layer_name = None
            if self.oaza_layer_path.suffix.lower() == '.gpkg':
                layer_name = self._select_gpkg_layer(self.oaza_layer_path)
                if layer_name is None:
                    return  # User cancelled

            self.dock.lineEditOazaLayer.setText(str(self.oaza_layer_path))

            # Load layer and populate field combo
            try:
                self.oaza_layer = load_admin_layer(self.oaza_layer_path, layer_name)
                self._populate_oaza_fields()
            except Exception as e:
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    f"大字レイヤーの読み込みに失敗しました:\n{e}"
                )
                self.oaza_layer = None

    def _populate_oaza_fields(self):
        """Populate Oaza field combo with layer fields."""
        self.dock.comboOazaField.clear()
        self.dock.comboOazaField.setEnabled(False)

        if not self.oaza_layer:
            return

        fields = self.oaza_layer.fields()
        field_names = [field.name() for field in fields]

        self.dock.comboOazaField.addItems(field_names)
        self.dock.comboOazaField.setEnabled(True)

        # Try to select common Oaza name fields by default
        # '大字' is prioritized as it's the most common field name in Japanese cadastral data
        default_fields = ['大字', 'S_NAME', 'OAZA_NAME', '名称', 'name']
        for default_field in default_fields:
            for i, name in enumerate(field_names):
                if name == default_field:
                    self.dock.comboOazaField.setCurrentIndex(i)
                    return

    def _select_gpkg_layer(self, gpkg_path: Path) -> Optional[str]:
        """
        Show dialog to select a layer from a GeoPackage.

        Args:
            gpkg_path: Path to the GeoPackage file

        Returns:
            Selected layer name, or None if cancelled
        """
        from osgeo import ogr

        # Open the GeoPackage and get layer names
        ds = ogr.Open(str(gpkg_path))
        if ds is None:
            QMessageBox.warning(
                self.dock,
                "エラー",
                f"GeoPackageを開けませんでした:\n{gpkg_path}"
            )
            return None

        layer_names = []
        layer_info = []
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            name = layer.GetName()
            feature_count = layer.GetFeatureCount()
            layer_names.append(name)
            layer_info.append(f"{name} ({feature_count}件)")

        ds = None  # Close the dataset

        if len(layer_names) == 0:
            QMessageBox.warning(
                self.dock,
                "エラー",
                "GeoPackageにレイヤーが見つかりません。"
            )
            return None

        if len(layer_names) == 1:
            return layer_names[0]

        # Multiple layers - let user choose
        selected, ok = QInputDialog.getItem(
            self.dock,
            "レイヤー選択",
            "レイヤーを選択してください:",
            layer_info,
            0,
            False
        )

        if ok and selected:
            # Extract layer name from the selection (remove feature count suffix)
            idx = layer_info.index(selected)
            return layer_names[idx]

        return None

    def _on_select_output_db(self):
        """Handle new output database file selection."""
        file_path, _ = QFileDialog.getSaveFileName(
            self.dock,
            "新規データベースを作成",
            str(Path.home() / "kozu_data.sqlite"),
            "SQLite Database (*.sqlite);;All Files (*.*)"
        )

        if file_path:
            self.output_db_path = Path(file_path)
            self.dock.lineEditOutputDb.setText(str(self.output_db_path))
            self.dock.lineEditCurrentDb.setText(str(self.output_db_path))
            self._update_import_button_state()
            # Notify other panels about the database change
            self._notify_database_changed()

    def _on_open_existing_db(self):
        """Handle opening an existing database."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "既存データベースを開く",
            str(Path.home()),
            "SQLite Database (*.sqlite);;All Files (*.*)"
        )

        if file_path:
            db_path = Path(file_path)
            if not db_path.exists():
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    "指定されたファイルが存在しません。"
                )
                return

            # Verify it's a valid database
            try:
                db = DatabaseManager(db_path)
                # Check if required tables exist
                with db.connection() as conn:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='t_fude_poly'"
                    )
                    if not cursor.fetchone():
                        QMessageBox.warning(
                            self.dock,
                            "エラー",
                            "このファイルは公図XMLデータベースではありません。"
                        )
                        return
            except Exception as e:
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    f"データベースを開けませんでした:\n{e}"
                )
                return

            self.output_db_path = db_path
            self.dock.lineEditOutputDb.setText(str(self.output_db_path))
            self.dock.lineEditCurrentDb.setText(str(self.output_db_path))

            # For existing database, import is not needed but we enable UI
            self._update_import_button_state()

            # Notify other panels about the database change
            self._notify_database_changed()

            # Show database info
            try:
                with db.connection() as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM t_fude_poly")
                    fude_count = cursor.fetchone()[0]
                    cursor = conn.execute("SELECT COUNT(*) FROM t_xml_meta")
                    xml_count = cursor.fetchone()[0]

                QMessageBox.information(
                    self.dock,
                    "データベースを開きました",
                    f"データベース: {db_path.name}\n"
                    f"XMLファイル数: {xml_count}\n"
                    f"筆ポリゴン数: {fude_count}"
                )
            except Exception as e:
                logger.warning(f"Could not get database info: {e}")

            logger.info(f"Opened existing database: {db_path}")

    def _notify_database_changed(self):
        """Notify other panels that the database has changed."""
        # This will be connected by the dock widget to update other panels
        pass  # The actual notification is handled via wrapper in dock widget

    def _update_import_button_state(self):
        """Enable/disable import button based on input validation."""
        can_import = (
            self.xml_folder is not None and
            self.xml_folder.exists() and
            self.output_db_path is not None
        )
        self.dock.btnStartImport.setEnabled(can_import)

    def _on_start_import(self):
        """Start the import process."""
        if self._thread and self._thread.isRunning():
            QMessageBox.warning(
                self.dock,
                "警告",
                "インポート処理が既に実行中です。"
            )
            return

        # Get municipality name field
        municipality_name_field = 'N03_004'
        if self.dock.comboMunicipalityField.isEnabled():
            municipality_name_field = self.dock.comboMunicipalityField.currentText()

        # Get Oaza name field
        oaza_name_field = 'S_NAME'
        if self.dock.comboOazaField.isEnabled():
            oaza_name_field = self.dock.comboOazaField.currentText()

        # Create worker
        self._worker = ImportWorker(
            xml_dir=self.xml_folder,
            db_path=self.output_db_path,
            municipality_layer=self.municipality_layer,
            municipality_name_field=municipality_name_field,
            oaza_layer=self.oaza_layer,
            oaza_name_field=oaza_name_field,
            include_subdirs=self.dock.chkIncludeSubdirs.isChecked()
        )

        # Create thread
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Connect signals
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_import_progress)
        self._worker.finished.connect(self._on_import_finished)
        self._worker.error.connect(self._on_import_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)

        # Update UI state
        self.dock.btnStartImport.setEnabled(False)
        self.dock.btnStartImport.setText("インポート中...")
        self.dock.progressBar.setValue(0)
        self.dock.lblProgressStatus.setText("開始中...")

        # Start thread
        self._thread.start()

    def _on_import_progress(self, percent: int, status: str):
        """Handle progress updates from worker."""
        self.dock.progressBar.setValue(percent)
        self.dock.lblProgressStatus.setText(status)

    def _on_import_finished(self, result: ImportResult):
        """Handle successful import completion."""
        self.dock.progressBar.setValue(100)
        self.dock.lblProgressStatus.setText("完了")
        self.dock.btnStartImport.setText("インポート開始")
        self._update_import_button_state()

        # Show result summary
        msg = (
            f"インポートが完了しました。\n\n"
            f"処理ファイル数: {result.files_processed}\n"
            f"失敗ファイル数: {result.files_failed}\n"
            f"総筆数: {result.total_parcels}\n"
            f"処理時間: {result.elapsed_seconds:.1f}秒"
        )

        # Show positioning info if available
        positioned_count = getattr(result, 'positioned_count', 0)
        if positioned_count > 0:
            msg += f"\n\n自動配置: {positioned_count}件の任意座標系XMLを大字中心に配置しました"

        if result.oaza_assignments:
            msg += "\n\n大字別ファイル数:"
            for oaza, count in sorted(result.oaza_assignments.items()):
                msg += f"\n  {oaza}: {count}件"

        if result.errors:
            msg += f"\n\nエラー ({len(result.errors)}件):"
            for err in result.errors[:5]:  # Show first 5 errors
                msg += f"\n  - {err[:50]}..."

        QMessageBox.information(self.dock, "インポート完了", msg)

        # Add layers to canvas if requested
        if self.dock.chkAddToCanvas.isChecked() and result.files_processed > 0:
            self._add_layers_to_canvas()

    def _on_import_error(self, error_msg: str):
        """Handle import error."""
        self.dock.lblProgressStatus.setText("エラー")
        self.dock.btnStartImport.setText("インポート開始")
        self._update_import_button_state()

        QMessageBox.critical(
            self.dock,
            "インポートエラー",
            f"インポート中にエラーが発生しました:\n{error_msg}"
        )

    def _cleanup_thread(self):
        """Clean up worker and thread after completion."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None

    def _add_layers_to_canvas(self):
        """Add imported layers to QGIS canvas with styling."""
        if not self.output_db_path or not self.output_db_path.exists():
            return

        try:
            # Add fude_poly layer
            uri = f"{self.output_db_path}|layername=t_fude_poly"
            layer = QgsVectorLayer(uri, "筆ポリゴン", "ogr")

            if layer.isValid():
                # Set CRS to JGD2011 Zone 8 (common for this area)
                crs = QgsCoordinateReferenceSystem("EPSG:6676")
                layer.setCrs(crs)

                # Apply styling: 30% opacity fill and labels
                self._apply_fude_style(layer)

                QgsProject.instance().addMapLayer(layer)
                logger.info("Added fude_poly layer to canvas with styling")
            else:
                logger.warning("Failed to create valid fude_poly layer")

            # Add xml_meta layer (map outlines)
            uri_meta = f"{self.output_db_path}|layername=t_xml_meta"
            layer_meta = QgsVectorLayer(uri_meta, "図面外郭", "ogr")

            if layer_meta.isValid():
                crs = QgsCoordinateReferenceSystem("EPSG:6676")
                layer_meta.setCrs(crs)

                # Apply outline-only style for map boundaries
                self._apply_meta_style(layer_meta)

                QgsProject.instance().addMapLayer(layer_meta)
                logger.info("Added xml_meta layer to canvas")

        except Exception as e:
            logger.error(f"Error adding layers to canvas: {e}", exc_info=True)
            QMessageBox.warning(
                self.dock,
                "警告",
                f"レイヤーの追加中にエラーが発生しました:\n{e}"
            )

    def _apply_fude_style(self, layer: QgsVectorLayer):
        """
        Apply style for fude polygon layer: 30% opacity fill and labels.

        Args:
            layer: The fude polygon layer
        """
        try:
            # Create fill symbol with 30% opacity (77 = 30% of 255)
            symbol = QgsFillSymbol.createSimple({
                'color': '100,150,200,77',  # Light blue with 30% opacity
                'outline_color': '50,100,150,255',
                'outline_width': '0.5'
            })
            layer.renderer().setSymbol(symbol)

            # Set up labeling
            label_settings = QgsPalLayerSettings()
            label_settings.fieldName = (
                '"oaza_name" || \' \' || "chiban" || \'\\n\' || '
                '\'公図記載面積 \' || round("area_sqm", 2) || \'㎡\''
            )
            label_settings.isExpression = True

            # Text format
            text_format = QgsTextFormat()
            text_format.setFont(QFont("MS Gothic", 8))
            text_format.setSize(8)
            text_format.setColor(QColor(0, 0, 0))

            # Buffer (halo) for readability
            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(1)
            buffer_settings.setColor(QColor(255, 255, 255))
            text_format.setBuffer(buffer_settings)

            label_settings.setFormat(text_format)

            # Enable labeling
            labeling = QgsVectorLayerSimpleLabeling(label_settings)
            layer.setLabeling(labeling)
            layer.setLabelsEnabled(True)

            layer.triggerRepaint()
            logger.info("Applied fude style with 30% opacity and labels")

        except Exception as e:
            logger.warning(f"Could not apply fude style: {e}")

    def _apply_meta_style(self, layer: QgsVectorLayer):
        """
        Apply style for xml_meta layer: outline only, no fill.

        Args:
            layer: The xml_meta layer
        """
        try:
            symbol = QgsFillSymbol.createSimple({
                'color': '0,0,0,0',  # Transparent fill
                'outline_color': '200,100,50,200',  # Orange outline
                'outline_width': '1.0'
            })
            layer.renderer().setSymbol(symbol)
            layer.triggerRepaint()
            logger.info("Applied meta layer outline style")

        except Exception as e:
            logger.warning(f"Could not apply meta style: {e}")
