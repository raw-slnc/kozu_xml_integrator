# -*- coding: utf-8 -*-
"""
Geometry Builder for KozuXmlIntegrator

This module builds QgsGeometry objects from parsed XML data.
Handles the conversion of GM_Curve references to closed polygons.

Note on coordinate systems:
- XML coordinates use Japanese convention: X=North, Y=East
- QGIS uses standard convention: X=East, Y=North
- Coordinates are swapped during conversion
"""

from typing import List, Tuple, Dict, Optional
from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsPolygon,
    QgsLineString,
    QgsMultiPolygon,
    QgsRectangle,
    QgsPoint,
)
import logging

from .xml_parser import XmlMapData, Fude, GmPoint, GmCurve, GmSurface

logger = logging.getLogger(__name__)


class GeometryBuilder:
    """
    Builds QgsGeometry objects from parsed XML map data.

    Handles the conversion from GM_Curve references to QgsPolygon,
    including coordinate system transformation (X/Y swap).
    """

    def __init__(self, xml_data: XmlMapData, swap_xy: bool = True):
        """
        Initialize geometry builder.

        Args:
            xml_data: Parsed XML data containing points, curves, and surfaces
            swap_xy: Whether to swap X/Y coordinates (Japan convention -> QGIS)
        """
        self.data = xml_data
        self.swap_xy = swap_xy
        self._geometry_cache: Dict[str, QgsGeometry] = {}

    def build_fude_polygon(self, fude: Fude) -> QgsGeometry:
        """
        Build a polygon geometry for a parcel (筆).

        Args:
            fude: Fude object containing shape reference

        Returns:
            QgsGeometry: Polygon geometry, or empty geometry if build fails
        """
        # Check cache first
        if fude.shape_ref in self._geometry_cache:
            return self._geometry_cache[fude.shape_ref]

        surface = self.data.surfaces.get(fude.shape_ref)
        if not surface:
            logger.warning(f"Surface not found for fude {fude.id}: {fude.shape_ref}")
            return QgsGeometry()

        # Build polygon from surface
        geom = self._build_surface_geometry(surface)
        self._geometry_cache[fude.shape_ref] = geom

        return geom

    def _build_surface_geometry(self, surface: GmSurface) -> QgsGeometry:
        """
        Build polygon geometry from GM_Surface.

        Args:
            surface: GmSurface object

        Returns:
            QgsGeometry: Polygon geometry
        """
        # Build exterior ring
        exterior_points = self._collect_ring_points(surface.exterior_curve_refs)
        if len(exterior_points) < 3:
            logger.warning(f"Insufficient exterior points for surface {surface.id}")
            return QgsGeometry()

        # Ensure ring is closed
        if exterior_points[0] != exterior_points[-1]:
            exterior_points.append(exterior_points[0])

        # Create QgsPolygon
        polygon = QgsPolygon()

        # Create exterior ring
        exterior_ring = QgsLineString([QgsPoint(x, y) for x, y in exterior_points])
        polygon.setExteriorRing(exterior_ring)

        # Add interior rings (holes) if any
        for interior_refs in surface.interior_curve_refs:
            interior_points = self._collect_ring_points(interior_refs)
            if len(interior_points) >= 3:
                if interior_points[0] != interior_points[-1]:
                    interior_points.append(interior_points[0])
                interior_ring = QgsLineString([QgsPoint(x, y) for x, y in interior_points])
                polygon.addInteriorRing(interior_ring)

        return QgsGeometry(polygon)

    def _collect_ring_points(self, curve_refs: List[str]) -> List[Tuple[float, float]]:
        """
        Collect coordinate points from a list of curve references.

        Handles curve orientation and ensures continuity.

        Args:
            curve_refs: List of curve IDs (may have '+' or '-' prefix for orientation)

        Returns:
            List of (x, y) coordinate tuples
        """
        points = []

        for ref in curve_refs:
            # Parse reference (may have orientation prefix)
            reverse = False
            curve_id = ref

            if ref.startswith('+'):
                curve_id = ref[1:]
            elif ref.startswith('-'):
                curve_id = ref[1:]
                reverse = True

            curve = self.data.curves.get(curve_id)
            if not curve:
                logger.debug(f"Curve not found: {curve_id}")
                continue

            curve_points = list(curve.points)

            # Reverse if negative orientation
            if reverse:
                curve_points = list(reversed(curve_points))

            # Transform coordinates if needed
            if self.swap_xy:
                curve_points = [(y, x) for x, y in curve_points]

            # Skip first point if we already have points (avoid duplicates at joints)
            if points and curve_points:
                # Check if first point of this curve matches last point of previous
                if len(curve_points) > 0:
                    first_pt = curve_points[0]
                    last_pt = points[-1]
                    if self._points_equal(first_pt, last_pt):
                        curve_points = curve_points[1:]

            points.extend(curve_points)

        return points

    def _points_equal(self, p1: Tuple[float, float], p2: Tuple[float, float],
                     tolerance: float = 1e-6) -> bool:
        """Check if two points are equal within tolerance."""
        return (abs(p1[0] - p2[0]) < tolerance and
                abs(p1[1] - p2[1]) < tolerance)

    def build_map_envelope(self) -> QgsGeometry:
        """
        Build bounding box polygon for the entire map.

        Returns:
            QgsGeometry: Rectangular polygon representing the map extent
        """
        all_points = self._collect_all_points()

        if not all_points:
            return QgsGeometry()

        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        rect = QgsRectangle(min_x, min_y, max_x, max_y)
        return QgsGeometry.fromRect(rect)

    def build_convex_hull(self) -> QgsGeometry:
        """
        Build convex hull polygon for the entire map.

        Returns:
            QgsGeometry: Convex hull polygon
        """
        all_points = self._collect_all_points()

        if len(all_points) < 3:
            return QgsGeometry()

        # Create multipoint geometry
        qgs_points = [QgsPointXY(x, y) for x, y in all_points]
        multipoint = QgsGeometry.fromMultiPointXY(qgs_points)

        return multipoint.convexHull()

    def build_union_polygon(self) -> QgsGeometry:
        """
        Build union of all parcel polygons.

        Returns:
            QgsGeometry: Union polygon representing all parcels
        """
        geometries = []

        for fude in self.data.fude_list:
            geom = self.build_fude_polygon(fude)
            if not geom.isEmpty():
                geometries.append(geom)

        if not geometries:
            return QgsGeometry()

        # Union all geometries
        result = geometries[0]
        for geom in geometries[1:]:
            result = result.combine(geom)

        return result

    def _collect_all_points(self) -> List[Tuple[float, float]]:
        """
        Collect all coordinate points from the map.

        Returns:
            List of (x, y) coordinate tuples
        """
        all_points = []

        # From GM_Point
        for point in self.data.points.values():
            if self.swap_xy:
                all_points.append((point.y, point.x))
            else:
                all_points.append((point.x, point.y))

        # From GM_Curve
        for curve in self.data.curves.values():
            for x, y in curve.points:
                if self.swap_xy:
                    all_points.append((y, x))
                else:
                    all_points.append((x, y))

        return all_points

    def get_fude_centroid(self, fude: Fude) -> Optional[QgsPointXY]:
        """
        Get the centroid of a parcel polygon.

        Args:
            fude: Fude object

        Returns:
            QgsPointXY or None if geometry is empty
        """
        geom = self.build_fude_polygon(fude)
        if geom.isEmpty():
            return None

        centroid = geom.centroid()
        if centroid.isEmpty():
            return None

        return centroid.asPoint()

    def get_map_bounds(self) -> Optional[QgsRectangle]:
        """
        Get the bounding rectangle of the entire map.

        Returns:
            QgsRectangle or None if no points
        """
        all_points = self._collect_all_points()

        if not all_points:
            return None

        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]

        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))

    def to_wkt(self, fude: Fude) -> str:
        """
        Get WKT representation of a parcel polygon.

        Args:
            fude: Fude object

        Returns:
            WKT string
        """
        geom = self.build_fude_polygon(fude)
        return geom.asWkt() if not geom.isEmpty() else ''

    def clear_cache(self):
        """Clear the geometry cache."""
        self._geometry_cache.clear()


def build_all_fude_geometries(xml_data: XmlMapData,
                             swap_xy: bool = True) -> Dict[str, QgsGeometry]:
    """
    Build geometries for all parcels in the XML data.

    Args:
        xml_data: Parsed XML data
        swap_xy: Whether to swap X/Y coordinates

    Returns:
        Dictionary mapping fude_id to QgsGeometry
    """
    builder = GeometryBuilder(xml_data, swap_xy=swap_xy)
    geometries = {}

    for fude in xml_data.fude_list:
        geom = builder.build_fude_polygon(fude)
        if not geom.isEmpty():
            geometries[fude.id] = geom

    return geometries
