# -*- coding: utf-8 -*-
"""
XML Joiner Module for KozuXmlIntegrator

Handles joining/merging of XML maps within each oaza (district):
1. Build relationship graph between XMLs using common chibans
2. Calculate Helmert transformation parameters for joining
3. Join XMLs into combined oaza units

Key principles:
- Public coordinate XMLs are fixed anchors (never transform)
- Arbitrary coordinate XMLs are transformed to fit anchors
- Topology within each XML is preserved
"""

from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field
import math
import logging
from collections import defaultdict

from qgis.core import QgsGeometry, QgsPointXY

logger = logging.getLogger(__name__)


@dataclass
class XmlRelationship:
    """Relationship between two XMLs based on common chibans."""
    xml_id_a: int
    xml_id_b: int
    common_chibans: List[str]
    match_score: float  # Number of common chibans / total chibans


@dataclass
class JoinResult:
    """Result of joining XMLs within an oaza."""
    oaza_name: str
    total_xmls: int
    joined_xmls: int
    isolated_xmls: List[int]  # XMLs that couldn't be joined
    has_public_anchor: bool
    transform_params: Dict[int, Dict[str, float]]  # xml_id -> {dx, dy, scale, rotation}


class XmlRelationshipGraph:
    """
    Graph representing relationships between XMLs based on common chibans.

    Nodes: XML IDs
    Edges: Common chibans between XMLs
    """

    def __init__(self):
        self.nodes: Set[int] = set()
        self.edges: Dict[Tuple[int, int], XmlRelationship] = {}
        self.adjacency: Dict[int, Set[int]] = defaultdict(set)

    def add_node(self, xml_id: int):
        """Add an XML node to the graph."""
        self.nodes.add(xml_id)

    def add_edge(self, relationship: XmlRelationship):
        """Add a relationship edge between two XMLs."""
        a, b = relationship.xml_id_a, relationship.xml_id_b
        key = (min(a, b), max(a, b))
        self.edges[key] = relationship
        self.adjacency[a].add(b)
        self.adjacency[b].add(a)

    def get_neighbors(self, xml_id: int) -> Set[int]:
        """Get XMLs connected to the given XML."""
        return self.adjacency.get(xml_id, set())

    def get_relationship(self, xml_id_a: int, xml_id_b: int) -> Optional[XmlRelationship]:
        """Get relationship between two XMLs."""
        key = (min(xml_id_a, xml_id_b), max(xml_id_a, xml_id_b))
        return self.edges.get(key)

    def get_connected_components(self) -> List[Set[int]]:
        """Get connected components of the graph."""
        visited = set()
        components = []

        for node in self.nodes:
            if node in visited:
                continue

            # BFS to find connected component
            component = set()
            queue = [node]

            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue

                visited.add(current)
                component.add(current)

                for neighbor in self.adjacency[current]:
                    if neighbor not in visited:
                        queue.append(neighbor)

            components.append(component)

        return components


