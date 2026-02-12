# -*- coding: utf-8 -*-
"""
Integration Panel Controller for KozuXmlIntegrator (v2)

Handles the integration tab UI logic:
- Complete workflow orchestration for XML integration
- Progress tracking for multi-step processing
- Results display and issue navigation
"""

from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

from qgis.PyQt.QtWidgets import (
    QWidget, QFileDialog, QMessageBox, QInputDialog, QTableWidgetItem
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsRectangle,
)

from ..core import (
    DatabaseManager,
    IntegrationEngine,
    IntegrationConfig,
    IntegrationProgress,
    IntegrationResult,
    load_admin_layer,
)

logger = logging.getLogger(__name__)


class IntegrationWorker(QObject):
    """
    Worker thread for running integration in background.

    Simplified 3-step process:
    1. Load XML data
    2. Join by oaza (using common chibans)
    3. Move center to Oaza_Shape center

    Emits progress signals to update the UI without blocking.
    """

    progress = pyqtSignal(object)  # IntegrationProgress
    finished = pyqtSignal(object)  # IntegrationResult
    error = pyqtSignal(str)

    def __init__(self, db_path: Path,
                 oaza_shape_layer: Optional[QgsVectorLayer] = None,
                 target_oaza: Optional[str] = None):
        super().__init__()
        self.db_path = db_path
        self.oaza_shape_layer = oaza_shape_layer
        self.target_oaza = target_oaza
        self._cancelled = False

    def run(self):
        """Execute the simplified 3-step integration process."""
        try:
            # Open database
            db = DatabaseManager(self.db_path)

            # Create config for simplified process
            config = IntegrationConfig(
                oaza_boundary_layer=self.oaza_shape_layer,
                skip_step2_if_public_anchor=True,
                run_leveling=False,  # No leveling in simplified process
            )

            # Create engine
            engine = IntegrationEngine(db, config)

            # Run simplified integration with progress callback
            def progress_callback(prog: IntegrationProgress):
                if self._cancelled:
                    return
                self.progress.emit(prog)

            result = engine.run_simplified_integration(
                target_oaza=self.target_oaza,
                oaza_shape_layer=self.oaza_shape_layer,
                progress_callback=progress_callback
            )

            self.finished.emit(result)

        except Exception as e:
            logger.error(f"Integration error: {e}", exc_info=True)
            self.error.emit(str(e))

    def cancel(self):
        """Request cancellation of the integration."""
        self._cancelled = True


