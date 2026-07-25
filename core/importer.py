# -*- coding: utf-8 -*-
"""
XML Importer Module for KozuXmlIntegrator

This module orchestrates the import process:
1. Parse XML files
2. Build geometries
3. Store in SpatiaLite database
4. Perform spatial join with administrative boundaries
5. Build search index

Supports batch processing with progress reporting.
"""

from pathlib import Path
from typing import List, Dict, Optional, Callable, Tuple
from dataclasses import dataclass
import logging
import tempfile
import zipfile
import time

from qgis.core import QgsVectorLayer

from .xml_parser import KozuXmlParser
from .geometry_builder import GeometryBuilder
from .database_manager import DatabaseManager, XmlMetaRecord, FudePolyRecord
from .spatial_join import SpatialJoiner, load_admin_layer, normalize_municipality_name
from .search_index import SearchIndex

logger = logging.getLogger(__name__)


@dataclass
class ImportProgress:
    """Progress information for import process"""
    total_files: int = 0
    completed_files: int = 0
    current_file: str = ''
    current_phase: str = ''  # 'parsing', 'building', 'storing', 'joining'
    parcels_processed: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    @property
    def progress_percent(self) -> float:
        if self.total_files == 0:
            return 0.0
        return (self.completed_files / self.total_files) * 100


@dataclass
class ImportResult:
    """Result of import operation"""
    success: bool
    files_processed: int
    files_failed: int
    total_parcels: int
    elapsed_seconds: float
    errors: List[str]
    oaza_assignments: Dict[str, int]


