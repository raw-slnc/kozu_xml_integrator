# -*- coding: utf-8 -*-
"""
Browse Panel Controller for KozuXmlIntegrator

Handles the browse/search tab UI logic:
- Database connection
- Oaza filtering
- Chiban search
- Zoom to selected parcel
"""

from pathlib import Path
from typing import Optional, List, Dict
import logging

from qgis.PyQt.QtWidgets import (
    QWidget, QFileDialog, QMessageBox, QTableWidgetItem
)
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsGeometry,
    QgsFeature,
)
from qgis.utils import iface

from ..core import (
    DatabaseManager,
    SearchIndex,
    SearchResult,
)

logger = logging.getLogger(__name__)


class BrowsePanelController:
    """
    Controller for the Browse/Search tab in the dock widget.

    Manages:
    - Database connection
    - Search functionality
    - Zoom to parcel
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
        self.search_index: Optional[SearchIndex] = None
        self.fude_layer: Optional[QgsVectorLayer] = None

        self._connect_signals()

    def _connect_signals(self):
        """Connect UI signals to handlers."""
        self.dock.btnSelectDatabase.clicked.connect(self._on_select_database)
        self.dock.comboOazaFilter.currentTextChanged.connect(self._on_oaza_changed)
        self.dock.btnSearch.clicked.connect(self._on_search)
        self.dock.lineEditChibanSearch.returnPressed.connect(self._on_search)
        self.dock.btnZoomToSelected.clicked.connect(self._on_zoom_to_selected)
        self.dock.tableResults.itemSelectionChanged.connect(self._on_selection_changed)

    def _on_select_database(self):
        """Handle database file selection."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "データベースを選択",
            str(Path.home()),
            "SQLite Database (*.sqlite);;All Files (*.*)"
        )

        if file_path:
            self._load_database(Path(file_path))

    def _load_database(self, db_path: Path):
        """Load database and populate UI."""
        try:
            self.db_path = db_path
            self.db = DatabaseManager(db_path)

            # Run migrations for existing databases
            try:
                self.db.migrate_database()
            except Exception as e:
                logger.warning(f"Database migration warning: {e}")

            # Build search index
            self.search_index = SearchIndex(self.db)
            self.search_index.build()

            self.dock.lineEditDatabase.setText(str(db_path))

            # Populate Oaza combo
            self._populate_oaza_combo()

            # Enable search controls
            self.dock.comboOazaFilter.setEnabled(True)
            self.dock.lineEditChibanSearch.setEnabled(True)
            self.dock.btnSearch.setEnabled(True)
            self.dock.tableResults.setEnabled(True)

            # Load fude layer for zoom functionality
            self._load_fude_layer()

            logger.info(f"Loaded database: {db_path}")

        except Exception as e:
            logger.error(f"Error loading database: {e}", exc_info=True)
            QMessageBox.critical(
                self.dock,
                "エラー",
                f"データベースの読み込みに失敗しました:\n{e}"
            )

    def _populate_oaza_combo(self):
        """Populate Oaza filter combo."""
        self.dock.comboOazaFilter.clear()
        self.dock.comboOazaFilter.addItem("（全て）")

        if self.search_index:
            oaza_list = self.search_index.get_oaza_list()
            self.dock.comboOazaFilter.addItems(oaza_list)

    def _load_fude_layer(self):
        """Load fude polygon layer for geometry operations."""
        if not self.db_path:
            return

        uri = f"{self.db_path}|layername=t_fude_poly"
        self.fude_layer = QgsVectorLayer(uri, "筆検索用", "ogr")

        if not self.fude_layer.isValid():
            logger.warning("Failed to load fude layer for search")
            self.fude_layer = None

    def add_public_crs_layer_to_project(self):
        """
        Add a layer showing only public coordinate (公共座標8系) data.

        This layer will be properly georeferenced with EPSG:6676.
        """
        if not self.db_path:
            QMessageBox.warning(
                self.dock,
                "警告",
                "先にデータベースを選択してください。"
            )
            return

        # Create layer with SQL subset filter
        uri = f"{self.db_path}|layername=t_fude_poly|subset=xml_meta_id IN (SELECT id FROM t_xml_meta WHERE crs_type = '公共座標8系')"  # nosec B608
        layer = QgsVectorLayer(uri, "公共座標8系_筆", "ogr")

        if not layer.isValid():
            QMessageBox.critical(
                self.dock,
                "エラー",
                "レイヤーの作成に失敗しました。"
            )
            return

        # Set CRS to EPSG:6676 (JGD2011 / Japan Plane Rectangular CS VIII)
        crs = QgsCoordinateReferenceSystem("EPSG:6676")
        layer.setCrs(crs)

        # Add to project
        QgsProject.instance().addMapLayer(layer)

        logger.info("Added public CRS layer (EPSG:6676) to project")
        QMessageBox.information(
            self.dock,
            "完了",
            f"公共座標8系レイヤーを追加しました。\n"
            f"CRS: EPSG:6676 (JGD2011 / Japan Plane Rectangular CS VIII)"
        )

    def add_arbitrary_crs_layer_to_project(self):
        """
        Add a layer showing only arbitrary coordinate (任意座標系) data.

        This layer will NOT be georeferenced - coordinates are local.
        """
        if not self.db_path:
            QMessageBox.warning(
                self.dock,
                "警告",
                "先にデータベースを選択してください。"
            )
            return

        uri = f"{self.db_path}|layername=t_fude_poly|subset=xml_meta_id IN (SELECT id FROM t_xml_meta WHERE crs_type = '任意座標系')"  # nosec B608
        layer = QgsVectorLayer(uri, "任意座標系_筆（変換前）", "ogr")

        if not layer.isValid():
            QMessageBox.critical(
                self.dock,
                "エラー",
                "レイヤーの作成に失敗しました。"
            )
            return

        # Add to project (no CRS set - local coordinates)
        QgsProject.instance().addMapLayer(layer)

        logger.info("Added arbitrary CRS layer to project")
        QMessageBox.information(
            self.dock,
            "完了",
            f"任意座標系レイヤーを追加しました。\n"
            f"注意: このデータは変換前のローカル座標です。"
        )

    def _on_oaza_changed(self, oaza: str):
        """Handle Oaza filter change."""
        # Clear search results when filter changes
        self.dock.tableResults.setRowCount(0)

    def _on_search(self):
        """Handle search button click."""
        if not self.search_index:
            return

        query = self.dock.lineEditChibanSearch.text().strip()
        oaza_filter = self.dock.comboOazaFilter.currentText()

        if oaza_filter == "（全て）":
            oaza_filter = None

        # Perform search
        if query:
            results = self.search_index.search_chiban(query, oaza=oaza_filter, limit=100)
        else:
            # If no query, show all in selected Oaza
            results = self._get_all_in_oaza(oaza_filter)

        self._populate_results(results)

    def _get_all_in_oaza(self, oaza: Optional[str], limit: int = 100) -> List[SearchResult]:
        """Get all parcels in an Oaza."""
        if not self.db:
            return []

        sql = """
            SELECT id, oaza_name, chiban, area_sqm
            FROM t_fude_poly
            WHERE 1=1
        """
        params = []

        if oaza:
            sql += " AND oaza_name = ?"
            params.append(oaza)

        sql += f" ORDER BY oaza_name, chiban LIMIT {limit}"

        results = []
        try:
            with self.db.connection() as conn:
                cursor = conn.execute(sql, params)
                for row in cursor.fetchall():
                    results.append(SearchResult(
                        fude_id=row[0],
                        oaza_name=row[1] or '',
                        chiban=row[2] or '',
                        area_sqm=row[3] or 0.0
                    ))
        except Exception as e:
            logger.error(f"Error getting parcels: {e}")

        return results

    def _populate_results(self, results: List[SearchResult]):
        """Populate results table."""
        self.dock.tableResults.setRowCount(0)

        for result in results:
            row = self.dock.tableResults.rowCount()
            self.dock.tableResults.insertRow(row)

            # Store fude_id in first column for retrieval
            oaza_item = QTableWidgetItem(result.oaza_name)
            oaza_item.setData(Qt.UserRole, result.fude_id)
            self.dock.tableResults.setItem(row, 0, oaza_item)

            self.dock.tableResults.setItem(row, 1, QTableWidgetItem(result.chiban))
            self.dock.tableResults.setItem(row, 2, QTableWidgetItem(f"{result.area_sqm:.2f}"))

        # Update status
        status = f"{len(results)}件の筆が見つかりました"
        logger.info(status)

    def _on_selection_changed(self):
        """Handle table selection change - auto zoom to selected parcel."""
        has_selection = len(self.dock.tableResults.selectedItems()) > 0
        self.dock.btnZoomToSelected.setEnabled(has_selection)

        # Auto-zoom when selection changes
        if has_selection:
            self._zoom_to_selected_parcel()

    def _on_zoom_to_selected(self):
        """Zoom to selected parcel (button handler)."""
        self._zoom_to_selected_parcel()

    def _zoom_to_selected_parcel(self):
        """Zoom to selected parcel with minimum scale 1:500."""
        selected_rows = self.dock.tableResults.selectionModel().selectedRows()
        if not selected_rows:
            return

        row = selected_rows[0].row()
        fude_id = self.dock.tableResults.item(row, 0).data(Qt.UserRole)

        if fude_id is None:
            return

        # Get geometry from database
        geom = self._get_fude_geometry(fude_id)
        if geom and not geom.isNull():
            canvas = iface.mapCanvas()

            # Transform geometry to map canvas CRS if needed
            src_crs = QgsCoordinateReferenceSystem("EPSG:6676")
            dst_crs = canvas.mapSettings().destinationCrs()
            if src_crs != dst_crs:
                transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                geom.transform(transform)

            # Zoom to geometry with buffer
            bbox = geom.boundingBox()
            bbox.scale(1.5)  # Add some padding

            # Set extent first
            canvas.setExtent(bbox)

            # Enforce minimum scale 1:500
            MIN_SCALE = 500
            if canvas.scale() < MIN_SCALE:
                canvas.zoomScale(MIN_SCALE)

            canvas.refresh()

            logger.info(f"Zoomed to fude {fude_id} (scale: 1:{canvas.scale():.0f})")

    def _get_fude_geometry(self, fude_id: int) -> Optional[QgsGeometry]:
        """Get geometry for a fude by ID."""
        if not self.fude_layer:
            return None

        # Query feature by ID
        for feature in self.fude_layer.getFeatures():
            if feature.id() == fude_id or feature['id'] == fude_id:
                return feature.geometry()

        # Try direct database query
        if self.db:
            try:
                with self.db.connection() as conn:
                    cursor = conn.execute(
                        "SELECT geom FROM t_fude_poly WHERE id = ?",
                        (fude_id,)
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        return QgsGeometry.fromWkt(row[0])
            except Exception as e:
                logger.error(f"Error getting geometry: {e}")

        return None

    def set_database(self, db_path: Path):
        """
        Set database from external source (e.g., after import).

        Args:
            db_path: Path to the database file
        """
        self._load_database(db_path)
