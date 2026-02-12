# -*- coding: utf-8 -*-
"""
Leveling Module for KozuXmlIntegrator

Handles overlap resolution through gradual leveling adjustments:
1. Detect overlaps between parcels
2. Apply gradual adjustments to resolve overlaps
3. Preserve topology (adjacency relationships)

Key constraints:
- Public coordinate parcels are NEVER adjusted (they have perfect boundaries)
- Adjustments are only applied to arbitrary coordinate parcels
- Changes are distributed gradually to avoid abrupt shape changes
"""

from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass
import math
import logging

from qgis.core import QgsGeometry, QgsPointXY

logger = logging.getLogger(__name__)


@dataclass
class OverlapInfo:
    """Information about an overlap between two parcels."""
    parcel_id_a: int
    parcel_id_b: int
    overlap_area: float
    overlap_geometry: QgsGeometry
    is_a_public: bool  # Is parcel A from public coordinate XML
    is_b_public: bool  # Is parcel B from public coordinate XML


@dataclass
class LevelingResult:
    """Result of leveling process."""
    total_overlaps_found: int
    overlaps_resolved: int
    overlaps_remaining: int
    iterations_used: int
    adjustments_made: Dict[int, Dict[str, float]]  # parcel_id -> adjustment info
    issues: List[str]  # Any issues encountered