class XmlImporter:
    """
    Orchestrates the XML import process.

    Handles batch import of multiple XML files with progress tracking
    and error handling.
    """

    def __init__(self, db: DatabaseManager,
                 admin_layer: Optional[QgsVectorLayer] = None,
                 admin_name_field: str = 'S_NAME',
                 municipality_layer: Optional[QgsVectorLayer] = None,
                 municipality_name_field: str = 'N03_004'):
        """
        Initialize importer.

        Args:
            db: Database manager
            admin_layer: Optional Oaza boundary layer for spatial join (assigns Oaza names)
            admin_name_field: Field name containing Oaza names
            municipality_layer: Optional municipality layer for validation
            municipality_name_field: Field name containing municipality names
        """
        self.db = db
        self.admin_layer = admin_layer
        self.admin_name_field = admin_name_field
        self.municipality_layer = municipality_layer
        self.municipality_name_field = municipality_name_field
        self._spatial_joiner: Optional[SpatialJoiner] = None
        self._municipality_joiner: Optional[SpatialJoiner] = None

        if admin_layer:
            self._spatial_joiner = SpatialJoiner(admin_layer, admin_name_field)

        if municipality_layer:
            self._municipality_joiner = SpatialJoiner(municipality_layer, municipality_name_field)

    def import_single_file(self, xml_path: Path,
                           progress_callback: Optional[Callable[[str], None]] = None
                           ) -> Tuple[bool, int, Optional[str]]:
        """
        Import a single XML file.

        Args:
            xml_path: Path to XML file
            progress_callback: Optional callback for progress updates

        Returns:
            Tuple of (success, parcel_count, error_message)
        """
        file_name = xml_path.name

        # Check if already imported
        if self.db.file_exists(file_name):
            logger.info(f"File already imported, skipping: {file_name}")
            return True, 0, None

        try:
            # Phase 1: Parse XML
            if progress_callback:
                progress_callback(f"Parsing {file_name}...")

            parser = KozuXmlParser(xml_path)
            xml_data = parser.parse()

            # Phase 2: Build geometries
            if progress_callback:
                progress_callback(f"Building geometries for {file_name}...")

            builder = GeometryBuilder(xml_data, swap_xy=True)

            # Build envelope geometry
            envelope = builder.build_convex_hull()
            envelope_wkt = envelope.asWkt() if not envelope.isEmpty() else ''

            # Determine Oaza name via spatial join
            oaza_name = ''
            if self._spatial_joiner and not envelope.isEmpty():
                oaza_name = self._spatial_joiner.find_oaza_for_geometry(envelope) or ''

            # Validate/determine municipality via spatial join (if municipality layer provided)
            spatial_municipality = ''
            if self._municipality_joiner and not envelope.isEmpty():
                spatial_municipality = self._municipality_joiner.find_oaza_for_geometry(envelope) or ''
                # Log if XML municipality differs from spatial join result
                xml_municipality = normalize_municipality_name(xml_data.header.municipality_name)
                if spatial_municipality and xml_municipality:
                    if xml_municipality != spatial_municipality:
                        logger.debug(
                            f"{file_name}: XML municipality '{xml_data.header.municipality_name}' "
                            f"-> spatial join: '{spatial_municipality}'"
                        )

            # Phase 3: Store metadata
            if progress_callback:
                progress_callback(f"Storing metadata for {file_name}...")

            meta_record = XmlMetaRecord(
                file_name=file_name,
                map_name=xml_data.header.map_name,
                municipality_code=xml_data.header.municipality_code,
                municipality_name=xml_data.header.municipality_name,
                oaza_name=oaza_name,
                crs_type=xml_data.header.crs_type,
                geodetic_type=xml_data.header.geodetic_type,
                transform_program=xml_data.header.transform_program,
                point_count=len(xml_data.points),
                curve_count=len(xml_data.curves),
                fude_count=len(xml_data.fude_list),
                scale_denominator=xml_data.header.scale_denominator,
                geom_wkt=envelope_wkt
            )

            meta_id = self.db.insert_xml_meta(meta_record)

            # Phase 4: Store parcel data
            if progress_callback:
                progress_callback(f"Storing {len(xml_data.fude_list)} parcels for {file_name}...")

            fude_records = []
            for fude in xml_data.fude_list:
                # QgsGeometry/GEOSオブジェクトをワーカースレッドで生成しないよう
                # WKT文字列と面積は純Python（shoelace公式）で取得する
                geom_wkt = builder.to_wkt(fude)
                area_sqm = builder.compute_fude_area(fude) if geom_wkt else 0.0

                fude_records.append(FudePolyRecord(
                    xml_meta_id=meta_id,
                    fude_id=fude.id,
                    oaza_code=fude.oaza_code,
                    oaza_name=fude.oaza_name,
                    chome_code=fude.chome_code,
                    koaza_code=fude.koaza_code,
                    yobi_code=fude.yobi_code,
                    chiban=fude.chiban,
                    coord_type=fude.coord_type,
                    area_sqm=area_sqm,
                    geom_wkt=geom_wkt
                ))

            parcel_count = self.db.insert_fude_batch(fude_records)

            logger.info(f"Imported {file_name}: {parcel_count} parcels, CRS: {xml_data.header.crs_type}")
            return True, parcel_count, None

        except Exception as e:
            error_msg = f"Error importing {file_name}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return False, 0, error_msg

    def import_directory(self, xml_dir: Path,
                         include_subdirs: bool = True,
                         progress_callback: Optional[Callable[[ImportProgress], None]] = None,
                         max_workers: int = 1) -> ImportResult:
        """
        Import all XML files from a directory.

        Args:
            xml_dir: Directory containing XML files
            include_subdirs: Whether to include subdirectories
            progress_callback: Optional callback for progress updates
            max_workers: Number of parallel workers (1 = sequential)

        Returns:
            ImportResult: Summary of import operation
        """
        start_time = time.time()

        # Find all XML files
        if include_subdirs:
            xml_files = list(xml_dir.rglob('*.xml'))
        else:
            xml_files = list(xml_dir.glob('*.xml'))

        if not xml_files:
            return ImportResult(
                success=False,
                files_processed=0,
                files_failed=0,
                total_parcels=0,
                elapsed_seconds=0,
                errors=["No XML files found"],
                oaza_assignments={}
            )

        progress = ImportProgress(total_files=len(xml_files))

        logger.info(f"Found {len(xml_files)} XML files to import")

        files_processed = 0
        files_failed = 0
        total_parcels = 0
        errors = []

        # Sequential processing (safer for SQLite)
        for xml_path in xml_files:
            progress.current_file = xml_path.name
            progress.current_phase = 'processing'

            if progress_callback:
                progress_callback(progress)

            success, parcel_count, error = self.import_single_file(
                xml_path,
                lambda msg: None  # Suppress per-file progress
            )

            if success:
                files_processed += 1
                total_parcels += parcel_count
            else:
                files_failed += 1
                if error:
                    errors.append(error)

            progress.completed_files += 1
            progress.parcels_processed = total_parcels

            if progress_callback:
                progress_callback(progress)

        elapsed = time.time() - start_time

        # Get Oaza assignment statistics
        oaza_stats = {}
        if self._spatial_joiner:
            all_meta = self.db.get_all_xml_meta()
            for meta in all_meta:
                oaza = meta.get('oaza_name', '')
                if oaza:
                    oaza_stats[oaza] = oaza_stats.get(oaza, 0) + 1

        return ImportResult(
            success=files_failed == 0,
            files_processed=files_processed,
            files_failed=files_failed,
            total_parcels=total_parcels,
            elapsed_seconds=elapsed,
            errors=errors,
            oaza_assignments=oaza_stats
        )

    def import_zip_file(self, zip_path: Path,
                        progress_callback: Optional[Callable[[ImportProgress], None]] = None
                        ) -> ImportResult:
        """
        Import all XML files contained in a ZIP archive.

        Supports nested ZIPs (outer ZIP containing inner ZIPs containing XMLs).
        All ZIPs are extracted to a temporary directory, then processed.

        Args:
            zip_path: Path to the ZIP file
            progress_callback: Optional callback for progress updates

        Returns:
            ImportResult: Summary of import operation
        """
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.namelist()
        except zipfile.BadZipFile:
            return ImportResult(
                success=False,
                files_processed=0,
                files_failed=0,
                total_parcels=0,
                elapsed_seconds=0,
                errors=[f"不正なZIPファイルです: {zip_path.name}"],
                oaza_assignments={}
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_path)

            # 内側のZIPも展開する（2重構造対応）
            for inner_zip in tmp_path.rglob('*.zip'):
                try:
                    with zipfile.ZipFile(inner_zip, 'r') as izf:
                        izf.extractall(inner_zip.parent)
                    inner_zip.unlink()
                except zipfile.BadZipFile:
                    logger.warning(f"Skipping bad inner ZIP: {inner_zip.name}")

            xml_files = list(tmp_path.rglob('*.xml'))
            if not xml_files:
                return ImportResult(
                    success=False,
                    files_processed=0,
                    files_failed=0,
                    total_parcels=0,
                    elapsed_seconds=0,
                    errors=[f"ZIPファイル内にXMLが見つかりません: {zip_path.name}"],
                    oaza_assignments={}
                )

            logger.info(f"Extracted {len(xml_files)} XML(s) from {zip_path.name}")
            return self.import_directory(
                tmp_path,
                include_subdirs=True,
                progress_callback=progress_callback
            )

    def import_files(self, xml_files: List[Path],
                     progress_callback: Optional[Callable[[ImportProgress], None]] = None
                     ) -> ImportResult:
        """
        Import a list of XML files.

        Args:
            xml_files: List of XML file paths
            progress_callback: Optional callback for progress updates

        Returns:
            ImportResult: Summary of import operation
        """
        start_time = time.time()

        progress = ImportProgress(total_files=len(xml_files))

        files_processed = 0
        files_failed = 0
        total_parcels = 0
        errors = []

        for xml_path in xml_files:
            progress.current_file = xml_path.name
            progress.current_phase = 'processing'

            if progress_callback:
                progress_callback(progress)

            success, parcel_count, error = self.import_single_file(xml_path)

            if success:
                files_processed += 1
                total_parcels += parcel_count
            else:
                files_failed += 1
                if error:
                    errors.append(error)

            progress.completed_files += 1
            progress.parcels_processed = total_parcels

            if progress_callback:
                progress_callback(progress)

        elapsed = time.time() - start_time

        # Get Oaza statistics
        oaza_stats = {}
        all_meta = self.db.get_all_xml_meta()
        for meta in all_meta:
            oaza = meta.get('oaza_name', '')
            if oaza:
                oaza_stats[oaza] = oaza_stats.get(oaza, 0) + 1

        return ImportResult(
            success=files_failed == 0,
            files_processed=files_processed,
            files_failed=files_failed,
            total_parcels=total_parcels,
            elapsed_seconds=elapsed,
            errors=errors,
            oaza_assignments=oaza_stats
        )


