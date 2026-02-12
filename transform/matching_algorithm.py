# -*- coding: utf-8 -*-
"""
Matching Algorithm for KozuXmlIntegrator

Algorithms for finding corresponding points between XML maps:
1. Common chiban (land number) matching
2. Boundary shape matching (Hausdorff distance)

Used to automatically find control point pairs for transformation.
"""

from typing import List, Tuple, Optional, Dict, Any, Set
from dataclasses import dataclass
import math
import logging

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)

logger = logging.getLogger(__name__)


@dataclass
class MatchCandidate:
    """A candidate match between two maps."""
    source_xml_id: int
    target_xml_id: int
    match_score: float
    common_chibans: int
    total_source_parcels: int
    total_target_parcels: int
    matching_pairs: List[Tuple[int, int, str]]  # (source_fude_id, target_fude_id, chiban)

    @property
    def jaccard_index(self) -> float:
        """Jaccard similarity coefficient."""
        total = self.total_source_parcels + self.total_target_parcels - self.common_chibans
        if total == 0:
            return 0.0
        return self.common_chibans / total


@dataclass
class ControlPointPair:
    """A pair of corresponding control points."""
    source_point: Tuple[float, float]
    target_point: Tuple[float, float]
    chiban: str
    source_fude_id: int
    target_fude_id: int


class ChibanMatcher:
    """
    Match maps by finding common land numbers (chiban).

    Uses the chiban (地番) attribute to find parcels that exist
    in both source and target maps, then uses their centroids
    as control points.
    """

    def __init__(self, min_score: float = 0.3):
        """
        Initialize matcher.

        Args:
            min_score: Minimum match score to consider (0-1)
        """
        self.min_score = min_score

    def find_matches(self,
                    source_parcels: List[Dict[str, Any]],
                    target_parcels: List[Dict[str, Any]]
                    ) -> Optional[MatchCandidate]:
        """
        Find matching parcels between source and target.

        Args:
            source_parcels: List of parcel dicts with 'chiban', 'geom_wkt', 'id' keys
            target_parcels: List of parcel dicts with same keys

        Returns:
            MatchCandidate if matches found, None otherwise
        """
        # Build chiban index for target
        target_by_chiban: Dict[str, Dict[str, Any]] = {}
        for parcel in target_parcels:
            chiban = parcel.get('chiban', '')
            if chiban:
                target_by_chiban[chiban] = parcel

        # Find matches
        matching_pairs = []
        source_chibans: Set[str] = set()

        for parcel in source_parcels:
            chiban = parcel.get('chiban', '')
            if chiban and chiban in target_by_chiban:
                source_chibans.add(chiban)
                target_parcel = target_by_chiban[chiban]
                matching_pairs.append((
                    parcel['id'],
                    target_parcel['id'],
                    chiban
                ))

        if not matching_pairs:
            return None

        # Calculate match score
        common_count = len(matching_pairs)
        total = len(source_parcels) + len(target_parcels)
        score = (2 * common_count) / total if total > 0 else 0

        if score < self.min_score:
            logger.debug(f"Match score {score:.3f} below threshold {self.min_score}")
            return None

        return MatchCandidate(
            source_xml_id=0,  # To be filled by caller
            target_xml_id=0,
            match_score=score,
            common_chibans=common_count,
            total_source_parcels=len(source_parcels),
            total_target_parcels=len(target_parcels),
            matching_pairs=matching_pairs
        )

    def get_control_points(self,
                          source_parcels: List[Dict[str, Any]],
                          target_parcels: List[Dict[str, Any]],
                          match: MatchCandidate
                          ) -> List[ControlPointPair]:
        """
        Extract control point pairs from matching parcels.

        Uses centroid of each matching parcel pair.

        Args:
            source_parcels: Source parcel list
            target_parcels: Target parcel list
            match: MatchCandidate from find_matches

        Returns:
            List of ControlPointPair objects
        """
        # Build ID lookup
        source_by_id = {p['id']: p for p in source_parcels}
        target_by_id = {p['id']: p for p in target_parcels}

        control_points = []

        for src_id, tgt_id, chiban in match.matching_pairs:
            src_parcel = source_by_id.get(src_id)
            tgt_parcel = target_by_id.get(tgt_id)

            if not src_parcel or not tgt_parcel:
                continue

            # Get centroids
            src_geom = QgsGeometry.fromWkt(src_parcel.get('geom_wkt', ''))
            tgt_geom = QgsGeometry.fromWkt(tgt_parcel.get('geom_wkt', ''))

            if src_geom.isEmpty() or tgt_geom.isEmpty():
                continue

            src_centroid = src_geom.centroid()
            tgt_centroid = tgt_geom.centroid()

            if src_centroid.isEmpty() or tgt_centroid.isEmpty():
                continue

            src_pt = src_centroid.asPoint()
            tgt_pt = tgt_centroid.asPoint()

            control_points.append(ControlPointPair(
                source_point=(src_pt.x(), src_pt.y()),
                target_point=(tgt_pt.x(), tgt_pt.y()),
                chiban=chiban,
                source_fude_id=src_id,
                target_fude_id=tgt_id
            ))

        logger.info(f"Extracted {len(control_points)} control point pairs")
        return control_points


