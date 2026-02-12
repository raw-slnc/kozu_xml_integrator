# -*- coding: utf-8 -*-
"""
Spatial Fitting Algorithm for KozuXmlIntegrator

Core transformation logic:
1. Municipality boundary = ultimate container (where everything must fit)
2. Oaza shapes = positioning GUIDES (NOT exact targets - they have gaps)
3. Public coordinate maps = ANCHORS (DO NOT transform these)
4. Chain positioning: public coords → adjacent arbitrary → next adjacent...
5. Chiban matching = hints for relative positioning

Design Philosophy:
- ALL arbitrary maps must be fitted into the municipal space
- Public coordinate maps are fixed anchors
- Position arbitrary maps relative to anchors using chiban relationships
- When no anchors available, use oaza shape as approximate guide
- Oaza_Shape.gpkg is based on forest planning maps (incomplete, inaccurate)
"""

from typing import List, Tuple, Optional, Dict, Any, Set
from dataclasses import dataclass, field
import math
import logging

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
    QgsFeature,
    QgsFeatureRequest,
    QgsWkbTypes,
)

logger = logging.getLogger(__name__)


@dataclass
class TransformParams:
    """Transformation parameters for a map."""
    translation_x: float
    translation_y: float
    scale: float
    rotation: float  # radians

    # Metadata
    source_xml_id: int
    source_centroid: Tuple[float, float]
    method: str  # 'anchor_chiban', 'chain', 'oaza_guide', 'municipality_fallback'
    anchor_xml_id: Optional[int] = None
    confidence: float = 1.0  # 0-1, how reliable the transformation is

    @property
    def rotation_degrees(self) -> float:
        return math.degrees(self.rotation)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_xml_id': self.source_xml_id,
            'method': self.method,
            'translation': (round(self.translation_x, 2), round(self.translation_y, 2)),
            'scale': round(self.scale, 4),
            'rotation_deg': round(self.rotation_degrees, 2),
            'confidence': round(self.confidence, 2),
            'anchor_xml_id': self.anchor_xml_id,
        }


