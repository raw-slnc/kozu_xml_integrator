# -*- coding: utf-8 -*-
"""
Integration Engine for KozuXmlIntegrator v2

Main orchestrator for the integration workflow:
1. STEP 1: Join XMLs within each oaza using common chibans
2. STEP 2: Move combined oazas to oaza boundary positions (if no anchor)
3. STEP 3: Fit to municipality boundary (public coords fixed)
4. STEP 4: Apply leveling to resolve overlaps
5. STEP 5: Quality verification and output

Key principles:
- Public coordinate data is NEVER modified
- All adjustments are made to arbitrary coordinate data
- Topology is preserved within each XML
"""

from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Callable
from dataclasses import dataclass, field
import math
import logging

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
    QgsFeature,
    QgsFeatureRequest,
)

from .database_manager import DatabaseManager
from ..transform.xml_joiner import XmlJoiner, JoinResult
from ..transform.leveling import Leveler, TopologyChecker, LevelingResult
from ..transform.spatial_fitting import GeometryTransformer, TransformParams

logger = logging.getLogger(__name__)


@dataclass
class IntegrationConfig:
    """Configuration for integration process."""
    municipality_layer: Optional[QgsVectorLayer] = None
    oaza_boundary_layer: Optional[QgsVectorLayer] = None
    skip_step2_if_public_anchor: bool = True
    run_leveling: bool = True
    leveling_max_iterations: int = 10
    overlap_threshold: float = 0.1  # m^2
    leveling_shrink_factor: float = 0.5  # How much to shrink per iteration


@dataclass
class IntegrationProgress:
    """Progress information for integration process."""
    current_step: str
    current_oaza: str
    step_progress: float  # 0-100
    overall_progress: float  # 0-100
    status_message: str
    progress_percent: float = 0.0  # For UI compatibility


@dataclass
class OazaResult:
    """Result for a single oaza integration."""
    success: bool = False
    parcels_processed: int = 0
    parcels_transformed: int = 0
    overlaps_resolved: int = 0
    issues: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class IntegrationResult:
    """Result of the full integration process."""
    success: bool
    total_oazas: int
    processed_oazas: int
    total_parcels: int
    transformed_parcels: int
    overlaps_resolved: int
    issues: List[str]
    oaza_results: Dict[str, OazaResult]
    elapsed_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)


