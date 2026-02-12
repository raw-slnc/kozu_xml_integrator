# -*- coding: utf-8 -*-
"""
Helmert Transform for KozuXmlIntegrator

4-parameter Helmert transformation (2D similarity transformation):
- Translation (dx, dy)
- Rotation (theta)
- Uniform scale (s)

Mathematical formulation:
    X' = a*X - b*Y + dx
    Y' = b*X + a*Y + dy

    where: a = s*cos(theta), b = s*sin(theta)

Uses least squares method for optimal parameter estimation.
"""

from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass
import math
import numpy as np
import logging

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsPolygon,
    QgsLineString,
    QgsPoint,
)

logger = logging.getLogger(__name__)


@dataclass
class HelmertParameters:
    """Parameters for Helmert transformation."""
    dx: float  # X translation
    dy: float  # Y translation
    rotation: float  # Rotation angle in radians
    scale: float  # Uniform scale factor

    # Derived parameters
    a: float = 0.0  # s * cos(theta)
    b: float = 0.0  # s * sin(theta)

    def __post_init__(self):
        """Calculate derived parameters."""
        self.a = self.scale * math.cos(self.rotation)
        self.b = self.scale * math.sin(self.rotation)

    @property
    def rotation_degrees(self) -> float:
        """Rotation angle in degrees."""
        return math.degrees(self.rotation)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            'dx': self.dx,
            'dy': self.dy,
            'rotation_rad': self.rotation,
            'rotation_deg': self.rotation_degrees,
            'scale': self.scale,
            'a': self.a,
            'b': self.b,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HelmertParameters':
        """Create from dictionary."""
        return cls(
            dx=data['dx'],
            dy=data['dy'],
            rotation=data['rotation_rad'],
            scale=data['scale']
        )


@dataclass
class TransformResult:
    """Result of Helmert transformation computation."""
    params: HelmertParameters
    rmse: float  # Root Mean Square Error
    residuals: List[Tuple[float, float]]  # (dx, dy) residuals per point
    num_points: int

    @property
    def is_valid(self) -> bool:
        """Check if transformation is valid."""
        return (
            self.num_points >= 2 and
            not math.isnan(self.rmse) and
            self.rmse < float('inf')
        )


