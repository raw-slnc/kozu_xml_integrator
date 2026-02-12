# -*- coding: utf-8 -*-
"""
Transform Panel Controller for KozuXmlIntegrator

Handles the coordinate transformation tab UI logic:
- Source map selection (arbitrary coordinate)
- Reference map matching (public coordinate)
- Transformation execution
"""

from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

from qgis.PyQt.QtWidgets import (
    QWidget, QMessageBox, QTableWidgetItem, QFileDialog
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, QObject, Qt
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)
from qgis.gui import QgsMapToolEmitPoint

from ..core import DatabaseManager
from ..transform import (
    HelmertTransformer,
    TPSTransformer,
    ChibanMatcher,
    TransformCandidateFinder,
    MatchCandidate,
    ControlPointPair,
)
from ..transform.spatial_fitting import (
    # New architecture for chain positioning
    TransformParams,
    PositioningGuideLoader,
    ChainPositioner,
    GeometryTransformer,
)

logger = logging.getLogger(__name__)


class MapSelectTool(QgsMapToolEmitPoint):
    """
    Map tool for selecting XML envelope on map.
    """

    selected = pyqtSignal(QgsPointXY)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas

    def canvasReleaseEvent(self, event):
        """Handle map click."""
        point = self.toMapCoordinates(event.pos())
        self.selected.emit(point)


class TransformWorker(QObject):
    """
    Worker thread for running transformation in background.
    """

    progress = pyqtSignal(int, str)  # percent, status message
    finished = pyqtSignal(dict)  # result dict
    error = pyqtSignal(str)

    def __init__(self, db: DatabaseManager,
                 source_xml_id: int,
                 control_points: List[ControlPointPair],
                 use_tps: bool = False,
                 exclude_outside_district: bool = True):
        super().__init__()
        self.db = db
        self.source_xml_id = source_xml_id
        self.control_points = control_points
        self.use_tps = use_tps
        self.exclude_outside_district = exclude_outside_district
        self._cancelled = False

    def run(self):
        """Execute the transformation process."""
        try:
            self.progress.emit(10, "制御点を準備中...")

            # Extract source and target points
            source_points = [cp.source_point for cp in self.control_points]
            target_points = [cp.target_point for cp in self.control_points]

            if len(source_points) < 2:
                self.error.emit("制御点が不足しています（最低2点必要）")
                return

            # Create transformer
            self.progress.emit(20, "変換パラメータを計算中...")

            if self.use_tps:
                transformer = TPSTransformer()
                transformer.compute_parameters(source_points, target_points)
            else:
                transformer = HelmertTransformer()
                result = transformer.compute_parameters(source_points, target_points)
                logger.info(f"Helmert RMSE: {result.rmse:.4f}")

            # Get parcels to transform
            self.progress.emit(30, "筆データを取得中...")
            all_parcels = self.db.get_fude_by_xml_id(self.source_xml_id)

            if not all_parcels:
                self.error.emit("変換対象の筆が見つかりません")
                return

            # Filter out "地区外" parcels if option is enabled
            skipped_outside_count = 0
            if self.exclude_outside_district:
                parcels = [p for p in all_parcels if '地区外' not in (p.get('chiban') or '')]
                skipped_outside_count = len(all_parcels) - len(parcels)
                if skipped_outside_count > 0:
                    logger.info(f"Excluded {skipped_outside_count} parcels with '地区外' chiban")
            else:
                parcels = all_parcels

            if not parcels:
                self.error.emit("変換対象の筆が見つかりません（全て「地区外」）")
                return

            # Transform each parcel
            total = len(parcels)
            transformed_count = 0

            for i, parcel in enumerate(parcels):
                if self._cancelled:
                    break

                progress_pct = 30 + int(60 * i / total)
                self.progress.emit(progress_pct, f"筆を変換中... ({i+1}/{total})")

                geom_wkt = parcel.get('geom_wkt', '')
                if not geom_wkt:
                    continue

                geom = QgsGeometry.fromWkt(geom_wkt)
                if geom.isEmpty():
                    continue

                # Transform geometry
                transformed_geom = transformer.transform_geometry(geom)

                if not transformed_geom.isEmpty():
                    # Update in database
                    self.db.update_fude_geometry(
                        parcel['id'],
                        transformed_geom.asWkt()
                    )
                    transformed_count += 1

            # Update xml_meta CRS type
            self.progress.emit(95, "メタデータを更新中...")
            self.db.update_xml_meta_crs(self.source_xml_id, '変換済み', 6676)

            self.progress.emit(100, "完了")

            self.finished.emit({
                'source_xml_id': self.source_xml_id,
                'transformed_count': transformed_count,
                'total_parcels': total,
                'skipped_outside_count': skipped_outside_count,
                'control_points_used': len(self.control_points),
            })

        except Exception as e:
            logger.error(f"Transform error: {e}", exc_info=True)
            self.error.emit(str(e))

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True


