# -*- coding: utf-8 -*-
"""
Core modules for KozuXmlIntegrator

This package contains the core functionality:
- xml_parser: XML streaming parser for cadastral map data
- geometry_builder: Build QgsGeometry from parsed XML data
- database_manager: SpatiaLite database management
- spatial_join: Spatial join with administrative boundaries
- search_index: Dynamic search index generation
- importer: Import orchestration
"""

from .xml_parser import (
    KozuXmlParser,
    XmlMapData,
    XmlMapHeader,
    GmPoint,
    GmCurve,
    GmSurface,
    Fude,
    parse_xml_file,
)

from .geometry_builder import (
    GeometryBuilder,
    build_all_fude_geometries,
)

from .database_manager import (
    DatabaseManager,
    XmlMetaRecord,
    FudePolyRecord,
)

from .spatial_join import (
    SpatialJoiner,
    load_admin_layer,
    normalize_municipality_name,
    match_municipality_names,
)

from .search_index import (
    SearchIndex,
    SearchResult,
)

from .importer import (
    XmlImporter,
    ImportProgress,
    ImportResult,
    create_database_and_import,
)

__all__ = [
    # xml_parser
    'KozuXmlParser',
    'XmlMapData',
    'XmlMapHeader',
    'GmPoint',
    'GmCurve',
    'GmSurface',
    'Fude',
    'parse_xml_file',
    # geometry_builder
    'GeometryBuilder',
    'build_all_fude_geometries',
    # database_manager
    'DatabaseManager',
    'XmlMetaRecord',
    'FudePolyRecord',
    # spatial_join
    'SpatialJoiner',
    'load_admin_layer',
    'normalize_municipality_name',
    'match_municipality_names',
    # search_index
    'SearchIndex',
    'SearchResult',
    # importer
    'XmlImporter',
    'ImportProgress',
    'ImportResult',
    'create_database_and_import',
]
