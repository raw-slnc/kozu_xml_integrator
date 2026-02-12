# -*- coding: utf-8 -*-
"""
Thin Plate Spline (TPS) Transform for KozuXmlIntegrator

TPS provides smooth non-linear transformation that:
- Exactly interpolates control points
- Minimizes bending energy
- Provides smooth transitions between regions

Used for chained placement where neighboring maps need
smooth warping to align with fixed reference points.
"""

from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass
import math
import numpy as np
import logging

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
)

logger = logging.getLogger(__name__)


def _tps_kernel(r: float) -> float:
    """
    TPS radial basis function: U(r) = r^2 * log(r)

    Special case: U(0) = 0 (limit as r -> 0)
    """
    if r < 1e-10:
        return 0.0
    return r * r * math.log(r)


def _compute_distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Compute Euclidean distance between two points."""
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)


@dataclass
class TPSParameters:
    """Parameters for TPS transformation."""
    # Affine part: [a0, ax, ay] for X; [b0, bx, by] for Y
    affine_x: np.ndarray  # shape (3,)
    affine_y: np.ndarray  # shape (3,)

    # Non-linear weights
    weights_x: np.ndarray  # shape (n,)
    weights_y: np.ndarray  # shape (n,)

    # Control points (source)
    control_points: List[Tuple[float, float]]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            'affine_x': self.affine_x.tolist(),
            'affine_y': self.affine_y.tolist(),
            'weights_x': self.weights_x.tolist(),
            'weights_y': self.weights_y.tolist(),
            'control_points': self.control_points,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TPSParameters':
        """Create from dictionary."""
        return cls(
            affine_x=np.array(data['affine_x']),
            affine_y=np.array(data['affine_y']),
            weights_x=np.array(data['weights_x']),
            weights_y=np.array(data['weights_y']),
            control_points=data['control_points'],
        )


@dataclass
class TPSResult:
    """Result of TPS computation."""
    params: TPSParameters
    bending_energy: float
    num_points: int

    @property
    def is_valid(self) -> bool:
        """Check if transformation is valid."""
        return self.num_points >= 3


class TPSTransformer:
    """
    Thin Plate Spline (TPS) transformation.

    Provides smooth non-linear transformation that exactly
    interpolates control points while minimizing bending energy.

    Mathematical formulation:
        f(x, y) = a0 + ax*x + ay*y + sum(wi * U(ri))

    where:
        U(r) = r^2 * log(r) is the TPS kernel
        ri = distance from (x, y) to i-th control point
    """

    def __init__(self):
        """Initialize transformer."""
        self._params: Optional[TPSParameters] = None
        self._result: Optional[TPSResult] = None

    def compute_parameters(self,
                          source_points: List[Tuple[float, float]],
                          target_points: List[Tuple[float, float]],
                          regularization: float = 0.0
                          ) -> TPSResult:
        """
        Compute TPS transformation parameters.

        Args:
            source_points: Control points in source coordinate system
            target_points: Corresponding points in target coordinate system
            regularization: Smoothing parameter (0 = exact interpolation)

        Returns:
            TPSResult with computed parameters

        Raises:
            ValueError: If fewer than 3 point pairs provided
        """
        if len(source_points) < 3:
            raise ValueError("At least 3 control point pairs required for TPS")

        if len(source_points) != len(target_points):
            raise ValueError("Source and target point lists must have same length")

        n = len(source_points)
        src = np.array(source_points)
        tgt = np.array(target_points)

        # Build kernel matrix K
        K = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                if i != j:
                    r = _compute_distance(source_points[i], source_points[j])
                    K[i, j] = _tps_kernel(r)

        # Add regularization
        if regularization > 0:
            K += regularization * np.eye(n)

        # Build P matrix (for affine part)
        P = np.zeros((n, 3))
        P[:, 0] = 1  # constant term
        P[:, 1] = src[:, 0]  # x
        P[:, 2] = src[:, 1]  # y

        # Build full system matrix
        # [K  P] [w]   [v]
        # [P' 0] [a] = [0]
        #
        # where v = target coordinates, w = weights, a = affine params

        L = np.zeros((n + 3, n + 3))
        L[:n, :n] = K
        L[:n, n:n+3] = P
        L[n:n+3, :n] = P.T

        # Right hand side for X and Y
        rhs_x = np.zeros(n + 3)
        rhs_x[:n] = tgt[:, 0]

        rhs_y = np.zeros(n + 3)
        rhs_y[:n] = tgt[:, 1]

        # Solve systems
        try:
            params_x = np.linalg.solve(L, rhs_x)
            params_y = np.linalg.solve(L, rhs_y)
        except np.linalg.LinAlgError:
            logger.error("Singular matrix in TPS computation")
            # Return identity-like transformation
            return self._create_identity_result(source_points, n)

        # Extract weights and affine parameters
        weights_x = params_x[:n]
        affine_x = params_x[n:]

        weights_y = params_y[:n]
        affine_y = params_y[n:]

        # Compute bending energy
        bending_x = weights_x @ K @ weights_x
        bending_y = weights_y @ K @ weights_y
        bending_energy = bending_x + bending_y

        self._params = TPSParameters(
            affine_x=affine_x,
            affine_y=affine_y,
            weights_x=weights_x,
            weights_y=weights_y,
            control_points=source_points
        )

        self._result = TPSResult(
            params=self._params,
            bending_energy=bending_energy,
            num_points=n
        )

        logger.info(f"TPS computed: {n} control points, "
                   f"bending energy={bending_energy:.6f}")

        return self._result

    def _create_identity_result(self,
                               source_points: List[Tuple[float, float]],
                               n: int) -> TPSResult:
        """Create identity transformation result."""
        params = TPSParameters(
            affine_x=np.array([0.0, 1.0, 0.0]),
            affine_y=np.array([0.0, 0.0, 1.0]),
            weights_x=np.zeros(n),
            weights_y=np.zeros(n),
            control_points=source_points
        )
        return TPSResult(
            params=params,
            bending_energy=0.0,
            num_points=n
        )

    def transform_point(self, x: float, y: float) -> Tuple[float, float]:
        """
        Transform a single point using TPS.

        Args:
            x: Source X coordinate
            y: Source Y coordinate

        Returns:
            Tuple of (x', y') transformed coordinates
        """
        if self._params is None:
            raise RuntimeError("Parameters not computed. Call compute_parameters first.")

        p = self._params

        # Affine part
        x_new = p.affine_x[0] + p.affine_x[1] * x + p.affine_x[2] * y
        y_new = p.affine_y[0] + p.affine_y[1] * x + p.affine_y[2] * y

        # Non-linear part
        for i, cp in enumerate(p.control_points):
            r = math.sqrt((x - cp[0])**2 + (y - cp[1])**2)
            u = _tps_kernel(r)
            x_new += p.weights_x[i] * u
            y_new += p.weights_y[i] * u

        return (x_new, y_new)

    def transform_points(self,
                        points: List[Tuple[float, float]]
                        ) -> List[Tuple[float, float]]:
        """
        Transform a list of points.

        Args:
            points: List of (x, y) tuples

        Returns:
            List of transformed (x', y') tuples
        """
        return [self.transform_point(x, y) for x, y in points]

    def transform_geometry(self, geometry: QgsGeometry) -> QgsGeometry:
        """
        Transform a QGIS geometry using TPS.

        Args:
            geometry: QgsGeometry to transform

        Returns:
            Transformed QgsGeometry
        """
        if geometry.isEmpty():
            return QgsGeometry()

        geom_type = geometry.type()

        if geom_type == 0:  # Point
            pt = geometry.asPoint()
            x_new, y_new = self.transform_point(pt.x(), pt.y())
            return QgsGeometry.fromPointXY(QgsPointXY(x_new, y_new))

        elif geom_type == 1:  # Line
            if geometry.isMultipart():
                lines = geometry.asMultiPolyline()
                new_lines = []
                for line in lines:
                    new_line = [QgsPointXY(*self.transform_point(pt.x(), pt.y()))
                               for pt in line]
                    new_lines.append(new_line)
                return QgsGeometry.fromMultiPolylineXY(new_lines)
            else:
                line = geometry.asPolyline()
                new_line = [QgsPointXY(*self.transform_point(pt.x(), pt.y()))
                           for pt in line]
                return QgsGeometry.fromPolylineXY(new_line)

        elif geom_type == 2:  # Polygon
            if geometry.isMultipart():
                polygons = geometry.asMultiPolygon()
                new_polygons = []
                for polygon in polygons:
                    new_rings = []
                    for ring in polygon:
                        new_ring = [QgsPointXY(*self.transform_point(pt.x(), pt.y()))
                                   for pt in ring]
                        new_rings.append(new_ring)
                    new_polygons.append(new_rings)
                return QgsGeometry.fromMultiPolygonXY(new_polygons)
            else:
                polygon = geometry.asPolygon()
                new_rings = []
                for ring in polygon:
                    new_ring = [QgsPointXY(*self.transform_point(pt.x(), pt.y()))
                               for pt in ring]
                    new_rings.append(new_ring)
                return QgsGeometry.fromPolygonXY(new_rings)

        logger.warning(f"Unsupported geometry type: {geom_type}")
        return geometry

    @property
    def parameters(self) -> Optional[TPSParameters]:
        """Get computed parameters."""
        return self._params

    @property
    def result(self) -> Optional[TPSResult]:
        """Get full computation result."""
        return self._result

    def set_parameters(self, params: TPSParameters):
        """
        Set transformation parameters directly.

        Args:
            params: TPSParameters to use
        """
        self._params = params


class ChainedTPSTransformer:
    """
    Chained TPS transformation for sequential map placement.

    Supports propagating transformations through connected maps
    where earlier transformations affect later ones.
    """

    def __init__(self):
        """Initialize chained transformer."""
        self._transformers: Dict[str, TPSTransformer] = {}
        self._chain_order: List[str] = []

    def add_transformation(self,
                          map_id: str,
                          source_points: List[Tuple[float, float]],
                          target_points: List[Tuple[float, float]],
                          depends_on: Optional[str] = None) -> TPSResult:
        """
        Add a transformation to the chain.

        Args:
            map_id: Unique identifier for this map
            source_points: Control points in source system
            target_points: Corresponding points in target system
            depends_on: ID of map this depends on (for chained transformation)

        Returns:
            TPSResult for this transformation
        """
        # If depends on another map, transform source points first
        if depends_on and depends_on in self._transformers:
            parent = self._transformers[depends_on]
            source_points = parent.transform_points(source_points)

        transformer = TPSTransformer()
        result = transformer.compute_parameters(source_points, target_points)

        self._transformers[map_id] = transformer
        self._chain_order.append(map_id)

        return result

    def transform_point(self, map_id: str, x: float, y: float) -> Tuple[float, float]:
        """
        Transform a point using the specified map's transformation.

        Args:
            map_id: Map identifier
            x: Source X coordinate
            y: Source Y coordinate

        Returns:
            Transformed coordinates
        """
        if map_id not in self._transformers:
            raise KeyError(f"No transformation found for map: {map_id}")

        return self._transformers[map_id].transform_point(x, y)

    def transform_geometry(self, map_id: str, geometry: QgsGeometry) -> QgsGeometry:
        """
        Transform a geometry using the specified map's transformation.

        Args:
            map_id: Map identifier
            geometry: QgsGeometry to transform

        Returns:
            Transformed geometry
        """
        if map_id not in self._transformers:
            raise KeyError(f"No transformation found for map: {map_id}")

        return self._transformers[map_id].transform_geometry(geometry)

    def get_transformer(self, map_id: str) -> Optional[TPSTransformer]:
        """Get transformer for a specific map."""
        return self._transformers.get(map_id)

    @property
    def chain_order(self) -> List[str]:
        """Get the order of transformations in the chain."""
        return self._chain_order.copy()
