# -*- coding: utf-8 -*-
"""
Search Index Module for KozuXmlIntegrator

This module provides dynamic search index generation from XML data.
Creates hierarchical index (Oaza -> Chiban) for efficient lookups
and prevents "empty" searches by only exposing existing data.
"""

from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
import re
import logging

from .database_manager import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Search result for a parcel"""
    fude_id: int
    oaza_name: str
    chiban: str
    area_sqm: float = 0.0


@dataclass
class SearchIndexEntry:
    """Entry in the search index"""
    oaza_name: str
    koaza_code: str
    chiban: str
    fude_count: int = 1
    xml_files: Set[str] = field(default_factory=set)


@dataclass
class OazaIndexNode:
    """Hierarchical index node for Oaza"""
    name: str
    fude_count: int = 0
    chiban_list: List[str] = field(default_factory=list)
    koaza_map: Dict[str, List[str]] = field(default_factory=dict)  # koaza -> chibans
    xml_files: Set[str] = field(default_factory=set)


class SearchIndex:
    """
    Dynamic search index for cadastral data.

    Builds hierarchical index from database data, providing:
    - Oaza-level navigation
    - Koaza-level filtering
    - Chiban search with autocomplete
    """

    def __init__(self, db: DatabaseManager):
        """
        Initialize search index.

        Args:
            db: Database manager instance
        """
        self.db = db
        self._oaza_index: Dict[str, OazaIndexNode] = {}
        self._chiban_to_oaza: Dict[str, Set[str]] = defaultdict(set)
        self._normalized_chibans: Dict[str, str] = {}  # normalized -> original

    def build(self) -> None:
        """Build the search index from database data."""
        logger.info("Building search index...")

        self._oaza_index.clear()
        self._chiban_to_oaza.clear()
        self._normalized_chibans.clear()

        with self.db.connection() as conn:
            cursor = conn.cursor()

            # Get all unique combinations
            cursor.execute("""
                SELECT DISTINCT
                    f.oaza_name,
                    f.koaza_code,
                    f.chiban,
                    m.file_name
                FROM t_fude_poly f
                JOIN t_xml_meta m ON f.xml_meta_id = m.id
                WHERE f.oaza_name IS NOT NULL AND f.oaza_name != ''
                ORDER BY f.oaza_name, f.koaza_code, f.chiban
            """)

            for row in cursor.fetchall():
                oaza_name = row[0]
                koaza_code = row[1] or ''
                chiban = row[2] or ''
                file_name = row[3]

                # Update Oaza index
                if oaza_name not in self._oaza_index:
                    self._oaza_index[oaza_name] = OazaIndexNode(name=oaza_name)

                node = self._oaza_index[oaza_name]
                node.fude_count += 1
                node.xml_files.add(file_name)

                if chiban and chiban not in node.chiban_list:
                    node.chiban_list.append(chiban)

                # Update Koaza map
                if koaza_code:
                    if koaza_code not in node.koaza_map:
                        node.koaza_map[koaza_code] = []
                    if chiban and chiban not in node.koaza_map[koaza_code]:
                        node.koaza_map[koaza_code].append(chiban)

                # Update reverse index
                if chiban:
                    self._chiban_to_oaza[chiban].add(oaza_name)
                    normalized = self._normalize_chiban(chiban)
                    self._normalized_chibans[normalized] = chiban

        # Sort chiban lists naturally
        for node in self._oaza_index.values():
            node.chiban_list.sort(key=self._chiban_sort_key)
            for koaza_code in node.koaza_map:
                node.koaza_map[koaza_code].sort(key=self._chiban_sort_key)

        logger.info(f"Search index built: {len(self._oaza_index)} Oaza, "
                   f"{sum(n.fude_count for n in self._oaza_index.values())} parcels")

    def _normalize_chiban(self, chiban: str) -> str:
        """
        Normalize chiban for search.

        Handles variations like:
        - "1-2" vs "1番2"
        - Full-width vs half-width numbers
        """
        # Convert full-width to half-width
        result = chiban.translate(str.maketrans(
            '０１２３４５６７８９',
            '0123456789'
        ))

        # Remove common separators and markers
        result = re.sub(r'[番号の\-－ー]', '', result)

        return result.lower()

    def _chiban_sort_key(self, chiban: str) -> Tuple:
        """
        Sort key for natural chiban ordering.

        Handles formats like "123-4", "123-4W1", etc.
        """
        # Extract numeric parts
        parts = re.split(r'[-－ー]', chiban)
        result = []

        for part in parts:
            # Try to extract leading number
            match = re.match(r'^(\d+)(.*)', part)
            if match:
                result.append((0, int(match.group(1)), match.group(2)))
            else:
                result.append((1, 0, part))

        return tuple(result)

    def get_oaza_list(self) -> List[str]:
        """Get sorted list of all Oaza names."""
        return sorted(self._oaza_index.keys())

    def get_oaza_info(self, oaza_name: str) -> Optional[OazaIndexNode]:
        """Get index node for a specific Oaza."""
        return self._oaza_index.get(oaza_name)

    def get_chiban_list(self, oaza_name: str,
                       koaza_code: Optional[str] = None) -> List[str]:
        """
        Get list of chibans for an Oaza.

        Args:
            oaza_name: Oaza name
            koaza_code: Optional Koaza code filter

        Returns:
            List of chibans
        """
        node = self._oaza_index.get(oaza_name)
        if not node:
            return []

        if koaza_code and koaza_code in node.koaza_map:
            return node.koaza_map[koaza_code]

        return node.chiban_list

    def get_koaza_list(self, oaza_name: str) -> List[str]:
        """Get list of Koaza codes for an Oaza."""
        node = self._oaza_index.get(oaza_name)
        if not node:
            return []
        return sorted(node.koaza_map.keys())

    def search_chiban(self, query: str, oaza: Optional[str] = None,
                     limit: int = 50) -> List[SearchResult]:
        """
        Search for chibans matching a query.

        Args:
            query: Search query (partial match supported)
            oaza: Optional Oaza filter
            limit: Maximum results to return

        Returns:
            List of SearchResult objects
        """
        if not query:
            return []

        # Query database for matching parcels
        results = []

        with self.db.connection() as conn:
            cursor = conn.cursor()

            sql = """
                SELECT id, oaza_name, chiban, area_sqm
                FROM t_fude_poly
                WHERE chiban LIKE ?
            """
            params = [f'%{query}%']

            if oaza:
                sql += " AND oaza_name = ?"
                params.append(oaza)

            sql += f" ORDER BY oaza_name, chiban LIMIT {limit}"

            cursor.execute(sql, params)

            for row in cursor.fetchall():
                results.append(SearchResult(
                    fude_id=row[0],
                    oaza_name=row[1] or '',
                    chiban=row[2] or '',
                    area_sqm=row[3] or 0.0
                ))

        return results

    def autocomplete_chiban(self, prefix: str, oaza_name: Optional[str] = None,
                           limit: int = 10) -> List[str]:
        """
        Autocomplete chiban from prefix.

        Args:
            prefix: Chiban prefix
            oaza_name: Optional Oaza filter
            limit: Maximum suggestions

        Returns:
            List of matching chibans
        """
        if not prefix:
            return []

        suggestions = []
        search_oazas = [oaza_name] if oaza_name else self._oaza_index.keys()

        for oaza in search_oazas:
            node = self._oaza_index.get(oaza)
            if not node:
                continue

            for chiban in node.chiban_list:
                if chiban.startswith(prefix):
                    if chiban not in suggestions:
                        suggestions.append(chiban)
                        if len(suggestions) >= limit:
                            return suggestions

        return suggestions

    def oaza_exists(self, oaza_name: str) -> bool:
        """Check if an Oaza exists in the index."""
        return oaza_name in self._oaza_index

    def chiban_exists(self, chiban: str, oaza_name: Optional[str] = None) -> bool:
        """Check if a chiban exists in the index."""
        if oaza_name:
            node = self._oaza_index.get(oaza_name)
            return node is not None and chiban in node.chiban_list

        return chiban in self._chiban_to_oaza

    def get_statistics(self) -> Dict:
        """Get index statistics."""
        total_fude = sum(n.fude_count for n in self._oaza_index.values())
        total_chiban = sum(len(n.chiban_list) for n in self._oaza_index.values())

        return {
            'oaza_count': len(self._oaza_index),
            'total_fude': total_fude,
            'unique_chiban': total_chiban,
            'oaza_distribution': {
                name: node.fude_count
                for name, node in sorted(
                    self._oaza_index.items(),
                    key=lambda x: x[1].fude_count,
                    reverse=True
                )
            }
        }


def build_search_index(db: DatabaseManager) -> SearchIndex:
    """
    Convenience function to build a search index.

    Args:
        db: Database manager

    Returns:
        SearchIndex: Built search index
    """
    index = SearchIndex(db)
    index.build()
    return index
