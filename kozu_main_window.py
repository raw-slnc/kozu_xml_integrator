"""
Main window for Kozu XML Integrator plugin.

This provides a standalone window with:
- Left panel: Operation tabs (Import, Preview)
- Right panel: Map canvas with cadastral map display
- Link between preview parcels and QGIS main window layers
"""
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QSettings, pyqtSignal, QVariant, QPointF, QRectF, QMarginsF
from qgis.PyQt.QtWidgets import (
    QMainWindow, QFileDialog, QMessageBox, QTreeWidgetItem, QShortcut,
    QApplication, QDialog, QFormLayout, QLineEdit, QDialogButtonBox, QWidget
)
from qgis.PyQt.QtGui import QColor, QKeySequence, QPainter, QPen, QFont, QPageSize, QPageLayout
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsGeometry, QgsFeature, QgsField, QgsPointXY,
    QgsFillSymbol, QgsFeatureRequest, QgsRectangle,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
    QgsMapSettings, QgsMapRendererCustomPainterJob
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsMapToolEmitPoint, QgsHighlight

from .core import DatabaseManager
import sip
import logging

logger = logging.getLogger(__name__)

# Load the UI file
FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), 'kozu_main_window.ui')
)

# GSI Tile URLs
GSI_TILE_URLS = {
    '国土地理院 標準地図': 'https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',
    '国土地理院 写真': 'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg',
    '国土地理院 淡色地図': 'https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png',
    'OpenStreetMap': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
}

# Match level colors
MATCH_COLORS = {
    'exact':  (QColor(0, 100, 255, 180), QColor(0, 100, 255, 60)),   # blue
    'parent': (QColor(255, 200, 0, 180), QColor(255, 200, 0, 60)),   # yellow
    'near':   (QColor(255, 50, 50, 180),  QColor(255, 50, 50, 60)),   # red
}

DRAG_THRESHOLD = 5  # pixels


class PreviewClickTool(QgsMapTool):
    """Map tool for the preview canvas: pan + click + Ctrl+drag."""

    clicked = pyqtSignal(QgsPointXY)
    position_adjusted = pyqtSignal(float, float)  # dx, dy in map coords

    def __init__(self, canvas):
        super().__init__(canvas)
        self._start_pos = None
        self._last_pos = None
        self._dragging = False
        self._ctrl_dragging = False
        self.setCursor(Qt.OpenHandCursor)

    def canvasPressEvent(self, event):
        self._start_pos = event.pos()
        self._last_pos = event.pos()
        self._dragging = False
        self._ctrl_dragging = False
        if event.modifiers() & Qt.ControlModifier:
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.ClosedHandCursor)

    def canvasMoveEvent(self, event):
        if self._start_pos is None:
            return
        delta = event.pos() - self._start_pos
        if not self._dragging and abs(delta.x()) + abs(delta.y()) > DRAG_THRESHOLD:
            self._dragging = True
            self._ctrl_dragging = bool(event.modifiers() & Qt.ControlModifier)

        if self._dragging:
            if self._ctrl_dragging:
                # Ctrl+drag: position adjustment
                current = self.toMapCoordinates(event.pos())
                last = self.toMapCoordinates(self._last_pos)
                dx = current.x() - last.x()
                dy = current.y() - last.y()
                self.position_adjusted.emit(dx, dy)
            else:
                # Normal drag: pan
                current = self.toMapCoordinates(event.pos())
                last = self.toMapCoordinates(self._last_pos)
                dx = current.x() - last.x()
                dy = current.y() - last.y()
                extent = self.canvas().extent()
                extent.setXMinimum(extent.xMinimum() - dx)
                extent.setXMaximum(extent.xMaximum() - dx)
                extent.setYMinimum(extent.yMinimum() - dy)
                extent.setYMaximum(extent.yMaximum() - dy)
                self.canvas().setExtent(extent)
                self.canvas().refresh()
            self._last_pos = event.pos()

    def canvasReleaseEvent(self, event):
        if not self._dragging:
            point = self.toMapCoordinates(event.pos())
            self.clicked.emit(point)
        self._start_pos = None
        self._last_pos = None
        self._dragging = False
        self._ctrl_dragging = False
        self.setCursor(Qt.OpenHandCursor)


class MainSelectTool(QgsMapTool):
    """Map tool for the QGIS main canvas: pan + click + arrow keys."""

    selected = pyqtSignal(QgsPointXY)

    PAN_FRACTION = 0.25  # Arrow key moves 25% of visible extent

    def __init__(self, canvas):
        super().__init__(canvas)
        self._start_pos = None
        self._last_pos = None
        self._dragging = False
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, event):
        self._start_pos = event.pos()
        self._last_pos = event.pos()
        self._dragging = False
        self.setCursor(Qt.ClosedHandCursor)

    def canvasMoveEvent(self, event):
        if self._start_pos is None:
            return
        delta = event.pos() - self._start_pos
        if not self._dragging and abs(delta.x()) + abs(delta.y()) > DRAG_THRESHOLD:
            self._dragging = True
        if self._dragging:
            current = self.toMapCoordinates(event.pos())
            last = self.toMapCoordinates(self._last_pos)
            dx = current.x() - last.x()
            dy = current.y() - last.y()
            extent = self.canvas().extent()
            extent.setXMinimum(extent.xMinimum() - dx)
            extent.setXMaximum(extent.xMaximum() - dx)
            extent.setYMinimum(extent.yMinimum() - dy)
            extent.setYMaximum(extent.yMaximum() - dy)
            self.canvas().setExtent(extent)
            self.canvas().refresh()
            self._last_pos = event.pos()

    def canvasReleaseEvent(self, event):
        if not self._dragging:
            point = self.toMapCoordinates(event.pos())
            self.selected.emit(point)
        self._start_pos = None
        self._last_pos = None
        self._dragging = False
        self.setCursor(Qt.CrossCursor)

    def keyPressEvent(self, event):
        extent = self.canvas().extent()
        dx = extent.width() * self.PAN_FRACTION
        dy = extent.height() * self.PAN_FRACTION
        key = event.key()
        if key == Qt.Key_Left:
            extent.setXMinimum(extent.xMinimum() - dx)
            extent.setXMaximum(extent.xMaximum() - dx)
        elif key == Qt.Key_Right:
            extent.setXMinimum(extent.xMinimum() + dx)
            extent.setXMaximum(extent.xMaximum() + dx)
        elif key == Qt.Key_Up:
            extent.setYMinimum(extent.yMinimum() + dy)
            extent.setYMaximum(extent.yMaximum() + dy)
        elif key == Qt.Key_Down:
            extent.setYMinimum(extent.yMinimum() - dy)
            extent.setYMaximum(extent.yMaximum() - dy)
        else:
            return
        self.canvas().setExtent(extent)
        self.canvas().refresh()