class IntegrationPanelController:
    """
    Controller for the Integration tab in the dock widget.

    Simplified 3-step process:
    1. Load XML data (database selection)
    2. Join by oaza (using common chibans)
    3. Move center to Oaza_Shape center

    Manages:
    - Data source selection
    - Integration process execution
    - Progress monitoring
    - Results display and navigation
    """

    def __init__(self, dock_widget):
        """
        Initialize controller.

        Args:
            dock_widget: The main dock widget instance
        """
        self.dock = dock_widget
        self.db_path: Optional[Path] = None
        self.db_manager: Optional[DatabaseManager] = None
        self.oaza_shape_layer: Optional[QgsVectorLayer] = None

        self._worker: Optional[IntegrationWorker] = None
        self._thread: Optional[QThread] = None
        self._last_result: Optional[IntegrationResult] = None

        self._connect_signals()

    def _connect_signals(self):
        """Connect UI signals to handlers."""
        self.dock.btnSelectIntegrationDb.clicked.connect(self._on_select_database)
        self.dock.btnSelectIntegrationOazaShape.clicked.connect(self._on_select_oaza_shape)
        self.dock.btnStartIntegration.clicked.connect(self._on_start_integration)
        self.dock.btnIntegrationPreview.clicked.connect(self._on_preview)
        self.dock.btnIntegrationZoomIssue.clicked.connect(self._on_zoom_to_issue)
        self.dock.tableIntegrationIssues.itemSelectionChanged.connect(
            self._on_issue_selection_changed
        )

    def set_database(self, db_path: Path):
        """
        Set database from external source (e.g., from Import tab).

        Args:
            db_path: Path to the database file
        """
        if not db_path or not db_path.exists():
            return

        try:
            self.db_path = db_path
            self.db_manager = DatabaseManager(db_path)
            self.dock.lineEditIntegrationDb.setText(str(db_path))

            # Get database info
            stats = self._get_database_stats()
            info_text = (
                f"XML: {stats.get('xml_count', 0)}件 / "
                f"筆: {stats.get('fude_count', 0)}件 / "
                f"公共座標: {stats.get('public_count', 0)}件"
            )
            self.dock.lblIntegrationDbInfo.setText(info_text)

            # Populate oaza filter
            self._populate_oaza_filter()

            self._update_start_button_state()
            logger.info(f"Integration panel loaded database: {db_path}")

        except Exception as e:
            logger.error(f"Failed to set database: {e}", exc_info=True)
            self.db_manager = None
            self.db_path = None

    def _on_select_database(self):
        """Handle database selection."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "データベースを選択",
            str(Path.home()),
            "SQLite Database (*.sqlite);;All Files (*.*)"
        )

        if file_path:
            self.db_path = Path(file_path)
            self.dock.lineEditIntegrationDb.setText(str(self.db_path))

            try:
                self.db_manager = DatabaseManager(self.db_path)

                # Get database info
                stats = self._get_database_stats()
                info_text = (
                    f"XML: {stats.get('xml_count', 0)}件 / "
                    f"筆: {stats.get('fude_count', 0)}件 / "
                    f"公共座標: {stats.get('public_count', 0)}件"
                )
                self.dock.lblIntegrationDbInfo.setText(info_text)

                # Populate oaza filter
                self._populate_oaza_filter()

                self._update_start_button_state()

            except Exception as e:
                logger.error(f"Failed to open database: {e}", exc_info=True)
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    f"データベースを開けませんでした:\n{e}"
                )
                self.db_manager = None
                self.db_path = None

    def _get_database_stats(self) -> Dict[str, int]:
        """Get basic database statistics."""
        stats = {}
        if not self.db_manager:
            return stats

        try:
            with self.db_manager.connection() as conn:
                cursor = conn.cursor()

                # XML count
                cursor.execute("SELECT COUNT(*) FROM t_xml_meta")
                stats['xml_count'] = cursor.fetchone()[0]

                # Fude count
                cursor.execute("SELECT COUNT(*) FROM t_fude_poly")
                stats['fude_count'] = cursor.fetchone()[0]

                # Public coordinate count
                cursor.execute("""
                    SELECT COUNT(*) FROM t_xml_meta
                    WHERE crs_type != '任意座標系' AND crs_type NOT LIKE '%任意%'
                """)
                stats['public_count'] = cursor.fetchone()[0]

        except Exception as e:
            logger.warning(f"Could not get database stats: {e}")

        return stats

    def _populate_oaza_filter(self):
        """Populate oaza filter combo with available oazas."""
        self.dock.comboIntegrationOazaFilter.clear()
        self.dock.comboIntegrationOazaFilter.addItem("（全て）")
        self.dock.comboIntegrationOazaFilter.setEnabled(False)

        if not self.db_manager:
            return

        try:
            with self.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT DISTINCT oaza_name FROM t_fude_poly
                    WHERE oaza_name IS NOT NULL AND oaza_name != ''
                    ORDER BY oaza_name
                """)

                oaza_names = [row[0] for row in cursor.fetchall()]

                for name in oaza_names:
                    self.dock.comboIntegrationOazaFilter.addItem(name)

                self.dock.comboIntegrationOazaFilter.setEnabled(True)

        except Exception as e:
            logger.warning(f"Could not populate oaza filter: {e}")

    def _on_select_oaza_shape(self):
        """Handle Oaza_Shape layer selection."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "Oaza_Shape.gpkg を選択",
            str(Path.home()),
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;All Files (*.*)"
        )

        if file_path:
            layer_path = Path(file_path)

            # For GeoPackage, let user select layer if multiple exist
            layer_name = None
            if layer_path.suffix.lower() == '.gpkg':
                layer_name = self._select_gpkg_layer(layer_path)
                if layer_name is None:
                    return

            try:
                self.oaza_shape_layer = load_admin_layer(layer_path, layer_name)
                self.dock.lineEditIntegrationOazaShape.setText(str(layer_path))
                self._update_start_button_state()
            except Exception as e:
                QMessageBox.warning(
                    self.dock,
                    "エラー",
                    f"Oaza_Shapeレイヤーの読み込みに失敗しました:\n{e}"
                )

    def _select_gpkg_layer(self, gpkg_path: Path) -> Optional[str]:
        """Show dialog to select a layer from a GeoPackage."""
        from osgeo import ogr

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

        ds = None

        if len(layer_names) == 0:
            QMessageBox.warning(
                self.dock,
                "エラー",
                "GeoPackageにレイヤーが見つかりません。"
            )
            return None

        if len(layer_names) == 1:
            return layer_names[0]

        selected, ok = QInputDialog.getItem(
            self.dock,
            "レイヤー選択",
            "レイヤーを選択してください:",
            layer_info,
            0,
            False
        )

        if ok and selected:
            idx = layer_info.index(selected)
            return layer_names[idx]

        return None

    def _update_start_button_state(self):
        """Enable/disable start button based on input validation."""
        can_start = (
            self.db_path is not None and
            self.db_path.exists() and
            self.db_manager is not None and
            self.oaza_shape_layer is not None
        )
        self.dock.btnStartIntegration.setEnabled(can_start)

    def _on_start_integration(self):
        """Start the simplified 3-step integration process."""
        if self._thread and self._thread.isRunning():
            QMessageBox.warning(
                self.dock,
                "警告",
                "統合処理が既に実行中です。"
            )
            return

        # Get target oaza
        target_oaza = None
        if self.dock.comboIntegrationOazaFilter.currentIndex() > 0:
            target_oaza = self.dock.comboIntegrationOazaFilter.currentText()

        # Create worker for simplified 3-step process
        self._worker = IntegrationWorker(
            db_path=self.db_path,
            oaza_shape_layer=self.oaza_shape_layer,
            target_oaza=target_oaza,
        )

        # Create thread
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Connect signals
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_integration_progress)
        self._worker.finished.connect(self._on_integration_finished)
        self._worker.error.connect(self._on_integration_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)

        # Update UI state
        self.dock.btnStartIntegration.setEnabled(False)
        self.dock.btnStartIntegration.setText("統合処理中...")
        self.dock.progressBarIntegration.setValue(0)
        self.dock.lblIntegrationProgress.setText("開始中...")
        self.dock.lblIntegrationStep.setText("")

        # Clear previous results
        self.dock.tableIntegrationIssues.setRowCount(0)
        self.dock.lblIntegrationResultSummary.setText("統合結果: - / 要確認: -")
        self.dock.btnIntegrationPreview.setEnabled(False)
        self.dock.btnIntegrationZoomIssue.setEnabled(False)

        # Start thread
        self._thread.start()

    def _on_integration_progress(self, prog: IntegrationProgress):
        """Handle progress updates from worker."""
        self.dock.progressBarIntegration.setValue(int(prog.progress_percent))

        # Format step name
        step_names = {
            'step1': 'STEP1: XML接合',
            'step2': 'STEP2: 大字境界配置',
            'step3': 'STEP3: 市町村境界フィッティング',
            'step4': 'STEP4: 平準化',
            'step5': 'STEP5: 品質検証',
        }
        step_name = step_names.get(prog.current_step, prog.current_step)
        self.dock.lblIntegrationStep.setText(step_name)

        # Status message
        status = prog.status_message
        if prog.current_oaza:
            status = f"{prog.current_oaza}: {status}"
        self.dock.lblIntegrationProgress.setText(status)

    def _on_integration_finished(self, result: IntegrationResult):
        """Handle successful integration completion."""
        self._last_result = result

        self.dock.progressBarIntegration.setValue(100)
        self.dock.lblIntegrationProgress.setText("完了")
        self.dock.lblIntegrationStep.setText("")
        self.dock.btnStartIntegration.setText("統合処理を開始")
        self._update_start_button_state()

        # Update result summary
        total_parcels = sum(r.parcels_processed for r in result.oaza_results.values())
        needs_review = sum(
            1 for r in result.oaza_results.values()
            if r.issues
        )
        self.dock.lblIntegrationResultSummary.setText(
            f"統合結果: {total_parcels}筆 / 要確認: {needs_review}大字"
        )

        # Populate issues table
        self._populate_issues_table(result)

        # Enable preview button
        self.dock.btnIntegrationPreview.setEnabled(True)

        # Show summary message
        msg = (
            f"統合処理が完了しました。\n\n"
            f"処理大字数: {len(result.oaza_results)}\n"
            f"総筆数: {total_parcels}\n"
            f"処理時間: {result.elapsed_seconds:.1f}秒"
        )

        if result.errors:
            msg += f"\n\nエラー ({len(result.errors)}件):"
            for err in result.errors[:5]:
                msg += f"\n  - {err[:50]}..."

        QMessageBox.information(self.dock, "統合完了", msg)

    def _populate_issues_table(self, result: IntegrationResult):
        """Populate the issues table with integration results."""
        table = self.dock.tableIntegrationIssues
        table.setRowCount(0)

        for oaza_name, oaza_result in result.oaza_results.items():
            if oaza_result.issues:
                for issue in oaza_result.issues:
                    row = table.rowCount()
                    table.insertRow(row)

                    table.setItem(row, 0, QTableWidgetItem(oaza_name))
                    table.setItem(row, 1, QTableWidgetItem(issue))
                    table.setItem(row, 2, QTableWidgetItem(
                        str(oaza_result.parcels_processed)
                    ))

    def _on_integration_error(self, error_msg: str):
        """Handle integration error."""
        self.dock.lblIntegrationProgress.setText("エラー")
        self.dock.lblIntegrationStep.setText("")
        self.dock.btnStartIntegration.setText("統合処理を開始")
        self._update_start_button_state()

        QMessageBox.critical(
            self.dock,
            "統合エラー",
            f"統合処理中にエラーが発生しました:\n{error_msg}"
        )

    def _cleanup_thread(self):
        """Clean up worker and thread after completion."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None

    def _on_preview(self):
        """Show preview of integration results on map."""
        if not self.db_path or not self.db_path.exists():
            return

        try:
            # Add fude_poly layer with reliability styling
            uri = f"{self.db_path}|layername=t_fude_poly"
            layer = QgsVectorLayer(uri, "統合結果_筆ポリゴン", "ogr")

            if layer.isValid():
                crs = QgsCoordinateReferenceSystem("EPSG:6676")
                layer.setCrs(crs)
                QgsProject.instance().addMapLayer(layer)

                # Apply categorized style based on reliability
                self._apply_reliability_style(layer)

                logger.info("Added integration result layer to canvas")
            else:
                logger.warning("Failed to create valid layer")

        except Exception as e:
            logger.error(f"Error adding preview layer: {e}", exc_info=True)
            QMessageBox.warning(
                self.dock,
                "警告",
                f"プレビューレイヤーの追加中にエラーが発生しました:\n{e}"
            )

    def _apply_output_style(self, layer: QgsVectorLayer):
        """Apply style for output layer: 30% opacity fill and labels."""
        from qgis.core import (
            QgsFillSymbol,
            QgsSingleSymbolRenderer,
            QgsPalLayerSettings,
            QgsTextFormat,
            QgsTextBufferSettings,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QColor, QFont

        # Create simple symbol with 30% opacity (77/255 ≈ 30%)
        symbol = QgsFillSymbol.createSimple({
            'color': '100,150,200,77',  # 77 = 30% of 255
            'outline_color': '50,100,150,255',
            'outline_width': '0.5'
        })

        renderer = QgsSingleSymbolRenderer(symbol)
        layer.setRenderer(renderer)

        # Set up labeling
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = '''"oaza_name" || ' ' || "chiban" || '\n' || '公図記載面積 ' || round("area_sqm", 2) || 'ha' || '\n' || 'フィールド計算面積' || round($area, 2) || 'ha' '''
        label_settings.isExpression = True

        # Text format
        text_format = QgsTextFormat()
        text_format.setFont(QFont("MS Gothic", 9))
        text_format.setColor(QColor(0, 0, 0))

        # Text buffer (halo)
        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(1)
        buffer_settings.setColor(QColor(255, 255, 255))
        text_format.setBuffer(buffer_settings)

        label_settings.setFormat(text_format)

        # Apply labeling
        labeling = QgsVectorLayerSimpleLabeling(label_settings)
        layer.setLabelsEnabled(True)
        layer.setLabeling(labeling)

        layer.triggerRepaint()

    def _apply_reliability_style(self, layer: QgsVectorLayer):
        """Apply categorized style based on reliability field with 30% opacity."""
        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsRendererCategory,
            QgsSymbol,
            QgsFillSymbol,
            QgsPalLayerSettings,
            QgsTextFormat,
            QgsTextBufferSettings,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QColor, QFont

        # Create categories for reliability levels (30% opacity = 77/255)
        categories = []

        # HIGH - Green (30% opacity)
        symbol_high = QgsFillSymbol.createSimple({
            'color': '100,200,100,77',
            'outline_color': '0,150,0,255',
            'outline_width': '0.5'
        })
        categories.append(QgsRendererCategory('HIGH', symbol_high, 'HIGH (高信頼度)'))

        # MEDIUM - Yellow (30% opacity)
        symbol_medium = QgsFillSymbol.createSimple({
            'color': '255,255,100,77',
            'outline_color': '200,200,0,255',
            'outline_width': '0.5'
        })
        categories.append(QgsRendererCategory('MEDIUM', symbol_medium, 'MEDIUM (中信頼度)'))

        # LOW - Red (30% opacity)
        symbol_low = QgsFillSymbol.createSimple({
            'color': '255,100,100,77',
            'outline_color': '200,0,0,255',
            'outline_width': '0.5'
        })
        categories.append(QgsRendererCategory('LOW', symbol_low, 'LOW (低信頼度)'))

        # NULL - Gray (30% opacity)
        symbol_null = QgsFillSymbol.createSimple({
            'color': '200,200,200,77',
            'outline_color': '100,100,100,255',
            'outline_width': '0.3'
        })
        categories.append(QgsRendererCategory('', symbol_null, '未処理'))

        renderer = QgsCategorizedSymbolRenderer('reliability', categories)
        layer.setRenderer(renderer)

        # Set up labeling
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = '''"oaza_name" || ' ' || "chiban" || '\n' || '公図記載面積 ' || round("area_sqm", 2) || 'ha' || '\n' || 'フィールド計算面積' || round($area, 2) || 'ha' '''
        label_settings.isExpression = True

        # Text format
        text_format = QgsTextFormat()
        text_format.setFont(QFont("MS Gothic", 9))
        text_format.setColor(QColor(0, 0, 0))

        # Text buffer (halo)
        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(1)
        buffer_settings.setColor(QColor(255, 255, 255))
        text_format.setBuffer(buffer_settings)

        label_settings.setFormat(text_format)

        # Apply labeling
        labeling = QgsVectorLayerSimpleLabeling(label_settings)
        layer.setLabelsEnabled(True)
        layer.setLabeling(labeling)

        layer.triggerRepaint()

    def _on_issue_selection_changed(self):
        """Handle issue table selection change."""
        has_selection = len(self.dock.tableIntegrationIssues.selectedItems()) > 0
        self.dock.btnIntegrationZoomIssue.setEnabled(has_selection)

    def _on_zoom_to_issue(self):
        """Zoom to selected issue location."""
        selected_items = self.dock.tableIntegrationIssues.selectedItems()
        if not selected_items:
            return

        # Get oaza name from first column
        row = selected_items[0].row()
        oaza_name = self.dock.tableIntegrationIssues.item(row, 0).text()

        if not self.db_manager or not oaza_name:
            return

        try:
            # Get bounding box for the oaza
            with self.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        MIN(MbrMinX(geom)) as min_x,
                        MIN(MbrMinY(geom)) as min_y,
                        MAX(MbrMaxX(geom)) as max_x,
                        MAX(MbrMaxY(geom)) as max_y
                    FROM t_fude_poly
                    WHERE oaza_name = ? AND geom IS NOT NULL
                """, (oaza_name,))

                row = cursor.fetchone()
                if row and row[0] is not None:
                    extent = QgsRectangle(row[0], row[1], row[2], row[3])
                    extent.scale(1.1)  # Add 10% margin

                    # Zoom to extent
                    from qgis.utils import iface
                    if iface and iface.mapCanvas():
                        iface.mapCanvas().setExtent(extent)
                        iface.mapCanvas().refresh()

        except Exception as e:
            logger.error(f"Error zooming to issue: {e}", exc_info=True)