class PositioningGuideLoader:
    """
    Loads positioning guides (Oaza shapes, municipality boundaries).

    These are GUIDES for approximate positioning, NOT exact targets.
    Oaza shapes have gaps (non-forest areas) and are based on
    forest planning maps which may not be accurate.
    """

    def __init__(self):
        self._oaza_layer: Optional[QgsVectorLayer] = None
        self._municipality_layer: Optional[QgsVectorLayer] = None
        self._oaza_cache: Dict[str, QgsGeometry] = {}
        self._municipality_geom: Optional[QgsGeometry] = None

    def load_oaza_layer(self, gpkg_path: str) -> bool:
        """Load Oaza shape layer as positioning GUIDE (not exact target)."""
        try:
            self._oaza_layer = QgsVectorLayer(gpkg_path, "oaza_guide", "ogr")

            if not self._oaza_layer.isValid():
                for layer_name in ['oaza', 'Oaza', 'OAZA', 'oaza_shape']:
                    uri = f"{gpkg_path}|layername={layer_name}"
                    self._oaza_layer = QgsVectorLayer(uri, "oaza_guide", "ogr")
                    if self._oaza_layer.isValid():
                        break

            if not self._oaza_layer.isValid():
                logger.warning(f"Could not load Oaza guide layer from {gpkg_path}")
                return False

            self._oaza_cache.clear()
            logger.info(f"Loaded Oaza guide layer: {self._oaza_layer.featureCount()} features")
            return True

        except Exception as e:
            logger.error(f"Error loading Oaza guide layer: {e}", exc_info=True)
            return False

    def load_municipality_layer(self, gpkg_path: str) -> bool:
        """Load municipality boundary as the ultimate container."""
        try:
            self._municipality_layer = QgsVectorLayer(gpkg_path, "municipality", "ogr")

            if not self._municipality_layer.isValid():
                for layer_name in ['municipality', 'admin', 'boundary', 'N03']:
                    uri = f"{gpkg_path}|layername={layer_name}"
                    self._municipality_layer = QgsVectorLayer(uri, "municipality", "ogr")
                    if self._municipality_layer.isValid():
                        break

            if not self._municipality_layer.isValid():
                logger.warning(f"Could not load municipality layer from {gpkg_path}")
                return False

            # Cache combined geometry
            combined = QgsGeometry()
            for feature in self._municipality_layer.getFeatures():
                geom = feature.geometry()
                if not geom.isEmpty():
                    if combined.isEmpty():
                        combined = geom
                    else:
                        combined = combined.combine(geom)

            self._municipality_geom = combined
            logger.info(f"Loaded municipality layer as container")
            return True

        except Exception as e:
            logger.error(f"Error loading municipality layer: {e}", exc_info=True)
            return False

    @property
    def has_oaza_guide(self) -> bool:
        return self._oaza_layer is not None and self._oaza_layer.isValid()

    @property
    def has_municipality(self) -> bool:
        return self._municipality_geom is not None and not self._municipality_geom.isEmpty()

    @property
    def municipality_geometry(self) -> Optional[QgsGeometry]:
        return self._municipality_geom

    def get_oaza_guide_geometry(self, oaza_name: str) -> Optional[QgsGeometry]:
        """
        Get Oaza shape as positioning GUIDE.

        Note: This is approximate - Oaza shapes have gaps and may not be accurate.
        """
        if oaza_name in self._oaza_cache:
            return self._oaza_cache[oaza_name]

        if not self.has_oaza_guide:
            return None

        name_field = self._find_name_field(self._oaza_layer,
            ['大字', '大字名', 'oaza_name', 'OAZA_NAME', 'S_NAME', 'name', 'NAME'])

        if not name_field:
            return None

        # Try exact match
        request = QgsFeatureRequest()
        request.setFilterExpression(f"\"{name_field}\" = '{oaza_name}'")

        for feature in self._oaza_layer.getFeatures(request):
            geom = feature.geometry()
            if not geom.isEmpty():
                self._oaza_cache[oaza_name] = geom
                return geom

        # Try partial match
        for feature in self._oaza_layer.getFeatures():
            attr_name = str(feature.attribute(name_field) or '')
            if oaza_name in attr_name or attr_name in oaza_name:
                geom = feature.geometry()
                if not geom.isEmpty():
                    self._oaza_cache[oaza_name] = geom
                    logger.info(f"Found Oaza guide '{oaza_name}' via partial match")
                    return geom

        return None

    def get_oaza_names(self) -> List[str]:
        """Get list of available Oaza names from guide layer."""
        if not self.has_oaza_guide:
            return []

        names = set()
        name_field = self._find_name_field(self._oaza_layer,
            ['大字', '大字名', 'oaza_name', 'OAZA_NAME', 'S_NAME', 'name', 'NAME'])

        if name_field:
            for feature in self._oaza_layer.getFeatures():
                name = feature.attribute(name_field)
                if name:
                    names.add(str(name))

        return sorted(names)

    def _find_name_field(self, layer: QgsVectorLayer, candidates: List[str]) -> Optional[str]:
        if not layer:
            return None

        field_names = [f.name() for f in layer.fields()]

        for candidate in candidates:
            if candidate in field_names:
                return candidate

        return field_names[0] if field_names else None


