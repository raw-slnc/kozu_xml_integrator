# -*- coding: utf-8 -*-
"""
XML Parser for Legal Cadastral Map (法務局地図XML)

This module provides streaming XML parsing for large cadastral map files
using lxml.iterparse for memory efficiency.

XML Structure:
- 地図 (root)
  - 空間属性: GM_Point, GM_Curve, GM_Surface (geometry data)
  - 主題属性: 筆界点, 筆界線, 筆 (thematic attributes)
"""

from typing import Dict, List, Tuple, Optional, Generator, Any
from dataclasses import dataclass, field
from pathlib import Path
from lxml import etree
import logging

logger = logging.getLogger(__name__)


# Namespace definitions
NS_DEFAULT = 'http://www.moj.go.jp/MINJI/tizuxml'
NS_ZMN = 'http://www.moj.go.jp/MINJI/tizuzumen'

NAMESPACES = {
    'default': NS_DEFAULT,
    'zmn': NS_ZMN
}


@dataclass
class GmPoint:
    """GM_Point coordinate data"""
    id: str
    x: float
    y: float


@dataclass
class GmCurve:
    """GM_Curve line data"""
    id: str
    points: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class GmSurface:
    """GM_Surface polygon data"""
    id: str
    exterior_curve_refs: List[str] = field(default_factory=list)
    interior_curve_refs: List[List[str]] = field(default_factory=list)


@dataclass
class Hikkaiten:
    """筆界点 (boundary point) data"""
    point_name: str
    shape_ref: str


@dataclass
class Hikkaisen:
    """筆界線 (boundary line) data"""
    shape_ref: str
    line_type: str  # 筆界線, 大字界線, 仮大字界線


@dataclass
class Fude:
    """筆 (parcel) data"""
    id: str
    oaza_code: str
    oaza_name: str
    chome_code: str
    koaza_code: str
    yobi_code: str
    chiban: str
    shape_ref: str
    coord_type: str  # 座標値種別 (e.g., "図上測量")


@dataclass
class XmlMapHeader:
    """XML map header information"""
    file_name: str
    map_name: str
    municipality_code: str
    municipality_name: str
    crs_type: str  # "任意座標系" or "公共座標8系"
    geodetic_type: Optional[str] = None  # 測地系判別
    transform_program: Optional[str] = None  # 変換プログラム
    transform_version: Optional[str] = None
    param_version: Optional[str] = None


@dataclass
class XmlMapData:
    """Complete parsed XML map data"""
    header: XmlMapHeader
    points: Dict[str, GmPoint] = field(default_factory=dict)
    curves: Dict[str, GmCurve] = field(default_factory=dict)
    surfaces: Dict[str, GmSurface] = field(default_factory=dict)
    hikkaiten: List[Hikkaiten] = field(default_factory=list)
    hikkaisen: List[Hikkaisen] = field(default_factory=list)
    fude_list: List[Fude] = field(default_factory=list)