class ShapeMatcher:
    """
    Match maps by boundary shape similarity.

    Uses Hausdorff distance (normalized) to compare
    map envelope shapes when chiban matching is insufficient.
    """

    def __init__(self, max_hausdorff: float = 0.1):
        """
        Initialize matcher.

        Args:
            max_hausdorff: Maximum normalized Hausdorff distance to accept
        """
        self.max_hausdorff = max_hausdorff

    def compute_similarity(self,
                          source_envelope: QgsGeometry,
                          target_envelope: QgsGeometry
                          ) -> float:
        """
        Compute shape similarity between two envelopes.

        Args:
            source_envelope: Source map envelope polygon
            target_envelope: Target map envelope polygon

        Returns:
            Similarity score (0-1, higher is more similar)
        """
        if source_envelope.isEmpty() or target_envelope.isEmpty():
            return 0.0

        # Normalize geometries to unit square for scale-invariant comparison
        src_normalized = self._normalize_geometry(source_envelope)
        tgt_normalized = self._normalize_geometry(target_envelope)

        # Compute Hausdorff distance on normalized shapes
        hausdorff = src_normalized.hausdorffDistance(tgt_normalized)

        # Convert to similarity (inverse of distance)
        # Max distance for unit square diagonal is sqrt(2)
        max_dist = math.sqrt(2)
        similarity = max(0, 1 - hausdorff / max_dist)

        return similarity

    def _normalize_geometry(self, geom: QgsGeometry) -> QgsGeometry:
        """
        Normalize geometry to fit in unit square centered at origin.

        Args:
            geom: Geometry to normalize

        Returns:
            Normalized geometry
        """
        bbox = geom.boundingBox()
        if bbox.isEmpty():
            return geom

        # Get scale and offset
        width = bbox.width()
        height = bbox.height()
        max_dim = max(width, height, 1e-10)

        center_x = bbox.center().x()
        center_y = bbox.center().y()

        # Transform: translate to origin, then scale
        if geom.type() == 2:  # Polygon
            polygon = geom.asPolygon()
            new_rings = []
            for ring in polygon:
                new_ring = []
                for pt in ring:
                    nx = (pt.x() - center_x) / max_dim
                    ny = (pt.y() - center_y) / max_dim
                    new_ring.append(QgsPointXY(nx, ny))
                new_rings.append(new_ring)
            return QgsGeometry.fromPolygonXY(new_rings)

        return geom

    def find_best_match(self,
                       source_envelope: QgsGeometry,
                       candidate_envelopes: List[Tuple[int, QgsGeometry]]
                       ) -> Optional[Tuple[int, float]]:
        """
        Find best matching candidate by shape similarity.

        Args:
            source_envelope: Source map envelope
            candidate_envelopes: List of (xml_meta_id, envelope) tuples

        Returns:
            Tuple of (best_match_id, similarity) or None
        """
        best_match = None
        best_score = 0.0

        for xml_id, envelope in candidate_envelopes:
            score = self.compute_similarity(source_envelope, envelope)
            if score > best_score:
                best_score = score
                best_match = xml_id

        if best_match is None or best_score < (1 - self.max_hausdorff):
            return None

        return (best_match, best_score)


class BoundaryMatcher:
    """
    Match maps by shared boundary detection.

    Detects maps that share common boundary segments,
    indicating they are neighbors that should be aligned.
    """

    def __init__(self, tolerance: float = 10.0):
        """
        Initialize matcher.

        Args:
            tolerance: Distance tolerance for boundary matching (in map units)
        """
        self.tolerance = tolerance

    def find_shared_boundary(self,
                            source_envelope: QgsGeometry,
                            target_envelope: QgsGeometry
                            ) -> Optional[QgsGeometry]:
        """
        Find shared boundary between two map envelopes.

        Args:
            source_envelope: Source map envelope
            target_envelope: Target map envelope

        Returns:
            Shared boundary geometry or None
        """
        if source_envelope.isEmpty() or target_envelope.isEmpty():
            return None

        # Buffer source slightly and intersect with target
        buffered = source_envelope.buffer(self.tolerance, 5)
        intersection = buffered.intersection(target_envelope)

        if intersection.isEmpty():
            return None

        # The shared boundary is where they touch
        # For polygons, this should be a line or multiline
        boundary = intersection.convertToType(1)  # Convert to line

        if boundary.isEmpty() or boundary.length() < self.tolerance:
            return None

        return boundary

    def get_boundary_control_points(self,
                                   shared_boundary: QgsGeometry,
                                   num_points: int = 5
                                   ) -> List[Tuple[float, float]]:
        """
        Extract control points along shared boundary.

        Args:
            shared_boundary: Shared boundary geometry
            num_points: Number of points to extract

        Returns:
            List of (x, y) control point tuples
        """
        if shared_boundary.isEmpty():
            return []

        length = shared_boundary.length()
        if length < 1e-10:
            return []

        points = []
        for i in range(num_points):
            distance = (i / (num_points - 1)) * length if num_points > 1 else 0
            pt = shared_boundary.interpolate(distance)
            if not pt.isEmpty():
                p = pt.asPoint()
                points.append((p.x(), p.y()))

        return points


