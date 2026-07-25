# -*- coding: utf-8 -*-
"""
Export Panel Controller for KozuXmlIntegrator

Handles the export tab UI logic:
- Oaza selection
- GeoPackage export
"""

from pathlib import Path
from typing import Optional
import logging

from qgis.PyQt.QtWidgets import (
    QFileDialog, QMessageBox
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
)

from ..core import DatabaseManager

logger = logging.getLogger(__name__)


class ExportWorker(QObject):
    """
    Worker thread for running export in background.
    """

    progress = pyqtSignal(str)  # status message
    finished = pyqtSignal(str)  # output file path
    error = pyqtSignal(str)

    def __init__(self, db_path: Path, output_path: Path, oaza_filter: Optional[str] = None):
        super().__init__()
        self.db_path = db_path
        self.output_path = output_path
        self.oaza_filter = oaza_filter

    def run(self):
        """Execute the export process."""
        try:
            self.progress.emit("データベースを読み込み中...")
            db = DatabaseManager(self.db_path)

            self.progress.emit("GeoPackageにエクスポート中...")
            db.export_to_geopackage(self.output_path, oaza_name=self.oaza_filter)

            self.finished.emit(str(self.output_path))

        except Exception as e:
            logger.error(f"Export error: {e}", exc_info=True)
            self.error.emit(str(e))


class ExportPanelController:
    """
    Controller for the Export tab in the dock widget.

    Manages:
    - Oaza selection for filtering
    - GeoPackage export
    """

    def __init__(self, dock_widget):
        """
        Initialize controller.

        Args:
            dock_widget: The main dock widget instance
        """
        self.dock = dock_widget
        self.db_path: Optional[Path] = None
        self.db: Optional[DatabaseManager] = None
        self.export_path: Optional[Path] = None

        self._worker: Optional[ExportWorker] = None
        self._thread: Optional[QThread] = None

        self._connect_signals()

    def _connect_signals(self):
        """Connect UI signals to handlers."""
        self.dock.btnSelectExportFile.clicked.connect(self._on_select_export_file)
        self.dock.btnExport.clicked.connect(self._on_export)

    def set_database(self, db_path: Path):
        """
        Set database for export operations.

        Args:
            db_path: Path to the database file
        """
        try:
            self.db_path = db_path
            self.db = DatabaseManager(db_path)

            # Run migrations for existing databases
            try:
                self.db.migrate_database()
            except Exception as e:
                logger.warning(f"Database migration warning: {e}")

            # Populate Oaza combo
            self._populate_oaza_combo()

            # Enable controls
            self.dock.comboExportOaza.setEnabled(True)

            logger.info(f"Export panel connected to database: {db_path}")

        except Exception as e:
            logger.error(f"Error setting database: {e}", exc_info=True)

    def _populate_oaza_combo(self):
        """Populate Oaza export combo."""
        self.dock.comboExportOaza.clear()
        self.dock.comboExportOaza.addItem("（全て）")

        if not self.db:
            return

        try:
            # Get unique Oaza names from database
            oaza_list = []
            with self.db.connection() as conn:
                cursor = conn.execute(
                    "SELECT DISTINCT oaza_name FROM t_fude_poly WHERE oaza_name IS NOT NULL ORDER BY oaza_name"
                )
                oaza_list = [row[0] for row in cursor.fetchall()]

            self.dock.comboExportOaza.addItems(oaza_list)

        except Exception as e:
            logger.error(f"Error getting Oaza list: {e}")

    def _on_select_export_file(self):
        """Handle export file selection."""
        default_name = "kozu_export.gpkg"
        oaza = self.dock.comboExportOaza.currentText()
        if oaza and oaza != "（全て）":
            default_name = f"kozu_{oaza}.gpkg"

        file_path, _ = QFileDialog.getSaveFileName(
            self.dock,
            "エクスポート先を指定",
            str(Path.home() / default_name),
            "GeoPackage (*.gpkg);;All Files (*.*)"
        )

        if file_path:
            self.export_path = Path(file_path)
            self.dock.lineEditExportFile.setText(str(self.export_path))
            self._update_export_button_state()

    def _update_export_button_state(self):
        """Enable/disable export button based on input validation."""
        can_export = (
            self.db_path is not None and
            self.db_path.exists() and
            self.export_path is not None
        )
        self.dock.btnExport.setEnabled(can_export)

    def _on_export(self):
        """Start the export process."""
        if self._thread and self._thread.isRunning():
            QMessageBox.warning(
                self.dock,
                "警告",
                "エクスポート処理が既に実行中です。"
            )
            return

        # Get Oaza filter
        oaza_filter = self.dock.comboExportOaza.currentText()
        if oaza_filter == "（全て）":
            oaza_filter = None

        # Create worker
        self._worker = ExportWorker(
            db_path=self.db_path,
            output_path=self.export_path,
            oaza_filter=oaza_filter
        )

        # Create thread
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Connect signals
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_export_progress)
        self._worker.finished.connect(self._on_export_finished)
        self._worker.error.connect(self._on_export_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)

        # Update UI state
        self.dock.btnExport.setEnabled(False)
        self.dock.btnExport.setText("エクスポート中...")

        # Start thread
        self._thread.start()

    def _on_export_progress(self, status: str):
        """Handle progress updates from worker."""
        logger.info(status)

    def _on_export_finished(self, output_path: str):
        """Handle successful export completion."""
        self.dock.btnExport.setText("GeoPackageにエクスポート")
        self._update_export_button_state()

        result = QMessageBox.question(
            self.dock,
            "エクスポート完了",
            f"GeoPackageへのエクスポートが完了しました。\n\n"
            f"ファイル: {output_path}\n\n"
            f"QGISにレイヤーを追加しますか?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )

        if result == QMessageBox.Yes:
            self._add_layer_to_canvas(Path(output_path))

    def _on_export_error(self, error_msg: str):
        """Handle export error."""
        self.dock.btnExport.setText("GeoPackageにエクスポート")
        self._update_export_button_state()

        QMessageBox.critical(
            self.dock,
            "エクスポートエラー",
            f"エクスポート中にエラーが発生しました:\n{error_msg}"
        )

    def _cleanup_thread(self):
        """Clean up worker and thread after completion."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None

    def _add_layer_to_canvas(self, gpkg_path: Path):
        """Add exported GeoPackage to QGIS canvas."""
        try:
            # Add fude_poly layer
            uri = f"{gpkg_path}|layername=fude_poly"
            layer = QgsVectorLayer(uri, gpkg_path.stem, "ogr")

            if layer.isValid():
                # Set CRS to JGD2011 Zone 8
                crs = QgsCoordinateReferenceSystem("EPSG:6676")
                layer.setCrs(crs)
                QgsProject.instance().addMapLayer(layer)
                logger.info(f"Added exported layer to canvas: {gpkg_path}")
            else:
                logger.warning(f"Failed to create valid layer from: {gpkg_path}")
                QMessageBox.warning(
                    self.dock,
                    "警告",
                    "レイヤーの追加に失敗しました。"
                )

        except Exception as e:
            logger.error(f"Error adding layer to canvas: {e}", exc_info=True)
            QMessageBox.warning(
                self.dock,
                "警告",
                f"レイヤーの追加中にエラーが発生しました:\n{e}"
            )
