# -*- coding: utf-8 -*-
"""
Database Manager for KozuXmlIntegrator

This module manages the SpatiaLite database for storing parsed XML data.
Provides CRUD operations and spatial query functionality.

Database Schema:
- t_xml_meta: Map metadata (file info, CRS type, envelope)
- t_fude_poly: Parcel polygons with attributes
- t_fude_edge: Edge-level boundary type management
- t_transform_log: Transformation history
"""

import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Generator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging

from qgis.core import (
    QgsVectorLayer,
    QgsDataSourceUri,
    QgsGeometry,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsCoordinateReferenceSystem,
)
from PyQt5.QtCore import QVariant

logger = logging.getLogger(__name__)


# Schema version for migration support
SCHEMA_VERSION = '2.0.0'  # Added reliability tracking for integration v2


# SQL statements for table creation
CREATE_TABLES_SQL = """
-- Schema info table
CREATE TABLE IF NOT EXISTS t_schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- XML metadata table
CREATE TABLE IF NOT EXISTS t_xml_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL UNIQUE,
    map_name TEXT,
    municipality_code TEXT,
    municipality_name TEXT,
    oaza_name TEXT,
    crs_type TEXT NOT NULL,
    geodetic_type TEXT,
    transform_program TEXT,
    point_count INTEGER DEFAULT 0,
    curve_count INTEGER DEFAULT 0,
    fude_count INTEGER DEFAULT 0,
    scale_denominator INTEGER,
    status TEXT DEFAULT 'imported',
    transform_params TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_date TIMESTAMP
);
SELECT AddGeometryColumn('t_xml_meta', 'geom', 0, 'POLYGON', 'XY');

-- Parcel polygon table
CREATE TABLE IF NOT EXISTS t_fude_poly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_meta_id INTEGER NOT NULL REFERENCES t_xml_meta(id) ON DELETE CASCADE,
    fude_id TEXT NOT NULL,
    oaza_code TEXT,
    oaza_name TEXT,
    chome_code TEXT,
    koaza_code TEXT,
    yobi_code TEXT,
    chiban TEXT,
    coord_type TEXT,
    area_sqm REAL,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_date TIMESTAMP,
    -- Integration v2: Reliability tracking
    reliability TEXT CHECK(reliability IN ('HIGH', 'MEDIUM', 'LOW')),
    needs_review INTEGER DEFAULT 0,
    review_reason TEXT,
    transform_method TEXT,
    UNIQUE(xml_meta_id, fude_id)
);
SELECT AddGeometryColumn('t_fude_poly', 'geom', 0, 'POLYGON', 'XY');

-- Edge boundary type table
CREATE TABLE IF NOT EXISTS t_fude_edge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fude_poly_id INTEGER NOT NULL REFERENCES t_fude_poly(id) ON DELETE CASCADE,
    edge_index INTEGER NOT NULL,
    boundary_type TEXT CHECK(boundary_type IN ('fixed', 'floating', 'connection', 'fixed_candidate')),
    auto_detected INTEGER DEFAULT 1,
    user_confirmed INTEGER DEFAULT 0,
    note TEXT
);
SELECT AddGeometryColumn('t_fude_edge', 'geom', 0, 'LINESTRING', 'XY');

-- Transformation log table
CREATE TABLE IF NOT EXISTS t_transform_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_xml_id INTEGER REFERENCES t_xml_meta(id),
    target_xml_id INTEGER REFERENCES t_xml_meta(id),
    transform_type TEXT,
    params TEXT,
    match_score REAL,
    rmse REAL,
    applied_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT,
    notes TEXT
);

-- Create spatial indexes
SELECT CreateSpatialIndex('t_xml_meta', 'geom');
SELECT CreateSpatialIndex('t_fude_poly', 'geom');
SELECT CreateSpatialIndex('t_fude_edge', 'geom');
"""


