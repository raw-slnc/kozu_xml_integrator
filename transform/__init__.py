# -*- coding: utf-8 -*-
"""
Transform modules for KozuXmlIntegrator

This package contains geometric transformation functionality:
- helmert_transform: 4-parameter Helmert transformation
- tps_transform: Thin Plate Spline transformation
- matching_algorithm: Map sheet matching algorithms
- spatial_fitting: Chain positioning with anchors (primary method)
- xml_joiner: XML joining within oaza using common chibans
- leveling: Overlap detection and gradual leveling adjustments
"""

from .helmert_transform import (
    HelmertTransformer,
    HelmertParameters,
    TransformResult,
    compute_helmert_from_centroids,
)

from .tps_transform import (
    TPSTransformer,
    TPSParameters,
    TPSResult,
    ChainedTPSTransformer,
)

from .matching_algorithm import (
    ChibanMatcher,
    ShapeMatcher,
    BoundaryMatcher,
    TransformCandidateFinder,
    MatchCandidate,
    ControlPointPair,
)

from .spatial_fitting import (
    # New architecture (PRIMARY)
    TransformParams,
    PositioningGuideLoader,
    ChainPositioner,
    GeometryTransformer,
    # Backward compatibility aliases
    ContainerLoader,
    RubberSheetFitter,
    SpatialFitResult,
    SpatialFitTransformer,
    OazaShapeLoader,
    SpatialFitter,
)

from .xml_joiner import (
    XmlRelationship,
    XmlRelationshipGraph,
    XmlJoiner,
    JoinResult,
)

from .leveling import (
    OverlapInfo,
    LevelingResult,
    OverlapDetector,
    Leveler,
    TopologyChecker,
)

__all__ = [
    # Helmert
    'HelmertTransformer',
    'HelmertParameters',
    'TransformResult',
    'compute_helmert_from_centroids',
    # TPS
    'TPSTransformer',
    'TPSParameters',
    'TPSResult',
    'ChainedTPSTransformer',
    # Matching
    'ChibanMatcher',
    'ShapeMatcher',
    'BoundaryMatcher',
    'TransformCandidateFinder',
    'MatchCandidate',
    'ControlPointPair',
    # Chain Positioning (PRIMARY method)
    'TransformParams',
    'PositioningGuideLoader',
    'ChainPositioner',
    'GeometryTransformer',
    # Backward compatibility aliases
    'ContainerLoader',
    'RubberSheetFitter',
    'SpatialFitResult',
    'SpatialFitTransformer',
    'OazaShapeLoader',
    'SpatialFitter',
    # XML Joiner (integration v2)
    'XmlRelationship',
    'XmlRelationshipGraph',
    'XmlJoiner',
    'JoinResult',
    # Leveling (integration v2)
    'OverlapInfo',
    'LevelingResult',
    'OverlapDetector',
    'Leveler',
    'TopologyChecker',
]