class KozuXmlParser:
    """
    Streaming XML parser for Legal Cadastral Map (法務局地図XML)

    Uses lxml.iterparse for memory-efficient parsing of large files.
    Processes elements incrementally and releases memory after processing.
    """

    def __init__(self, xml_path: Path):
        """
        Initialize parser with XML file path.

        Args:
            xml_path: Path to the XML file
        """
        self.xml_path = Path(xml_path)
        if not self.xml_path.exists():
            raise FileNotFoundError(f"XML file not found: {xml_path}")

    def parse(self) -> XmlMapData:
        """
        Parse the entire XML file.

        Returns:
            XmlMapData: Complete parsed data
        """
        logger.info(f"Parsing XML file: {self.xml_path}")

        # First, parse the header (small, so regular parsing is fine)
        header = self._parse_header()

        # Initialize data containers
        data = XmlMapData(header=header)

        # Stream parse the spatial and thematic attributes
        self._parse_spatial_attributes(data)
        self._parse_thematic_attributes(data)

        logger.info(f"Parsed: {len(data.points)} points, {len(data.curves)} curves, "
                   f"{len(data.surfaces)} surfaces, {len(data.fude_list)} parcels")

        return data

    def _parse_header(self) -> XmlMapHeader:
        """
        Parse XML header information.

        Returns:
            XmlMapHeader: Header data
        """
        # Use iterparse but only for header elements (fast, early exit)
        header_data = {
            'file_name': self.xml_path.name,
            'map_name': '',
            'municipality_code': '',
            'municipality_name': '',
            'crs_type': '',
            'geodetic_type': None,
            'transform_program': None,
            'transform_version': None,
            'param_version': None,
        }

        # Parse only header elements
        # Open file explicitly to ensure proper cleanup
        with open(self.xml_path, 'rb') as f:
            context = etree.iterparse(
                f,
                events=('end',),
                tag=[
                    f'{{{NS_DEFAULT}}}地図名',
                    f'{{{NS_DEFAULT}}}市区町村コード',
                    f'{{{NS_DEFAULT}}}市区町村名',
                    f'{{{NS_DEFAULT}}}座標系',
                    f'{{{NS_DEFAULT}}}測地系判別',
                    f'{{{NS_DEFAULT}}}変換プログラム',
                    f'{{{NS_DEFAULT}}}変換プログラムバージョン',
                    f'{{{NS_DEFAULT}}}変換パラメータバージョン',
                    f'{{{NS_DEFAULT}}}空間属性',  # Stop at this element
                ]
            )

            for event, elem in context:
                local_name = elem.tag.split('}')[-1]

                if local_name == '地図名':
                    header_data['map_name'] = elem.text or ''
                elif local_name == '市区町村コード':
                    header_data['municipality_code'] = elem.text or ''
                elif local_name == '市区町村名':
                    header_data['municipality_name'] = elem.text or ''
                elif local_name == '座標系':
                    header_data['crs_type'] = elem.text or ''
                elif local_name == '測地系判別':
                    header_data['geodetic_type'] = elem.text
                elif local_name == '変換プログラム':
                    header_data['transform_program'] = elem.text
                elif local_name == '変換プログラムバージョン':
                    header_data['transform_version'] = elem.text
                elif local_name == '変換パラメータバージョン':
                    header_data['param_version'] = elem.text
                elif local_name == '空間属性':
                    # Stop parsing header, spatial data starts here
                    break

                # Clear element to free memory
                elem.clear()

        return XmlMapHeader(**header_data)

    def _parse_spatial_attributes(self, data: XmlMapData) -> None:
        """
        Parse spatial attributes (GM_Point, GM_Curve, GM_Surface).

        Args:
            data: XmlMapData to populate
        """
        with open(self.xml_path, 'rb') as f:
            context = etree.iterparse(
                f,
                events=('end',),
                tag=[
                    f'{{{NS_ZMN}}}GM_Point',
                    f'{{{NS_ZMN}}}GM_Curve',
                    f'{{{NS_ZMN}}}GM_Surface',
                ]
            )

            for event, elem in context:
                local_name = elem.tag.split('}')[-1]

                try:
                    if local_name == 'GM_Point':
                        point = self._parse_gm_point(elem)
                        if point:
                            data.points[point.id] = point
                    elif local_name == 'GM_Curve':
                        curve = self._parse_gm_curve(elem, data.points)
                        if curve:
                            data.curves[curve.id] = curve
                    elif local_name == 'GM_Surface':
                        surface = self._parse_gm_surface(elem)
                        if surface:
                            data.surfaces[surface.id] = surface
                except Exception as e:
                    logger.warning(f"Error parsing {local_name}: {e}")

                # Clear element and ancestors to free memory
                elem.clear()
                while elem.getprevious() is not None:
                    parent = elem.getparent()
                    if parent is not None:
                        del parent[0]
                    else:
                        break

    def _parse_thematic_attributes(self, data: XmlMapData) -> None:
        """
        Parse thematic attributes (筆界点, 筆界線, 筆).

        Args:
            data: XmlMapData to populate
        """
        with open(self.xml_path, 'rb') as f:
            context = etree.iterparse(
                f,
                events=('end',),
                tag=[
                    f'{{{NS_DEFAULT}}}筆界点',
                    f'{{{NS_DEFAULT}}}筆界線',
                    f'{{{NS_DEFAULT}}}筆',
                ]
            )

            for event, elem in context:
                local_name = elem.tag.split('}')[-1]

                try:
                    if local_name == '筆界点':
                        hikkaiten = self._parse_hikkaiten(elem)
                        if hikkaiten:
                            data.hikkaiten.append(hikkaiten)
                    elif local_name == '筆界線':
                        hikkaisen = self._parse_hikkaisen(elem)
                        if hikkaisen:
                            data.hikkaisen.append(hikkaisen)
                    elif local_name == '筆':
                        fude = self._parse_fude(elem)
                        if fude:
                            data.fude_list.append(fude)
                except Exception as e:
                    logger.warning(f"Error parsing {local_name}: {e}")

                # Clear element to free memory
                elem.clear()
                while elem.getprevious() is not None:
                    parent = elem.getparent()
                    if parent is not None:
                        del parent[0]
                    else:
                        break

    def _parse_gm_point(self, elem: etree._Element) -> Optional[GmPoint]:
        """Parse GM_Point element."""
        point_id = elem.get('id')
        if not point_id:
            return None

        # Find X and Y coordinates
        x_elem = elem.find(f'.//{{{NS_ZMN}}}X')
        y_elem = elem.find(f'.//{{{NS_ZMN}}}Y')

        if x_elem is None or y_elem is None:
            return None

        try:
            x = float(x_elem.text)
            y = float(y_elem.text)
            return GmPoint(id=point_id, x=x, y=y)
        except (ValueError, TypeError):
            return None

    def _parse_gm_curve(self, elem: etree._Element,
                        points_dict: Optional[Dict[str, 'GmPoint']] = None) -> Optional[GmCurve]:
        """Parse GM_Curve element.

        Handles both direct coordinates and indirect point references.

        Args:
            elem: GM_Curve XML element
            points_dict: Dictionary of already-parsed GM_Point objects for
                        resolving indirect references
        """
        curve_id = elem.get('id')
        if not curve_id:
            return None

        points = []

        # Find all coordinate pairs in GM_PointArray.column
        for column in elem.findall(f'.//{{{NS_ZMN}}}GM_PointArray.column'):
            # Try direct position first
            pos_direct = column.find(f'.//{{{NS_ZMN}}}GM_Position.direct')
            if pos_direct is not None:
                x_elem = pos_direct.find(f'{{{NS_ZMN}}}X')
                y_elem = pos_direct.find(f'{{{NS_ZMN}}}Y')

                if x_elem is not None and y_elem is not None:
                    try:
                        x = float(x_elem.text)
                        y = float(y_elem.text)
                        points.append((x, y))
                    except (ValueError, TypeError):
                        continue
            else:
                # Try indirect position (reference to GM_Point)
                pos_indirect = column.find(f'.//{{{NS_ZMN}}}GM_Position.indirect')
                if pos_indirect is not None and points_dict is not None:
                    # Look for point reference
                    point_ref = pos_indirect.find(f'.//{{{NS_ZMN}}}GM_PointRef.point')
                    if point_ref is not None:
                        point_id = point_ref.get('idref')
                        if point_id and point_id in points_dict:
                            pt = points_dict[point_id]
                            points.append((pt.x, pt.y))

        return GmCurve(id=curve_id, points=points)

    def _parse_gm_surface(self, elem: etree._Element) -> Optional[GmSurface]:
        """Parse GM_Surface element."""
        surface_id = elem.get('id')
        if not surface_id:
            return None

        exterior_refs = []
        interior_refs = []

        # Parse exterior ring
        exterior = elem.find(f'.//{{{NS_ZMN}}}GM_SurfaceBoundary.exterior')
        if exterior is not None:
            for gen in exterior.findall(f'.//{{{NS_ZMN}}}GM_CompositeCurve.generator'):
                idref = gen.get('idref')
                if idref:
                    exterior_refs.append(idref)

        # Parse interior rings (holes)
        for interior in elem.findall(f'.//{{{NS_ZMN}}}GM_SurfaceBoundary.interior'):
            int_refs = []
            for gen in interior.findall(f'.//{{{NS_ZMN}}}GM_CompositeCurve.generator'):
                idref = gen.get('idref')
                if idref:
                    int_refs.append(idref)
            if int_refs:
                interior_refs.append(int_refs)

        return GmSurface(
            id=surface_id,
            exterior_curve_refs=exterior_refs,
            interior_curve_refs=interior_refs
        )

    def _parse_hikkaiten(self, elem: etree._Element) -> Optional[Hikkaiten]:
        """Parse 筆界点 element."""
        point_name_elem = elem.find(f'{{{NS_DEFAULT}}}点番名')
        shape_elem = elem.find(f'{{{NS_DEFAULT}}}形状')

        if point_name_elem is None or shape_elem is None:
            return None

        return Hikkaiten(
            point_name=point_name_elem.text or '',
            shape_ref=shape_elem.get('idref', '')
        )

    def _parse_hikkaisen(self, elem: etree._Element) -> Optional[Hikkaisen]:
        """Parse 筆界線 element."""
        shape_elem = elem.find(f'{{{NS_DEFAULT}}}形状')
        line_type_elem = elem.find(f'{{{NS_DEFAULT}}}線種別')

        shape_ref = shape_elem.get('idref', '') if shape_elem is not None else ''
        line_type = line_type_elem.text if line_type_elem is not None else ''

        return Hikkaisen(shape_ref=shape_ref, line_type=line_type)

    def _parse_fude(self, elem: etree._Element) -> Optional[Fude]:
        """Parse 筆 element."""
        fude_id = elem.get('id')
        if not fude_id:
            return None

        def get_text(tag: str) -> str:
            el = elem.find(f'{{{NS_DEFAULT}}}{tag}')
            return el.text if el is not None and el.text else ''

        shape_elem = elem.find(f'{{{NS_DEFAULT}}}形状')
        shape_ref = shape_elem.get('idref', '') if shape_elem is not None else ''

        return Fude(
            id=fude_id,
            oaza_code=get_text('大字コード'),
            oaza_name=get_text('大字名'),
            chome_code=get_text('丁目コード'),
            koaza_code=get_text('小字コード'),
            yobi_code=get_text('予備コード'),
            chiban=get_text('地番'),
            shape_ref=shape_ref,
            coord_type=get_text('座標値種別')
        )

    def iterate_fude(self) -> Generator[Tuple[Fude, Dict, Dict], None, None]:
        """
        Iterate over parcels with their geometry data.

        Memory-efficient generator that yields one parcel at a time
        with the necessary geometry data to build its polygon.

        Yields:
            Tuple of (Fude, points_dict, curves_dict) for each parcel
        """
        # This would require a more complex two-pass approach
        # For now, use the full parse method
        data = self.parse()

        for fude in data.fude_list:
            yield fude, data.points, data.curves, data.surfaces


def parse_xml_file(xml_path: Path) -> XmlMapData:
    """
    Convenience function to parse an XML file.

    Args:
        xml_path: Path to the XML file

    Returns:
        XmlMapData: Parsed data
    """
    parser = KozuXmlParser(xml_path)
    return parser.parse()