def create_database_and_import(db_path: Path,
                               xml_dir: Path,
                               admin_layer_path: Optional[Path] = None,
                               admin_name_field: str = 'S_NAME',
                               progress_callback: Optional[Callable[[ImportProgress], None]] = None
                               ) -> ImportResult:
    """
    Convenience function to create database and import XML files.

    Args:
        db_path: Path for new SpatiaLite database
        xml_dir: Directory containing XML files
        admin_layer_path: Optional path to administrative boundary layer
        admin_name_field: Field name for Oaza names
        progress_callback: Optional progress callback

    Returns:
        ImportResult: Summary of import operation
    """
    # Create database
    db = DatabaseManager(db_path)
    db.create_database()

    # Load admin layer if provided
    admin_layer = None
    if admin_layer_path and admin_layer_path.exists():
        try:
            admin_layer = load_admin_layer(admin_layer_path)
        except Exception as e:
            logger.warning(f"Could not load admin layer: {e}")

    # Run import
    importer = XmlImporter(db, admin_layer, admin_name_field)
    result = importer.import_directory(xml_dir, progress_callback=progress_callback)

    # Post-import processing
    if result.files_processed > 0:
        # Update SRID for public coordinate data (EPSG:6676)
        try:
            xml_count, fude_count = db.update_srid_for_public_crs(6676)
            logger.info(f"Updated SRID: {xml_count} xml_meta, {fude_count} fude_poly to EPSG:6676")
        except Exception as e:
            logger.warning(f"Could not update SRID: {e}")

        # Build search index
        try:
            index = SearchIndex(db)
            index.build()
        except Exception as e:
            logger.warning(f"Could not build search index: {e}")

    return result