class XmlJoiner:
    """
    Joins XML maps within each oaza using common chiban matching.

    Processing flow:
    1. For each oaza, identify public coordinate XMLs (anchors)
    2. Build relationship graph based on common chibans
    3. Starting from anchors, calculate transformation parameters
    4. Transform arbitrary coordinate XMLs to join with anchors
    """

    def __init__(self, db_manager):
        """
        Initialize joiner.

        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager

    def build_relationship_graph(self, oaza_name: str) -> XmlRelationshipGraph:
        """
        Build relationship graph for XMLs in an oaza.

        Args:
            oaza_name: Name of the oaza to process

        Returns:
            XmlRelationshipGraph with XMLs as nodes and common chibans as edges
        """
        graph = XmlRelationshipGraph()

        # Get all XMLs in this oaza
        xml_ids = self._get_xmls_in_oaza(oaza_name)

        for xml_id in xml_ids:
            graph.add_node(xml_id)

        # Build chiban index: chiban -> [(xml_id, parcel_info), ...]
        chiban_index = self._build_chiban_index(xml_ids)

        # Find relationships between XMLs
        for xml_id_a in xml_ids:
            for xml_id_b in xml_ids:
                if xml_id_a >= xml_id_b:
                    continue

                common_chibans = self._find_common_chibans(
                    xml_id_a, xml_id_b, chiban_index
                )

                if common_chibans:
                    # Calculate match score
                    total_a = len(self._get_chibans_for_xml(xml_id_a, chiban_index))
                    total_b = len(self._get_chibans_for_xml(xml_id_b, chiban_index))
                    score = len(common_chibans) / max(min(total_a, total_b), 1)

                    relationship = XmlRelationship(
                        xml_id_a=xml_id_a,
                        xml_id_b=xml_id_b,
                        common_chibans=common_chibans,
                        match_score=score
                    )
                    graph.add_edge(relationship)

        logger.info(f"Built relationship graph for oaza '{oaza_name}': "
                   f"{len(graph.nodes)} XMLs, {len(graph.edges)} relationships")

        return graph

    def join_oaza(self, oaza_name: str) -> JoinResult:
        """
        Join all XMLs within an oaza.

        Args:
            oaza_name: Name of the oaza to process

        Returns:
            JoinResult with transformation parameters for each XML
        """
        # Build relationship graph
        graph = self.build_relationship_graph(oaza_name)

        # Identify anchor XMLs (public coordinate)
        anchor_ids = self._get_anchor_xmls(oaza_name)
        has_anchor = len(anchor_ids) > 0

        # Get connected components
        components = graph.get_connected_components()

        # Calculate transformation parameters
        transform_params = {}
        joined_xmls = set()
        isolated_xmls = []

        for component in components:
            # Check if component has an anchor
            component_anchors = component & anchor_ids

            if component_anchors:
                # Process component starting from anchor
                params = self._process_component_with_anchor(
                    component, component_anchors, graph
                )
                transform_params.update(params)
                joined_xmls.update(component)
            else:
                # No anchor - mark as isolated or process separately
                if len(component) == 1:
                    isolated_xmls.extend(component)
                else:
                    # Multiple XMLs connected but no anchor
                    # Join them together (relative positioning)
                    params = self._process_component_without_anchor(
                        component, graph
                    )
                    transform_params.update(params)
                    joined_xmls.update(component)

        return JoinResult(
            oaza_name=oaza_name,
            total_xmls=len(graph.nodes),
            joined_xmls=len(joined_xmls),
            isolated_xmls=isolated_xmls,
            has_public_anchor=has_anchor,
            transform_params=transform_params
        )

    def calculate_helmert_params(self, source_xml_id: int, target_xml_id: int,
                                 common_chibans: List[str]) -> Optional[Dict[str, float]]:
        """
        Calculate Helmert transformation parameters between two XMLs.

        Args:
            source_xml_id: XML to transform
            target_xml_id: Reference XML (anchor)
            common_chibans: List of common chiban values

        Returns:
            Dict with dx, dy, scale, rotation (radians) or None if failed
        """
        if len(common_chibans) < 2:
            logger.warning(f"Not enough common chibans for Helmert: {len(common_chibans)}")
            return None

        # Get centroid pairs for common chibans
        source_points = []
        target_points = []

        for chiban in common_chibans:
            src_centroid = self._get_parcel_centroid(source_xml_id, chiban)
            tgt_centroid = self._get_parcel_centroid(target_xml_id, chiban)

            if src_centroid and tgt_centroid:
                source_points.append(src_centroid)
                target_points.append(tgt_centroid)

        if len(source_points) < 2:
            logger.warning(f"Not enough valid centroid pairs: {len(source_points)}")
            return None

        # Compute Helmert parameters using least squares
        return self._compute_helmert(source_points, target_points)

    def _get_xmls_in_oaza(self, oaza_name: str) -> List[int]:
        """Get all XML IDs that have parcels in the given oaza."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT xml_meta_id
                FROM t_fude_poly
                WHERE oaza_name = ?
            """, (oaza_name,))
            return [row[0] for row in cursor.fetchall()]

    def _get_anchor_xmls(self, oaza_name: str) -> Set[int]:
        """Get XML IDs that are public coordinate (anchors)."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT f.xml_meta_id
                FROM t_fude_poly f
                JOIN t_xml_meta m ON f.xml_meta_id = m.id
                WHERE f.oaza_name = ?
                AND m.crs_type != '任意座標系'
                AND m.crs_type NOT LIKE '%任意%'
            """, (oaza_name,))
            return {row[0] for row in cursor.fetchall()}

    def _build_chiban_index(self, xml_ids: List[int]) -> Dict[str, List[Tuple[int, Dict]]]:
        """Build index of chiban -> [(xml_id, parcel_info), ...]."""
        index = defaultdict(list)

        for xml_id in xml_ids:
            parcels = self.db.get_fude_by_xml_id(xml_id)
            for parcel in parcels:
                chiban = parcel.get('chiban')
                if chiban and chiban.strip() and '地区外' not in chiban:
                    index[chiban].append((xml_id, parcel))

        return index

    def _find_common_chibans(self, xml_id_a: int, xml_id_b: int,
                            chiban_index: Dict) -> List[str]:
        """Find chibans that exist in both XMLs."""
        common = []
        for chiban, entries in chiban_index.items():
            xml_ids_with_chiban = {e[0] for e in entries}
            if xml_id_a in xml_ids_with_chiban and xml_id_b in xml_ids_with_chiban:
                common.append(chiban)
        return common

    def _get_chibans_for_xml(self, xml_id: int, chiban_index: Dict) -> Set[str]:
        """Get all chibans for an XML."""
        chibans = set()
        for chiban, entries in chiban_index.items():
            for entry_xml_id, _ in entries:
                if entry_xml_id == xml_id:
                    chibans.add(chiban)
        return chibans

    def _get_parcel_centroid(self, xml_id: int, chiban: str) -> Optional[Tuple[float, float]]:
        """Get centroid of a parcel identified by XML ID and chiban."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT AsText(geom) as geom_wkt
                FROM t_fude_poly
                WHERE xml_meta_id = ? AND chiban = ?
                LIMIT 1
            """, (xml_id, chiban))
            row = cursor.fetchone()

            if row and row['geom_wkt']:
                geom = QgsGeometry.fromWkt(row['geom_wkt'])
                if not geom.isEmpty():
                    centroid = geom.centroid().asPoint()
                    return (centroid.x(), centroid.y())

        return None

    def _process_component_with_anchor(self, component: Set[int],
                                       anchors: Set[int],
                                       graph: XmlRelationshipGraph
                                       ) -> Dict[int, Dict[str, float]]:
        """
        Process a connected component that has anchor(s).

        Anchors are fixed; other XMLs are transformed to join with anchors.
        """
        params = {}

        # Anchors have identity transform (no change)
        for anchor_id in anchors:
            params[anchor_id] = {'dx': 0, 'dy': 0, 'scale': 1.0, 'rotation': 0.0,
                                'method': 'anchor', 'reliability': 'HIGH'}

        # BFS from anchors to transform other XMLs
        processed = set(anchors)
        queue = list(anchors)

        while queue:
            current = queue.pop(0)

            for neighbor in graph.get_neighbors(current):
                if neighbor in processed:
                    continue

                # Get relationship
                rel = graph.get_relationship(current, neighbor)
                if not rel:
                    continue

                # Calculate transform from neighbor to current
                helmert = self.calculate_helmert_params(
                    neighbor, current, rel.common_chibans
                )

                if helmert:
                    # If current has been transformed, compose transforms
                    if current in params and params[current]['method'] != 'anchor':
                        helmert = self._compose_transforms(helmert, params[current])

                    # Determine reliability based on distance from anchor
                    reliability = 'HIGH' if current in anchors else 'MEDIUM'

                    params[neighbor] = {
                        **helmert,
                        'method': 'chiban_join',
                        'reliability': reliability,
                        'reference_xml': current
                    }

                    processed.add(neighbor)
                    queue.append(neighbor)
                else:
                    logger.warning(f"Failed to calculate Helmert for XML {neighbor}")

        return params

    def _process_component_without_anchor(self, component: Set[int],
                                          graph: XmlRelationshipGraph
                                          ) -> Dict[int, Dict[str, float]]:
        """
        Process a connected component without anchor.

        Pick one XML as reference and transform others relative to it.
        All will need external positioning later.
        """
        params = {}

        # Pick first XML as relative reference
        reference_id = min(component)
        params[reference_id] = {'dx': 0, 'dy': 0, 'scale': 1.0, 'rotation': 0.0,
                               'method': 'relative_reference', 'reliability': 'LOW'}

        # BFS from reference
        processed = {reference_id}
        queue = [reference_id]

        while queue:
            current = queue.pop(0)

            for neighbor in graph.get_neighbors(current):
                if neighbor in processed:
                    continue

                rel = graph.get_relationship(current, neighbor)
                if not rel:
                    continue

                helmert = self.calculate_helmert_params(
                    neighbor, current, rel.common_chibans
                )

                if helmert:
                    if current != reference_id and current in params:
                        helmert = self._compose_transforms(helmert, params[current])

                    params[neighbor] = {
                        **helmert,
                        'method': 'relative_join',
                        'reliability': 'LOW',
                        'reference_xml': current
                    }

                    processed.add(neighbor)
                    queue.append(neighbor)

        return params

    def _compute_helmert(self, source_pts: List[Tuple[float, float]],
                        target_pts: List[Tuple[float, float]]
                        ) -> Optional[Dict[str, float]]:
        """
        Compute Helmert transformation parameters using least squares.

        Returns: {'dx': float, 'dy': float, 'scale': float, 'rotation': float}
        """
        try:
            import numpy as np
        except ImportError:
            logger.error("NumPy required for Helmert computation")
            return None

        n = len(source_pts)
        if n < 2:
            return None

        # Build matrices
        A = np.zeros((2*n, 4))
        b = np.zeros(2*n)

        for i, (src, tgt) in enumerate(zip(source_pts, target_pts)):
            sx, sy = src
            tx, ty = tgt

            A[2*i, 0] = sx
            A[2*i, 1] = -sy
            A[2*i, 2] = 1
            A[2*i, 3] = 0

            A[2*i+1, 0] = sy
            A[2*i+1, 1] = sx
            A[2*i+1, 2] = 0
            A[2*i+1, 3] = 1

            b[2*i] = tx
            b[2*i+1] = ty

        try:
            result, residuals, rank, s = np.linalg.lstsq(A, b, rcond=None)
            a, b_val, dx, dy = result

            scale = math.sqrt(a*a + b_val*b_val)
            rotation = math.atan2(b_val, a)

            # Sanity checks
            if scale < 0.1 or scale > 10:
                logger.warning(f"Unreasonable Helmert scale: {scale}")
                return None

            return {
                'dx': dx,
                'dy': dy,
                'scale': scale,
                'rotation': rotation
            }

        except Exception as e:
            logger.error(f"Helmert computation failed: {e}")
            return None

    def _compose_transforms(self, t1: Dict[str, float],
                           t2: Dict[str, float]) -> Dict[str, float]:
        """Compose two Helmert transforms: apply t2 after t1."""
        # Combined scale and rotation
        scale = t1['scale'] * t2['scale']
        rotation = t1['rotation'] + t2['rotation']

        # Combined translation
        cos_r = math.cos(t2['rotation'])
        sin_r = math.sin(t2['rotation'])

        dx = t2['scale'] * (cos_r * t1['dx'] - sin_r * t1['dy']) + t2['dx']
        dy = t2['scale'] * (sin_r * t1['dx'] + cos_r * t1['dy']) + t2['dy']

        return {
            'dx': dx,
            'dy': dy,
            'scale': scale,
            'rotation': rotation
        }