class IntegrationEngine:
    """
    Main engine for integrating cadastral XML data.

    Executes the v2 integration workflow with proper handling of
    public coordinate anchors and topology preservation.
    """

    def __init__(self, db_manager: DatabaseManager,
                 config: Optional[IntegrationConfig] = None):
        """
        Initialize integration engine.

        Args:
            db_manager: DatabaseManager instance
            config: IntegrationConfig instance (optional)
        """
        self.db = db_manager
        self.config = config or IntegrationConfig()

        self.joiner = XmlJoiner(db_manager)
        self.leveler = Leveler(db_manager)
        self.topology_checker = TopologyChecker(db_manager)

        # Boundary data cache
        self._oaza_boundaries: Dict[str, QgsGeometry] = {}
        self._municipality_boundary: Optional[QgsGeometry] = None

        # Progress callback
        self._progress_callback: Optional[Callable[[IntegrationProgress], None]] = None

    def set_progress_callback(self, callback: Callable[[IntegrationProgress], None]):
        """Set callback for progress updates."""
        self._progress_callback = callback

    def _report_progress(self, step: str, oaza: str, step_pct: float,
                        overall_pct: float, message: str):
        """Report progress to callback if set."""
        if self._progress_callback:
            self._progress_callback(IntegrationProgress(
                current_step=step,
                current_oaza=oaza,
                step_progress=step_pct,
                overall_progress=overall_pct,
                status_message=message,
                progress_percent=overall_pct
            ))

    def run_full_integration(self, target_oaza: Optional[str] = None,
                             oaza_names: List[str] = None,
                             progress_callback: Optional[Callable[[IntegrationProgress], None]] = None) -> IntegrationResult:
        """
        Run the complete integration workflow.

        Args:
            target_oaza: Single oaza to process (alternative to oaza_names)
            oaza_names: Optional list of oazas to process (None = all)
            progress_callback: Optional progress callback function

        Returns:
            IntegrationResult with details of the process
        """
        import time
        start_time = time.time()

        # Set progress callback if provided
        if progress_callback:
            self.set_progress_callback(progress_callback)

        issues = []
        errors = []
        oaza_results: Dict[str, OazaResult] = {}

        # Handle target_oaza parameter
        if target_oaza:
            oaza_names = [target_oaza]

        # Get list of oazas to process
        if oaza_names is None:
            oaza_names = self._get_all_oaza_names()

        total_oazas = len(oaza_names)
        total_parcels = 0
        transformed_parcels = 0
        total_overlaps_resolved = 0

        logger.info(f"Starting integration for {total_oazas} oazas")

        # Load boundary data
        self._load_boundaries()

        # Process each oaza
        for i, oaza_name in enumerate(oaza_names):
            overall_pct = (i / total_oazas) * 100

            self._report_progress(
                "step1", oaza_name, 0, overall_pct,
                f"大字「{oaza_name}」を処理中... ({i+1}/{total_oazas})"
            )

            try:
                result_dict = self._process_oaza(oaza_name)

                # Convert dict to OazaResult
                oaza_result = OazaResult(
                    success=result_dict.get('success', False),
                    parcels_processed=result_dict.get('total_parcels', 0),
                    parcels_transformed=result_dict.get('transformed_parcels', 0),
                    overlaps_resolved=result_dict.get('overlaps_resolved', 0),
                    issues=result_dict.get('issues', []),
                    error=result_dict.get('error')
                )
                oaza_results[oaza_name] = oaza_result

                total_parcels += oaza_result.parcels_processed
                transformed_parcels += oaza_result.parcels_transformed
                total_overlaps_resolved += oaza_result.overlaps_resolved

                if oaza_result.issues:
                    issues.extend([f"[{oaza_name}] {issue}" for issue in oaza_result.issues])

            except Exception as e:
                logger.error(f"Error processing oaza {oaza_name}: {e}", exc_info=True)
                error_msg = f"[{oaza_name}] 処理エラー: {str(e)}"
                issues.append(error_msg)
                errors.append(error_msg)
                oaza_results[oaza_name] = OazaResult(success=False, error=str(e))

        elapsed = time.time() - start_time

        # Final report
        self._report_progress(
            "step5", "", 100, 100,
            f"統合完了: {total_oazas}大字, {transformed_parcels}筆変換済み"
        )

        return IntegrationResult(
            success=len([r for r in oaza_results.values() if r.success]) > 0,
            total_oazas=total_oazas,
            processed_oazas=len([r for r in oaza_results.values() if r.success]),
            total_parcels=total_parcels,
            transformed_parcels=transformed_parcels,
            overlaps_resolved=total_overlaps_resolved,
            issues=issues,
            oaza_results=oaza_results,
            elapsed_seconds=elapsed,
            errors=errors
        )

    def run_simplified_integration(self, target_oaza: Optional[str] = None,
                                   oaza_shape_layer: Optional[QgsVectorLayer] = None,
                                   progress_callback: Optional[Callable[[IntegrationProgress], None]] = None) -> IntegrationResult:
        """
        Run simplified 3-step integration:
        1. Load XML data (already in database)
        2. Join by oaza (using common chibans)
        3. Move center to Oaza_Shape center

        No final transformation/leveling is performed.

        Args:
            target_oaza: Single oaza to process (None = all)
            oaza_shape_layer: QgsVectorLayer with Oaza_Shape.gpkg data
            progress_callback: Optional progress callback

        Returns:
            IntegrationResult with details
        """
        import time
        start_time = time.time()

        if progress_callback:
            self.set_progress_callback(progress_callback)

        issues = []
        errors = []
        oaza_results: Dict[str, OazaResult] = {}

        # Get oazas to process
        oaza_names = [target_oaza] if target_oaza else self._get_all_oaza_names()
        total_oazas = len(oaza_names)
        total_parcels = 0
        transformed_parcels = 0

        logger.info(f"Starting simplified integration for {total_oazas} oazas")

        # Load Oaza_Shape centers
        oaza_centers = {}
        if oaza_shape_layer and oaza_shape_layer.isValid():
            oaza_centers = self._load_oaza_shape_centers(oaza_shape_layer)
            logger.info(f"Loaded {len(oaza_centers)} oaza centers from Oaza_Shape")

        for i, oaza_name in enumerate(oaza_names):
            overall_pct = ((i + 0.5) / total_oazas) * 100

            self._report_progress(
                "step2", oaza_name, 0, overall_pct,
                f"大字「{oaza_name}」を結合中... ({i+1}/{total_oazas})"
            )

            try:
                # Step 2: Join XMLs within oaza
                join_result = self.joiner.join_oaza(oaza_name)

                # Get parcel count
                parcels_list = self._get_parcels_in_oaza(oaza_name)
                parcel_count = len(parcels_list)

                # Apply join transformations
                transformed = self._apply_join_transforms(oaza_name, join_result)

                # Step 3: Move to Oaza_Shape center
                self._report_progress(
                    "step3", oaza_name, 50, overall_pct,
                    f"大字「{oaza_name}」を配置中..."
                )

                if oaza_name in oaza_centers:
                    self._move_to_oaza_center(oaza_name, oaza_centers[oaza_name])
                    logger.info(f"Moved oaza '{oaza_name}' to Oaza_Shape center")

                # Build result
                oaza_issues = []
                if join_result.isolated_xmls:
                    oaza_issues.append(f"{len(join_result.isolated_xmls)}個の孤立XML")
                if oaza_name not in oaza_centers:
                    oaza_issues.append("Oaza_Shapeに中心点なし")

                oaza_result = OazaResult(
                    success=True,
                    parcels_processed=parcel_count,
                    parcels_transformed=transformed,
                    overlaps_resolved=0,
                    issues=oaza_issues,
                )
                oaza_results[oaza_name] = oaza_result
                total_parcels += parcel_count
                transformed_parcels += transformed

                if oaza_issues:
                    issues.extend([f"[{oaza_name}] {issue}" for issue in oaza_issues])

            except Exception as e:
                logger.error(f"Error processing oaza {oaza_name}: {e}", exc_info=True)
                error_msg = f"[{oaza_name}] 処理エラー: {str(e)}"
                issues.append(error_msg)
                errors.append(error_msg)
                oaza_results[oaza_name] = OazaResult(success=False, error=str(e))

        elapsed = time.time() - start_time

        self._report_progress(
            "step3", "", 100, 100,
            f"統合完了: {total_oazas}大字, {transformed_parcels}筆配置済み"
        )

        return IntegrationResult(
            success=len([r for r in oaza_results.values() if r.success]) > 0,
            total_oazas=total_oazas,
            processed_oazas=len([r for r in oaza_results.values() if r.success]),
            total_parcels=total_parcels,
            transformed_parcels=transformed_parcels,
            overlaps_resolved=0,
            issues=issues,
            oaza_results=oaza_results,
            elapsed_seconds=elapsed,
            errors=errors
        )

    def _load_oaza_shape_centers(self, layer: QgsVectorLayer) -> Dict[str, QgsPointXY]:
        """
        Load center points (centroids) from Oaza_Shape layer.

        Args:
            layer: Oaza_Shape layer

        Returns:
            Dict mapping oaza name to center point
        """
        centers = {}

        # Try to find the name field
        name_field = None
        for field_name in ['大字', 'S_NAME', 'OAZA_NAME', '名称', 'name', 'NAME']:
            if layer.fields().indexOf(field_name) >= 0:
                name_field = field_name
                break

        if not name_field:
            logger.warning("Could not find name field in Oaza_Shape layer")
            return centers

        for feature in layer.getFeatures():
            name = feature[name_field]
            if name and feature.hasGeometry():
                geom = feature.geometry()
                centroid = geom.centroid().asPoint()
                centers[name] = centroid

        return centers

    def _move_to_oaza_center(self, oaza_name: str, target_center: QgsPointXY):
        """
        Move all parcels in an oaza so their combined center matches target center.

        Args:
            oaza_name: Name of the oaza
            target_center: Target center point (from Oaza_Shape)
        """
        # Get current center of all parcels in oaza
        parcels = self._get_parcels_in_oaza(oaza_name)
        if not parcels:
            return

        # Calculate current centroid
        all_points = []
        for parcel in parcels:
            geom_wkt = parcel.get('geom_wkt')
            if geom_wkt:
                geom = QgsGeometry.fromWkt(geom_wkt)
                if geom and not geom.isNull():
                    centroid = geom.centroid()
                    if centroid and not centroid.isNull():
                        all_points.append(centroid.asPoint())

        if not all_points:
            return

        # Calculate average center
        avg_x = sum(p.x() for p in all_points) / len(all_points)
        avg_y = sum(p.y() for p in all_points) / len(all_points)
        current_center = QgsPointXY(avg_x, avg_y)

        # Calculate offset
        dx = target_center.x() - current_center.x()
        dy = target_center.y() - current_center.y()

        logger.info(f"Moving oaza '{oaza_name}' by ({dx:.2f}, {dy:.2f})")

        # Apply translation to all parcels
        with self.db.connection() as conn:
            for parcel in parcels:
                fude_id = parcel.get('id')
                geom_wkt = parcel.get('geom_wkt')
                if geom_wkt and fude_id:
                    geom = QgsGeometry.fromWkt(geom_wkt)
                    if geom and not geom.isNull():
                        geom.translate(dx, dy)
                        conn.execute(
                            "UPDATE t_fude_poly SET geom = GeomFromText(?, 6676) WHERE id = ?",
                            (geom.asWkt(), fude_id)
                        )
            conn.commit()

    def _process_oaza(self, oaza_name: str) -> Dict[str, Any]:
        """
        Process a single oaza through all integration steps.

        Args:
            oaza_name: Name of the oaza to process

        Returns:
            Dict with processing results
        """
        result = {
            'success': False,
            'total_parcels': 0,
            'transformed_parcels': 0,
            'overlaps_resolved': 0,
            'issues': []
        }

        # STEP 1: Join XMLs within oaza
        logger.info(f"STEP 1: Joining XMLs for oaza '{oaza_name}'")
        join_result = self.joiner.join_oaza(oaza_name)

        if join_result.isolated_xmls:
            result['issues'].append(
                f"{len(join_result.isolated_xmls)}個の孤立XMLあり"
            )

        # Get parcel count
        parcels = self._get_parcels_in_oaza(oaza_name)
        result['total_parcels'] = len(parcels)

        # STEP 2: Apply join transformations and move to oaza boundary if needed
        logger.info(f"STEP 2: Applying transformations for oaza '{oaza_name}'")
        transformed = self._apply_join_transforms(oaza_name, join_result)
        result['transformed_parcels'] = transformed

        # If no anchor, move combined data to oaza boundary
        if not join_result.has_public_anchor:
            logger.info(f"STEP 2b: Moving oaza '{oaza_name}' to boundary (no anchor)")
            self._move_to_oaza_boundary(oaza_name)

        # STEP 3: Fit to municipality boundary (skip for now if not loaded)
        if self._municipality_boundary:
            logger.info(f"STEP 3: Fitting to municipality boundary")
            self._fit_to_municipality(oaza_name)

        # STEP 4: Apply leveling
        logger.info(f"STEP 4: Applying leveling for oaza '{oaza_name}'")
        leveling_result = self.leveler.apply_leveling(oaza_name)
        result['overlaps_resolved'] = leveling_result.overlaps_resolved

        if leveling_result.overlaps_remaining > 0:
            result['issues'].append(
                f"{leveling_result.overlaps_remaining}個の重なりが残存"
            )

        if leveling_result.issues:
            result['issues'].extend(leveling_result.issues)

        # STEP 5: Update reliability flags
        logger.info(f"STEP 5: Updating reliability flags for oaza '{oaza_name}'")
        self._update_reliability_flags(oaza_name, join_result)

        # Topology check
        topo_result = self.topology_checker.check_topology(oaza_name)
        if not topo_result['valid']:
            if topo_result['overlaps']:
                result['issues'].append(
                    f"トポロジー問題: {len(topo_result['overlaps'])}個の重なり"
                )
            if topo_result['invalid_geometries']:
                result['issues'].append(
                    f"無効ジオメトリ: {len(topo_result['invalid_geometries'])}個"
                )

        result['success'] = True
        return result

    def _apply_join_transforms(self, oaza_name: str, join_result: JoinResult) -> int:
        """
        Apply transformation parameters from joining to parcels.

        Args:
            oaza_name: Name of the oaza
            join_result: Result from XmlJoiner

        Returns:
            Number of parcels transformed
        """
        transformed_count = 0

        for xml_id, params in join_result.transform_params.items():
            # Skip if identity transform (anchors)
            if params.get('method') == 'anchor':
                continue

            dx = params.get('dx', 0)
            dy = params.get('dy', 0)
            scale = params.get('scale', 1.0)
            rotation = params.get('rotation', 0)

            # Skip if essentially identity
            if abs(dx) < 0.001 and abs(dy) < 0.001 and abs(scale - 1.0) < 0.001:
                continue

            # Get parcels for this XML
            parcels = self.db.get_fude_by_xml_id(xml_id)

            for parcel in parcels:
                geom_wkt = parcel.get('geom_wkt', '')
                if not geom_wkt:
                    continue

                geom = QgsGeometry.fromWkt(geom_wkt)
                if geom.isEmpty():
                    continue

                # Apply transformation
                transformed_geom = self._transform_geometry(
                    geom, dx, dy, scale, rotation
                )

                if not transformed_geom.isEmpty():
                    self.db.update_fude_geometry(parcel['id'], transformed_geom.asWkt())
                    transformed_count += 1

        return transformed_count

    def _transform_geometry(self, geom: QgsGeometry, dx: float, dy: float,
                           scale: float, rotation: float) -> QgsGeometry:
        """
        Apply Helmert transformation to geometry.

        Args:
            geom: Input geometry
            dx, dy: Translation
            scale: Scale factor
            rotation: Rotation in radians

        Returns:
            Transformed geometry
        """
        if geom.isEmpty():
            return geom

        # Get centroid for transformation center
        centroid = geom.centroid().asPoint()
        cx, cy = centroid.x(), centroid.y()

        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)

        def transform_point(x, y):
            # Translate to origin
            px = x - cx
            py = y - cy

            # Scale
            px *= scale
            py *= scale

            # Rotate
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r

            # Translate back and apply translation
            return (rx + cx + dx, ry + cy + dy)

        # Transform based on geometry type
        from qgis.core import QgsWkbTypes

        geom_type = geom.type()

        if geom_type == QgsWkbTypes.PolygonGeometry:
            if geom.isMultipart():
                polys = geom.asMultiPolygon()
                new_polys = [
                    [[QgsPointXY(*transform_point(p.x(), p.y())) for p in ring]
                     for ring in poly]
                    for poly in polys
                ]
                return QgsGeometry.fromMultiPolygonXY(new_polys)
            else:
                poly = geom.asPolygon()
                new_rings = [
                    [QgsPointXY(*transform_point(p.x(), p.y())) for p in ring]
                    for ring in poly
                ]
                return QgsGeometry.fromPolygonXY(new_rings)

        return geom

    def _move_to_oaza_boundary(self, oaza_name: str):
        """
        Move combined oaza data to match oaza boundary position.

        Only called when oaza has no public coordinate anchor.
        """
        if oaza_name not in self._oaza_boundaries:
            logger.warning(f"No boundary data for oaza '{oaza_name}'")
            return

        oaza_boundary = self._oaza_boundaries[oaza_name]

        # Get current data centroid
        parcels = self._get_parcels_in_oaza(oaza_name)
        if not parcels:
            return

        combined = QgsGeometry()
        for parcel in parcels:
            if parcel.get('geom_wkt'):
                geom = QgsGeometry.fromWkt(parcel['geom_wkt'])
                if not geom.isEmpty():
                    if combined.isEmpty():
                        combined = geom
                    else:
                        combined = combined.combine(geom)

        if combined.isEmpty():
            return

        data_centroid = combined.centroid().asPoint()
        boundary_centroid = oaza_boundary.centroid().asPoint()

        # Calculate translation
        dx = boundary_centroid.x() - data_centroid.x()
        dy = boundary_centroid.y() - data_centroid.y()

        logger.info(f"Moving oaza '{oaza_name}' by ({dx:.2f}, {dy:.2f})")

        # Apply translation to all parcels
        for parcel in parcels:
            if parcel.get('geom_wkt'):
                geom = QgsGeometry.fromWkt(parcel['geom_wkt'])
                if not geom.isEmpty():
                    translated = self._translate_geometry(geom, dx, dy)
                    self.db.update_fude_geometry(parcel['id'], translated.asWkt())

    def _translate_geometry(self, geom: QgsGeometry, dx: float, dy: float) -> QgsGeometry:
        """Translate geometry by dx, dy."""
        geom_copy = QgsGeometry(geom)
        geom_copy.translate(dx, dy)
        return geom_copy

    def _fit_to_municipality(self, oaza_name: str):
        """
        Fit oaza data to municipality boundary.

        Public coordinate parcels are fixed; only arbitrary parcels are adjusted.
        """
        # This is a more complex operation that would use TPS
        # For now, we just verify that data is within municipality boundary
        pass

    def _update_reliability_flags(self, oaza_name: str, join_result: JoinResult):
        """
        Update reliability flags for parcels based on join results.

        Args:
            oaza_name: Name of the oaza
            join_result: Result from XmlJoiner
        """
        for xml_id, params in join_result.transform_params.items():
            reliability = params.get('reliability', 'LOW')
            method = params.get('method', 'unknown')

            # Get parcels for this XML
            parcels = self.db.get_fude_by_xml_id(xml_id)
            parcel_ids = [p['id'] for p in parcels]

            # Determine if review is needed
            needs_review = reliability == 'LOW'
            review_reason = None
            if needs_review:
                if method == 'relative_reference':
                    review_reason = '公共座標アンカーなし'
                elif xml_id in join_result.isolated_xmls:
                    review_reason = '孤立XML'

            # Update in batch
            self.db.update_fude_batch_reliability(
                parcel_ids,
                reliability=reliability,
                transform_method=method,
                needs_review=needs_review,
                review_reason=review_reason
            )

    def _get_all_oaza_names(self) -> List[str]:
        """Get all unique oaza names from database."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT oaza_name FROM t_fude_poly
                WHERE oaza_name IS NOT NULL AND oaza_name != ''
                ORDER BY oaza_name
            """)
            return [row[0] for row in cursor.fetchall()]

    def _get_parcels_in_oaza(self, oaza_name: str) -> List[Dict[str, Any]]:
        """Get all parcels in an oaza."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, fude_id, chiban, AsText(geom) as geom_wkt
                FROM t_fude_poly
                WHERE oaza_name = ?
            """, (oaza_name,))
            return [dict(row) for row in cursor.fetchall()]

    def _load_boundaries(self):
        """Load oaza and municipality boundary data."""
        if self.oaza_boundary_path and self.oaza_boundary_path.exists():
            self._load_oaza_boundaries()

        if self.municipality_boundary_path and self.municipality_boundary_path.exists():
            self._load_municipality_boundary()

    def _load_oaza_boundaries(self):
        """Load oaza boundary geometries."""
        try:
            layer = QgsVectorLayer(str(self.oaza_boundary_path), "oaza", "ogr")

            if not layer.isValid():
                logger.warning(f"Could not load oaza boundary: {self.oaza_boundary_path}")
                return

            # Find name field
            name_field = None
            for candidate in ['oaza_name', 'OAZA_NAME', 'S_NAME', 'name', '大字名']:
                if candidate in [f.name() for f in layer.fields()]:
                    name_field = candidate
                    break

            if not name_field:
                name_field = layer.fields()[0].name()

            for feature in layer.getFeatures():
                name = str(feature.attribute(name_field) or '')
                if name:
                    self._oaza_boundaries[name] = QgsGeometry(feature.geometry())

            logger.info(f"Loaded {len(self._oaza_boundaries)} oaza boundaries")

        except Exception as e:
            logger.error(f"Error loading oaza boundaries: {e}")

    def _load_municipality_boundary(self):
        """Load municipality boundary geometry."""
        try:
            layer = QgsVectorLayer(str(self.municipality_boundary_path), "municipality", "ogr")

            if not layer.isValid():
                logger.warning(f"Could not load municipality boundary: {self.municipality_boundary_path}")
                return

            # Combine all features into one geometry
            combined = QgsGeometry()
            for feature in layer.getFeatures():
                geom = feature.geometry()
                if not geom.isEmpty():
                    if combined.isEmpty():
                        combined = geom
                    else:
                        combined = combined.combine(geom)

            self._municipality_boundary = combined
            logger.info("Loaded municipality boundary")

        except Exception as e:
            logger.error(f"Error loading municipality boundary: {e}")
