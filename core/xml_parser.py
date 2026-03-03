# -*- coding: utf-8 -*-
"""
XML Parser for Legal Cadastral Map (法務局地図XML)

This module parses cadastral map files using defusedxml.ElementTree
（stdlib xml.etree.ElementTree の安全なラッパー）。
lxml は Windows の QThread 起動時に xmlDictFree クラッシュを引き起こすため
使用しない（lxml の Cython ジェネレータが PyGen_Finalize 経由で GC される問題）。

XML Structure:
- 地図 (root)
  - 空間属性: GM_Point, GM_Curve, GM_Surface (geometry data)
  - 主題属性: 筆界点, 筆界線, 筆 (thematic attributes)
"""

from typing import Dict, List, Tuple, Optional, Generator, Any
from dataclasses import dataclass, field
from pathlib import Path
import defusedxml.ElementTree as ET
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
    scale_denominator: Optional[int] = None  # 縮尺分母 (e.g. 600 for 1:600)
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
    XML parser for Legal Cadastral Map (法務局地図XML)

    Parses the full XML tree with stdlib xml.etree.ElementTree.parse(),
    then navigates with find()/iter(). lxml is intentionally avoided
    to prevent xmlDictFree crashes on Windows when QThread starts.
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

        # lxml は Cython ジェネレータ（_MultiTagMatcher.iter_elements 等）を生成する。
        # これらが GC される際に PyGen_Finalize → xmlDictFree が呼ばれ、
        # Windows の QThread 起動時（PyGILState_Release）にアクセス違反になる。
        # → stdlib xml.etree.ElementTree を使用し lxml を完全に排除する。
        with open(self.xml_path, 'rb') as f:
            tree = ET.parse(f)
        root = tree.getroot()

        header = self._parse_header(root)
        header.scale_denominator = self._parse_scale(root)

        data = XmlMapData(header=header)

        self._parse_spatial_attributes(root, data)
        self._parse_thematic_attributes(root, data)

        logger.info(f"Parsed: {len(data.points)} points, {len(data.curves)} curves, "
                   f"{len(data.surfaces)} surfaces, {len(data.fude_list)} parcels")

        return data

    def _parse_header(self, root: ET.Element) -> XmlMapHeader:
        """
        Parse XML header information from pre-parsed root element.

        Args:
            root: Root element of the parsed XML tree

        Returns:
            XmlMapHeader: Header data
        """
        def find_text(tag: str) -> str:
            elem = root.find(f'.//{{{NS_DEFAULT}}}{tag}')
            return elem.text if elem is not None and elem.text else ''

        return XmlMapHeader(
            file_name=self.xml_path.name,
            map_name=find_text('地図名'),
            municipality_code=find_text('市区町村コード'),
            municipality_name=find_text('市区町村名'),
            crs_type=find_text('座標系'),
            geodetic_type=find_text('測地系判別') or None,
            transform_program=find_text('変換プログラム') or None,
            transform_version=find_text('変換プログラムバージョン') or None,
            param_version=find_text('変換パラメータバージョン') or None,
        )

    def _parse_scale(self, root: ET.Element) -> Optional[int]:
        """
        Parse scale denominator from 図郭 elements.

        1XML = 1縮尺 が保証されているため、最初の <縮尺分母> を返す。

        Args:
            root: Root element of the parsed XML tree

        Returns:
            Scale denominator (e.g. 600 for 1:600), or None if not found
        """
        try:
            elem = root.find(f'.//{{{NS_DEFAULT}}}縮尺分母')
            if elem is not None and elem.text:
                val = int(elem.text)
                if val > 0:
                    return val
        except Exception as e:
            logger.warning(f"Error parsing scale from {self.xml_path}: {e}")

        return None

    def _parse_spatial_attributes(self, root: ET.Element, data: XmlMapData) -> None:
        """
        Parse spatial attributes (GM_Point, GM_Curve, GM_Surface).

        Args:
            root: Root element of the parsed XML tree
            data: XmlMapData to populate
        """
        for elem in root.iter(f'{{{NS_ZMN}}}GM_Point'):
            try:
                point = self._parse_gm_point(elem)
                if point:
                    data.points[point.id] = point
            except Exception as e:
                logger.warning(f"Error parsing GM_Point: {e}")

        for elem in root.iter(f'{{{NS_ZMN}}}GM_Curve'):
            try:
                curve = self._parse_gm_curve(elem, data.points)
                if curve:
                    data.curves[curve.id] = curve
            except Exception as e:
                logger.warning(f"Error parsing GM_Curve: {e}")

        for elem in root.iter(f'{{{NS_ZMN}}}GM_Surface'):
            try:
                surface = self._parse_gm_surface(elem)
                if surface:
                    data.surfaces[surface.id] = surface
            except Exception as e:
                logger.warning(f"Error parsing GM_Surface: {e}")

    def _parse_thematic_attributes(self, root: ET.Element, data: XmlMapData) -> None:
        """
        Parse thematic attributes (筆界点, 筆界線, 筆).

        Args:
            root: Root element of the parsed XML tree
            data: XmlMapData to populate
        """
        # stdlib ET.Element.iter() は単一タグのみ対応のため、タグごとにループを分ける
        for elem in root.iter(f'{{{NS_DEFAULT}}}筆界点'):
            try:
                hikkaiten = self._parse_hikkaiten(elem)
                if hikkaiten:
                    data.hikkaiten.append(hikkaiten)
            except Exception as e:
                logger.warning(f"Error parsing 筆界点: {e}")

        for elem in root.iter(f'{{{NS_DEFAULT}}}筆界線'):
            try:
                hikkaisen = self._parse_hikkaisen(elem)
                if hikkaisen:
                    data.hikkaisen.append(hikkaisen)
            except Exception as e:
                logger.warning(f"Error parsing 筆界線: {e}")

        for elem in root.iter(f'{{{NS_DEFAULT}}}筆'):
            try:
                fude = self._parse_fude(elem)
                if fude:
                    data.fude_list.append(fude)
            except Exception as e:
                logger.warning(f"Error parsing 筆: {e}")

    def _parse_gm_point(self, elem: ET.Element) -> Optional[GmPoint]:
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

    def _parse_gm_curve(self, elem: ET.Element,
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

    def _parse_gm_surface(self, elem: ET.Element) -> Optional[GmSurface]:
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

    def _parse_hikkaiten(self, elem: ET.Element) -> Optional[Hikkaiten]:
        """Parse 筆界点 element."""
        point_name_elem = elem.find(f'{{{NS_DEFAULT}}}点番名')
        shape_elem = elem.find(f'{{{NS_DEFAULT}}}形状')

        if point_name_elem is None or shape_elem is None:
            return None

        return Hikkaiten(
            point_name=point_name_elem.text or '',
            shape_ref=shape_elem.get('idref', '')
        )

    def _parse_hikkaisen(self, elem: ET.Element) -> Optional[Hikkaisen]:
        """Parse 筆界線 element."""
        shape_elem = elem.find(f'{{{NS_DEFAULT}}}形状')
        line_type_elem = elem.find(f'{{{NS_DEFAULT}}}線種別')

        shape_ref = shape_elem.get('idref', '') if shape_elem is not None else ''
        line_type = line_type_elem.text if line_type_elem is not None else ''

        return Hikkaisen(shape_ref=shape_ref, line_type=line_type)

    def _parse_fude(self, elem: ET.Element) -> Optional[Fude]:
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