class ChainPositioner:
    """
    Chain positioning algorithm.

    Positions arbitrary coordinate maps in a chain starting from
    public coordinate anchors:

    1. Public coordinate maps = fixed anchors (never transform)
    2. Find arbitrary maps with chiban matches to anchors
    3. Calculate position relative to anchor using matched chibans
    4. Use newly positioned maps as secondary anchors
    5. Continue until all maps are positioned
    6. Fallback: Use Oaza guide for maps with no anchor relationships
    """

    def __init__(self, db_manager, guide_loader: PositioningGuideLoader):
        """
        Initialize positioner.

        Args:
            db_manager: DatabaseManager instance
            guide_loader: PositioningGuideLoader with loaded guides
        """
        self.db = db_manager
        self.guide = guide_loader
        self._positioned: Dict[int, TransformParams] = {}
        self._anchors: Set[int] = set()  # Public coordinate map IDs

    def position_single_map(self, xml_id: int) -> Optional[TransformParams]:
        """
        Position a single arbitrary coordinate map.

        This is useful for interactive single-map transformation.
        Uses the same logic as batch positioning but for one map.

        Args:
            xml_id: ID of the map to position

        Returns:
            TransformParams if successful, None otherwise
        """
        # Initialize anchors if not done
        if not self._anchors:
            self._identify_anchors()

        # Check if map is already a public coordinate anchor
        if xml_id in self._anchors:
            logger.info(f"Map {xml_id} is a public coordinate anchor - no transformation needed")
            return None

        # Get map info
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, file_name, fude_count
                    FROM t_xml_meta WHERE id = ?
                """, (xml_id,))
                row = cursor.fetchone()
                if not row:
                    return None
                map_info = dict(row)
        except Exception as e:
            logger.error(f"Error getting map info: {e}")
            return None

        # Try positioning methods in order
        params = self._try_position_map(xml_id, map_info)

        if params is None:
            params = self._fallback_position(xml_id, map_info)

        if params:
            self._positioned[xml_id] = params
            logger.info(f"Positioned single map {xml_id} using {params.method}")

        return params

    def position_all_maps(self, exclude_outside_district: bool = True) -> Dict[int, TransformParams]:
        """
        Position all arbitrary coordinate maps.

        Returns:
            Dict mapping xml_meta_id to TransformParams
        """
        self._positioned.clear()
        self._anchors.clear()

        # Step 1: Identify anchors (public coordinate maps)
        self._identify_anchors()

        if not self._anchors:
            logger.warning("No public coordinate anchors found")

        # Step 2: Get all arbitrary coordinate maps to position
        arbitrary_maps = self._get_arbitrary_maps()
        logger.info(f"Need to position {len(arbitrary_maps)} arbitrary maps")

        if not arbitrary_maps:
            return {}

        # Step 3: Chain positioning in waves
        positioned_this_wave = True
        wave = 0

        while positioned_this_wave and len(self._positioned) < len(arbitrary_maps):
            wave += 1
            positioned_this_wave = False

            for xml_id, map_info in arbitrary_maps.items():
                if xml_id in self._positioned:
                    continue

                params = self._try_position_map(xml_id, map_info)
                if params:
                    self._positioned[xml_id] = params
                    positioned_this_wave = True
                    logger.debug(f"Wave {wave}: Positioned map {xml_id} using {params.method}")

        # Step 4: Fallback for remaining maps
        for xml_id, map_info in arbitrary_maps.items():
            if xml_id not in self._positioned:
                params = self._fallback_position(xml_id, map_info)
                if params:
                    self._positioned[xml_id] = params
                    logger.info(f"Fallback positioned map {xml_id}")

        logger.info(f"Positioned {len(self._positioned)}/{len(arbitrary_maps)} maps")
        return self._positioned

    def get_anchors(self) -> Set[int]:
        """Get the set of public coordinate anchor IDs."""
        if not self._anchors:
            self._identify_anchors()
        return self._anchors.copy()

    def _identify_anchors(self):
        """Identify public coordinate maps as anchors."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id FROM t_xml_meta
                    WHERE crs_type != '任意座標系'
                    AND crs_type NOT LIKE '%任意%'
                """)
                self._anchors = {row[0] for row in cursor.fetchall()}

            logger.info(f"Identified {len(self._anchors)} public coordinate anchors")

        except Exception as e:
            logger.error(f"Error identifying anchors: {e}", exc_info=True)

    def _get_arbitrary_maps(self) -> Dict[int, Dict[str, Any]]:
        """Get all arbitrary coordinate maps that need positioning."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, file_name, fude_count
                    FROM t_xml_meta
                    WHERE crs_type = '任意座標系'
                """)
                return {row['id']: dict(row) for row in cursor.fetchall()}

        except Exception as e:
            logger.error(f"Error getting arbitrary maps: {e}", exc_info=True)
            return {}

    def _try_position_map(self, xml_id: int, map_info: Dict) -> Optional[TransformParams]:
        """
        Try to position a map using available anchors.

        Priority:
        1. Direct chiban match to public coordinate anchor
        2. Chiban match to already-positioned map
        """
        # Get map's oaza and parcels
        source_parcels = self.db.get_fude_by_xml_id(xml_id)
        if not source_parcels:
            return None

        source_chibans = {p.get('chiban'): p for p in source_parcels if p.get('chiban')}
        source_oazas = set(p.get('oaza_name') for p in source_parcels if p.get('oaza_name'))

        # Try matching to anchors first
        for anchor_id in self._anchors:
            params = self._match_to_anchor(xml_id, source_parcels, source_chibans, anchor_id)
            if params:
                return params

        # Try matching to already-positioned maps
        for positioned_id in self._positioned:
            params = self._match_to_positioned(xml_id, source_parcels, source_chibans, positioned_id)
            if params:
                return params

        return None

    def _match_to_anchor(self, xml_id: int, source_parcels: List, source_chibans: Dict,
                        anchor_id: int) -> Optional[TransformParams]:
        """Try to match source map to a public coordinate anchor."""
        anchor_parcels = self.db.get_fude_by_xml_id(anchor_id)
        if not anchor_parcels:
            return None

        anchor_chibans = {p.get('chiban'): p for p in anchor_parcels if p.get('chiban')}

        # Find common chibans
        common = set(source_chibans.keys()) & set(anchor_chibans.keys())
        if len(common) < 2:  # Need at least 2 points for transformation
            return None

        # Calculate transformation from matched chibans
        return self._calculate_transform_from_matches(
            xml_id, source_chibans, anchor_chibans, common,
            anchor_id, 'anchor_chiban'
        )

    def _match_to_positioned(self, xml_id: int, source_parcels: List, source_chibans: Dict,
                            positioned_id: int) -> Optional[TransformParams]:
        """Try to match source map to an already-positioned map."""
        positioned_parcels = self.db.get_fude_by_xml_id(positioned_id)
        if not positioned_parcels:
            return None

        positioned_chibans = {p.get('chiban'): p for p in positioned_parcels if p.get('chiban')}

        # Find common chibans
        common = set(source_chibans.keys()) & set(positioned_chibans.keys())
        if len(common) < 2:
            return None

        # Get the positioned map's transform
        base_params = self._positioned[positioned_id]

        # Calculate transformation relative to positioned map
        return self._calculate_transform_from_matches(
            xml_id, source_chibans, positioned_chibans, common,
            positioned_id, 'chain', base_params
        )

    def _calculate_transform_from_matches(self, xml_id: int,
                                         source_chibans: Dict, target_chibans: Dict,
                                         common_chibans: Set,
                                         anchor_id: int, method: str,
                                         base_params: TransformParams = None
                                         ) -> Optional[TransformParams]:
        """Calculate transformation parameters from matched chibans."""
        # Extract control points (centroids of matched parcels)
        source_points = []
        target_points = []

        for chiban in common_chibans:
            src_parcel = source_chibans[chiban]
            tgt_parcel = target_chibans[chiban]

            src_geom_wkt = src_parcel.get('geom_wkt', '')
            tgt_geom_wkt = tgt_parcel.get('geom_wkt', '')

            if not src_geom_wkt or not tgt_geom_wkt:
                continue

            src_geom = QgsGeometry.fromWkt(src_geom_wkt)
            tgt_geom = QgsGeometry.fromWkt(tgt_geom_wkt)

            if src_geom.isEmpty() or tgt_geom.isEmpty():
                continue

            src_centroid = src_geom.centroid().asPoint()
            tgt_centroid = tgt_geom.centroid().asPoint()

            # If chaining from a positioned map, apply that transform first
            if base_params:
                transformer = GeometryTransformer(base_params)
                tx, ty = transformer.transform_point(tgt_centroid.x(), tgt_centroid.y())
                tgt_centroid = QgsPointXY(tx, ty)

            source_points.append((src_centroid.x(), src_centroid.y()))
            target_points.append((tgt_centroid.x(), tgt_centroid.y()))

        if len(source_points) < 2:
            return None

        # Calculate Helmert transformation parameters
        params = self._compute_helmert(source_points, target_points)

        if params is None:
            return None

        # Calculate source centroid for the transformation
        source_envelope = self._get_map_envelope(xml_id)
        if source_envelope:
            centroid = source_envelope.centroid().asPoint()
            source_centroid = (centroid.x(), centroid.y())
        else:
            # Use average of source points
            source_centroid = (
                sum(p[0] for p in source_points) / len(source_points),
                sum(p[1] for p in source_points) / len(source_points)
            )

        dx, dy, scale, rotation = params

        confidence = min(1.0, len(common_chibans) / 10.0)  # More matches = more confidence

        return TransformParams(
            translation_x=dx,
            translation_y=dy,
            scale=scale,
            rotation=rotation,
            source_xml_id=xml_id,
            source_centroid=source_centroid,
            method=method,
            anchor_xml_id=anchor_id,
            confidence=confidence,
        )

    def _compute_helmert(self, source_pts: List[Tuple[float, float]],
                        target_pts: List[Tuple[float, float]]
                        ) -> Optional[Tuple[float, float, float, float]]:
        """
        Compute Helmert transformation parameters using least squares.

        Returns: (dx, dy, scale, rotation) or None if failed
        """
        import numpy as np

        n = len(source_pts)
        if n < 2:
            return None

        # Build matrices for least squares
        # X' = a*X - b*Y + dx
        # Y' = b*X + a*Y + dy
        # where a = s*cos(theta), b = s*sin(theta)

        A = np.zeros((2*n, 4))
        b = np.zeros(2*n)

        for i, (src, tgt) in enumerate(zip(source_pts, target_pts)):
            sx, sy = src
            tx, ty = tgt

            A[2*i, 0] = sx    # a coefficient
            A[2*i, 1] = -sy   # b coefficient
            A[2*i, 2] = 1     # dx
            A[2*i, 3] = 0     # dy

            A[2*i+1, 0] = sy  # a coefficient
            A[2*i+1, 1] = sx  # b coefficient
            A[2*i+1, 2] = 0   # dx
            A[2*i+1, 3] = 1   # dy

            b[2*i] = tx
            b[2*i+1] = ty

        try:
            # Solve least squares
            result, residuals, rank, s = np.linalg.lstsq(A, b, rcond=None)

            a, b_val, dx, dy = result

            # Extract scale and rotation
            scale = math.sqrt(a*a + b_val*b_val)
            rotation = math.atan2(b_val, a)

            # Sanity checks
            if scale < 0.01 or scale > 100:
                logger.warning(f"Unreasonable scale: {scale}")
                return None

            return (dx, dy, scale, rotation)

        except Exception as e:
            logger.error(f"Helmert computation failed: {e}")
            return None

    def _fallback_position(self, xml_id: int, map_info: Dict) -> Optional[TransformParams]:
        """
        Fallback positioning when no anchor relationship exists.

        Uses Oaza guide shape for approximate positioning.
        """
        # Get map's oaza
        source_parcels = self.db.get_fude_by_xml_id(xml_id)
        if not source_parcels:
            return None

        source_oazas = set(p.get('oaza_name') for p in source_parcels if p.get('oaza_name'))

        if not source_oazas:
            return self._municipality_fallback(xml_id, source_parcels)

        oaza_name = list(source_oazas)[0]  # Use first oaza

        # Get source envelope
        source_envelope = self._get_map_envelope(xml_id)
        if not source_envelope:
            return None

        # Get Oaza guide geometry
        oaza_guide = self.guide.get_oaza_guide_geometry(oaza_name)

        if oaza_guide and not oaza_guide.isEmpty():
            return self._fit_to_oaza_guide(xml_id, source_envelope, oaza_guide)

        # Final fallback: municipality boundary
        return self._municipality_fallback(xml_id, source_parcels)

    def _fit_to_oaza_guide(self, xml_id: int, source_envelope: QgsGeometry,
                          oaza_guide: QgsGeometry) -> TransformParams:
        """
        Fit map to Oaza guide (approximate positioning).

        Note: Oaza shapes are guides, not exact targets.
        """
        source_centroid = source_envelope.centroid().asPoint()
        guide_centroid = oaza_guide.centroid().asPoint()

        # Translation to guide centroid
        dx = guide_centroid.x() - source_centroid.x()
        dy = guide_centroid.y() - source_centroid.y()

        # Scale based on area ratio (but limited - Oaza guide is approximate)
        source_area = source_envelope.area()
        guide_area = oaza_guide.area()

        if source_area > 1e-10:
            raw_scale = math.sqrt(guide_area / source_area)
            # Limit scale more conservatively since Oaza guide is approximate
            scale = max(0.5, min(2.0, raw_scale))
        else:
            scale = 1.0

        return TransformParams(
            translation_x=dx,
            translation_y=dy,
            scale=scale,
            rotation=0.0,  # No rotation for guide-based positioning
            source_xml_id=xml_id,
            source_centroid=(source_centroid.x(), source_centroid.y()),
            method='oaza_guide',
            confidence=0.5,  # Lower confidence for guide-based positioning
        )

    def _municipality_fallback(self, xml_id: int, source_parcels: List) -> Optional[TransformParams]:
        """
        Final fallback: position somewhere in municipality.

        This is the last resort when no other positioning is available.
        """
        if not self.guide.has_municipality:
            logger.warning(f"No municipality container for map {xml_id}")
            return None

        source_envelope = self._get_map_envelope(xml_id)
        if not source_envelope:
            return None

        source_centroid = source_envelope.centroid().asPoint()
        municipality_centroid = self.guide.municipality_geometry.centroid().asPoint()

        dx = municipality_centroid.x() - source_centroid.x()
        dy = municipality_centroid.y() - source_centroid.y()

        return TransformParams(
            translation_x=dx,
            translation_y=dy,
            scale=1.0,  # No scaling for fallback
            rotation=0.0,
            source_xml_id=xml_id,
            source_centroid=(source_centroid.x(), source_centroid.y()),
            method='municipality_fallback',
            confidence=0.2,  # Low confidence
        )

    def _get_map_envelope(self, xml_id: int) -> Optional[QgsGeometry]:
        """Get map envelope geometry."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT AsText(geom) as geom_wkt
                    FROM t_xml_meta
                    WHERE id = ?
                """, (xml_id,))
                row = cursor.fetchone()

                if row and row['geom_wkt']:
                    geom = QgsGeometry.fromWkt(row['geom_wkt'])
                    if not geom.isEmpty():
                        return geom

            # Fallback: compute from parcels
            parcels = self.db.get_fude_by_xml_id(xml_id)
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

            return combined.convexHull() if not combined.isEmpty() else None

        except Exception as e:
            logger.error(f"Error getting map envelope: {e}")
            return None


class GeometryTransformer:
    """
    Transforms geometries using TransformParams.
    """

    def __init__(self, params: TransformParams):
        self.params = params

        self.scale = params.scale
        self.cos_r = math.cos(params.rotation)
        self.sin_r = math.sin(params.rotation)
        self.cx, self.cy = params.source_centroid
        self.dx = params.translation_x
        self.dy = params.translation_y

    def transform_point(self, x: float, y: float) -> Tuple[float, float]:
        """Transform a single point."""
        # Translate to origin
        px = x - self.cx
        py = y - self.cy

        # Scale
        px *= self.scale
        py *= self.scale

        # Rotate
        rx = px * self.cos_r - py * self.sin_r
        ry = px * self.sin_r + py * self.cos_r

        # Translate to target
        return (rx + self.cx + self.dx, ry + self.cy + self.dy)

    def transform_geometry(self, geom: QgsGeometry) -> QgsGeometry:
        """Transform a complete geometry."""
        if geom.isEmpty():
            return geom

        geom_type = geom.type()

        if geom_type == QgsWkbTypes.PointGeometry:
            pt = geom.asPoint()
            nx, ny = self.transform_point(pt.x(), pt.y())
            return QgsGeometry.fromPointXY(QgsPointXY(nx, ny))

        elif geom_type == QgsWkbTypes.LineGeometry:
            if geom.isMultipart():
                lines = geom.asMultiPolyline()
                new_lines = [[QgsPointXY(*self.transform_point(p.x(), p.y())) for p in line] for line in lines]
                return QgsGeometry.fromMultiPolylineXY(new_lines)
            else:
                line = geom.asPolyline()
                new_line = [QgsPointXY(*self.transform_point(p.x(), p.y())) for p in line]
                return QgsGeometry.fromPolylineXY(new_line)

        elif geom_type == QgsWkbTypes.PolygonGeometry:
            if geom.isMultipart():
                polys = geom.asMultiPolygon()
                new_polys = [[[QgsPointXY(*self.transform_point(p.x(), p.y())) for p in ring] for ring in poly] for poly in polys]
                return QgsGeometry.fromMultiPolygonXY(new_polys)
            else:
                poly = geom.asPolygon()
                new_rings = [[QgsPointXY(*self.transform_point(p.x(), p.y())) for p in ring] for ring in poly]
                return QgsGeometry.fromPolygonXY(new_rings)

        return geom


# Backward compatibility aliases
ContainerLoader = PositioningGuideLoader
RubberSheetFitter = ChainPositioner
SpatialFitResult = TransformParams
SpatialFitTransformer = GeometryTransformer
OazaShapeLoader = PositioningGuideLoader
SpatialFitter = ChainPositioner
