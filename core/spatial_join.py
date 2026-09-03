# -*- coding: utf-8 -*-
"""
Spatial Join Module for KozuXmlIntegrator

This module provides spatial join functionality between XML map data
and administrative boundary data (大字/Oaza polygons).

Used to automatically assign Oaza names to imported XML data
based on spatial intersection.
"""

from pathlib import Path
from typing import List, Dict, Optional
import logging
import re

from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsSpatialIndex,
    QgsRectangle,
)

from .database_manager import DatabaseManager

logger = logging.getLogger(__name__)


def normalize_municipality_name(name: str) -> str:
    """
    Normalize municipality name by stripping prefecture/county prefixes and
    forest-register style "former municipality" decoration.

    Examples:
        "賀茂郡西伊豆町" -> "西伊豆町"
        "静岡県下田市" -> "下田市"
        "西伊豆町" -> "西伊豆町" (unchanged)
        "旧西伊豆町" -> "西伊豆町"
        "旧静岡市、旧美和村（旧静岡市）" -> "静岡市"
        "島田市　　※旧島田市（金谷町含む）" -> "島田市"

    Args:
        name: Municipality name (possibly decorated / prefixed)

    Returns:
        Normalized bare municipality name
    """
    if not name:
        return ''

    result = name

    # Forest-register decoration: keep the part before the first 、 , or ※,
    # drop any (...) / （...）, then a leading 旧.
    result = re.split(r'[、,※]', result, 1)[0]
    result = re.sub(r'[（(].*?[）)]', '', result)

    # Prefecture(県) / County(郡) prefix
    for pattern in (r'^.*?郡', r'^.*?県'):
        result = re.sub(pattern, '', result)

    result = result.replace(' ', '').replace('　', '')
    result = re.sub(r'^旧', '', result)

    return result


def match_municipality_names(xml_name: str, reference_names: List[str]) -> Optional[str]:
    """
    Find matching municipality name from reference list.

    Handles cases where XML uses full name (e.g., "賀茂郡西伊豆町")
    but reference uses short name (e.g., "西伊豆町").

    Args:
        xml_name: Municipality name from XML
        reference_names: List of reference municipality names

    Returns:
        Matching reference name, or None if no match
    """
    if not xml_name:
        return None

    # Direct match
    if xml_name in reference_names:
        return xml_name

    # Try normalized match
    xml_normalized = normalize_municipality_name(xml_name)

    for ref_name in reference_names:
        ref_normalized = normalize_municipality_name(ref_name)
        if xml_normalized == ref_normalized:
            return ref_name

        # Also check if xml_normalized ends with ref_name or vice versa
        if xml_normalized.endswith(ref_name) or ref_name.endswith(xml_normalized):
            return ref_name

    return None


class SpatialJoiner:
    """
    Performs spatial join between XML map envelopes and administrative boundaries.

    Uses spatial index for efficient lookups and determines Oaza assignment
    based on the largest intersection area.
    """

    def __init__(self, admin_layer: QgsVectorLayer, name_field: str = 'S_NAME'):
        """
        Initialize spatial joiner.

        Args:
            admin_layer: Vector layer containing administrative boundaries
            name_field: Field name containing the Oaza name
        """
        self.admin_layer = admin_layer
        self.name_field = name_field
        self._spatial_index: Optional[QgsSpatialIndex] = None
        self._features_cache: Dict[int, QgsFeature] = {}

        self._build_index()

    def _build_index(self) -> None:
        """Build spatial index for efficient lookups."""
        logger.info("Building spatial index for administrative boundaries...")

        self._spatial_index = QgsSpatialIndex()

        for feature in self.admin_layer.getFeatures():
            self._spatial_index.addFeature(feature)
            self._features_cache[feature.id()] = feature

        logger.info(f"Indexed {len(self._features_cache)} administrative features")

    def find_oaza_for_geometry(self, geom: QgsGeometry) -> Optional[str]:
        """
        Find the Oaza name for a given geometry.

        Uses spatial intersection and selects the Oaza with the
        largest intersection area.

        Args:
            geom: Geometry to find Oaza for

        Returns:
            str: Oaza name, or None if no intersection found
        """
        if geom.isEmpty():
            return None

        # Get candidate features using spatial index
        bbox = geom.boundingBox()
        candidate_ids = self._spatial_index.intersects(bbox)

        if not candidate_ids:
            return None

        # Find feature with largest intersection area
        best_oaza = None
        best_area = 0.0

        for fid in candidate_ids:
            feature = self._features_cache.get(fid)
            if feature is None:
                continue

            admin_geom = feature.geometry()
            if admin_geom.isEmpty():
                continue

            # Check actual intersection
            if not geom.intersects(admin_geom):
                continue

            # Calculate intersection area
            intersection = geom.intersection(admin_geom)
            if intersection.isEmpty():
                continue

            area = intersection.area()
            if area > best_area:
                best_area = area
                best_oaza = feature[self.name_field]

        return best_oaza

    def find_oaza_for_point(self, x: float, y: float) -> Optional[str]:
        """
        Find the Oaza name for a point coordinate.

        Args:
            x: X coordinate
            y: Y coordinate

        Returns:
            str: Oaza name, or None if point is outside all boundaries
        """
        from qgis.core import QgsPointXY

        point_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        return self.find_oaza_for_geometry(point_geom)

    def find_oaza_for_bounds(self, min_x: float, min_y: float,
                             max_x: float, max_y: float) -> Optional[str]:
        """
        Find the Oaza name for a bounding box.

        Args:
            min_x, min_y, max_x, max_y: Bounding box coordinates

        Returns:
            str: Oaza name, or None if no intersection found
        """
        bbox_geom = QgsGeometry.fromRect(
            QgsRectangle(min_x, min_y, max_x, max_y)
        )
        return self.find_oaza_for_geometry(bbox_geom)

    def get_all_oaza_names(self) -> List[str]:
        """Get list of all Oaza names in the administrative layer."""
        names = set()
        for feature in self._features_cache.values():
            name = feature[self.name_field]
            if name:
                names.add(name)
        return sorted(names)

    def get_oaza_geometry(self, oaza_name: str) -> Optional[QgsGeometry]:
        """Get the geometry for a specific Oaza."""
        for feature in self._features_cache.values():
            if feature[self.name_field] == oaza_name:
                return feature.geometry()
        return None