class TransformPanelController:
    """
    Controller for the Transform tab in the dock widget.

    Manages:
    - Source map selection
    - Reference map matching
    - Transformation execution
    """

    def __init__(self, dock_widget, iface=None):
        """
        Initialize controller.

        Args:
            dock_widget: The main dock widget instance
            iface: QGIS interface instance (optional)
        """
        self.dock = dock_widget
        self.iface = iface
        self.db: Optional[DatabaseManager] = None
        self.db_path: Optional[Path] = None

        self._worker: Optional[TransformWorker] = None
        self._thread: Optional[QThread] = None

        self._current_source_id: Optional[int] = None
        self._current_matches: List[MatchCandidate] = []
        self._current_control_points: List[ControlPointPair] = []

        # Map selection tool
        self._map_tool: Optional[MapSelectTool] = None
        self._previous_map_tool = None

        # Envelope layer for map selection
        self._envelope_layer: Optional[QgsVectorLayer] = None

        # Oaza coordinate system info cache
        self._oaza_crs_info: Dict[str, Dict[str, int]] = {}

        # Positioning guide loader for spatial fitting (PRIMARY method)
        # Supports both Oaza shapes (guides) and Municipality boundaries (containers)
        self._guide_loader: Optional[PositioningGuideLoader] = None
        self._chain_positioner: Optional[ChainPositioner] = None
        self._current_transform_params: Optional[TransformParams] = None
        self._oaza_shape_path: Optional[Path] = None

        self._connect_signals()

    def _connect_signals(self):
        """Connect UI signals to handlers."""
        # Database selection
        self.dock.btnSelectTransformDb.clicked.connect(self._on_select_database)

        # Oaza shape layer selection (PRIMARY transformation method)
        self.dock.btnSelectOazaShape.clicked.connect(self._on_select_oaza_shape)

        # Oaza filter
        self.dock.comboTransformOazaFilter.currentIndexChanged.connect(
            self._on_oaza_filter_changed
        )

        # Source selection
        self.dock.comboTransformSource.currentIndexChanged.connect(
            self._on_source_changed
        )
        self.dock.btnSelectOnMap.clicked.connect(self._on_select_on_map)

        # Spatial fitting (PRIMARY method) and chiban matching (helper)
        self.dock.btnSpatialFit.clicked.connect(self._on_spatial_fit)
        self.dock.btnFindMatches.clicked.connect(self._on_find_matches)

        # Match selection and transformation execution
        self.dock.tableMatchCandidates.itemSelectionChanged.connect(
            self._on_match_selected
        )
        self.dock.btnTransform.clicked.connect(self._on_transform)
        self.dock.btnTransformAll.clicked.connect(self._on_transform_all)

    def _on_select_database(self):
        """Handle database selection button click."""
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "データベースを開く",
            "",
            "SQLite Database (*.sqlite *.db);;All Files (*)"
        )

        if file_path:
            self.set_database(Path(file_path))
            self.dock.lineEditTransformDb.setText(file_path)

    def _on_select_oaza_shape(self):
        """Handle Oaza shape layer selection button click.

        This is the PRIMARY transformation reference - the "container" (器)
        into which arbitrary coordinate maps will be fitted.
        """
        file_path, _ = QFileDialog.getOpenFileName(
            self.dock,
            "大字形状レイヤーを選択（変換の器）",
            "",
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;All Files (*)"
        )

        if file_path:
            self._load_oaza_shape_layer(Path(file_path))

    def _load_oaza_shape_layer(self, gpkg_path: Path):
        """
        Load Oaza shape layer as positioning GUIDE (not exact target).

        Note: Oaza shapes are approximate guides, not exact containers.
        They have gaps (non-forest areas) and are based on forest planning maps.

        Args:
            gpkg_path: Path to Oaza_Shape.gpkg or similar
        """
        self._oaza_shape_path = gpkg_path
        self.dock.lineEditOazaShape.setText(str(gpkg_path))

        # Initialize positioning guide loader
        self._guide_loader = PositioningGuideLoader()

        if self._guide_loader.load_oaza_layer(str(gpkg_path)):
            oaza_names = self._guide_loader.get_oaza_names()
            self.dock.lblOazaShapeInfo.setText(
                f"読み込み完了: {len(oaza_names)}大字（位置ガイド）\n"
                f"※ガイドとして使用（公共座標図面が優先アンカー）"
            )
            self.dock.lblOazaShapeInfo.setStyleSheet("color: #008800; font-size: 11px;")

            # Enable spatial fitting button if source is selected
            if self._current_source_id is not None:
                self.dock.btnSpatialFit.setEnabled(True)

            logger.info(f"Loaded Oaza guide layer: {len(oaza_names)} oazas")
        else:
            self.dock.lblOazaShapeInfo.setText(
                "読み込みエラー: 大字形状レイヤーを開けませんでした"
            )
            self.dock.lblOazaShapeInfo.setStyleSheet("color: #880000; font-size: 11px;")
            self._guide_loader = None

    def _on_spatial_fit(self):
        """
        Execute spatial fitting - PRIMARY transformation method.

        Uses chain positioning algorithm:
        1. Public coordinate maps = fixed anchors (never transform)
        2. Find chiban matches to anchors for relative positioning
        3. Fallback to Oaza guide when no anchor relationships
        4. Final fallback to municipality boundary

        This works REGARDLESS of direct chiban matching availability.
        """
        if not self.db or self._current_source_id is None:
            QMessageBox.warning(
                self.dock,
                "警告",
                "データベースと変換対象を選択してください。"
            )
            return

        try:
            # Get source map info
            map_info = self.db.get_xml_meta_by_id(self._current_source_id)
            if not map_info:
                QMessageBox.warning(self.dock, "エラー", "図面情報を取得できません。")
                return

            # Check if this is a public coordinate map (anchor)
            crs_type = map_info.get('crs_type', '')
            if crs_type != '任意座標系' and '任意' not in crs_type:
                QMessageBox.information(
                    self.dock,
                    "変換不要",
                    f"この図面は公共座標系（{crs_type}）です。\n"
                    "位置が確定しているため変換は不要です。\n\n"
                    "※公共座標図面は他の図面の位置決めアンカーとして使用されます。"
                )
                return

            # Initialize chain positioner
            if not self._guide_loader:
                self._guide_loader = PositioningGuideLoader()

            self._chain_positioner = ChainPositioner(self.db, self._guide_loader)

            # Position single map using chain algorithm
            transform_params = self._chain_positioner.position_single_map(self._current_source_id)

            if transform_params is None:
                # Get more info for error message
                anchors = self._chain_positioner.get_anchors()
                QMessageBox.warning(
                    self.dock,
                    "位置決めエラー",
                    f"この図面の位置を決定できませんでした。\n\n"
                    f"公共座標アンカー数: {len(anchors)}\n"
                    f"大字ガイド: {'あり' if self._guide_loader and self._guide_loader.has_oaza_guide else 'なし'}\n\n"
                    "地番マッチングを試すか、大字形状レイヤーを読み込んでください。"
                )
                return

            # Store result and update UI
            self._current_transform_params = transform_params
            self._show_transform_params(transform_params)

            # Enable transform button
            self.dock.btnTransform.setEnabled(True)

            logger.info(f"Chain positioning completed: {transform_params.to_dict()}")

        except Exception as e:
            logger.error(f"Spatial fitting error: {e}", exc_info=True)
            QMessageBox.critical(
                self.dock,
                "エラー",
                f"空間フィッティング中にエラーが発生しました:\n{e}"
            )

    def _get_source_envelope(self, xml_meta_id: int) -> Optional[QgsGeometry]:
        """
        Get the envelope geometry of a source map.

        Args:
            xml_meta_id: ID of the XML meta record

        Returns:
            QgsGeometry of the envelope, or None
        """
        try:
            # Try to get from t_xml_meta.geom first
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT AsText(geom) as geom_wkt
                    FROM t_xml_meta
                    WHERE id = ?
                """, (xml_meta_id,))
                row = cursor.fetchone()

                if row and row['geom_wkt']:
                    geom = QgsGeometry.fromWkt(row['geom_wkt'])
                    if not geom.isEmpty():
                        return geom

            # Fallback: compute from fude polygons
            parcels = self.db.get_fude_by_xml_id(xml_meta_id)
            if not parcels:
                return None

            combined = QgsGeometry()
            for parcel in parcels:
                geom_wkt = parcel.get('geom_wkt', '')
                if geom_wkt:
                    geom = QgsGeometry.fromWkt(geom_wkt)
                    if not geom.isEmpty():
                        if combined.isEmpty():
                            combined = geom
                        else:
                            combined = combined.combine(geom)

            if not combined.isEmpty():
                # Return convex hull as envelope
                return combined.convexHull()

            return None

        except Exception as e:
            logger.error(f"Error getting source envelope: {e}", exc_info=True)
            return None

    def _show_transform_params(self, params: TransformParams):
        """
        Display transformation parameters in UI.

        Args:
            params: TransformParams from chain positioning
        """
        info = params.to_dict()

        # Method descriptions
        method_names = {
            'anchor_chiban': '公共座標アンカー（地番マッチ）',
            'chain': 'チェーン配置（隣接図面経由）',
            'oaza_guide': '大字ガイド（近似配置）',
            'municipality_fallback': '市町村フォールバック',
        }
        method_desc = method_names.get(info['method'], info['method'])

        # Update info label
        anchor_info = f"\nアンカー図面ID: {info['anchor_xml_id']}" if info['anchor_xml_id'] else ""
        self.dock.lblTransformRefInfo.setText(
            f"【位置決め結果】\n"
            f"方式: {method_desc}\n"
            f"スケール: {info['scale']:.4f}\n"
            f"回転: {info['rotation_deg']:.1f}°\n"
            f"信頼度: {info['confidence']:.0%}{anchor_info}"
        )

        # Update status
        self.dock.lblTransformStatus.setText(
            f"位置決め準備完了 - 「変換実行」で適用"
        )

    def set_database(self, db_path: Path):
        """
        Set the database to use.

        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path
        self.db = DatabaseManager(db_path)

        # Run migrations for existing databases
        try:
            self.db.migrate_database()
        except Exception as e:
            logger.warning(f"Database migration warning: {e}")

        # Update UI
        self.dock.lineEditTransformDb.setText(str(db_path))

        # Get database stats
        try:
            arbitrary_maps = self.db.get_xml_meta_by_crs_type('任意座標系')
            public_maps = self.db.get_xml_meta_by_crs_type('公共座標8系')

            # Also check for other CRS types (debugging)
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT crs_type, COUNT(*) FROM t_xml_meta GROUP BY crs_type")
                crs_stats = cursor.fetchall()
                logger.info(f"Database CRS types: {dict(crs_stats)}")

            if len(public_maps) == 0:
                # Check what CRS types actually exist
                crs_info = ", ".join([f"{row[0]}:{row[1]}" for row in crs_stats])
                self.dock.lblTransformDbInfo.setText(
                    f"任意座標: {len(arbitrary_maps)}図面\n座標系: {crs_info}"
                )
                logger.warning(f"No public coordinate maps found. CRS stats: {crs_stats}")
            else:
                self.dock.lblTransformDbInfo.setText(
                    f"任意座標: {len(arbitrary_maps)}図面 / 公共座標: {len(public_maps)}図面"
                )
        except Exception as e:
            logger.warning(f"Failed to get database stats: {e}", exc_info=True)
            self.dock.lblTransformDbInfo.setText("データベース接続済み")

        # Enable map selection if iface available
        if self.iface:
            self.dock.btnSelectOnMap.setEnabled(True)

        self._load_oaza_list()
        self._load_source_maps()
        self._load_envelope_layer()

    def _load_oaza_list(self):
        """Load Oaza list from database for filtering.

        Uses t_fude_poly.oaza_name as the primary source since t_xml_meta.oaza_name
        may be empty for many maps.
        """
        self.dock.comboTransformOazaFilter.clear()
        self.dock.comboTransformOazaFilter.setEnabled(False)

        if not self.db:
            return

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()

                # Get oaza list from t_fude_poly (筆データ) with CRS info from t_xml_meta
                # This captures all oaza that actually have parcel data
                cursor.execute("""
                    SELECT
                        f.oaza_name,
                        COUNT(DISTINCT CASE WHEN m.crs_type = '任意座標系' THEN m.id END) as arbitrary_map_count,
                        COUNT(DISTINCT CASE WHEN m.crs_type != '任意座標系' AND m.crs_type NOT LIKE '%任意%' THEN m.id END) as public_map_count,
                        COUNT(CASE WHEN m.crs_type = '任意座標系' THEN 1 END) as arbitrary_fude_count,
                        COUNT(CASE WHEN m.crs_type != '任意座標系' AND m.crs_type NOT LIKE '%任意%' THEN 1 END) as public_fude_count
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE f.oaza_name IS NOT NULL AND f.oaza_name != ''
                    GROUP BY f.oaza_name
                    ORDER BY f.oaza_name
                """)

                oaza_data = cursor.fetchall()
                logger.info(f"Found {len(oaza_data)} oaza from t_fude_poly")

                # Store for later use (map counts for display)
                self._oaza_crs_info = {}
                for row in oaza_data:
                    self._oaza_crs_info[row['oaza_name']] = {
                        'arbitrary': row['arbitrary_map_count'],
                        'public': row['public_map_count'],
                        'arbitrary_fude': row['arbitrary_fude_count'],
                        'public_fude': row['public_fude_count']
                    }

                oaza_names = [row['oaza_name'] for row in oaza_data]

            if not oaza_names:
                self.dock.comboTransformOazaFilter.addItem("（大字情報なし）", None)
                # Still enable so user can see all maps
                self._load_source_maps()
                return

            # Add "All" option
            self.dock.comboTransformOazaFilter.addItem("（全て）", "__ALL__")

            for oaza_name in oaza_names:
                info = self._oaza_crs_info.get(oaza_name, {})
                arbitrary = info.get('arbitrary', 0)
                public = info.get('public', 0)

                # Show availability status in the label
                if public > 0 and arbitrary > 0:
                    status = "★"  # Both available - transformation possible
                elif public > 0:
                    status = "◎"  # Only public - no transformation needed
                elif arbitrary > 0:
                    status = "△"  # Only arbitrary - no reference available
                else:
                    status = "?"  # Unknown

                label = f"{status} {oaza_name} (任意:{arbitrary}図面, 公共:{public}図面)"
                self.dock.comboTransformOazaFilter.addItem(label, oaza_name)

            self.dock.comboTransformOazaFilter.setEnabled(True)

            # Show legend in info label
            self._update_oaza_legend()

        except Exception as e:
            logger.error(f"Error loading Oaza list: {e}", exc_info=True)
            self.dock.comboTransformOazaFilter.addItem("（読み込みエラー）", None)

    def _update_oaza_legend(self):
        """Update the database info label with oaza legend."""
        if not hasattr(self, '_oaza_crs_info'):
            return

        transformable = sum(1 for info in self._oaza_crs_info.values()
                          if info['arbitrary'] > 0 and info['public'] > 0)
        arbitrary_only = sum(1 for info in self._oaza_crs_info.values()
                            if info['arbitrary'] > 0 and info['public'] == 0)

        legend = (
            f"★変換可能: {transformable}大字 / △参照なし: {arbitrary_only}大字\n"
            f"※ 同一大字に公共座標の参照図面が必要です"
        )

        # Append to existing info
        current_text = self.dock.lblTransformDbInfo.text()
        if "★" not in current_text:
            self.dock.lblTransformDbInfo.setText(f"{current_text}\n{legend}")

    def _on_oaza_filter_changed(self, index: int):
        """Handle Oaza filter selection change."""
        # Reload source maps with the new filter
        self._load_source_maps()

    def _load_source_maps(self):
        """Load arbitrary coordinate maps into source combo.

        Filters by oaza from t_fude_poly rather than t_xml_meta.oaza_name.
        """
        self.dock.comboTransformSource.clear()
        self.dock.comboTransformSource.setEnabled(False)

        if not self.db:
            return

        # Get selected Oaza filter
        selected_oaza = self.dock.comboTransformOazaFilter.currentData()

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()

                if selected_oaza and selected_oaza != "__ALL__":
                    # Get xml_meta IDs that have parcels in the selected oaza
                    cursor.execute("""
                        SELECT DISTINCT m.id, m.file_name, m.map_name, m.crs_type, m.fude_count,
                               (SELECT GROUP_CONCAT(DISTINCT f2.oaza_name)
                                FROM t_fude_poly f2 WHERE f2.xml_meta_id = m.id
                                AND f2.oaza_name IS NOT NULL AND f2.oaza_name != '') as oaza_list
                        FROM t_xml_meta m
                        JOIN t_fude_poly f ON f.xml_meta_id = m.id
                        WHERE m.crs_type = '任意座標系'
                        AND f.oaza_name = ?
                        ORDER BY m.file_name
                    """, (selected_oaza,))
                else:
                    # Get all arbitrary coordinate maps with their oaza info
                    cursor.execute("""
                        SELECT m.id, m.file_name, m.map_name, m.crs_type, m.fude_count,
                               (SELECT GROUP_CONCAT(DISTINCT f2.oaza_name)
                                FROM t_fude_poly f2 WHERE f2.xml_meta_id = m.id
                                AND f2.oaza_name IS NOT NULL AND f2.oaza_name != '') as oaza_list
                        FROM t_xml_meta m
                        WHERE m.crs_type = '任意座標系'
                        ORDER BY m.file_name
                    """)

                maps = [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Error loading source maps: {e}", exc_info=True)
            maps = []

        if not maps:
            if selected_oaza and selected_oaza != "__ALL__":
                self.dock.lblTransformSourceInfo.setText(f"「{selected_oaza}」に変換対象の図面がありません")
            else:
                self.dock.lblTransformSourceInfo.setText("変換対象の図面がありません（全て公共座標系）")
            self.dock.btnFindMatches.setEnabled(False)
            return

        self.dock.comboTransformSource.addItem("-- 選択してください --", None)

        for map_info in maps:
            # Use oaza_list from t_fude_poly
            oaza_list = map_info.get('oaza_list', '')
            if oaza_list:
                # Show first oaza if multiple
                first_oaza = oaza_list.split(',')[0] if ',' in oaza_list else oaza_list
                oaza_label = f"[{first_oaza}] "
            else:
                oaza_label = ""
            label = f"{oaza_label}{map_info['file_name']} ({map_info['fude_count']}筆)"
            self.dock.comboTransformSource.addItem(label, map_info['id'])

        self.dock.comboTransformSource.setEnabled(True)
        # Enable find matches button - will be properly controlled by source selection
        self.dock.btnFindMatches.setEnabled(False)  # Start disabled until source selected

        # Check if transform all is possible
        ref_maps = self.db.get_xml_meta_by_crs_type('公共座標8系')
        if maps and ref_maps:
            self.dock.btnTransformAll.setEnabled(True)

    def _load_envelope_layer(self):
        """Load envelope layer for map-based selection."""
        if not self.db or not self.db_path:
            return

        try:
            # Create a virtual layer from t_xml_meta with geometries
            uri = f"{self.db_path}|layername=t_xml_meta"
            self._envelope_layer = QgsVectorLayer(uri, "xml_envelopes", "ogr")

            if not self._envelope_layer.isValid():
                logger.warning("Failed to load envelope layer for map selection")
                self._envelope_layer = None

        except Exception as e:
            logger.error(f"Error loading envelope layer: {e}")
            self._envelope_layer = None

    def _on_select_on_map(self):
        """Handle map selection button click."""
        if not self.iface:
            QMessageBox.warning(
                self.dock,
                "警告",
                "QGIS インターフェースが利用できません。"
            )
            return

        if not self.db:
            QMessageBox.warning(
                self.dock,
                "警告",
                "まずデータベースを選択してください。"
            )
            return

        # Save previous tool
        self._previous_map_tool = self.iface.mapCanvas().mapTool()

        # Create and set map tool
        self._map_tool = MapSelectTool(self.iface.mapCanvas())
        self._map_tool.selected.connect(self._on_map_clicked)
        self.iface.mapCanvas().setMapTool(self._map_tool)

        # Update button state
        self.dock.btnSelectOnMap.setText("選択中...")
        self.dock.btnSelectOnMap.setEnabled(False)

        # Show instruction
        self.iface.messageBar().pushInfo(
            "公図XML整合ツール",
            "地図上でクリックして図面を選択してください（任意座標系の図面のみ）"
        )

    def _on_map_clicked(self, point: QgsPointXY):
        """Handle map click for selection."""
        try:
            # Reset map tool
            if self._previous_map_tool:
                self.iface.mapCanvas().setMapTool(self._previous_map_tool)
            self._previous_map_tool = None

            # Reset button
            self.dock.btnSelectOnMap.setText("地図で選択")
            self.dock.btnSelectOnMap.setEnabled(True)

            if not self.db:
                return

            # Find XML at clicked point
            # Query database for xml_meta with geometry containing the point
            selected_id = self._find_xml_at_point(point)

            if selected_id is None:
                self.iface.messageBar().pushWarning(
                    "公図XML整合ツール",
                    "クリックした位置に任意座標系の図面が見つかりませんでした"
                )
                return

            # Find the item in combo box and select it
            for i in range(self.dock.comboTransformSource.count()):
                if self.dock.comboTransformSource.itemData(i) == selected_id:
                    self.dock.comboTransformSource.setCurrentIndex(i)
                    self.iface.messageBar().pushSuccess(
                        "公図XML整合ツール",
                        f"図面を選択しました: {self.dock.comboTransformSource.currentText()}"
                    )
                    return

            self.iface.messageBar().pushWarning(
                "公図XML整合ツール",
                "選択した図面は任意座標系ではありません"
            )

        except Exception as e:
            logger.error(f"Error handling map click: {e}", exc_info=True)
            self.dock.btnSelectOnMap.setText("地図で選択")
            self.dock.btnSelectOnMap.setEnabled(True)

    def _find_xml_at_point(self, point: QgsPointXY) -> Optional[int]:
        """
        Find XML envelope containing the given point.

        Args:
            point: Map point clicked by user

        Returns:
            xml_meta ID if found, None otherwise
        """
        if not self.db:
            return None

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()

                # Find xml_meta with geometry containing the point
                # Only return arbitrary coordinate maps
                cursor.execute("""
                    SELECT id FROM t_xml_meta
                    WHERE crs_type = '任意座標系'
                    AND MbrContains(geom, MakePoint(?, ?, 6676))
                    LIMIT 1
                """, (point.x(), point.y()))

                result = cursor.fetchone()
                if result:
                    return result[0]

                # If no direct hit, try a small buffer search
                buffer_size = 100  # meters
                cursor.execute("""
                    SELECT id, Distance(geom, MakePoint(?, ?, 6676)) as dist
                    FROM t_xml_meta
                    WHERE crs_type = '任意座標系'
                    AND dist < ?
                    ORDER BY dist
                    LIMIT 1
                """, (point.x(), point.y(), buffer_size))

                result = cursor.fetchone()
                if result:
                    return result[0]

            return None

        except Exception as e:
            logger.error(f"Error finding XML at point: {e}", exc_info=True)
            return None

    def _on_source_changed(self, index: int):
        """Handle source map selection change."""
        self._current_source_id = self.dock.comboTransformSource.currentData()
        self._current_transform_params = None  # Clear previous transform params

        if self._current_source_id is None:
            self.dock.lblTransformSourceInfo.setText("筆数: - / 座標系: -")
            self.dock.btnFindMatches.setEnabled(False)
            self.dock.btnSpatialFit.setEnabled(False)
            self._clear_matches()
            return

        # Get map info
        map_info = self.db.get_xml_meta_by_id(self._current_source_id)
        if map_info:
            # Get oaza info from t_fude_poly for this map
            try:
                with self.db.connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT GROUP_CONCAT(DISTINCT oaza_name) as oaza_list
                        FROM t_fude_poly
                        WHERE xml_meta_id = ?
                        AND oaza_name IS NOT NULL AND oaza_name != ''
                    """, (self._current_source_id,))
                    row = cursor.fetchone()
                    oaza_list = row['oaza_list'] if row and row['oaza_list'] else '不明'
            except Exception:
                oaza_list = '不明'

            self.dock.lblTransformSourceInfo.setText(
                f"筆数: {map_info['fude_count']} / 大字: {oaza_list}"
            )

        # Spatial fitting (PRIMARY) - always available (uses chain positioning)
        # Can work with or without Oaza guide layer
        self.dock.btnSpatialFit.setEnabled(True)

        # Chiban matching (helper) - always available
        self.dock.btnFindMatches.setEnabled(True)

        self._clear_matches()

    def _clear_matches(self):
        """Clear match candidates table."""
        self.dock.tableMatchCandidates.setRowCount(0)
        self.dock.tableMatchCandidates.setEnabled(False)
        self.dock.btnTransform.setEnabled(False)
        self._current_matches = []
        self._current_control_points = []

    def _on_find_matches(self):
        """Find matching reference maps."""
        if not self.db or self._current_source_id is None:
            return

        self.dock.lblTransformRefInfo.setText("マッチング候補を検索中...")
        self._clear_matches()

        try:
            # Get source oaza from t_fude_poly (not t_xml_meta which may be empty)
            with self.db.connection() as conn:
                cursor = conn.cursor()

                # Get oaza names from source map's parcels
                cursor.execute("""
                    SELECT DISTINCT oaza_name
                    FROM t_fude_poly
                    WHERE xml_meta_id = ?
                    AND oaza_name IS NOT NULL AND oaza_name != ''
                """, (self._current_source_id,))
                source_oazas = [row[0] for row in cursor.fetchall()]
                source_oaza_str = ', '.join(source_oazas) if source_oazas else '不明'

                logger.info(f"Source map {self._current_source_id} oazas (from t_fude_poly): {source_oazas}")

                # Check if there are any reference maps at all
                cursor.execute("""
                    SELECT crs_type, COUNT(*) as cnt
                    FROM t_xml_meta
                    WHERE crs_type != '任意座標系'
                    AND crs_type NOT LIKE '%任意%'
                    GROUP BY crs_type
                """)
                ref_stats = cursor.fetchall()

                # Check if there are reference maps with parcels in the same oaza
                # Use t_fude_poly.oaza_name for comparison
                if source_oazas:
                    placeholders = ','.join('?' * len(source_oazas))
                    cursor.execute(f"""
                        SELECT DISTINCT m.id, m.file_name, f.oaza_name, m.crs_type
                        FROM t_xml_meta m
                        JOIN t_fude_poly f ON f.xml_meta_id = m.id
                        WHERE f.oaza_name IN ({placeholders})
                        AND m.crs_type != '任意座標系'
                        AND m.crs_type NOT LIKE '%任意%'
                    """, source_oazas)
                    same_oaza_refs = cursor.fetchall()
                else:
                    same_oaza_refs = []

                # Get all reference map oazas for diagnostics (from t_fude_poly)
                cursor.execute("""
                    SELECT DISTINCT f.oaza_name
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE m.crs_type != '任意座標系'
                    AND m.crs_type NOT LIKE '%任意%'
                    AND f.oaza_name IS NOT NULL AND f.oaza_name != ''
                """)
                ref_oazas = [row[0] for row in cursor.fetchall()]

            if not ref_stats:
                self.dock.lblTransformRefInfo.setText(
                    "参照図面（公共座標系）がありません\n"
                    "※ 全ての図面が任意座標系のため変換できません"
                )
                logger.warning("No public coordinate reference maps found in database")
                return

            logger.info(f"Reference map stats: {[dict(row) for row in ref_stats]}")
            logger.info(f"Source oazas: {source_oazas}, Reference oazas: {ref_oazas}")

            # If no reference maps in same oaza, show detailed message
            if not same_oaza_refs and source_oazas:
                self.dock.lblTransformRefInfo.setText(
                    f"【地理的不一致】\n"
                    f"ソース図面の大字「{source_oaza_str}」には\n"
                    f"公共座標の参照図面がありません。\n\n"
                    f"参照図面がある大字: {', '.join(ref_oazas) if ref_oazas else 'なし'}"
                )
                logger.warning(f"No reference maps in oazas {source_oazas}. Available ref oazas: {ref_oazas}")
                return

            finder = TransformCandidateFinder(self.db)
            matches = finder.find_candidates(
                self._current_source_id,
                target_crs_type='公共座標8系',
                max_candidates=10
            )

            self._current_matches = matches

            if not matches:
                # Get more diagnostic info
                source_parcels = self.db.get_fude_by_xml_id(self._current_source_id)
                source_chibans = set(p.get('chiban', '') for p in source_parcels if p.get('chiban'))

                # Check what chibans exist in same oaza reference maps
                ref_chibans_sample = []
                if source_oazas:
                    with self.db.connection() as conn:
                        cursor = conn.cursor()
                        placeholders = ','.join('?' * len(source_oazas))
                        cursor.execute(f"""
                            SELECT DISTINCT f.chiban
                            FROM t_fude_poly f
                            JOIN t_xml_meta m ON f.xml_meta_id = m.id
                            WHERE f.oaza_name IN ({placeholders})
                            AND m.crs_type != '任意座標系'
                            AND m.crs_type NOT LIKE '%任意%'
                            AND f.chiban IS NOT NULL AND f.chiban != ''
                            LIMIT 20
                        """, source_oazas)
                        ref_chibans_sample = [row[0] for row in cursor.fetchall()]

                source_sample = list(source_chibans)[:10]

                self.dock.lblTransformRefInfo.setText(
                    f"【地番不一致】\n"
                    f"共通する地番が見つかりませんでした。\n\n"
                    f"ソースの地番例: {', '.join(source_sample)}\n"
                    f"参照の地番例: {', '.join(ref_chibans_sample) if ref_chibans_sample else 'なし'}"
                )
                logger.warning(f"No matching chibans. Source: {source_sample}, Ref: {ref_chibans_sample}")
                return

            # Populate table
            self.dock.tableMatchCandidates.setRowCount(len(matches))
            self.dock.tableMatchCandidates.setEnabled(True)

            for row, match in enumerate(matches):
                # Get target map info
                target_info = self.db.get_xml_meta_by_id(match.target_xml_id)
                target_name = target_info['file_name'] if target_info else str(match.target_xml_id)

                # Get target oaza from t_fude_poly
                try:
                    with self.db.connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT GROUP_CONCAT(DISTINCT oaza_name) as oaza_list
                            FROM t_fude_poly
                            WHERE xml_meta_id = ?
                            AND oaza_name IS NOT NULL AND oaza_name != ''
                        """, (match.target_xml_id,))
                        oaza_row = cursor.fetchone()
                        target_oaza = oaza_row['oaza_list'] if oaza_row and oaza_row['oaza_list'] else ''
                except Exception:
                    target_oaza = ''

                # Show oaza in name
                display_name = f"[{target_oaza}] {target_name}" if target_oaza else target_name

                self.dock.tableMatchCandidates.setItem(
                    row, 0, QTableWidgetItem(display_name)
                )
                self.dock.tableMatchCandidates.setItem(
                    row, 1, QTableWidgetItem(str(match.common_chibans))
                )
                self.dock.tableMatchCandidates.setItem(
                    row, 2, QTableWidgetItem(f"{match.match_score:.2f}")
                )

            self.dock.lblTransformRefInfo.setText(
                f"{len(matches)}件のマッチング候補が見つかりました"
            )

        except Exception as e:
            logger.error(f"Match finding error: {e}", exc_info=True)
            self.dock.lblTransformRefInfo.setText(f"エラー: {e}")

    def _on_match_selected(self):
        """Handle match candidate selection."""
        selected_rows = self.dock.tableMatchCandidates.selectedItems()
        if not selected_rows:
            self.dock.btnTransform.setEnabled(False)
            return

        row = self.dock.tableMatchCandidates.currentRow()
        if row < 0 or row >= len(self._current_matches):
            return

        match = self._current_matches[row]

        # Get control points
        try:
            finder = TransformCandidateFinder(self.db)
            self._current_control_points = finder.get_control_points_for_match(match)

            if len(self._current_control_points) >= 2:
                self.dock.btnTransform.setEnabled(True)
                self.dock.lblTransformStatus.setText(
                    f"制御点: {len(self._current_control_points)}点"
                )
            else:
                self.dock.btnTransform.setEnabled(False)
                self.dock.lblTransformStatus.setText("制御点が不足しています")

        except Exception as e:
            logger.error(f"Control point extraction error: {e}", exc_info=True)
            self.dock.btnTransform.setEnabled(False)

    def _on_transform(self):
        """Execute transformation for selected map.

        Supports two transformation methods:
        1. Chain positioning (PRIMARY) - uses TransformParams from ChainPositioner
        2. Chiban matching (helper) - uses control points from direct matching
        """
        if not self.db or self._current_source_id is None:
            return

        if self._thread and self._thread.isRunning():
            QMessageBox.warning(
                self.dock,
                "警告",
                "変換処理が既に実行中です。"
            )
            return

        exclude_outside = self.dock.chkExcludeOutsideDistrict.isChecked()

        # Determine which transformation method to use
        if self._current_transform_params is not None:
            # PRIMARY method: Chain positioning (spatial fit)
            self._execute_spatial_fit_transform(exclude_outside)
        elif self._current_control_points:
            # Helper method: Chiban matching control points
            self._execute_control_point_transform(exclude_outside)
        else:
            QMessageBox.warning(
                self.dock,
                "警告",
                "変換パラメータがありません。\n"
                "「空間フィッティング」または「地番マッチング」を実行してください。"
            )

    def _execute_spatial_fit_transform(self, exclude_outside: bool):
        """Execute transformation using chain positioning result.

        This is the PRIMARY transformation method.
        """
        params = self._current_transform_params

        # Debug: Log transformation parameters
        logger.info(f"=== Starting spatial fit transform ===")
        logger.info(f"Source XML ID: {self._current_source_id}")
        logger.info(f"Transform params: {params.to_dict()}")
        logger.info(f"Database path: {self.db_path}")

        self.dock.progressBarTransform.setValue(0)
        self.dock.lblTransformStatus.setText("チェーン位置決め変換中...")
        self.dock.btnTransform.setEnabled(False)
        self.dock.btnSpatialFit.setEnabled(False)

        try:
            # Create transformer from params
            transformer = GeometryTransformer(params)

            # Get parcels to transform
            all_parcels = self.db.get_fude_by_xml_id(self._current_source_id)

            if not all_parcels:
                QMessageBox.warning(self.dock, "エラー", "変換対象の筆が見つかりません。")
                return

            # Filter out "地区外" if requested
            if exclude_outside:
                parcels = [p for p in all_parcels if '地区外' not in (p.get('chiban') or '')]
                skipped = len(all_parcels) - len(parcels)
            else:
                parcels = all_parcels
                skipped = 0

            if not parcels:
                QMessageBox.warning(self.dock, "エラー", "変換対象の筆が見つかりません（全て「地区外」）。")
                return

            # Transform each parcel
            total = len(parcels)
            transformed_count = 0

            for i, parcel in enumerate(parcels):
                progress_pct = int(100 * i / total)
                self.dock.progressBarTransform.setValue(progress_pct)

                geom_wkt = parcel.get('geom_wkt', '')
                if not geom_wkt:
                    continue

                geom = QgsGeometry.fromWkt(geom_wkt)
                if geom.isEmpty():
                    continue

                # Transform geometry
                transformed_geom = transformer.transform_geometry(geom)

                if not transformed_geom.isEmpty():
                    new_wkt = transformed_geom.asWkt()
                    self.db.update_fude_geometry(parcel['id'], new_wkt)
                    transformed_count += 1
                    # Debug: Log first parcel transformation
                    if transformed_count == 1:
                        old_centroid = geom.centroid().asPoint()
                        new_centroid = transformed_geom.centroid().asPoint()
                        logger.info(f"Transform sample: parcel {parcel['id']}")
                        logger.info(f"  Old centroid: ({old_centroid.x():.2f}, {old_centroid.y():.2f})")
                        logger.info(f"  New centroid: ({new_centroid.x():.2f}, {new_centroid.y():.2f})")
                        logger.info(f"  Translation: dx={params.translation_x:.2f}, dy={params.translation_y:.2f}")

            # Update xml_meta CRS type with method info
            method_suffix = {
                'anchor_chiban': 'アンカー',
                'chain': 'チェーン',
                'oaza_guide': 'ガイド',
                'municipality_fallback': 'FB',
            }.get(params.method, '')
            self.db.update_xml_meta_crs(self._current_source_id, f'変換済み（{method_suffix}）', 6676)

            self.dock.progressBarTransform.setValue(100)
            self.dock.lblTransformStatus.setText("変換完了")

            # Debug: Verify transformation was written by re-reading from database
            logger.info(f"=== Transform completed ===")
            logger.info(f"Transformed {transformed_count}/{total} parcels")

            # Verify by reading back a transformed parcel
            try:
                test_parcels = self.db.get_fude_by_xml_id(self._current_source_id)
                if test_parcels:
                    test_parcel = test_parcels[0]
                    logger.info(f"Verification - First parcel after transform:")
                    logger.info(f"  coord_type: {test_parcel.get('coord_type')}")
                    test_geom = QgsGeometry.fromWkt(test_parcel.get('geom_wkt', ''))
                    if not test_geom.isEmpty():
                        centroid = test_geom.centroid().asPoint()
                        logger.info(f"  centroid: ({centroid.x():.2f}, {centroid.y():.2f})")
            except Exception as e:
                logger.error(f"Verification read failed: {e}")

            # Show result
            skipped_msg = f"\n「地区外」除外: {skipped}筆" if skipped > 0 else ""
            QMessageBox.information(
                self.dock,
                "変換完了",
                f"座標変換が完了しました。\n\n"
                f"変換筆数: {transformed_count}/{total}\n"
                f"方式: {params.method}\n"
                f"スケール: {params.scale:.4f}\n"
                f"回転: {params.rotation_degrees:.1f}°{skipped_msg}"
            )

            # Reload source maps
            self._load_source_maps()

        except Exception as e:
            logger.error(f"Spatial fit transform error: {e}", exc_info=True)
            QMessageBox.critical(
                self.dock,
                "変換エラー",
                f"座標変換中にエラーが発生しました:\n{e}"
            )
        finally:
            self.dock.btnTransform.setEnabled(True)
            self.dock.btnSpatialFit.setEnabled(True)

    def _execute_control_point_transform(self, exclude_outside: bool):
        """Execute transformation using chiban matching control points.

        This is the HELPER transformation method (secondary to spatial fitting).
        """
        use_tps = self.dock.radioTPS.isChecked()

        # Create worker
        self._worker = TransformWorker(
            db=self.db,
            source_xml_id=self._current_source_id,
            control_points=self._current_control_points,
            use_tps=use_tps,
            exclude_outside_district=exclude_outside
        )

        # Create thread
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Connect signals
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_transform_progress)
        self._worker.finished.connect(self._on_transform_finished)
        self._worker.error.connect(self._on_transform_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)

        # Update UI
        self.dock.btnTransform.setEnabled(False)
        self.dock.btnTransformAll.setEnabled(False)
        self.dock.progressBarTransform.setValue(0)

        # Start
        self._thread.start()

    def _on_transform_all(self):
        """Transform all arbitrary coordinate maps using chain positioning.

        This is the batch transformation that:
        1. Identifies all public coordinate maps as anchors
        2. Chains arbitrary maps from anchors using chiban relationships
        3. Falls back to Oaza guide or municipality boundary
        """
        if not self.db:
            QMessageBox.warning(self.dock, "警告", "データベースを選択してください。")
            return

        # Confirm with user
        reply = QMessageBox.question(
            self.dock,
            "一括変換の確認",
            "全ての任意座標系図面を一括変換します。\n\n"
            "【処理内容】\n"
            "1. 公共座標図面をアンカー（固定点）として使用\n"
            "2. 地番マッチングでアンカーとの位置関係を計算\n"
            "3. チェーン配置で順次位置決め\n"
            "4. マッチングがない場合は大字ガイドを使用\n\n"
            "※公共座標図面は変換されません（アンカーとして保持）\n\n"
            "続行しますか？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        exclude_outside = self.dock.chkExcludeOutsideDistrict.isChecked()

        self.dock.progressBarTransform.setValue(0)
        self.dock.lblTransformStatus.setText("一括変換を開始...")
        self.dock.btnTransform.setEnabled(False)
        self.dock.btnTransformAll.setEnabled(False)
        self.dock.btnSpatialFit.setEnabled(False)

        try:
            # Initialize guide loader if not already done
            if not self._guide_loader:
                self._guide_loader = PositioningGuideLoader()

            # Create chain positioner
            positioner = ChainPositioner(self.db, self._guide_loader)

            # Position all maps
            self.dock.lblTransformStatus.setText("位置決めパラメータを計算中...")
            all_params = positioner.position_all_maps(exclude_outside_district=exclude_outside)

            if not all_params:
                QMessageBox.warning(
                    self.dock,
                    "変換対象なし",
                    "変換対象の図面がありません。\n"
                    "全ての図面が既に公共座標系か、変換済みの可能性があります。"
                )
                return

            # Get anchors info
            anchors = positioner.get_anchors()

            # Transform each map
            total_maps = len(all_params)
            total_parcels_transformed = 0
            total_parcels_skipped = 0
            maps_transformed = 0
            method_counts = {}

            for i, (xml_id, params) in enumerate(all_params.items()):
                progress_pct = int(100 * i / total_maps)
                self.dock.progressBarTransform.setValue(progress_pct)
                self.dock.lblTransformStatus.setText(
                    f"図面を変換中... ({i+1}/{total_maps})"
                )

                # Count method usage
                method_counts[params.method] = method_counts.get(params.method, 0) + 1

                # Create transformer
                transformer = GeometryTransformer(params)

                # Get parcels
                all_parcels = self.db.get_fude_by_xml_id(xml_id)
                if not all_parcels:
                    continue

                # Filter out "地区外" if requested
                if exclude_outside:
                    parcels = [p for p in all_parcels if '地区外' not in (p.get('chiban') or '')]
                    total_parcels_skipped += len(all_parcels) - len(parcels)
                else:
                    parcels = all_parcels

                if not parcels:
                    continue

                # Transform parcels
                for parcel in parcels:
                    geom_wkt = parcel.get('geom_wkt', '')
                    if not geom_wkt:
                        continue

                    geom = QgsGeometry.fromWkt(geom_wkt)
                    if geom.isEmpty():
                        continue

                    transformed_geom = transformer.transform_geometry(geom)
                    if not transformed_geom.isEmpty():
                        self.db.update_fude_geometry(parcel['id'], transformed_geom.asWkt())
                        total_parcels_transformed += 1

                # Update xml_meta CRS type
                method_suffix = {
                    'anchor_chiban': 'アンカー',
                    'chain': 'チェーン',
                    'oaza_guide': 'ガイド',
                    'municipality_fallback': 'FB',
                }.get(params.method, '')
                self.db.update_xml_meta_crs(xml_id, f'変換済み（{method_suffix}）', 6676)
                maps_transformed += 1

            self.dock.progressBarTransform.setValue(100)
            self.dock.lblTransformStatus.setText("一括変換完了")

            # Build summary message
            method_summary = "\n".join([
                f"  - {method}: {count}図面"
                for method, count in sorted(method_counts.items())
            ])

            skipped_msg = f"\n「地区外」除外: {total_parcels_skipped}筆" if total_parcels_skipped > 0 else ""

            QMessageBox.information(
                self.dock,
                "一括変換完了",
                f"一括変換が完了しました。\n\n"
                f"【結果】\n"
                f"公共座標アンカー: {len(anchors)}図面（変換なし）\n"
                f"変換図面数: {maps_transformed}/{total_maps}\n"
                f"変換筆数: {total_parcels_transformed}{skipped_msg}\n\n"
                f"【位置決め方式】\n{method_summary}"
            )

            # Reload source maps
            self._load_source_maps()

        except Exception as e:
            logger.error(f"Batch transform error: {e}", exc_info=True)
            QMessageBox.critical(
                self.dock,
                "一括変換エラー",
                f"一括変換中にエラーが発生しました:\n{e}"
            )

        finally:
            self.dock.btnTransform.setEnabled(True)
            self.dock.btnTransformAll.setEnabled(True)
            self.dock.btnSpatialFit.setEnabled(True)

    def _on_transform_progress(self, percent: int, status: str):
        """Handle transform progress."""
        self.dock.progressBarTransform.setValue(percent)
        self.dock.lblTransformStatus.setText(status)

    def _on_transform_finished(self, result: dict):
        """Handle transform completion."""
        self.dock.progressBarTransform.setValue(100)
        self.dock.lblTransformStatus.setText("変換完了")
        self.dock.btnTransform.setEnabled(True)
        self.dock.btnTransformAll.setEnabled(True)

        # Build result message
        skipped = result.get('skipped_outside_count', 0)
        skipped_msg = f"\n「地区外」除外: {skipped}筆" if skipped > 0 else ""

        QMessageBox.information(
            self.dock,
            "変換完了",
            f"座標変換が完了しました。\n\n"
            f"変換筆数: {result['transformed_count']}/{result['total_parcels']}\n"
            f"使用制御点: {result['control_points_used']}点{skipped_msg}"
        )

        # Reload source maps (to update status)
        self._load_source_maps()

    def _on_transform_error(self, error_msg: str):
        """Handle transform error."""
        self.dock.lblTransformStatus.setText("エラー")
        self.dock.btnTransform.setEnabled(True)
        self.dock.btnTransformAll.setEnabled(True)

        QMessageBox.critical(
            self.dock,
            "変換エラー",
            f"座標変換中にエラーが発生しました:\n{error_msg}"
        )

    def _cleanup_thread(self):
        """Clean up worker and thread."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