class OverlapDetector:
    """
    Detects overlaps between parcels.
    """

    def __init__(self, db_manager):
        """
        Initialize detector.

        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager

    def detect_overlaps(self, oaza_name: str = None,
                       min_overlap_area: float = 1.0) -> List[OverlapInfo]:
        """
        Detect all overlaps between parcels.

        Args:
            oaza_name: Optional filter by oaza
            min_overlap_area: Minimum overlap area to report (sq meters)

        Returns:
            List of OverlapInfo objects
        """
        overlaps = []

        # Get all parcels with their CRS type
        parcels = self._get_parcels_with_crs(oaza_name)

        # Build spatial index for efficient overlap detection
        parcel_geoms = {}
        parcel_is_public = {}

        for parcel in parcels:
            if parcel['geom_wkt']:
                geom = QgsGeometry.fromWkt(parcel['geom_wkt'])
                if not geom.isEmpty():
                    parcel_geoms[parcel['id']] = geom
                    parcel_is_public[parcel['id']] = self._is_public_crs(parcel['crs_type'])

        # Check all pairs for overlaps
        parcel_ids = list(parcel_geoms.keys())

        for i, id_a in enumerate(parcel_ids):
            geom_a = parcel_geoms[id_a]
            bbox_a = geom_a.boundingBox()

            for id_b in parcel_ids[i+1:]:
                geom_b = parcel_geoms[id_b]

                # Quick bounding box check
                if not bbox_a.intersects(geom_b.boundingBox()):
                    continue

                # Precise intersection check
                intersection = geom_a.intersection(geom_b)

                if intersection.isEmpty():
                    continue

                # Check if it's a real overlap (area > 0, not just touching)
                overlap_area = intersection.area()

                if overlap_area >= min_overlap_area:
                    overlaps.append(OverlapInfo(
                        parcel_id_a=id_a,
                        parcel_id_b=id_b,
                        overlap_area=overlap_area,
                        overlap_geometry=intersection,
                        is_a_public=parcel_is_public[id_a],
                        is_b_public=parcel_is_public[id_b]
                    ))

        logger.info(f"Detected {len(overlaps)} overlaps (min area: {min_overlap_area})")
        return overlaps

    def _get_parcels_with_crs(self, oaza_name: str = None) -> List[Dict[str, Any]]:
        """Get parcels with their CRS type information."""
        with self.db.connection() as conn:
            cursor = conn.cursor()

            if oaza_name:
                cursor.execute("""
                    SELECT f.id, f.fude_id, f.oaza_name, f.chiban,
                           m.crs_type, AsText(f.geom) as geom_wkt
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE f.oaza_name = ? AND f.geom IS NOT NULL
                """, (oaza_name,))
            else:
                cursor.execute("""
                    SELECT f.id, f.fude_id, f.oaza_name, f.chiban,
                           m.crs_type, AsText(f.geom) as geom_wkt
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE f.geom IS NOT NULL
                """)

            return [dict(row) for row in cursor.fetchall()]

    def _is_public_crs(self, crs_type: str) -> bool:
        """Check if CRS type indicates public coordinate data."""
        if not crs_type:
            return False
        return crs_type != '任意座標系' and '任意' not in crs_type


class Leveler:
    """
    Applies leveling adjustments to resolve overlaps.

    Key principle: Public coordinate parcels are NEVER adjusted.
    All adjustments are made to arbitrary coordinate parcels only.
    """

    def __init__(self, db_manager, max_iterations: int = 10,
                 convergence_threshold: float = 0.1,
                 damping_factor: float = 0.3):
        """
        Initialize leveler.

        Args:
            db_manager: DatabaseManager instance
            max_iterations: Maximum leveling iterations
            convergence_threshold: Stop when total overlap area below this (sq meters)
            damping_factor: How much of the adjustment to apply per iteration (0-1)
        """
        self.db = db_manager
        self.max_iterations = max_iterations
        self.convergence_threshold = convergence_threshold
        self.damping_factor = damping_factor
        self.detector = OverlapDetector(db_manager)

    def apply_leveling(self, oaza_name: str = None) -> LevelingResult:
        """
        Apply leveling to resolve overlaps.

        Args:
            oaza_name: Optional filter by oaza

        Returns:
            LevelingResult with details of adjustments made
        """
        total_adjustments = {}
        issues = []
        initial_overlaps = 0

        for iteration in range(self.max_iterations):
            # Detect current overlaps
            overlaps = self.detector.detect_overlaps(oaza_name)

            if iteration == 0:
                initial_overlaps = len(overlaps)

            # Check for convergence
            total_overlap_area = sum(o.overlap_area for o in overlaps)

            if total_overlap_area < self.convergence_threshold:
                logger.info(f"Leveling converged after {iteration + 1} iterations")
                break

            # Calculate and apply adjustments
            adjustments = self._calculate_adjustments(overlaps)

            if not adjustments:
                logger.info("No adjustments possible")
                break

            # Apply adjustments to database
            self._apply_adjustments(adjustments)

            # Track total adjustments
            for parcel_id, adj in adjustments.items():
                if parcel_id not in total_adjustments:
                    total_adjustments[parcel_id] = {'total_shrink': 0, 'iterations': 0}
                total_adjustments[parcel_id]['total_shrink'] += adj.get('shrink_amount', 0)
                total_adjustments[parcel_id]['iterations'] += 1

            logger.info(f"Iteration {iteration + 1}: {len(overlaps)} overlaps, "
                       f"total area: {total_overlap_area:.2f}")

        # Final check
        final_overlaps = self.detector.detect_overlaps(oaza_name)

        # Check for unresolvable overlaps (both public)
        for overlap in final_overlaps:
            if overlap.is_a_public and overlap.is_b_public:
                issues.append(
                    f"Unresolvable overlap between public parcels "
                    f"{overlap.parcel_id_a} and {overlap.parcel_id_b}"
                )

        return LevelingResult(
            total_overlaps_found=initial_overlaps,
            overlaps_resolved=initial_overlaps - len(final_overlaps),
            overlaps_remaining=len(final_overlaps),
            iterations_used=iteration + 1 if 'iteration' in dir() else 0,
            adjustments_made=total_adjustments,
            issues=issues
        )

    def _calculate_adjustments(self, overlaps: List[OverlapInfo]
                               ) -> Dict[int, Dict[str, Any]]:
        """
        Calculate adjustments for each parcel to resolve overlaps.

        Key rule: Public coordinate parcels are NEVER adjusted.
        If overlap is between public and arbitrary, arbitrary takes 100% of adjustment.
        If overlap is between two arbitrary, split the adjustment.
        """
        adjustments = {}

        for overlap in overlaps:
            shrink_amount = overlap.overlap_area * self.damping_factor

            if overlap.is_a_public and overlap.is_b_public:
                # Both public - cannot adjust (should be flagged as issue)
                continue

            elif overlap.is_a_public:
                # A is public, B must absorb all adjustment
                self._add_adjustment(adjustments, overlap.parcel_id_b,
                                    shrink_amount, overlap.overlap_geometry)

            elif overlap.is_b_public:
                # B is public, A must absorb all adjustment
                self._add_adjustment(adjustments, overlap.parcel_id_a,
                                    shrink_amount, overlap.overlap_geometry)

            else:
                # Both arbitrary - split the adjustment
                half_shrink = shrink_amount / 2
                self._add_adjustment(adjustments, overlap.parcel_id_a,
                                    half_shrink, overlap.overlap_geometry)
                self._add_adjustment(adjustments, overlap.parcel_id_b,
                                    half_shrink, overlap.overlap_geometry)

        return adjustments

    def _add_adjustment(self, adjustments: Dict[int, Dict], parcel_id: int,
                       shrink_amount: float, overlap_geom: QgsGeometry):
        """Add adjustment for a parcel."""
        if parcel_id not in adjustments:
            adjustments[parcel_id] = {
                'shrink_amount': 0,
                'shrink_directions': []  # List of (centroid_x, centroid_y) to shrink toward
            }

        adjustments[parcel_id]['shrink_amount'] += shrink_amount

        # Calculate direction to shrink (away from overlap)
        overlap_centroid = overlap_geom.centroid().asPoint()
        adjustments[parcel_id]['shrink_directions'].append(
            (overlap_centroid.x(), overlap_centroid.y())
        )

    def _apply_adjustments(self, adjustments: Dict[int, Dict]):
        """Apply calculated adjustments to parcels in database."""
        for parcel_id, adj in adjustments.items():
            # Get current geometry
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT AsText(geom) as geom_wkt FROM t_fude_poly WHERE id = ?
                """, (parcel_id,))
                row = cursor.fetchone()

                if not row or not row['geom_wkt']:
                    continue

                geom = QgsGeometry.fromWkt(row['geom_wkt'])
                if geom.isEmpty():
                    continue

                # Calculate shrink transformation
                new_geom = self._shrink_geometry(
                    geom,
                    adj['shrink_amount'],
                    adj['shrink_directions']
                )

                if new_geom and not new_geom.isEmpty():
                    # Update in database
                    self.db.update_fude_geometry(parcel_id, new_geom.asWkt())

    def _shrink_geometry(self, geom: QgsGeometry, shrink_amount: float,
                        shrink_directions: List[Tuple[float, float]]) -> QgsGeometry:
        """
        Shrink geometry toward its centroid, away from overlap areas.

        Uses a gentle buffer-based approach for simplicity.
        """
        # Calculate shrink distance from area
        # Approximate: if we shrink by distance d, area reduction ≈ perimeter * d
        perimeter = geom.length()
        if perimeter > 0:
            shrink_distance = shrink_amount / perimeter
        else:
            shrink_distance = 0.1  # fallback

        # Limit shrink distance to prevent over-shrinking
        shrink_distance = min(shrink_distance, 1.0)  # max 1 meter per iteration

        # Apply negative buffer (shrink)
        if shrink_distance > 0.01:  # Only if meaningful
            shrunk = geom.buffer(-shrink_distance, 5)

            # Validate result
            if shrunk.isEmpty() or shrunk.area() < geom.area() * 0.5:
                # Shrink too aggressive, use smaller amount
                shrunk = geom.buffer(-shrink_distance * 0.5, 5)

            if not shrunk.isEmpty():
                return shrunk

        return geom