def load_admin_layer(layer_path: Path, layer_name: Optional[str] = None) -> QgsVectorLayer:
    """
    Load administrative boundary layer from file.

    Supports GeoPackage, Shapefile, and other OGR-supported formats.

    Args:
        layer_path: Path to the layer file
        layer_name: Name of the layer (for GeoPackage with multiple layers)

    Returns:
        QgsVectorLayer: Loaded vector layer
    """
    path_str = str(layer_path)

    if layer_path.suffix.lower() == '.gpkg' and layer_name:
        uri = f"{path_str}|layername={layer_name}"
    else:
        uri = path_str

    layer = QgsVectorLayer(uri, 'admin_boundaries', 'ogr')

    if not layer.isValid():
        raise ValueError(f"Failed to load layer from: {layer_path}")

    return layer


def assign_oaza_to_xml_meta(db: DatabaseManager, admin_layer: QgsVectorLayer,
                            name_field: str = 'S_NAME') -> Dict[str, int]:
    """
    Assign Oaza names to all XML metadata records in the database.

    Args:
        db: Database manager
        admin_layer: Administrative boundary layer
        name_field: Field name containing Oaza names

    Returns:
        Dict mapping Oaza name to count of assigned records
    """
    logger.info("Assigning Oaza names to XML metadata...")

    joiner = SpatialJoiner(admin_layer, name_field)
    assignment_counts = {}

    # Get all XML metadata with geometry
    all_meta = db.get_all_xml_meta()

    for meta in all_meta:
        if not meta.get('geom_wkt'):
            continue

        geom = QgsGeometry.fromWkt(meta['geom_wkt'])
        if geom.isEmpty():
            continue

        oaza_name = joiner.find_oaza_for_geometry(geom)

        if oaza_name:
            db.update_xml_meta_oaza(meta['id'], oaza_name)
            assignment_counts[oaza_name] = assignment_counts.get(oaza_name, 0) + 1
            logger.debug(f"Assigned {meta['file_name']} to Oaza: {oaza_name}")
        else:
            logger.warning(f"No Oaza found for {meta['file_name']}")

    logger.info(f"Oaza assignment complete: {sum(assignment_counts.values())} records assigned")
    return assignment_counts


def update_fude_oaza_from_spatial_join(db: DatabaseManager, admin_layer: QgsVectorLayer,
                                       name_field: str = 'S_NAME',
                                       batch_size: int = 1000) -> int:
    """
    Update Oaza names for parcel records based on spatial join.

    This is more accurate than using the XML metadata envelope,
    as it assigns Oaza based on each parcel's actual location.

    Args:
        db: Database manager
        admin_layer: Administrative boundary layer
        name_field: Field name containing Oaza names
        batch_size: Number of records to process per batch

    Returns:
        int: Number of updated records
    """
    logger.info("Updating parcel Oaza names via spatial join...")

    joiner = SpatialJoiner(admin_layer, name_field)
    updated_count = 0

    with db.connection() as conn:
        cursor = conn.cursor()

        # Get parcels without Oaza assignment or with different Oaza from XML
        cursor.execute("""
            SELECT f.id, AsText(f.geom) as geom_wkt, f.oaza_name as current_oaza
            FROM t_fude_poly f
            WHERE f.geom IS NOT NULL
        """)

        batch = []
        for row in cursor.fetchall():
            row_dict = dict(row)
            geom = QgsGeometry.fromWkt(row_dict['geom_wkt'])

            if geom.isEmpty():
                continue

            # Use centroid for faster lookup
            centroid = geom.centroid()
            if centroid.isEmpty():
                continue

            oaza_name = joiner.find_oaza_for_geometry(centroid)

            if oaza_name and oaza_name != row_dict['current_oaza']:
                batch.append((oaza_name, row_dict['id']))

            if len(batch) >= batch_size:
                cursor.executemany(
                    "UPDATE t_fude_poly SET oaza_name = ? WHERE id = ?",
                    batch
                )
                conn.commit()
                updated_count += len(batch)
                logger.debug(f"Updated {updated_count} parcel records")
                batch = []

        # Process remaining batch
        if batch:
            cursor.executemany(
                "UPDATE t_fude_poly SET oaza_name = ? WHERE id = ?",
                batch
            )
            conn.commit()
            updated_count += len(batch)

    logger.info(f"Updated {updated_count} parcel Oaza assignments")
    return updated_count