@dataclass
class XmlMetaRecord:
    """Data class for t_xml_meta record"""
    id: Optional[int] = None
    file_name: str = ''
    map_name: str = ''
    municipality_code: str = ''
    municipality_name: str = ''
    oaza_name: str = ''
    crs_type: str = ''
    geodetic_type: Optional[str] = None
    transform_program: Optional[str] = None
    point_count: int = 0
    curve_count: int = 0
    fude_count: int = 0
    scale_denominator: Optional[int] = None
    status: str = 'imported'
    transform_params: Optional[str] = None
    geom_wkt: str = ''


@dataclass
class FudePolyRecord:
    """Data class for t_fude_poly record"""
    id: Optional[int] = None
    xml_meta_id: int = 0
    fude_id: str = ''
    oaza_code: str = ''
    oaza_name: str = ''
    chome_code: str = ''
    koaza_code: str = ''
    yobi_code: str = ''
    chiban: str = ''
    coord_type: str = ''
    area_sqm: float = 0.0
    geom_wkt: str = ''
    # Integration v2: Reliability tracking
    reliability: Optional[str] = None  # HIGH, MEDIUM, LOW
    needs_review: int = 0
    review_reason: Optional[str] = None
    transform_method: Optional[str] = None


class DatabaseManager:
    """
    SpatiaLite database manager for cadastral map data.

    Provides database creation, data insertion, querying, and
    export functionality.
    """

    def __init__(self, db_path: Path):
        """
        Initialize database manager.

        Args:
            db_path: Path to the SpatiaLite database file
        """
        self.db_path = Path(db_path)
        self._connection: Optional[sqlite3.Connection] = None

    @contextmanager
    def connection(self):
        """
        Context manager for database connection.

        Yields:
            sqlite3.Connection: Database connection
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        # Enable SpatiaLite extension
        conn.enable_load_extension(True)
        try:
            conn.load_extension('mod_spatialite')
        except sqlite3.OperationalError:
            # Try alternative names
            try:
                conn.load_extension('libspatialite')
            except sqlite3.OperationalError:
                logger.warning("Could not load SpatiaLite extension")

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_database(self) -> None:
        """
        Create a new database with the required schema.

        Initializes SpatiaLite metadata and creates all required tables.
        """
        logger.info(f"Creating database: {self.db_path}")

        with self.connection() as conn:
            cursor = conn.cursor()

            # Initialize SpatiaLite metadata
            cursor.execute("SELECT InitSpatialMetaData(1)")

            # Create tables (execute each statement separately)
            for statement in CREATE_TABLES_SQL.split(';'):
                statement = statement.strip()
                if statement:
                    try:
                        cursor.execute(statement)
                    except sqlite3.OperationalError as e:
                        if 'already exists' not in str(e).lower():
                            logger.warning(f"SQL error: {e}")

            # Set schema version
            cursor.execute(
                "INSERT OR REPLACE INTO t_schema_info (key, value) VALUES (?, ?)",
                ('schema_version', SCHEMA_VERSION)
            )

            conn.commit()

        logger.info("Database created successfully")

    def migrate_database(self) -> None:
        """
        Run database migrations to update schema for existing databases.

        Adds missing columns that were added in later versions.
        """
        logger.info(f"Running database migrations on: {self.db_path}")

        with self.connection() as conn:
            cursor = conn.cursor()

            # Check and add missing columns to t_xml_meta
            self._add_column_if_missing(cursor, 't_xml_meta', 'scale_denominator', 'INTEGER')
            self._add_column_if_missing(cursor, 't_xml_meta', 'updated_date', 'TIMESTAMP')

            # Check and add missing columns to t_fude_poly
            self._add_column_if_missing(cursor, 't_fude_poly', 'import_date', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
            self._add_column_if_missing(cursor, 't_fude_poly', 'updated_date', 'TIMESTAMP')

            # Integration v2: Reliability tracking columns
            self._add_column_if_missing(cursor, 't_fude_poly', 'reliability', 'TEXT')
            self._add_column_if_missing(cursor, 't_fude_poly', 'needs_review', 'INTEGER DEFAULT 0')
            self._add_column_if_missing(cursor, 't_fude_poly', 'review_reason', 'TEXT')
            self._add_column_if_missing(cursor, 't_fude_poly', 'transform_method', 'TEXT')

            # Update schema version
            cursor.execute(
                "INSERT OR REPLACE INTO t_schema_info (key, value) VALUES (?, ?)",
                ('schema_version', SCHEMA_VERSION)
            )

            conn.commit()

        logger.info("Database migration completed")

    def _add_column_if_missing(self, cursor: sqlite3.Cursor, table: str, column: str, column_def: str) -> bool:
        """
        Add a column to a table if it doesn't exist.

        Args:
            cursor: Database cursor
            table: Table name
            column: Column name
            column_def: Column definition (type and constraints)

        Returns:
            True if column was added, False if it already existed
        """
        # Get existing columns
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]

        if column not in columns:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
                logger.info(f"Added column {column} to {table}")
                return True
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add column {column} to {table}: {e}")
                return False

        return False

    def insert_xml_meta(self, record: XmlMetaRecord) -> int:
        """
        Insert XML metadata record.

        Args:
            record: XmlMetaRecord object

        Returns:
            int: Inserted record ID
        """
        with self.connection() as conn:
            cursor = conn.cursor()

            if record.geom_wkt:
                cursor.execute("""
                    INSERT INTO t_xml_meta
                    (file_name, map_name, municipality_code, municipality_name,
                     oaza_name, crs_type, geodetic_type, transform_program,
                     point_count, curve_count, fude_count, scale_denominator,
                     status, geom)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GeomFromText(?, 0))
                """, (
                    record.file_name, record.map_name, record.municipality_code,
                    record.municipality_name, record.oaza_name, record.crs_type,
                    record.geodetic_type, record.transform_program,
                    record.point_count, record.curve_count, record.fude_count,
                    record.scale_denominator, record.status, record.geom_wkt
                ))
            else:
                cursor.execute("""
                    INSERT INTO t_xml_meta
                    (file_name, map_name, municipality_code, municipality_name,
                     oaza_name, crs_type, geodetic_type, transform_program,
                     point_count, curve_count, fude_count, scale_denominator,
                     status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.file_name, record.map_name, record.municipality_code,
                    record.municipality_name, record.oaza_name, record.crs_type,
                    record.geodetic_type, record.transform_program,
                    record.point_count, record.curve_count, record.fude_count,
                    record.scale_denominator, record.status
                ))

            return cursor.lastrowid

    def insert_fude_batch(self, records: List[FudePolyRecord],
                         batch_size: int = 1000) -> int:
        """
        Insert parcel records in batches for efficiency.

        Args:
            records: List of FudePolyRecord objects
            batch_size: Number of records per batch

        Returns:
            int: Total number of inserted records
        """
        total_inserted = 0

        with self.connection() as conn:
            cursor = conn.cursor()

            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]

                for record in batch:
                    try:
                        if record.geom_wkt:
                            cursor.execute("""
                                INSERT INTO t_fude_poly
                                (xml_meta_id, fude_id, oaza_code, oaza_name,
                                 chome_code, koaza_code, yobi_code, chiban,
                                 coord_type, area_sqm, geom)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GeomFromText(?, 0))
                            """, (
                                record.xml_meta_id, record.fude_id, record.oaza_code,
                                record.oaza_name, record.chome_code, record.koaza_code,
                                record.yobi_code, record.chiban, record.coord_type,
                                record.area_sqm, record.geom_wkt
                            ))
                        else:
                            cursor.execute("""
                                INSERT INTO t_fude_poly
                                (xml_meta_id, fude_id, oaza_code, oaza_name,
                                 chome_code, koaza_code, yobi_code, chiban,
                                 coord_type, area_sqm)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                record.xml_meta_id, record.fude_id, record.oaza_code,
                                record.oaza_name, record.chome_code, record.koaza_code,
                                record.yobi_code, record.chiban, record.coord_type,
                                record.area_sqm
                            ))
                        total_inserted += 1
                    except sqlite3.IntegrityError as e:
                        logger.warning(f"Duplicate fude: {record.fude_id} - {e}")

                conn.commit()
                logger.debug(f"Inserted batch: {i} to {i + len(batch)}")

        return total_inserted

    def get_xml_meta_by_id(self, meta_id: int) -> Optional[Dict[str, Any]]:
        """Get XML metadata by ID."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, file_name, map_name, municipality_code, municipality_name,
                       oaza_name, crs_type, geodetic_type, transform_program,
                       point_count, curve_count, fude_count, scale_denominator,
                       status, AsText(geom) as geom_wkt
                FROM t_xml_meta WHERE id = ?
            """, (meta_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_xml_meta_by_filename(self, filename: str) -> Optional[Dict[str, Any]]:
        """Get XML metadata by filename."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, file_name, map_name, municipality_code, municipality_name,
                       oaza_name, crs_type, geodetic_type, transform_program,
                       point_count, curve_count, fude_count, scale_denominator,
                       status, AsText(geom) as geom_wkt
                FROM t_xml_meta WHERE file_name = ?
            """, (filename,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_xml_meta(self) -> List[Dict[str, Any]]:
        """Get all XML metadata records."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, file_name, map_name, municipality_code, municipality_name,
                       oaza_name, crs_type, geodetic_type, transform_program,
                       point_count, curve_count, fude_count, scale_denominator,
                       status, AsText(geom) as geom_wkt
                FROM t_xml_meta ORDER BY file_name
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_fude_by_xml_id(self, xml_meta_id: int) -> List[Dict[str, Any]]:
        """Get all parcels for a specific XML file."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, xml_meta_id, fude_id, oaza_code, oaza_name,
                       chome_code, koaza_code, yobi_code, chiban, coord_type,
                       area_sqm, AsText(geom) as geom_wkt
                FROM t_fude_poly WHERE xml_meta_id = ?
            """, (xml_meta_id,))
            return [dict(row) for row in cursor.fetchall()]

    def search_fude_by_chiban(self, chiban: str,
                              oaza_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search parcels by land number (chiban)."""
        with self.connection() as conn:
            cursor = conn.cursor()

            if oaza_name:
                cursor.execute("""
                    SELECT f.id, f.xml_meta_id, f.fude_id, f.oaza_code, f.oaza_name,
                           f.chome_code, f.koaza_code, f.chiban, f.coord_type,
                           m.file_name, m.crs_type,
                           AsText(f.geom) as geom_wkt
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE f.chiban LIKE ? AND f.oaza_name = ?
                """, (f'%{chiban}%', oaza_name))
            else:
                cursor.execute("""
                    SELECT f.id, f.xml_meta_id, f.fude_id, f.oaza_code, f.oaza_name,
                           f.chome_code, f.koaza_code, f.chiban, f.coord_type,
                           m.file_name, m.crs_type,
                           AsText(f.geom) as geom_wkt
                    FROM t_fude_poly f
                    JOIN t_xml_meta m ON f.xml_meta_id = m.id
                    WHERE f.chiban LIKE ?
                """, (f'%{chiban}%',))

            return [dict(row) for row in cursor.fetchall()]

    def get_distinct_oaza_names(self) -> List[str]:
        """Get list of distinct oaza names in the database."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT oaza_name FROM t_fude_poly
                WHERE oaza_name IS NOT NULL AND oaza_name != ''
                ORDER BY oaza_name
            """)
            return [row[0] for row in cursor.fetchall()]

    def get_xml_meta_by_oaza(self, oaza_name: str) -> List[Dict[str, Any]]:
        """Get XML metadata for a specific oaza."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT m.id, m.file_name, m.map_name, m.crs_type,
                       m.fude_count, m.scale_denominator, m.status
                FROM t_xml_meta m
                JOIN t_fude_poly f ON m.id = f.xml_meta_id
                WHERE f.oaza_name = ?
                ORDER BY m.file_name
            """, (oaza_name,))
            return [dict(row) for row in cursor.fetchall()]

    def get_xml_meta_by_crs_type(self, crs_type: str) -> List[Dict[str, Any]]:
        """Get XML metadata by coordinate system type."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, file_name, map_name, municipality_code, municipality_name,
                       oaza_name, crs_type, fude_count, scale_denominator, status
                FROM t_xml_meta WHERE crs_type = ?
                ORDER BY file_name
            """, (crs_type,))
            return [dict(row) for row in cursor.fetchall()]

    def update_xml_meta_oaza(self, meta_id: int, oaza_name: str) -> None:
        """Update oaza_name for XML metadata."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE t_xml_meta SET oaza_name = ?, updated_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (oaza_name, meta_id))

    def update_xml_meta_crs(self, meta_id: int, crs_type: str, srid: int = 6676) -> None:
        """
        Update CRS type for XML metadata after transformation.

        Note: The SRID parameter is kept for API compatibility but not applied
        to geometry due to SpatiaLite schema constraints. The actual CRS is
        tracked in the crs_type text field.

        Args:
            meta_id: XML metadata ID
            crs_type: New CRS type (e.g., '変換済み')
            srid: SRID (not applied to geometry, kept for API compatibility)
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE t_xml_meta
                SET crs_type = ?, updated_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (crs_type, meta_id))
            # Note: SRID update removed due to SpatiaLite geometry column constraint
            # The geometry was created with SRID=0 and cannot be changed

    def update_fude_geometry(self, fude_id: int, geom_wkt: str, srid: int = 0) -> None:
        """
        Update parcel geometry after transformation.

        Note: SRID is kept at 0 to match the geometry column constraint.
        The actual coordinate system is tracked in xml_meta.crs_type.

        Args:
            fude_id: Parcel ID (t_fude_poly.id)
            geom_wkt: New geometry in WKT format
            srid: SRID for the geometry (default: 0 to match schema constraint)
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            # Use SRID=0 to match the geometry column constraint defined in schema
            # The actual CRS is tracked in t_xml_meta.crs_type
            cursor.execute("""
                UPDATE t_fude_poly
                SET geom = GeomFromText(?, 0),
                    coord_type = '変換済み',
                    updated_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (geom_wkt, fude_id))
            rows_affected = cursor.rowcount
            if rows_affected == 0:
                logger.warning(f"update_fude_geometry: No rows updated for fude_id={fude_id}")
            else:
                logger.debug(f"update_fude_geometry: Updated fude_id={fude_id}")

    def update_fude_reliability(self, fude_id: int, reliability: str,
                                transform_method: str = None,
                                needs_review: bool = False,
                                review_reason: str = None) -> None:
        """
        Update reliability tracking fields for a parcel.

        Args:
            fude_id: Parcel ID (t_fude_poly.id)
            reliability: Reliability level (HIGH, MEDIUM, LOW)
            transform_method: Method used for transformation
            needs_review: Whether manual review is needed
            review_reason: Reason for needing review
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE t_fude_poly
                SET reliability = ?,
                    transform_method = ?,
                    needs_review = ?,
                    review_reason = ?,
                    updated_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (reliability, transform_method, 1 if needs_review else 0,
                  review_reason, fude_id))

    def update_fude_batch_reliability(self, fude_ids: List[int], reliability: str,
                                      transform_method: str = None,
                                      needs_review: bool = False,
                                      review_reason: str = None) -> int:
        """
        Update reliability tracking fields for multiple parcels.

        Args:
            fude_ids: List of parcel IDs
            reliability: Reliability level (HIGH, MEDIUM, LOW)
            transform_method: Method used for transformation
            needs_review: Whether manual review is needed
            review_reason: Reason for needing review

        Returns:
            Number of rows updated
        """
        if not fude_ids:
            return 0

        with self.connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(fude_ids))
            cursor.execute(f"""
                UPDATE t_fude_poly
                SET reliability = ?,
                    transform_method = ?,
                    needs_review = ?,
                    review_reason = ?,
                    updated_date = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
            """, [reliability, transform_method, 1 if needs_review else 0,  # nosec B608
                  review_reason] + fude_ids)
            return cursor.rowcount

    def get_parcels_needing_review(self, oaza_name: str = None) -> List[Dict[str, Any]]:
        """
        Get parcels that need manual review.

        Args:
            oaza_name: Optional filter by oaza name

        Returns:
            List of parcel records needing review
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            if oaza_name:
                cursor.execute("""
                    SELECT id, fude_id, oaza_name, chiban, reliability,
                           review_reason, AsText(geom) as geom_wkt
                    FROM t_fude_poly
                    WHERE needs_review = 1 AND oaza_name = ?
                    ORDER BY review_reason, chiban
                """, (oaza_name,))
            else:
                cursor.execute("""
                    SELECT id, fude_id, oaza_name, chiban, reliability,
                           review_reason, AsText(geom) as geom_wkt
                    FROM t_fude_poly
                    WHERE needs_review = 1
                    ORDER BY oaza_name, review_reason, chiban
                """)
            return [dict(row) for row in cursor.fetchall()]

    def export_to_geopackage(self, gpkg_path: Path, oaza_name: Optional[str] = None,
                            xml_meta_ids: Optional[List[int]] = None) -> bool:
        """
        Export data to GeoPackage format.

        Args:
            gpkg_path: Output GeoPackage path
            oaza_name: Filter by oaza name (optional)
            xml_meta_ids: Filter by XML metadata IDs (optional)

        Returns:
            bool: True if successful
        """
        from qgis.core import QgsVectorFileWriter, QgsCoordinateTransformContext, QgsWkbTypes

        with self.connection() as conn:
            cursor = conn.cursor()

            # Build query based on filters
            where_clauses = []
            params = []

            if oaza_name:
                where_clauses.append("f.oaza_name = ?")
                params.append(oaza_name)

            if xml_meta_ids:
                placeholders = ','.join('?' * len(xml_meta_ids))
                where_clauses.append(f"f.xml_meta_id IN ({placeholders})")
                params.extend(xml_meta_ids)

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            cursor.execute(f"""
                SELECT f.id, f.fude_id, f.oaza_code, f.oaza_name, f.chiban,
                       f.coord_type, m.file_name, m.crs_type,
                       AsText(f.geom) as geom_wkt
                FROM t_fude_poly f
                JOIN t_xml_meta m ON f.xml_meta_id = m.id
                WHERE {where_sql} AND f.geom IS NOT NULL
            """, params)  # nosec B608

            rows = cursor.fetchall()

            # Debug: Log export statistics
            transformed_count = sum(1 for row in rows if dict(row).get('coord_type') == '変換済み')
            logger.info(f"export_to_geopackage: Found {len(rows)} rows, {transformed_count} transformed")

        if not rows:
            logger.warning("No data to export")
            return False

        # Create memory layer
        fields = QgsFields()
        fields.append(QgsField('id', QVariant.Int))
        fields.append(QgsField('fude_id', QVariant.String))
        fields.append(QgsField('oaza_code', QVariant.String))
        fields.append(QgsField('oaza_name', QVariant.String))
        fields.append(QgsField('chiban', QVariant.String))
        fields.append(QgsField('coord_type', QVariant.String))
        fields.append(QgsField('file_name', QVariant.String))
        fields.append(QgsField('crs_type', QVariant.String))

        # Create features
        features = []
        for row in rows:
            row_dict = dict(row)
            feat = QgsFeature(fields)
            feat.setAttributes([
                row_dict['id'], row_dict['fude_id'], row_dict['oaza_code'],
                row_dict['oaza_name'], row_dict['chiban'], row_dict['coord_type'],
                row_dict['file_name'], row_dict['crs_type']
            ])

            geom = QgsGeometry.fromWkt(row_dict['geom_wkt'])
            feat.setGeometry(geom)
            features.append(feat)

        # Write to GeoPackage
        crs = QgsCoordinateReferenceSystem('EPSG:6676')  # JGD2011 Zone 8
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'GPKG'
        options.fileEncoding = 'UTF-8'

        writer = QgsVectorFileWriter.create(
            str(gpkg_path), fields, QgsWkbTypes.Polygon,
            crs, QgsCoordinateTransformContext(), options
        )

        if writer.hasError() != QgsVectorFileWriter.NoError:
            logger.error(f"Error creating GeoPackage: {writer.errorMessage()}")
            return False

        for feat in features:
            writer.addFeature(feat)

        del writer
        logger.info(f"Exported {len(features)} features to {gpkg_path}")
        return True

    def get_statistics(self) -> Dict[str, Any]:
        """Get database statistics."""
        with self.connection() as conn:
            cursor = conn.cursor()

            stats = {}

            cursor.execute("SELECT COUNT(*) FROM t_xml_meta")
            stats['xml_file_count'] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM t_fude_poly")
            stats['fude_count'] = cursor.fetchone()[0]

            cursor.execute("""
                SELECT crs_type, COUNT(*) as cnt
                FROM t_xml_meta GROUP BY crs_type
            """)
            stats['crs_distribution'] = {row[0]: row[1] for row in cursor.fetchall()}

            cursor.execute("""
                SELECT oaza_name, COUNT(*) as cnt
                FROM t_fude_poly WHERE oaza_name IS NOT NULL
                GROUP BY oaza_name ORDER BY cnt DESC
            """)
            stats['oaza_distribution'] = {row[0]: row[1] for row in cursor.fetchall()}

            return stats

    def vacuum(self) -> None:
        """Vacuum the database to optimize space."""
        with self.connection() as conn:
            conn.execute("VACUUM")

    def file_exists(self, filename: str) -> bool:
        """Check if an XML file has already been imported."""
        return self.get_xml_meta_by_filename(filename) is not None

    def update_srid_for_public_crs(self, srid: int = 6676) -> Tuple[int, int]:
        """
        Update SRID for public coordinate (公共座標8系) data.

        Sets the proper SRID for geometries from files with crs_type='公共座標8系'.
        This enables correct georeferencing in QGIS.

        Args:
            srid: SRID to set (default: 6676 for JGD2011 / CS VIII)

        Returns:
            Tuple of (xml_meta_count, fude_poly_count) updated
        """
        with self.connection() as conn:
            cursor = conn.cursor()

            # Update geometry_columns table
            cursor.execute(
                "UPDATE geometry_columns SET srid = ? WHERE f_table_name = 't_xml_meta'",
                (srid,)
            )
            cursor.execute(
                "UPDATE geometry_columns SET srid = ? WHERE f_table_name = 't_fude_poly'",
                (srid,)
            )

            # Update actual geometry SRID for public coordinate data
            cursor.execute("""
                UPDATE t_xml_meta
                SET geom = SetSRID(geom, ?)
                WHERE crs_type = '公共座標8系' AND geom IS NOT NULL
            """, (srid,))
            xml_count = cursor.rowcount

            cursor.execute("""
                UPDATE t_fude_poly
                SET geom = SetSRID(geom, ?)
                WHERE xml_meta_id IN (SELECT id FROM t_xml_meta WHERE crs_type = '公共座標8系')
                AND geom IS NOT NULL
            """, (srid,))
            fude_count = cursor.rowcount

            conn.commit()

            logger.info(f"Updated SRID to {srid}: {xml_count} xml_meta, {fude_count} fude_poly")

        return xml_count, fude_count

    def get_layer_uri(self, table_name: str) -> str:
        """
        Get QGIS data source URI for a table.

        Args:
            table_name: Name of the table

        Returns:
            str: URI string for QgsVectorLayer
        """
        uri = QgsDataSourceUri()
        uri.setDatabase(str(self.db_path))
        uri.setDataSource('', table_name, 'geom')
        return uri.uri()

    def get_as_qgs_layer(self, table_name: str, layer_name: Optional[str] = None) -> QgsVectorLayer:
        """
        Get table as QgsVectorLayer.

        Args:
            table_name: Name of the table
            layer_name: Display name for the layer (default: table_name)

        Returns:
            QgsVectorLayer: Vector layer
        """
        uri = self.get_layer_uri(table_name)
        name = layer_name or table_name
        return QgsVectorLayer(uri, name, 'spatialite')