class TransformCandidateFinder:
    """
    Find transformation candidates for a source map.

    Combines multiple matching strategies to find the best
    target maps for alignment.
    """

    def __init__(self, db_manager):
        """
        Initialize finder.

        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager
        self.chiban_matcher = ChibanMatcher()
        self.shape_matcher = ShapeMatcher()
        self.boundary_matcher = BoundaryMatcher()

    def find_candidates(self,
                       source_xml_id: int,
                       target_crs_type: str = '公共座標8系',
                       max_candidates: int = 10
                       ) -> List[MatchCandidate]:
        """
        Find transformation candidates for a source map.

        Args:
            source_xml_id: ID of source XML map
            target_crs_type: Target CRS type to search for (or pattern like '公共座標')
            max_candidates: Maximum number of candidates to return

        Returns:
            List of MatchCandidate objects, sorted by score
        """
        # Get source parcels
        source_parcels = self.db.get_fude_by_xml_id(source_xml_id)
        source_meta = self.db.get_xml_meta_by_id(source_xml_id)

        if not source_parcels or not source_meta:
            logger.warning(f"No data found for source XML {source_xml_id}")
            return []

        logger.info(f"Source map {source_xml_id}: {len(source_parcels)} parcels, CRS: {source_meta.get('crs_type')}")

        # Get candidate targets - try exact match first, then pattern match
        target_metas = self.db.get_xml_meta_by_crs_type(target_crs_type)

        # If no exact matches, try to find any non-arbitrary maps
        if not target_metas:
            logger.info(f"No exact match for CRS '{target_crs_type}', searching for all public coordinate maps")
            target_metas = self._get_public_coordinate_maps()

        logger.info(f"Found {len(target_metas)} potential reference maps")

        candidates = []

        for target_meta in target_metas:
            target_id = target_meta['id']
            if target_id == source_xml_id:
                continue

            target_parcels = self.db.get_fude_by_xml_id(target_id)
            if not target_parcels:
                continue

            # Try chiban matching
            match = self.chiban_matcher.find_matches(source_parcels, target_parcels)

            if match:
                match.source_xml_id = source_xml_id
                match.target_xml_id = target_id
                candidates.append(match)
                logger.debug(f"Found match with {target_meta.get('file_name')}: score={match.match_score:.3f}, common={match.common_chibans}")

        if not candidates:
            logger.warning(f"No matching candidates found for source {source_xml_id}")
            # Log some debug info about chibans
            source_chibans = set(p.get('chiban', '') for p in source_parcels if p.get('chiban'))
            logger.debug(f"Source chibans sample: {list(source_chibans)[:10]}")

        # Sort by score
        candidates.sort(key=lambda m: m.match_score, reverse=True)

        return candidates[:max_candidates]

    def _get_public_coordinate_maps(self) -> List[Dict[str, Any]]:
        """Get all maps that are NOT in arbitrary coordinate system."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, file_name, map_name, municipality_code, municipality_name,
                           oaza_name, crs_type, fude_count, status
                    FROM t_xml_meta
                    WHERE crs_type != '任意座標系'
                    AND crs_type NOT LIKE '%任意%'
                    ORDER BY file_name
                """)
                result = [dict(row) for row in cursor.fetchall()]
                logger.info(f"Found {len(result)} non-arbitrary maps: {[m.get('crs_type') for m in result[:5]]}")
                return result
        except Exception as e:
            logger.error(f"Error getting public coordinate maps: {e}")
            return []

    def get_control_points_for_match(self,
                                    match: MatchCandidate
                                    ) -> List[ControlPointPair]:
        """
        Get control points for a match candidate.

        Args:
            match: MatchCandidate object

        Returns:
            List of ControlPointPair objects
        """
        source_parcels = self.db.get_fude_by_xml_id(match.source_xml_id)
        target_parcels = self.db.get_fude_by_xml_id(match.target_xml_id)

        return self.chiban_matcher.get_control_points(
            source_parcels, target_parcels, match
        )