class CrosshairOverlay(QWidget):
    """Transparent overlay that draws a crosshair at the center of the canvas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(0, 0, 0, 160))
        pen.setWidthF(0.5)
        painter.setPen(pen)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2
        painter.drawLine(cx, 0, cx, h)
        painter.drawLine(0, cy, w, cy)
        painter.end()


class KozuMainWindow(QMainWindow, FORM_CLASS):
    """Main window for Kozu XML Integrator."""

    # Signals
    database_changed = pyqtSignal(str)

    def __init__(self, iface, parent=None):
        """Initialize the main window."""
        super().__init__(parent)
        self.setupUi(self)

        self.iface = iface
        self.db_path: Optional[Path] = None
        self.db: Optional[DatabaseManager] = None

        # Preview components
        self.map_canvas: Optional[QgsMapCanvas] = None
        self.tile_layer: Optional[QgsRasterLayer] = None
        self.overlay_tile_layer: Optional[QgsRasterLayer] = None
        self.preview_layer: Optional[QgsVectorLayer] = None

        # Current preview state
        self._current_xml_meta_id: Optional[int] = None
        self._current_municipality: str = ''  # Municipality name from XML meta
        self._current_scale: int = 600  # Default scale denominator
        self._max_zoom_out_scale: float = 0  # Zoom-out limit (scale denominator)
        self._full_extent = None  # Full data extent for zoom-out limit
        self._scale_guard: bool = False  # Guard against recursive scale changes
        self._is_arbitrary: bool = True  # Whether current preview is arbitrary coords

        # Link feature state
        self._link_config: Optional[dict] = None
        self._highlights: list = []  # QgsHighlight on preview canvas
        self._main_highlights: list = []  # QgsHighlight on main canvas
        self._prev_main_tool = None  # Saved main canvas tool
        self._main_select_tool: Optional[MainSelectTool] = None
        self._preview_tool: Optional[PreviewClickTool] = None
        self._scale_sync_guard: bool = False  # Guard for bidirectional scale sync

        # Initialize UI
        self._setup_map_canvas()
        self._connect_signals()
        self._load_settings()

        # Default position: top-left of screen
        self.move(0, 0)

        # Ctrl+F: toggle fullscreen
        self._fullscreen_shortcut = QShortcut(
            QKeySequence('Ctrl+F'), self
        )
        self._fullscreen_shortcut.activated.connect(self._toggle_fullscreen)

    def _setup_map_canvas(self):
        """Set up the preview map canvas."""
        self.map_canvas = QgsMapCanvas(self.frameMapCanvas)
        self.map_canvas.setCanvasColor(QColor(255, 255, 255))
        self.map_canvas.enableAntiAliasing(True)

        layout = self.frameMapCanvas.layout()
        layout.addWidget(self.map_canvas)

        # Crosshair overlay
        self._crosshair = CrosshairOverlay(self.map_canvas)
        self._crosshair.resize(self.map_canvas.size())
        self.map_canvas.resizeEvent = self._on_canvas_resize

        # Preview tool: pan + click + Ctrl+drag
        self._preview_tool = PreviewClickTool(self.map_canvas)
        self._preview_tool.clicked.connect(self._on_preview_clicked)
        self._preview_tool.position_adjusted.connect(self._on_position_adjusted)
        self.map_canvas.setMapTool(self._preview_tool)

        # Zoom-out limit
        self.map_canvas.scaleChanged.connect(self._on_scale_changed)

        # Default CRS (JGD2011 / Japan Plane Rectangular CS VIII)
        crs = QgsCoordinateReferenceSystem('EPSG:6676')
        self.map_canvas.setDestinationCrs(crs)

    def _on_canvas_resize(self, event):
        """Keep crosshair overlay sized to canvas."""
        QgsMapCanvas.resizeEvent(self.map_canvas, event)
        self._crosshair.resize(event.size())

    def _toggle_fullscreen(self):
        """Toggle between fullscreen and normal window mode."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _connect_signals(self):
        """Connect UI signals."""
        # Database buttons
        self.btnOpenDb.clicked.connect(self._on_open_database)
        self.btnNewDb.clicked.connect(self._on_new_database)

        # Import tab
        self.btnSelectXmlFolder.clicked.connect(self._on_select_xml_folder)
        self.btnStartImport.clicked.connect(self._on_start_import)

        # Preview tab
        self.comboOaza.currentIndexChanged.connect(self._on_oaza_selected)
        self.treeXmlFiles.itemSelectionChanged.connect(self._on_xml_selected)
        self.chkShowChiban.stateChanged.connect(self._update_labels)
        self.chkShowEstArea.stateChanged.connect(self._update_labels)
        self.btnLinkSettings.clicked.connect(self._on_link_settings)
        self.btnLinkClear.clicked.connect(self._on_link_clear)
        self.chkEnableMainSelect.stateChanged.connect(self._on_enable_main_select)

        # Mutual display options
        self.chkMutualEnabled.stateChanged.connect(
            lambda state: self.chkMutualNear.setEnabled(state == Qt.Checked)
        )

        # Clear highlights when match level options change
        self.chkMatchExact.stateChanged.connect(lambda _: self._clear_highlights())
        self.chkMatchParent.stateChanged.connect(lambda _: self._clear_highlights())
        self.chkMatchNear.stateChanged.connect(lambda _: self._clear_highlights())
        self.chkMutualEnabled.stateChanged.connect(lambda _: self._clear_highlights())
        self.chkMutualNear.stateChanged.connect(lambda _: self._clear_highlights())

        # Tile controls
        self.chkShowTile.stateChanged.connect(self._on_tile_toggle)
        self.comboTileSource.currentIndexChanged.connect(self._on_tile_source_changed)

        # Overlay tile controls
        self.chkOverlayTile.stateChanged.connect(self._on_overlay_tile_toggle)
        self.comboOverlaySource.currentIndexChanged.connect(self._on_overlay_source_changed)
        self.spinOverlayOpacity.valueChanged.connect(self._on_overlay_opacity_changed)

        # Refresh overlay source list each time the dropdown opens
        _original_show_popup = self.comboOverlaySource.showPopup
        def _refreshed_show_popup():
            self._populate_overlay_sources()
            _original_show_popup()
        self.comboOverlaySource.showPopup = _refreshed_show_popup

        # PDF export
        self.btnExportPdf.clicked.connect(self._on_export_pdf)

        # GPKG export
        self.btnExportGpkg.clicked.connect(self._on_export_gpkg)

        # Scale sync: main canvas → preview canvas (when tile is active)
        self.iface.mapCanvas().scaleChanged.connect(self._on_main_canvas_scale_changed)

    def _load_settings(self):
        """Load saved settings."""
        settings = QSettings()
        settings.beginGroup('KozuXmlIntegrator')
        last_db = settings.value('last_database', '')
        if last_db and Path(last_db).exists():
            self._open_database(Path(last_db))

        # Overlay tile settings
        overlay_source_name = settings.value('overlay_source_name', '')
        overlay_enabled = settings.value('overlay_tile_enabled', False, type=bool)
        overlay_opacity = settings.value('overlay_tile_opacity', 50, type=int)
        settings.endGroup()

        self.spinOverlayOpacity.setValue(overlay_opacity)

        # Populate combo and restore saved source
        self._populate_overlay_sources()
        if overlay_source_name:
            idx = self.comboOverlaySource.findText(overlay_source_name)
            if idx >= 0:
                self.comboOverlaySource.setCurrentIndex(idx)
        if overlay_enabled and self.comboOverlaySource.currentData():
            self.chkOverlayTile.setChecked(True)

        # Load link config
        from .ui.link_settings_dialog import LinkSettingsDialog
        saved_config = LinkSettingsDialog.load_from_settings()
        if saved_config:
            self._apply_link_config(saved_config)

    def _save_settings(self):
        """Save current settings."""
        settings = QSettings()
        settings.beginGroup('KozuXmlIntegrator')
        if self.db_path:
            settings.setValue('last_database', str(self.db_path))
        settings.setValue('overlay_source_name', self.comboOverlaySource.currentText())
        settings.setValue('overlay_tile_enabled', self.chkOverlayTile.isChecked())
        settings.setValue('overlay_tile_opacity', self.spinOverlayOpacity.value())
        settings.endGroup()

    # ─── Database ───────────────────────────────────────────

    def _on_open_database(self):
        """Handle open existing database."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "データベースを開く",
            str(Path.home()),
            "SQLite Database (*.sqlite *.db);;GeoPackage (*.gpkg);;All Files (*.*)"
        )
        if file_path:
            self._open_database(Path(file_path))

    def _on_new_database(self):
        """Handle create new database."""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "新規データベースを作成",
            str(Path.home() / "kozu_data.sqlite"),
            "SQLite Database (*.sqlite);;All Files (*.*)"
        )
        if file_path:
            path = Path(file_path)
            if path.exists():
                path.unlink()
            self._open_database(path, create_new=True)

    def _open_database(self, path: Path, create_new: bool = False):
        """Open or create a database."""
        try:
            self.db = DatabaseManager(path)
            if create_new:
                self.db.create_database()
            self.db_path = path

            self.lineEditGlobalDb.setText(str(path))
            self._update_db_info()
            self._update_ui_state()
            self._populate_oaza_combo()
            self._save_settings()
            self.database_changed.emit(str(path))

            logger.info(f"Database opened: {path}")

        except Exception as e:
            logger.error(f"Failed to open database: {e}")
            QMessageBox.critical(self, "エラー", f"データベースを開けませんでした:\n{e}")

    def _update_db_info(self):
        """Update database info label."""
        if not self.db:
            self.lblDbInfo.setText("ファイル数: - / 筆数: -")
            return
        try:
            with self.db.connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM t_xml_meta")
                file_count = cursor.fetchone()[0]
                cursor = conn.execute("SELECT COUNT(*) FROM t_fude_poly")
                fude_count = cursor.fetchone()[0]
            self.lblDbInfo.setText(f"ファイル数: {file_count} / 筆数: {fude_count}")
        except Exception:
            self.lblDbInfo.setText("ファイル数: - / 筆数: -")

    def _update_ui_state(self):
        """Update UI element states based on current data."""
        has_db = self.db is not None

        # Import tab
        self.btnStartImport.setEnabled(
            has_db and self.lineEditXmlFolder.text() != ''
        )

        # Preview tab
        self.comboOaza.setEnabled(has_db)
        self.treeXmlFiles.setEnabled(has_db)

    def _populate_oaza_combo(self):
        """Populate the oaza combo box from database."""
        if not self.db:
            return
        try:
            with self.db.connection() as conn:
                cursor = conn.execute("""
                    SELECT DISTINCT oaza_name FROM t_fude_poly
                    WHERE oaza_name IS NOT NULL AND oaza_name != ''
                    ORDER BY oaza_name
                """)
                oaza_names = [row[0] for row in cursor.fetchall()]

            self.comboOaza.blockSignals(True)
            self.comboOaza.clear()
            self.comboOaza.addItem("（大字を選択）")
            self.comboOaza.addItems(oaza_names)
            self.comboOaza.blockSignals(False)

        except Exception as e:
            logger.error(f"Failed to populate oaza combo: {e}")

    # ─── Import Tab ─────────────────────────────────────────

    def _on_select_xml_folder(self):
        """Handle XML folder selection."""
        folder = QFileDialog.getExistingDirectory(
            self, "XMLフォルダを選択", str(Path.home())
        )
        if folder:
            self.lineEditXmlFolder.setText(folder)
            self._update_ui_state()

    def _on_start_import(self):
        """Start import process."""
        if not self.db or not self.lineEditXmlFolder.text():
            return

        from .ui.import_panel import ImportWorker
        from qgis.PyQt.QtCore import QThread

        xml_folder = Path(self.lineEditXmlFolder.text())
        include_subdirs = self.chkIncludeSubdirs.isChecked()

        self._import_worker = ImportWorker(
            xml_dir=xml_folder,
            db_path=self.db_path,
            municipality_layer=None,
            municipality_name_field=None,
            oaza_layer=None,
            oaza_name_field=None,
            include_subdirs=include_subdirs
        )

        self._import_thread = QThread()
        self._import_worker.moveToThread(self._import_thread)

        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.progress.connect(self._on_import_progress)
        self._import_worker.finished.connect(self._on_import_finished)
        self._import_worker.error.connect(self._on_import_error)
        self._import_worker.finished.connect(self._import_thread.quit)
        self._import_worker.error.connect(self._import_thread.quit)

        self.btnStartImport.setEnabled(False)
        self.btnStartImport.setText("インポート中...")
        self.progressBar.setValue(0)
        self.lblProgressStatus.setText("開始中...")

        self._import_thread.start()

    def _on_import_progress(self, percent: int, status: str):
        """Handle import progress."""
        self.progressBar.setValue(percent)
        self.lblProgressStatus.setText(status)

    def _on_import_finished(self, result):
        """Handle import completion."""
        self.progressBar.setValue(100)
        self.lblProgressStatus.setText("完了")
        self.btnStartImport.setText("インポート開始")
        self._update_ui_state()
        self._update_db_info()
        self._populate_oaza_combo()

        QMessageBox.information(
            self, "インポート完了",
            f"インポートが完了しました。\n\n"
            f"処理ファイル数: {result.files_processed}\n"
            f"失敗ファイル数: {result.files_failed}\n"
            f"総筆数: {result.total_parcels}"
        )

    def _on_import_error(self, error_msg: str):
        """Handle import error."""
        self.lblProgressStatus.setText("エラー")
        self.btnStartImport.setText("インポート開始")
        self._update_ui_state()
        QMessageBox.critical(self, "エラー", f"インポート中にエラーが発生しました:\n{error_msg}")

    # ─── Preview Tab ────────────────────────────────────────

    @staticmethod
    def _extract_scale_from_name(map_name: str) -> Optional[int]:
        """Extract scale denominator from map name.

        Examples:
            '松崎町雲見１０００図無Ｈ２１' → 1000
            '松崎町岩科南側６００図無Ｈ２１' → 600
            '松崎町岩科南側－３図無Ｈ２１' → None (no scale in name)
        """
        normalized = unicodedata.normalize('NFKC', map_name)
        m = re.search(r'(\d+)(?=図)', normalized)
        if m:
            val = int(m.group(1))
            if val >= 100:  # Filter out small numbers like '-3図'
                return val
        return None

    def _on_oaza_selected(self, index: int):
        """Handle oaza selection: populate XML file list."""
        self._clear_highlights()
        self.treeXmlFiles.clear()
        self._current_xml_meta_id = None

        oaza_name = self.comboOaza.currentText()
        if oaza_name == "（大字を選択）" or not self.db:
            return

        try:
            with self.db.connection() as conn:
                cursor = conn.execute("""
                    SELECT DISTINCT m.id, m.file_name, m.map_name, m.crs_type, m.fude_count
                    FROM t_xml_meta m
                    JOIN t_fude_poly f ON f.xml_meta_id = m.id
                    WHERE f.oaza_name = ?
                    ORDER BY m.map_name
                """, (oaza_name,))
                rows = cursor.fetchall()

            for row in rows:
                xml_id, file_name, map_name, crs_type, fude_count = row

                # Extract scale from map name
                scale = self._extract_scale_from_name(map_name or '')
                scale_str = f"1:{scale}" if scale else "不明"

                # Shorten crs_type for display
                if crs_type and '任意' in crs_type:
                    crs_short = '任意'
                elif crs_type and '公共' in crs_type:
                    crs_short = '公共'
                else:
                    crs_short = crs_type or ''

                item = QTreeWidgetItem([
                    map_name or file_name,
                    scale_str,
                    str(fude_count or 0),
                    crs_short
                ])
                item.setData(0, Qt.UserRole, xml_id)
                item.setData(1, Qt.UserRole, scale)  # Store scale as int
                self.treeXmlFiles.addTopLevelItem(item)

            # Auto-resize columns
            for i in range(4):
                self.treeXmlFiles.resizeColumnToContents(i)

        except Exception as e:
            logger.error(f"Failed to load XML list for oaza '{oaza_name}': {e}")

    def _on_xml_selected(self):
        """Handle XML file selection: display parcels on map canvas."""
        items = self.treeXmlFiles.selectedItems()
        if not items:
            return

        item = items[0]
        xml_meta_id = item.data(0, Qt.UserRole)
        scale = item.data(1, Qt.UserRole)
        map_name = item.text(0)

        self._current_xml_meta_id = xml_meta_id
        self._current_scale = scale or 600

        self._load_xml_preview(xml_meta_id, map_name)

    def _load_xml_preview(self, xml_meta_id: int, map_name: str = ''):
        """Load parcels from a specific XML into the map canvas."""
        if not self.db:
            return

        try:
            with self.db.connection() as conn:
                # Get municipality name from xml_meta
                muni_row = conn.execute(
                    "SELECT municipality_name FROM t_xml_meta WHERE id = ?",
                    (xml_meta_id,)
                ).fetchone()
                self._current_municipality = muni_row[0] if muni_row and muni_row[0] else ''

                cursor = conn.execute("""
                    SELECT id, oaza_name, chiban, AsText(geom) as wkt,
                           coord_type, area_sqm
                    FROM t_fude_poly
                    WHERE xml_meta_id = ? AND geom IS NOT NULL
                """, (xml_meta_id,))
                rows = cursor.fetchall()

            # Clear highlights from previous selection
            self._clear_highlights()

            # Remove old preview layer
            if self.preview_layer:
                QgsProject.instance().removeMapLayer(self.preview_layer.id())
                self.preview_layer = None

            # Create memory layer
            self.preview_layer = QgsVectorLayer(
                "Polygon?crs=EPSG:6676", "preview", "memory"
            )
            provider = self.preview_layer.dataProvider()

            provider.addAttributes([
                QgsField("id", QVariant.Int),
                QgsField("oaza_name", QVariant.String),
                QgsField("chiban", QVariant.String),
                QgsField("coord_type", QVariant.String),
                QgsField("area_sqm", QVariant.Double),
                QgsField("est_area", QVariant.Double),
            ])
            self.preview_layer.updateFields()

            # Build features
            features = []
            crs_types = set()
            scale_factor = (self._current_scale / 1000.0) ** 2

            for row in rows:
                fid, oaza, chiban, wkt, coord_type, area_sqm = row
                if not wkt:
                    continue
                geom = QgsGeometry.fromWkt(wkt)
                if not geom or geom.isNull():
                    continue

                # Estimate real area
                est_area = None
                if area_sqm and coord_type == '図上測量':
                    est_area = area_sqm * scale_factor
                elif area_sqm:
                    est_area = area_sqm  # Public coords: already real

                feat = QgsFeature()
                feat.setGeometry(geom)
                feat.setAttributes([fid, oaza, chiban, coord_type, area_sqm, est_area])
                features.append(feat)
                if coord_type:
                    crs_types.add(coord_type)

            provider.addFeatures(features)

            # Track coordinate type
            self._is_arbitrary = '測量成果' not in crs_types

            # Apply polygon style
            symbol = QgsFillSymbol.createSimple({
                'color': '150,150,255,10',
                'outline_color': '0,0,150,255',
                'outline_width': '0.2'
            })
            self.preview_layer.renderer().setSymbol(symbol)

            # Apply labels
            self._apply_labels_to_layer()

            # Update info labels
            fude_count = len(features)
            crs_info = ', '.join(crs_types) if crs_types else '不明'
            muni_info = f" / {self._current_municipality}" if self._current_municipality else ""
            self.lblPreviewInfo.setText(f"筆数: {fude_count} / 座標系: {crs_info}{muni_info}")
            self.lblPreviewTitle.setText(f"公図プレビュー: {map_name}")

            # Tile availability
            has_public = not self._is_arbitrary
            self.chkShowTile.setEnabled(has_public)
            self.comboTileSource.setEnabled(has_public and self.chkShowTile.isChecked())

            if has_public:
                self.lblTileStatus.setText("公共座標系データ - タイル表示可能")
                self.lblPreviewCrs.setText("CRS: EPSG:6676 (JGD2011 / Japan Plane Rectangular CS VIII)")
                # Auto-enable tile for public coordinate data (unless locked)
                if not self.chkTileLock.isChecked():
                    self.chkShowTile.setChecked(True)
            else:
                if not self.chkTileLock.isChecked():
                    self.chkShowTile.setChecked(False)
                self.lblTileStatus.setText("任意座標系データ - タイル表示不可")
                scale_str = f"1:{self._current_scale}" if self._current_scale else "不明"
                self.lblPreviewCrs.setText(f"CRS: 任意座標系 / 縮尺: {scale_str}")

            # Update GPKG export button state
            self._update_export_gpkg_state()

            # Set up canvas layers
            self._update_canvas_layers()

            # Zoom to extent and set zoom-out limit
            self._max_zoom_out_scale = 0  # Reset before setExtent to avoid blocking
            if features:
                extent = self.preview_layer.extent()
                extent.scale(1.1)
                self.map_canvas.setExtent(extent)
                self._update_zoom_limit()

            self.map_canvas.refresh()

            # If main select is active, re-zoom to oaza for new XML
            if self.chkEnableMainSelect.isChecked():
                self._zoom_main_to_oaza()

        except Exception as e:
            logger.error(f"Failed to load preview for xml_meta_id={xml_meta_id}: {e}")

    def _apply_labels_to_layer(self):
        """Apply chiban/area labels to the preview layer based on checkbox state."""
        if not self.preview_layer:
            return

        show_chiban = self.chkShowChiban.isChecked()
        show_est_area = self.chkShowEstArea.isChecked()

        if not show_chiban and not show_est_area:
            self.preview_layer.setLabelsEnabled(False)
            return

        # Build label expression
        parts = []
        if show_chiban:
            parts.append('"chiban"')
        if show_est_area:
            parts.append("format_number(\"est_area\", 0) || 'm²'")

        expression = " || '\\n' || ".join(parts) if len(parts) > 1 else parts[0]

        settings = QgsPalLayerSettings()
        settings.fieldName = expression
        settings.isExpression = True
        settings.placement = QgsPalLayerSettings.OverPoint

        text_format = QgsTextFormat()
        text_format.setSize(7)
        text_format.setColor(QColor(0, 0, 100))

        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(1.0)
        buffer_settings.setColor(QColor(255, 255, 255))
        text_format.setBuffer(buffer_settings)

        settings.setFormat(text_format)

        self.preview_layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        self.preview_layer.setLabelsEnabled(True)

    def _update_labels(self):
        """Handle label toggle checkbox changes."""
        if self.preview_layer:
            self._apply_labels_to_layer()
            self.map_canvas.refresh()

    def _update_zoom_limit(self):
        """Recalculate zoom-out limit from the current preview layer extent."""
        if not self.preview_layer or self.preview_layer.featureCount() == 0:
            self._max_zoom_out_scale = 0
            return
        extent = self.preview_layer.extent()
        extent.scale(1.1)
        # Calculate what scale would be needed to show this extent
        canvas_width = self.map_canvas.width()
        canvas_height = self.map_canvas.height()
        if canvas_width > 0 and canvas_height > 0:
            scale_x = extent.width() / canvas_width
            scale_y = extent.height() / canvas_height
            # Use the larger ratio (fully fits)
            map_units_per_pixel = max(scale_x, scale_y)
            self._max_zoom_out_scale = map_units_per_pixel * 25.4 * self.map_canvas.mapSettings().outputDpi()
        self._full_extent = extent

    def _on_scale_changed(self, new_scale: float):
        """Handle preview canvas scale changes: zoom-out limit + scale sync."""
        if self._scale_guard:
            return

        # Zoom-out limit (only when tile background is NOT active)
        if (not self.chkShowTile.isChecked()
                and self._max_zoom_out_scale > 0
                and new_scale > self._max_zoom_out_scale
                and self._full_extent):
            self._scale_guard = True
            self.map_canvas.setExtent(self._full_extent)
            self.map_canvas.refresh()
            self._scale_guard = False
            return

        # Scale sync: preview → main (only when tile is active after georef)
        if self._scale_sync_guard or self._is_arbitrary:
            return
        if not self.chkShowTile.isChecked():
            return
        self._scale_sync_guard = True
        self.iface.mapCanvas().zoomScale(new_scale)
        self._scale_sync_guard = False

    def _on_main_canvas_scale_changed(self, new_scale: float):
        """Sync preview scale when QGIS main canvas scale changes."""
        if self._scale_sync_guard or self._is_arbitrary:
            return
        if not self.isVisible() or not self.chkShowTile.isChecked():
            return
        self._scale_sync_guard = True
        self._scale_guard = True  # Bypass zoom-out limit during sync
        self.map_canvas.zoomScale(new_scale)
        self._scale_guard = False
        self._scale_sync_guard = False

    # ─── Link Settings ──────────────────────────────────────

    def _on_link_settings(self):
        """Open link settings dialog."""
        from .ui.link_settings_dialog import LinkSettingsDialog

        dlg = LinkSettingsDialog(self, self._link_config)
        if dlg.exec_() == LinkSettingsDialog.Accepted:
            config = dlg.get_config()
            if config:
                self._apply_link_config(config)
                LinkSettingsDialog.save_to_settings(config)
            else:
                QMessageBox.warning(self, "連携設定", "すべての項目を選択してください。")

    def _apply_link_config(self, config: dict):
        """Apply link configuration and update UI."""
        mode = config.get('match_mode', 'spatial')
        # Resolve layer IDs from names if needed
        if 'lot_layer_id' not in config:
            for layer_id, layer in QgsProject.instance().mapLayers().items():
                if isinstance(layer, QgsVectorLayer):
                    if layer.name() == config.get('lot_layer_name'):
                        config['lot_layer_id'] = layer_id
                    if mode == 'spatial' and layer.name() == config.get('oaza_layer_name'):
                        config['oaza_layer_id'] = layer_id

        self._link_config = config
        lot_name = config.get('lot_layer_name', '?')
        mode_label = "属性" if mode == 'attribute' else "空間"
        self.lblLinkStatus.setText(f"連携: {lot_name} ({mode_label})")
        self.chkEnableMainSelect.setEnabled(True)
        self.btnLinkClear.setEnabled(True)

    def _on_link_clear(self):
        """Clear link configuration completely."""
        self._clear_highlights()
        self.chkEnableMainSelect.setChecked(False)
        self.chkEnableMainSelect.setEnabled(False)
        self.btnLinkClear.setEnabled(False)
        self._link_config = None
        self.lblLinkStatus.setText("未設定")
        # Clear persisted settings
        QSettings().remove('KozuXmlIntegrator/LinkConfig')

    def _get_link_layers(self):
        """Resolve link config to actual layers.

        Returns (oaza_layer, lot_layer).
        In attribute mode, oaza_layer is always None.
        """
        if not self._link_config:
            return None, None
        mode = self._link_config.get('match_mode', 'spatial')
        lot_id = self._link_config.get('lot_layer_id')
        lot_layer = QgsProject.instance().mapLayer(lot_id) if lot_id else None

        if mode == 'attribute':
            return None, lot_layer

        oaza_id = self._link_config.get('oaza_layer_id')
        oaza_layer = QgsProject.instance().mapLayer(oaza_id) if oaza_id else None
        return oaza_layer, lot_layer

    # ─── CRS Helpers ───────────────────────────────────────

    _PREVIEW_CRS = QgsCoordinateReferenceSystem('EPSG:6676')

    def _transform_point_to_preview_crs(self, point: QgsPointXY,
                                         source_crs: QgsCoordinateReferenceSystem) -> QgsPointXY:
        """Transform a point from source CRS to EPSG:6676 (preview canvas CRS).

        Returns the point unchanged if CRSs match or source is invalid.
        """
        if not source_crs.isValid() or source_crs == self._PREVIEW_CRS:
            return point
        transform = QgsCoordinateTransform(
            source_crs, self._PREVIEW_CRS, QgsProject.instance()
        )
        return transform.transform(point)

    def _transform_rect_to_layer_crs(self, rect: QgsRectangle,
                                      canvas_crs: QgsCoordinateReferenceSystem,
                                      layer: QgsVectorLayer) -> QgsRectangle:
        """Transform a rectangle from canvas CRS to layer CRS for spatial queries."""
        layer_crs = layer.crs()
        if not layer_crs.isValid() or not canvas_crs.isValid() or layer_crs == canvas_crs:
            return rect
        transform = QgsCoordinateTransform(
            canvas_crs, layer_crs, QgsProject.instance()
        )
        return transform.transformBoundingBox(rect)

    # ─── Matching Logic ─────────────────────────────────────

    @staticmethod
    def _oaza_filter_expr(column: str, value: str) -> str:
        """Build a filter expression for oaza name prefix matching.

        Matches if either name is a prefix of the other, covering cases like:
          column='大沢里' vs value='大沢里祢宜畑'  (column is prefix of value)
          column='大沢里祢宜畑' vs value='大沢里'  (value is prefix of column)
          column='大沢里' vs value='大沢里'        (exact match)
        """
        safe = value.replace("'", "''")
        return (
            f'"{column}" = \'{safe}\' OR '
            f'\'{safe}\' LIKE "{column}" || \'%\' OR '
            f'"{column}" LIKE \'{safe}%\''
        )

    def _build_lot_filter(self, oaza_name: str):
        """Build a QgsFeatureRequest for lot layer filtered by oaza.

        Returns (request, oaza_geom):
          - attribute mode: request with oaza filter expression, oaza_geom=None
          - spatial mode: request with bounding box filter, oaza_geom or None
        """
        mode = self._link_config.get('match_mode', 'spatial')
        oaza_col = self._link_config.get('oaza_column', '')

        if mode == 'attribute':
            if oaza_col and oaza_name:
                expr = self._oaza_filter_expr(oaza_col, oaza_name)
                return QgsFeatureRequest().setFilterExpression(expr), None
            return QgsFeatureRequest(), None

        # spatial mode: get oaza geometry from oaza_layer
        oaza_layer, _ = self._get_link_layers()
        oaza_geom = None
        if oaza_layer and oaza_col and oaza_name:
            geoms = [f.geometry() for f in oaza_layer.getFeatures(
                QgsFeatureRequest().setFilterExpression(
                    self._oaza_filter_expr(oaza_col, oaza_name)))]
            if geoms:
                oaza_geom = QgsGeometry.unaryUnion(geoms)

        if oaza_geom:
            return QgsFeatureRequest().setFilterRect(oaza_geom.boundingBox()), oaza_geom
        return QgsFeatureRequest(), None

    # Regex to strip suffixes: X(n/m), X（n/m）, Wn, Vn, -内
    _CHIBAN_SUFFIX_RE = re.compile(
        r'(?:X[（(]\d+/\d+[）)]|[WV]\d+|-内)$'
    )

    @staticmethod
    def _parse_chiban(chiban) -> tuple:
        """Parse chiban into (parent, branch).

        Strips variant/split suffixes first (handles stacking):
          '3558X(4/4)' → ('3558', '')
          '3558-1X(4/4)' → ('3558', '1')
          '247-3W1' → ('247', '3')
          '1084-1-内' → ('1084', '1')
          '170-内W1' → ('170', '')
        Then splits on first '-' into (parent, branch).
        """
        s = str(chiban).strip() if chiban else ''
        # Strip suffixes iteratively (handles stacked: e.g. -内W1)
        while True:
            s2 = KozuMainWindow._CHIBAN_SUFFIX_RE.sub('', s)
            if s2 == s:
                break
            s = s2
        parts = s.split('-', 1)
        return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], '')

    @staticmethod
    def _match_level(src_parent: str, src_branch: str,
                     tgt_parent: str, tgt_branch: str) -> Optional[str]:
        """Return match level: 'exact', 'parent', 'near', or None."""
        src = f"{src_parent}-{src_branch}" if src_branch else src_parent
        tgt = f"{tgt_parent}-{tgt_branch}" if tgt_branch else tgt_parent
        if src == tgt:
            return 'exact'
        if src_parent == tgt_parent:
            return 'parent'
        # Near: same length, only last digit differs
        if (len(src_parent) == len(tgt_parent) > 0
                and src_parent[:-1] == tgt_parent[:-1]):
            return 'near'
        return None

    def _enabled_match_levels(self) -> set:
        """Return the set of match levels enabled by the checkboxes."""
        levels = set()
        if self.chkMatchExact.isChecked():
            levels.add('exact')
        if self.chkMatchParent.isChecked():
            levels.add('parent')
        if self.chkMatchNear.isChecked():
            levels.add('near')
        return levels

    # ─── Highlight Management ───────────────────────────────

    def _clear_highlights(self):
        """Remove all highlight overlays from both canvases."""
        logger.info(
            f"[Highlight] clearing: preview={len(self._highlights)}, "
            f"main={len(self._main_highlights)}"
        )
        for h in self._highlights:
            if not sip.isdeleted(h):
                h.hide()
                scene = self.map_canvas.scene()
                if scene:
                    scene.removeItem(h)
                sip.delete(h)
        self._highlights.clear()
        for h in self._main_highlights:
            if not sip.isdeleted(h):
                h.hide()
                scene = self.iface.mapCanvas().scene()
                if scene:
                    scene.removeItem(h)
                sip.delete(h)
        self._main_highlights.clear()
        # Force canvas repaint to remove visual artifacts
        self.map_canvas.refresh()
        self.iface.mapCanvas().refresh()

    def _add_highlight(self, canvas, geom, layer, level: str):
        """Create a QgsHighlight on the specified canvas."""
        outline_color, fill_color = MATCH_COLORS.get(level, MATCH_COLORS['near'])
        h = QgsHighlight(canvas, geom, layer)
        h.setColor(outline_color)
        h.setFillColor(fill_color)
        h.setWidth(2)
        h.show()
        return h

    # ─── Behavior A: Preview Click → Main Window ────────────

    def _on_preview_clicked(self, point: QgsPointXY):
        """Handle click on preview canvas: find parcel and highlight matches in main window."""
        logger.info(f"[BehaviorA] click at ({point.x():.3f}, {point.y():.3f})")

        if not self._link_config or not self.preview_layer:
            logger.info("[BehaviorA] aborted: no link_config or no preview_layer")
            return

        self._clear_highlights()

        # Find clicked feature in preview layer
        feat = self._find_feature_at_point(self.preview_layer, point, self.map_canvas)
        if not feat:
            logger.info("[BehaviorA] aborted: no feature found at click point")
            return

        src_chiban = feat['chiban'] or ''
        src_oaza = feat['oaza_name'] or ''
        src_parent, src_branch = self._parse_chiban(src_chiban)
        logger.info(
            f"[BehaviorA] clicked parcel: chiban={src_chiban}, oaza={src_oaza}, "
            f"parent={src_parent}, branch={src_branch}"
        )
        if not src_parent:
            logger.info("[BehaviorA] aborted: no parent chiban")
            return

        _, lot_layer = self._get_link_layers()
        if not lot_layer:
            logger.info("[BehaviorA] aborted: lot_layer not found")
            return

        parent_col = self._link_config.get('parent_column', '')
        branch_col = self._link_config.get('branch_column', '')
        municipality_col = self._link_config.get('municipality_column', '')

        # Build filtered request for lot layer (attribute or spatial)
        request, oaza_geom = self._build_lot_filter(src_oaza)

        # Search lot layer for matches
        enabled_levels = self._enabled_match_levels()
        best_level = None
        best_bbox = None
        best_lot_feat = None
        match_priority = {'exact': 0, 'parent': 1, 'near': 2}
        combined_extent = QgsRectangle()

        for lot_feat in lot_layer.getFeatures(request):
            # Spatial post-filter (only in spatial mode when oaza_geom exists)
            if oaza_geom and not lot_feat.geometry().intersects(oaza_geom):
                continue
            # Municipality post-filter: skip if lot municipality not in XML municipality
            if municipality_col and self._current_municipality:
                lot_muni = str(lot_feat[municipality_col]) if lot_feat[municipality_col] else ''
                if lot_muni and lot_muni not in self._current_municipality:
                    continue

            tgt_parent = str(lot_feat[parent_col]) if lot_feat[parent_col] else ''
            tgt_branch = str(lot_feat[branch_col]) if lot_feat[branch_col] else ''

            level = self._match_level(src_parent, src_branch, tgt_parent, tgt_branch)
            if level and level in enabled_levels:
                h = self._add_highlight(
                    self.iface.mapCanvas(), lot_feat.geometry(), lot_layer, level
                )
                self._main_highlights.append(h)
                bbox = lot_feat.geometry().boundingBox()
                combined_extent.combineExtentWith(bbox)

                if best_level is None or match_priority[level] < match_priority.get(best_level, 99):
                    best_level = level
                    best_bbox = bbox
                    best_lot_feat = lot_feat

        logger.info(
            f"[BehaviorA] match result: best_level={best_level}, "
            f"highlights={len(self._main_highlights)}, "
            f"is_arbitrary={self._is_arbitrary}"
        )
        if best_lot_feat:
            raw_target = best_lot_feat.geometry().centroid().asPoint()
            target_centroid = self._transform_point_to_preview_crs(
                raw_target, lot_layer.crs()
            )
            src_centroid = feat.geometry().centroid().asPoint()
            logger.info(
                f"[BehaviorA] align: lot CRS={lot_layer.crs().authid()}, "
                f"target=({target_centroid.x():.3f}, {target_centroid.y():.3f}), "
                f"source=({src_centroid.x():.3f}, {src_centroid.y():.3f}), "
                f"is_arbitrary={self._is_arbitrary}"
            )
            # Geometry translation (skip if tile lock is on to preserve manual adjustments)
            if not self.chkTileLock.isChecked():
                if self._is_arbitrary:
                    self._georeference_preview(target_centroid, source_centroid=src_centroid)
                else:
                    self._translate_preview(target_centroid, src_centroid)

            # Center main window on best match (keep current scale)
            best_center = best_lot_feat.geometry().centroid().asPoint()
            main_canvas = self.iface.mapCanvas()
            me = main_canvas.extent()
            hw = me.width() / 2
            hh = me.height() / 2
            main_canvas.setExtent(QgsRectangle(
                best_center.x() - hw, best_center.y() - hh,
                best_center.x() + hw, best_center.y() + hh
            ))
            main_canvas.refresh()

        # --- Step 2: Reverse highlight (main match → preview) ---
        if (self.chkMutualEnabled.isChecked()
                and best_lot_feat and best_level in ('exact', 'parent')):
            pivot_parent = str(best_lot_feat[parent_col]) if best_lot_feat[parent_col] else ''
            pivot_branch = str(best_lot_feat[branch_col]) if best_lot_feat[branch_col] else ''
            # exact pivot → full comparison; parent pivot → exact-only (shown as yellow)
            if best_level == 'exact':
                reverse_levels = set(enabled_levels)
                if not self.chkMutualNear.isChecked():
                    reverse_levels.discard('near')
            else:
                reverse_levels = {'exact'}

            for pf in self.preview_layer.getFeatures():
                pf_chiban = pf['chiban'] or ''
                pf_parent, pf_branch = self._parse_chiban(pf_chiban)
                rlevel = self._match_level(pivot_parent, pivot_branch, pf_parent, pf_branch)
                if rlevel and rlevel in reverse_levels:
                    # Degraded pivot: display exact matches as 'parent' (yellow)
                    display_level = rlevel if best_level == 'exact' else 'parent'
                    h = self._add_highlight(
                        self.map_canvas, pf.geometry(), self.preview_layer, display_level
                    )
                    self._highlights.append(h)

    # ─── Behavior B: Main Window Click → Preview ────────────

    def _on_enable_main_select(self, state: int):
        """Toggle main window selection mode."""
        if state == Qt.Checked:
            if not self._link_config:
                self.chkEnableMainSelect.setChecked(False)
                return
            self._activate_main_select_tool()
        else:
            self._clear_highlights()
            # Restore previous tool
            if self._prev_main_tool:
                self.iface.mapCanvas().setMapTool(self._prev_main_tool)
                self._prev_main_tool = None
            self._main_select_tool = None
            self.lblLinkStatus.setText("連携設定済み")

    def _activate_main_select_tool(self):
        """Activate the main select tool on the QGIS main canvas."""
        # Save current tool and set our tool
        self._prev_main_tool = self.iface.mapCanvas().mapTool()
        self._main_select_tool = MainSelectTool(self.iface.mapCanvas())
        self._main_select_tool.selected.connect(self._on_main_clicked)
        self.iface.mapCanvas().setMapTool(self._main_select_tool)

        # Set the lot layer as active so clicks hit it
        _, lot_layer = self._get_link_layers()
        if lot_layer:
            self.iface.setActiveLayer(lot_layer)

        # Zoom main window to oaza extent and show chiban range
        self._zoom_main_to_oaza()

    def _zoom_main_to_oaza(self):
        """Zoom main canvas to lot features matching the XML's parent numbers within the oaza."""
        if not self.preview_layer or not self._link_config:
            return

        # Collect oaza name and parent number set from preview features
        oaza_names = set()
        parent_set = set()
        for feat in self.preview_layer.getFeatures():
            oaza = feat['oaza_name']
            if oaza:
                oaza_names.add(oaza)
            chiban = feat['chiban'] or ''
            parent, _ = self._parse_chiban(chiban)
            if parent and parent.isdigit():
                parent_set.add(parent)

        if not oaza_names:
            return

        # Build chiban range info
        if parent_set:
            sorted_parents = sorted(int(p) for p in parent_set)
            range_str = f"地番: {sorted_parents[0]}〜{sorted_parents[-1]}"
        else:
            range_str = "地番: (数値なし)"

        oaza_list = ', '.join(sorted(oaza_names))
        self.lblLinkStatus.setText(f"{oaza_list} / {range_str}")

    def _on_main_clicked(self, point: QgsPointXY):
        """Handle click on main canvas: find feature and highlight matches in preview."""
        if not self._link_config or not self.preview_layer:
            return

        self._clear_highlights()

        _, lot_layer = self._get_link_layers()
        if not lot_layer:
            return

        # Find clicked feature in lot layer
        feat = self._find_feature_at_point(lot_layer, point, self.iface.mapCanvas())
        if not feat:
            return

        parent_col = self._link_config.get('parent_column', '')
        branch_col = self._link_config.get('branch_column', '')
        municipality_col = self._link_config.get('municipality_column', '')
        tgt_parent = str(feat[parent_col]) if feat[parent_col] else ''
        tgt_branch = str(feat[branch_col]) if feat[branch_col] else ''
        if not tgt_parent:
            return

        # Get centroid of clicked main window feature (for tile overlay)
        # Transform from lot layer CRS to preview CRS (EPSG:6676)
        raw_centroid = feat.geometry().centroid().asPoint()
        main_centroid = self._transform_point_to_preview_crs(raw_centroid, lot_layer.crs())
        logger.info(
            f"[BehaviorB] clicked lot: {tgt_parent}-{tgt_branch}, "
            f"lot_layer CRS: {lot_layer.crs().authid()}, "
            f"raw centroid: ({raw_centroid.x():.3f}, {raw_centroid.y():.3f}), "
            f"transformed centroid: ({main_centroid.x():.3f}, {main_centroid.y():.3f})"
        )

        # Step 1: Identify matching features (collect IDs and best match, no highlights yet)
        enabled_levels = self._enabled_match_levels()
        best_level = None
        best_feat_id = None
        match_priority = {'exact': 0, 'parent': 1, 'near': 2}
        matched_ids = {}  # feat_id → level

        for preview_feat in self.preview_layer.getFeatures():
            src_chiban = preview_feat['chiban'] or ''
            src_parent, src_branch = self._parse_chiban(src_chiban)

            level = self._match_level(src_parent, src_branch, tgt_parent, tgt_branch)
            if level and level in enabled_levels:
                matched_ids[preview_feat.id()] = level
                if best_level is None or match_priority[level] < match_priority.get(best_level, 99):
                    best_level = level
                    best_feat_id = preview_feat.id()

        if not matched_ids:
            return

        # Step 2: Translate preview geometries to align with main (BEFORE creating highlights)
        # Skip if tile lock is on to preserve manual adjustments
        if best_feat_id is not None and not self.chkTileLock.isChecked():
            best_f = self.preview_layer.getFeature(best_feat_id)
            src_centroid = best_f.geometry().centroid().asPoint()
            logger.info(
                f"[BehaviorB] align: target=({main_centroid.x():.3f}, {main_centroid.y():.3f}), "
                f"source=({src_centroid.x():.3f}, {src_centroid.y():.3f}), "
                f"is_arbitrary={self._is_arbitrary}"
            )
            if self._is_arbitrary:
                self._georeference_preview(main_centroid, source_centroid=src_centroid)
            else:
                self._translate_preview(main_centroid, src_centroid)

        # Step 3: Create highlights with correct (post-georef) coordinates
        best_feat = None
        combined_extent = QgsRectangle()
        for fid, level in matched_ids.items():
            pf = self.preview_layer.getFeature(fid)
            h = self._add_highlight(
                self.map_canvas, pf.geometry(), self.preview_layer, level
            )
            self._highlights.append(h)
            combined_extent.combineExtentWith(pf.geometry().boundingBox())
            if fid == best_feat_id:
                best_feat = pf

        # Center preview on best match (keep current scale)
        if best_feat:
            best_center = best_feat.geometry().centroid().asPoint()
            pe = self.map_canvas.extent()
            hw = pe.width() / 2
            hh = pe.height() / 2
            self._scale_guard = True
            self.map_canvas.setExtent(QgsRectangle(
                best_center.x() - hw, best_center.y() - hh,
                best_center.x() + hw, best_center.y() + hh
            ))
            self._scale_guard = False

        self.map_canvas.refresh()

        # --- Step 2: Reverse highlight (preview match → main window) ---
        if (self.chkMutualEnabled.isChecked()
                and best_feat and best_level in ('exact', 'parent')):
            pivot_chiban = best_feat['chiban'] or ''
            pivot_parent, pivot_branch = self._parse_chiban(pivot_chiban)
            # exact pivot → full comparison; parent pivot → exact-only (shown as yellow)
            if best_level == 'exact':
                reverse_levels = set(enabled_levels)
                if not self.chkMutualNear.isChecked():
                    reverse_levels.discard('near')
            else:
                reverse_levels = {'exact'}

            _, lot_layer2 = self._get_link_layers()
            if lot_layer2:
                pivot_oaza = best_feat['oaza_name'] or ''
                req, oaza_geom = self._build_lot_filter(pivot_oaza)

                main_combined = QgsRectangle()
                best_main_bbox = None
                for lf in lot_layer2.getFeatures(req):
                    if oaza_geom and not lf.geometry().intersects(oaza_geom):
                        continue
                    if municipality_col and self._current_municipality:
                        lf_muni = str(lf[municipality_col]) if lf[municipality_col] else ''
                        if lf_muni and lf_muni not in self._current_municipality:
                            continue
                    lf_parent = str(lf[parent_col]) if lf[parent_col] else ''
                    lf_branch = str(lf[branch_col]) if lf[branch_col] else ''
                    rlevel = self._match_level(pivot_parent, pivot_branch, lf_parent, lf_branch)
                    if rlevel and rlevel in reverse_levels:
                        display_level = rlevel if best_level == 'exact' else 'parent'
                        h = self._add_highlight(
                            self.iface.mapCanvas(), lf.geometry(), lot_layer2, display_level
                        )
                        self._main_highlights.append(h)
                        main_combined.combineExtentWith(lf.geometry().boundingBox())
                        if rlevel == 'exact' and best_main_bbox is None:
                            best_main_bbox = lf.geometry().boundingBox()

                self.iface.mapCanvas().refresh()

    # ─── Tile Overlay (Georeference Arbitrary → Real) ───────

    def _georeference_preview(self, target_centroid: QgsPointXY,
                              source_centroid: Optional[QgsPointXY] = None):
        """Convert arbitrary-coordinate preview to real coordinates.

        Aligns source_centroid (arbitrary mm coords) to target_centroid (EPSG:6676).
        If source_centroid is None, falls back to average centroid of all features.
        """
        if not self.preview_layer:
            return

        scale_m = self._current_scale / 1000.0  # mm → m

        if source_centroid:
            ref_x = source_centroid.x()
            ref_y = source_centroid.y()
        else:
            # Fallback: average centroid of all preview features
            all_points = []
            for feat in self.preview_layer.getFeatures():
                centroid = feat.geometry().centroid()
                if centroid and not centroid.isNull():
                    all_points.append(centroid.asPoint())
            if not all_points:
                return
            ref_x = sum(p.x() for p in all_points) / len(all_points)
            ref_y = sum(p.y() for p in all_points) / len(all_points)

        # Transform: scale from mm to m, then translate to target centroid
        dx = target_centroid.x() - ref_x * scale_m
        dy = target_centroid.y() - ref_y * scale_m

        logger.info(
            f"[Georef] scale=1:{self._current_scale}, scale_m={scale_m}, "
            f"ref(mm)=({ref_x:.3f}, {ref_y:.3f}), "
            f"target(m)=({target_centroid.x():.3f}, {target_centroid.y():.3f}), "
            f"offset dx={dx:.3f}, dy={dy:.3f}"
        )

        # Update all feature geometries: scale (mm → m) + translate
        self.preview_layer.startEditing()
        ref_verified = False
        for feat in self.preview_layer.getFeatures():
            geom = feat.geometry()
            new_wkt = self._transform_geometry_wkt(geom.asWkt(), scale_m, dx, dy)
            new_geom = QgsGeometry.fromWkt(new_wkt)
            if new_geom and not new_geom.isNull():
                self.preview_layer.changeGeometry(feat.id(), new_geom)
                # Log the reference feature's transform result for verification
                if not ref_verified and source_centroid:
                    old_c = geom.centroid().asPoint()
                    new_c = new_geom.centroid().asPoint()
                    if (abs(old_c.x() - ref_x) < 1.0 and abs(old_c.y() - ref_y) < 1.0):
                        logger.info(
                            f"[Georef] ref feature: "
                            f"old=({old_c.x():.3f}, {old_c.y():.3f}) → "
                            f"new=({new_c.x():.3f}, {new_c.y():.3f}), "
                            f"expected≈({target_centroid.x():.3f}, {target_centroid.y():.3f})"
                        )
                        ref_verified = True

        self.preview_layer.commitChanges()

        # Update canvas extent to new coordinate space BEFORE enabling tile
        self._is_arbitrary = False
        self._max_zoom_out_scale = 0  # Temporarily disable zoom limit
        extent = self.preview_layer.extent()
        extent.scale(1.1)
        self.map_canvas.setExtent(extent)

        # Enable tile (unless locked)
        self.chkShowTile.setEnabled(True)
        if not self.chkTileLock.isChecked():
            self.chkShowTile.setChecked(True)
        self.lblTileStatus.setText("座標変換済み - タイル表示可能")
        self.lblPreviewCrs.setText("CRS: EPSG:6676 (座標変換済み)")

        # Enable GPKG export (now scaled)
        self._update_export_gpkg_state()

        # Recalculate zoom-out limit for new coordinate space
        self._update_zoom_limit()
        self.map_canvas.refresh()

        # Sync preview scale to main canvas (initial sync after georef)
        main_scale = self.iface.mapCanvas().scale()
        self._scale_sync_guard = True
        self._scale_guard = True
        self.map_canvas.zoomScale(main_scale)
        self._scale_guard = False
        self._scale_sync_guard = False

    @staticmethod
    def _transform_geometry_wkt(wkt: str, scale: float, dx: float, dy: float) -> str:
        """Transform WKT geometry: scale each coordinate then translate."""
        import re as re_mod

        def replace_coord(match):
            x = float(match.group(1)) * scale + dx
            y = float(match.group(2)) * scale + dy
            return f"{x} {y}"

        # Match coordinate pairs (two consecutive numbers separated by space)
        return re_mod.sub(
            r'(-?[\d.]+(?:[eE][+-]?\d+)?)\s+(-?[\d.]+(?:[eE][+-]?\d+)?)',
            replace_coord,
            wkt
        )

    def _translate_preview(self, target_centroid: QgsPointXY,
                           source_centroid: QgsPointXY):
        """Translate all preview geometries so source_centroid aligns with target_centroid.

        Used for public coordinate data where no scale conversion is needed.
        Only a simple (dx, dy) translation is applied.
        """
        if not self.preview_layer:
            logger.info("[Translate] aborted: no preview_layer")
            return

        dx = target_centroid.x() - source_centroid.x()
        dy = target_centroid.y() - source_centroid.y()

        logger.info(
            f"[Translate] source=({source_centroid.x():.3f}, {source_centroid.y():.3f}), "
            f"target=({target_centroid.x():.3f}, {target_centroid.y():.3f}), "
            f"offset dx={dx:.3f}, dy={dy:.3f}"
        )

        count = 0
        self.preview_layer.startEditing()
        for feat in self.preview_layer.getFeatures():
            geom = feat.geometry()
            old_c = geom.centroid().asPoint()
            geom.translate(dx, dy)
            self.preview_layer.changeGeometry(feat.id(), geom)
            count += 1
            if count == 1:
                new_c = geom.centroid().asPoint()
                logger.info(
                    f"[Translate] first feature: "
                    f"({old_c.x():.3f}, {old_c.y():.3f}) → "
                    f"({new_c.x():.3f}, {new_c.y():.3f})"
                )
        ok = self.preview_layer.commitChanges()
        logger.info(f"[Translate] committed {count} features, success={ok}")

        # Shift the current view to follow the translation (don't zoom to full extent)
        current_extent = self.map_canvas.extent()
        shifted_extent = QgsRectangle(
            current_extent.xMinimum() + dx,
            current_extent.yMinimum() + dy,
            current_extent.xMaximum() + dx,
            current_extent.yMaximum() + dy,
        )
        self._scale_guard = True
        self.map_canvas.setExtent(shifted_extent)
        self.map_canvas.refresh()
        self._scale_guard = False

        # Recalculate zoom-out limit
        self._update_zoom_limit()

    # ─── Ctrl+Drag Position Adjustment ──────────────────────

    def _on_position_adjusted(self, dx: float, dy: float):
        """Handle Ctrl+drag to move preview features."""
        if not self.preview_layer:
            return

        # Clear stale highlights (they don't move with features)
        self._clear_highlights()

        self.preview_layer.startEditing()
        for feat in self.preview_layer.getFeatures():
            geom = feat.geometry()
            geom.translate(dx, dy)
            self.preview_layer.changeGeometry(feat.id(), geom)
        self.preview_layer.commitChanges()

        # Update zoom-out limit to track moved features
        self._update_zoom_limit()
        self.map_canvas.refresh()

    # ─── Spatial Query Helper ───────────────────────────────

    @staticmethod
    def _find_feature_at_point(layer: QgsVectorLayer, point: QgsPointXY,
                               canvas: QgsMapCanvas) -> Optional[QgsFeature]:
        """Find the feature at a clicked point using a small search rectangle.

        Handles CRS differences between canvas and layer.
        """
        # Transform point from canvas CRS to layer CRS
        canvas_crs = canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs.isValid() and layer_crs.isValid() and canvas_crs != layer_crs:
            transform = QgsCoordinateTransform(
                canvas_crs, layer_crs, QgsProject.instance()
            )
            search_point = transform.transform(point)
        else:
            search_point = point

        # Search tolerance: 10 pixels in map units (in layer CRS)
        map_units_per_pixel = canvas.mapUnitsPerPixel()
        tolerance = map_units_per_pixel * 10

        # If CRS units differ significantly, adjust tolerance
        if canvas_crs.isValid() and layer_crs.isValid() and canvas_crs != layer_crs:
            # Use a generous tolerance in layer units
            layer_extent = layer.extent()
            if layer_extent.width() > 0 and canvas.extent().width() > 0:
                scale_ratio = layer_extent.width() / canvas.extent().width()
                tolerance = tolerance * scale_ratio

        search_rect = QgsRectangle(
            search_point.x() - tolerance, search_point.y() - tolerance,
            search_point.x() + tolerance, search_point.y() + tolerance
        )

        request = QgsFeatureRequest().setFilterRect(search_rect)
        search_geom = QgsGeometry.fromPointXY(search_point)
        best_feat = None
        best_dist = float('inf')

        for feat in layer.getFeatures(request):
            geom = feat.geometry()
            if geom.contains(search_geom):
                return QgsFeature(feat)
            dist = geom.distance(search_geom)
            if dist < best_dist:
                best_dist = dist
                best_feat = QgsFeature(feat)

        return best_feat

    # ─── Tile Controls ──────────────────────────────────────

    def _on_tile_toggle(self, state: int):
        """Handle tile visibility toggle."""
        show_tile = state == Qt.Checked
        self.comboTileSource.setEnabled(show_tile)

        if show_tile:
            self._load_tile_layer()
        else:
            self.tile_layer = None
            self._update_canvas_layers()
        self.map_canvas.refresh()

    def _on_tile_source_changed(self, index: int):
        """Handle tile source change."""
        if self.chkShowTile.isChecked():
            self._load_tile_layer()

    def _load_tile_layer(self):
        """Load tile layer based on current selection."""
        tile_name = self.comboTileSource.currentText()
        tile_url = GSI_TILE_URLS.get(tile_name)
        if not tile_url:
            return

        uri = f"type=xyz&url={tile_url}&zmax=18&zmin=2"
        self.tile_layer = QgsRasterLayer(uri, tile_name, "wms")

        if self.tile_layer.isValid():
            self._update_canvas_layers()
            self.map_canvas.refresh()

    def _update_canvas_layers(self):
        """Rebuild the preview canvas layer stack.

        Order (top to bottom): preview_layer → overlay_tile → tile_layer
        """
        layers = []
        if self.preview_layer:
            layers.append(self.preview_layer)
        if self.overlay_tile_layer and self.chkOverlayTile.isChecked():
            layers.append(self.overlay_tile_layer)
        if self.tile_layer and self.chkShowTile.isChecked():
            layers.append(self.tile_layer)
        self.map_canvas.setLayers(layers)

    # ─── Overlay Tile Controls ──────────────────────────────

    def _on_overlay_tile_toggle(self, state: int):
        """Handle overlay tile visibility toggle."""
        if state == Qt.Checked:
            self._load_overlay_tile()
        else:
            self.overlay_tile_layer = None
            self._update_canvas_layers()
        self.map_canvas.refresh()

    def _on_overlay_source_changed(self, index: int):
        """Handle overlay source combo selection change."""
        if self.chkOverlayTile.isChecked():
            self._load_overlay_tile()

    def _on_overlay_opacity_changed(self, value: int):
        """Handle overlay opacity change."""
        if self.overlay_tile_layer:
            self.overlay_tile_layer.renderer().setOpacity(1.0 - value / 100.0)
            self.map_canvas.refresh()

    def _populate_overlay_sources(self):
        """Populate overlay source combo with XYZ connections and project layers."""
        combo = self.comboOverlaySource
        prev_text = combo.currentText()

        combo.blockSignals(True)
        combo.clear()

        # Section 1: XYZ connections from QGIS settings
        xyz_items = self._get_xyz_connections()
        if xyz_items:
            combo.addItem("── XYZ接続 ──")
            combo.model().item(combo.count() - 1).setEnabled(False)
            for name, meta in xyz_items:
                combo.addItem(name, meta)

        # Section 2: Raster layers from current project
        raster_items = self._get_project_raster_layers()
        if raster_items:
            combo.addItem("── プロジェクトレイヤー ──")
            combo.model().item(combo.count() - 1).setEnabled(False)
            for name, meta in raster_items:
                combo.addItem(name, meta)

        # Restore previous selection
        if prev_text:
            idx = combo.findText(prev_text)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(-1)

        combo.blockSignals(False)

    @staticmethod
    def _get_xyz_connections() -> list:
        """Read XYZ tile connections from QGIS global settings.

        Returns list of (display_name, metadata_dict) tuples.
        """
        results = []
        settings = QSettings()
        settings.beginGroup('qgis/connections-xyz')
        for name in sorted(settings.childGroups()):
            settings.beginGroup(name)
            url = settings.value('url', '')
            zmin = settings.value('zmin', 0, type=int)
            zmax = settings.value('zmax', 18, type=int)
            settings.endGroup()
            if url:
                meta = {
                    'type': 'xyz',
                    'url': url,
                    'zmin': zmin,
                    'zmax': zmax,
                }
                results.append((name, meta))
        settings.endGroup()
        return results

    @staticmethod
    def _get_project_raster_layers() -> list:
        """Collect raster layers from the current QGIS project.

        Returns list of (display_name, metadata_dict) tuples.
        """
        results = []
        for layer_id, layer in QgsProject.instance().mapLayers().items():
            if isinstance(layer, QgsRasterLayer):
                meta = {
                    'type': 'project_layer',
                    'layer_id': layer_id,
                    'source': layer.source(),
                    'provider': layer.providerType(),
                }
                results.append((layer.name(), meta))
        return results

    def _load_overlay_tile(self):
        """Load overlay tile layer from the selected combo source."""
        meta = self.comboOverlaySource.currentData()
        if not meta:
            self.overlay_tile_layer = None
            self._update_canvas_layers()
            self.map_canvas.refresh()
            return

        source_type = meta.get('type')
        layer = None
        display_name = self.comboOverlaySource.currentText()

        if source_type == 'xyz':
            url = meta['url']
            zmin = meta.get('zmin', 0)
            zmax = meta.get('zmax', 18)
            uri = f"type=xyz&url={url}&zmax={zmax}&zmin={zmin}"
            layer = QgsRasterLayer(uri, f"オーバーレイ: {display_name}", "wms")

        elif source_type == 'project_layer':
            # Clone from existing project layer to preserve style/renderer
            layer_id = meta.get('layer_id', '')
            src_layer = QgsProject.instance().mapLayer(layer_id)
            if src_layer:
                layer = src_layer.clone()
                layer.setName(f"オーバーレイ: {display_name}")
            else:
                # Fallback: create from source/provider
                layer = QgsRasterLayer(
                    meta['source'], f"オーバーレイ: {display_name}", meta['provider']
                )

        if layer and layer.isValid():
            opacity = self.spinOverlayOpacity.value()
            layer.renderer().setOpacity(1.0 - opacity / 100.0)
            self.overlay_tile_layer = layer
            self._update_canvas_layers()
            self.map_canvas.refresh()
        else:
            logger.warning(f"[Overlay] failed to load source: {display_name}")
            self.overlay_tile_layer = None

    # ─── PDF Export ──────────────────────────────────────────

    def _on_export_pdf(self):
        """Show PDF export dialog and generate PDF."""
        if not self.preview_layer:
            QMessageBox.warning(self, "PDF出力", "プレビューデータがありません。")
            return

        # Build default values
        oaza_default = self.comboOaza.currentText()
        if oaza_default == "（大字を選択）":
            oaza_default = ""
        scale_default = f"1:{self._current_scale}" if self._current_scale else ""

        # --- Modal dialog ---
        dlg = QDialog(self)
        dlg.setWindowTitle("PDF出力設定")
        dlg.setMinimumWidth(350)
        form = QFormLayout(dlg)

        edit_title = QLineEdit(dlg)
        edit_title.setPlaceholderText("タイトルを入力...")
        form.addRow("タイトル:", edit_title)

        edit_scale = QLineEdit(scale_default, dlg)
        form.addRow("縮尺:", edit_scale)

        edit_oaza = QLineEdit(oaza_default, dlg)
        form.addRow("大字名:", edit_oaza)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg
        )
        buttons.button(QDialogButtonBox.Ok).setText("出力")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec_() != QDialog.Accepted:
            return

        title = edit_title.text().strip()
        scale_text = edit_scale.text().strip()
        oaza_text = edit_oaza.text().strip()

        # Choose save path
        save_path, _ = QFileDialog.getSaveFileName(
            self, "PDF出力先", "", "PDF (*.pdf)"
        )
        if not save_path:
            return

        self._render_pdf(save_path, title, scale_text, oaza_text)

    def _render_pdf(self, path: str, title: str, scale_text: str, oaza_text: str):
        """Render the current preview canvas to a PDF file."""
        from qgis.PyQt.QtCore import QSize

        # --- Page setup: A4 Landscape ---
        page_size = QPageSize(QPageSize.A4)
        margin = QMarginsF(15, 15, 15, 15)  # mm
        layout = QPageLayout(page_size, QPageLayout.Landscape, margin,
                             QPageLayout.Millimeter)

        writer = None
        try:
            from qgis.PyQt.QtGui import QPdfWriter
        except ImportError:
            pass
        if writer is None:
            try:
                from qgis.PyQt.QtPrintSupport import QPdfWriter
            except ImportError:
                pass

        # Resolve QPdfWriter
        try:
            writer = QPdfWriter(path)
        except Exception:
            QMessageBox.critical(self, "PDF出力", "QPdfWriterの初期化に失敗しました。")
            return

        writer.setPageLayout(layout)
        writer.setResolution(300)

        dpi = 300
        painter = QPainter()
        if not painter.begin(writer):
            QMessageBox.critical(self, "PDF出力", "PDFの書き込みを開始できません。")
            return

        try:
            page_rect = painter.viewport()  # full page in device pixels
            # Convert margins from mm to device pixels
            margin_px = int(15 * dpi / 25.4)

            # Drawing area (inside margins)
            draw_left = margin_px
            draw_top = margin_px
            draw_w = page_rect.width() - 2 * margin_px
            draw_h = page_rect.height() - 2 * margin_px

            # --- Header ---
            header_h = int(12 * dpi / 25.4)  # 12mm height
            header_font = QFont("Noto Sans CJK JP", 10)
            header_font.setPixelSize(int(3.5 * dpi / 25.4))  # ~3.5mm
            painter.setFont(header_font)

            # Title (left)
            if title:
                painter.drawText(draw_left, draw_top, draw_w // 2, header_h,
                                 Qt.AlignLeft | Qt.AlignVCenter, title)
            # Scale (center)
            if scale_text:
                painter.drawText(draw_left, draw_top, draw_w, header_h,
                                 Qt.AlignHCenter | Qt.AlignVCenter,
                                 f"縮尺: {scale_text}")
            # Oaza (right)
            if oaza_text:
                painter.drawText(draw_left, draw_top, draw_w, header_h,
                                 Qt.AlignRight | Qt.AlignVCenter,
                                 f"大字: {oaza_text}")

            # --- Map area ---
            map_top = draw_top + header_h + int(2 * dpi / 25.4)  # 2mm gap
            map_h = draw_h - header_h - int(2 * dpi / 25.4)
            map_rect = QRectF(draw_left, map_top, draw_w, map_h)

            # --- Double-line border (0.3pt black) ---
            pen03 = QPen(QColor(0, 0, 0))
            line_w = 0.3 * dpi / 72.0  # 0.3pt in device pixels
            pen03.setWidthF(line_w)
            painter.setPen(pen03)
            painter.setBrush(Qt.NoBrush)

            # Outer border
            painter.drawRect(map_rect)

            # Inner border (1.5pt inset)
            inset = 1.5 * dpi / 72.0  # 1.5pt in device pixels
            inner_rect = map_rect.adjusted(inset, inset, -inset, -inset)
            painter.drawRect(inner_rect)

            # --- Render map inside inner border (clipped) ---
            map_margin = inset + line_w
            render_rect = inner_rect.adjusted(map_margin, map_margin,
                                              -map_margin, -map_margin)

            # Set up map settings from current canvas
            ms = QgsMapSettings()
            ms.setDestinationCrs(self.map_canvas.mapSettings().destinationCrs())
            ms.setExtent(self.map_canvas.extent())
            ms.setOutputSize(QSize(int(render_rect.width()), int(render_rect.height())))
            ms.setOutputDpi(dpi)
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setLayers(self.map_canvas.layers())

            # Clip to inner border and render map
            painter.save()
            painter.setClipRect(render_rect)
            painter.translate(render_rect.topLeft())
            job = QgsMapRendererCustomPainterJob(ms, painter)
            job.start()
            job.waitForFinished()
            painter.restore()

            # Redraw borders on top (in case map bleeds into border area)
            painter.setPen(pen03)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(map_rect)
            painter.drawRect(inner_rect)

        finally:
            painter.end()

        QMessageBox.information(self, "PDF出力", f"PDFを出力しました:\n{path}")

    # ─── GPKG Export ────────────────────────────────────────

    def _update_export_gpkg_state(self):
        """Enable/disable GPKG export button based on preview state."""
        enabled = (
            self.preview_layer is not None
            and self.preview_layer.featureCount() > 0
            and not self._is_arbitrary
        )
        self.btnExportGpkg.setEnabled(enabled)

    def _on_export_gpkg(self):
        """Export scaled preview data to GeoPackage."""
        if not self.preview_layer or self._is_arbitrary:
            QMessageBox.warning(
                self, "エクスポート",
                "スケール済みのプレビューデータがありません。"
            )
            return

        oaza_name = self.comboOaza.currentText()
        if oaza_name == "（大字を選択）":
            oaza_name = "export"

        save_path, _ = QFileDialog.getSaveFileName(
            self, "GeoPackageエクスポート",
            str(Path.home() / f"{oaza_name}.gpkg"),
            "GeoPackage (*.gpkg)"
        )
        if not save_path:
            return

        try:
            self._write_gpkg(save_path)
            QMessageBox.information(
                self, "エクスポート",
                f"GeoPackageを出力しました:\n{save_path}"
            )
        except Exception as e:
            logger.error(f"GPKG export failed: {e}")
            QMessageBox.critical(
                self, "エクスポート",
                f"エクスポート中にエラーが発生しました:\n{e}"
            )

    def _write_gpkg(self, path: str):
        """Write current preview features to a GeoPackage file."""
        from qgis.core import QgsVectorFileWriter, QgsFields

        crs = self.map_canvas.mapSettings().destinationCrs()

        # Define output fields
        fields = QgsFields()
        fields.append(QgsField("oaza_name", QVariant.String))
        fields.append(QgsField("chiban", QVariant.String))
        fields.append(QgsField("est_area", QVariant.Double))

        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, self.preview_layer.wkbType(),
            crs, "GPKG"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            raise RuntimeError(writer.errorMessage())

        for feat in self.preview_layer.getFeatures():
            out_feat = QgsFeature()
            out_feat.setGeometry(feat.geometry())
            out_feat.setAttributes([
                feat["oaza_name"],
                feat["chiban"],
                feat["est_area"],
            ])
            writer.addFeature(out_feat)

        del writer  # Flush and close

    # ─── Lifecycle ──────────────────────────────────────────

    def closeEvent(self, event):
        """Handle window close (hide, not destroy). State is preserved."""
        self._save_settings()

        # Restore main canvas tool if we changed it
        if self._prev_main_tool:
            self.iface.mapCanvas().setMapTool(self._prev_main_tool)
            self._prev_main_tool = None

        self._clear_highlights()

        # DB connection and UI state are preserved for re-show
        event.accept()

    def showEvent(self, event):
        """Handle window re-show. Restore main select tool if needed."""
        super().showEvent(event)
        # Re-activate main select tool if checkbox is still checked
        if self.chkEnableMainSelect.isChecked() and self._link_config:
            self._activate_main_select_tool()