class HelmertTransformer:
    """
    2D Helmert (similarity) transformation.

    Computes optimal transformation parameters using least squares
    from a set of corresponding control points.
    """

    def __init__(self):
        """Initialize transformer."""
        self._params: Optional[HelmertParameters] = None
        self._result: Optional[TransformResult] = None

    def compute_parameters(self,
                          source_points: List[Tuple[float, float]],
                          target_points: List[Tuple[float, float]]
                          ) -> TransformResult:
        """
        Compute transformation parameters from control point pairs.

        Uses least squares method to find optimal parameters that
        minimize the sum of squared residuals.

        Args:
            source_points: List of (x, y) points in source CRS
            target_points: List of (x, y) points in target CRS

        Returns:
            TransformResult with computed parameters and statistics

        Raises:
            ValueError: If fewer than 2 point pairs provided
        """
        if len(source_points) < 2 or len(target_points) < 2:
            raise ValueError("At least 2 control point pairs required")

        if len(source_points) != len(target_points):
            raise ValueError("Source and target point lists must have same length")

        n = len(source_points)

        # Build design matrix A and observation vector L
        # For each point: [X' = a*X - b*Y + dx, Y' = b*X + a*Y + dy]
        A = np.zeros((2 * n, 4))
        L = np.zeros(2 * n)

        for i, ((xs, ys), (xt, yt)) in enumerate(zip(source_points, target_points)):
            # X equation
            A[2*i, 0] = xs    # coefficient for a
            A[2*i, 1] = -ys   # coefficient for b
            A[2*i, 2] = 1     # coefficient for dx
            A[2*i, 3] = 0     # coefficient for dy
            L[2*i] = xt

            # Y equation
            A[2*i+1, 0] = ys   # coefficient for a
            A[2*i+1, 1] = xs   # coefficient for b
            A[2*i+1, 2] = 0    # coefficient for dx
            A[2*i+1, 3] = 1    # coefficient for dy
            L[2*i+1] = yt

        # Solve using least squares: (A'A)^-1 * A'L
        try:
            AtA = A.T @ A
            AtL = A.T @ L
            params_vec = np.linalg.solve(AtA, AtL)
        except np.linalg.LinAlgError:
            logger.error("Singular matrix in Helmert computation")
            # Return identity transformation
            return TransformResult(
                params=HelmertParameters(0, 0, 0, 1),
                rmse=float('inf'),
                residuals=[(0, 0)] * n,
                num_points=n
            )

        a, b, dx, dy = params_vec

        # Calculate rotation and scale from a, b
        scale = math.sqrt(a*a + b*b)
        rotation = math.atan2(b, a)

        params = HelmertParameters(
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale=scale
        )

        # Calculate residuals
        residuals = []
        sum_sq_residuals = 0.0

        for (xs, ys), (xt, yt) in zip(source_points, target_points):
            x_transformed = a * xs - b * ys + dx
            y_transformed = b * xs + a * ys + dy

            res_x = xt - x_transformed
            res_y = yt - y_transformed

            residuals.append((res_x, res_y))
            sum_sq_residuals += res_x**2 + res_y**2

        # RMSE
        rmse = math.sqrt(sum_sq_residuals / (2 * n))

        self._params = params
        self._result = TransformResult(
            params=params,
            rmse=rmse,
            residuals=residuals,
            num_points=n
        )

        logger.info(f"Helmert params: dx={dx:.3f}, dy={dy:.3f}, "
                   f"rot={params.rotation_degrees:.4f}deg, scale={scale:.6f}, "
                   f"RMSE={rmse:.4f}")

        return self._result

    def transform_point(self, x: float, y: float) -> Tuple[float, float]:
        """
        Transform a single point.

        Args:
            x: Source X coordinate
            y: Source Y coordinate

        Returns:
            Tuple of (x', y') transformed coordinates
        """
        if self._params is None:
            raise RuntimeError("Parameters not computed. Call compute_parameters first.")

        p = self._params
        x_new = p.a * x - p.b * y + p.dx
        y_new = p.b * x + p.a * y + p.dy

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
        Transform a QGIS geometry.

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

    def inverse_transform_point(self, x: float, y: float) -> Tuple[float, float]:
        """
        Apply inverse transformation to a point.

        Args:
            x: Target X coordinate
            y: Target Y coordinate

        Returns:
            Tuple of (x', y') inverse-transformed coordinates
        """
        if self._params is None:
            raise RuntimeError("Parameters not computed. Call compute_parameters first.")

        p = self._params

        # Inverse: solve for source given target
        # x_t = a*x_s - b*y_s + dx
        # y_t = b*x_s + a*y_s + dy
        #
        # Rearranging:
        # x_s = (a*(x_t - dx) + b*(y_t - dy)) / (a^2 + b^2)
        # y_s = (a*(y_t - dy) - b*(x_t - dx)) / (a^2 + b^2)

        det = p.a**2 + p.b**2
        if abs(det) < 1e-12:
            raise ValueError("Transformation is singular, cannot invert")

        x_shifted = x - p.dx
        y_shifted = y - p.dy

        x_src = (p.a * x_shifted + p.b * y_shifted) / det
        y_src = (p.a * y_shifted - p.b * x_shifted) / det

        return (x_src, y_src)

    @property
    def parameters(self) -> Optional[HelmertParameters]:
        """Get computed parameters."""
        return self._params

    @property
    def result(self) -> Optional[TransformResult]:
        """Get full computation result."""
        return self._result

    def set_parameters(self, params: HelmertParameters):
        """
        Set transformation parameters directly.

        Args:
            params: HelmertParameters to use
        """
        self._params = params


def compute_helmert_from_centroids(
    source_polygons: List[QgsGeometry],
    target_polygons: List[QgsGeometry]
) -> Optional[TransformResult]:
    """
    Compute Helmert transformation from polygon centroid correspondences.

    Convenience function that extracts centroids and computes transformation.

    Args:
        source_polygons: List of source polygons
        target_polygons: List of corresponding target polygons

    Returns:
        TransformResult or None if computation fails
    """
    if len(source_polygons) != len(target_polygons):
        logger.error("Source and target polygon lists must have same length")
        return None

    source_points = []
    target_points = []

    for src, tgt in zip(source_polygons, target_polygons):
        src_centroid = src.centroid()
        tgt_centroid = tgt.centroid()

        if src_centroid.isEmpty() or tgt_centroid.isEmpty():
            continue

        src_pt = src_centroid.asPoint()
        tgt_pt = tgt_centroid.asPoint()

        source_points.append((src_pt.x(), src_pt.y()))
        target_points.append((tgt_pt.x(), tgt_pt.y()))

    if len(source_points) < 2:
        logger.error("Insufficient valid centroid pairs")
        return None

    transformer = HelmertTransformer()
    return transformer.compute_parameters(source_points, target_points)