class TopologyChecker:
    """
    Checks topology after leveling to ensure validity.
    """

    def __init__(self, db_manager):
        self.db = db_manager

    def check_topology(self, oaza_name: str = None) -> Dict[str, Any]:
        """
        Check topology for issues.

        Returns:
            Dict with:
            - valid: bool
            - overlaps: list of overlap issues
            - gaps: list of significant gaps
            - invalid_geometries: list of invalid geometry IDs
        """
        result = {
            'valid': True,
            'overlaps': [],
            'gaps': [],
            'invalid_geometries': []
        }

        # Check for remaining overlaps
        detector = OverlapDetector(self.db)
        overlaps = detector.detect_overlaps(oaza_name, min_overlap_area=0.5)

        if overlaps:
            result['valid'] = False
            result['overlaps'] = [
                {
                    'parcel_a': o.parcel_id_a,
                    'parcel_b': o.parcel_id_b,
                    'area': o.overlap_area
                }
                for o in overlaps
            ]

        # Check for invalid geometries
        invalid = self._find_invalid_geometries(oaza_name)
        if invalid:
            result['valid'] = False
            result['invalid_geometries'] = invalid

        return result

    def _find_invalid_geometries(self, oaza_name: str = None) -> List[int]:
        """Find parcels with invalid geometries."""
        invalid_ids = []

        with self.db.connection() as conn:
            cursor = conn.cursor()

            if oaza_name:
                cursor.execute("""
                    SELECT id, AsText(geom) as geom_wkt
                    FROM t_fude_poly
                    WHERE oaza_name = ? AND geom IS NOT NULL
                """, (oaza_name,))
            else:
                cursor.execute("""
                    SELECT id, AsText(geom) as geom_wkt
                    FROM t_fude_poly
                    WHERE geom IS NOT NULL
                """)

            for row in cursor.fetchall():
                if row['geom_wkt']:
                    geom = QgsGeometry.fromWkt(row['geom_wkt'])
                    if geom.isEmpty() or not geom.isGeosValid():
                        invalid_ids.append(row['id'])

        return invalid_ids
