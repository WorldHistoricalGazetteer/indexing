# processing/feature_ids.py

"""
Shared numeric feature ID encoding/decoding for vector tile feature IDs.

MapLibre GL JS ``feature-state`` requires each vector tile feature to carry
a numeric ``id`` at the GeoJSON top level (not inside ``properties``). The
ID must be unique within the tileset and fit within JavaScript's
``Number.MAX_SAFE_INTEGER`` (2^53 - 1).

Layout (53 bits total):
  bits 52-48 (5 bits): namespace code  (0=osm, 1=ohm, 2=m49, 3-31 reserved)
  bits 47-0  (48 bits): source ID      (max ~281 trillion)

For authorities with non-numeric source IDs (PeriodO, Cliopatria), use the
lower 53 bits of a stable hash of the string ID.

For the OSM/OHM Miscellaneous tileset (mixed namespaces): use 1 high bit to
discriminate osm (0) from ohm (1), with 52 bits for the source ID.
"""

import hashlib

NAMESPACE_CODES = {'osm': 0, 'ohm': 1, 'm49': 2, 'un': 3, 'po': 4, 'clio': 5}
NAMESPACE_NAMES = {v: k for k, v in NAMESPACE_CODES.items()}
_NS_SHIFT = 48
_ID_MASK = (1 << 48) - 1  # 0xFFFF_FFFF_FFFF
_MAX_SAFE_INT = (1 << 53) - 1


def encode_feature_id(namespace: str, source_id: int | str) -> int:
    """
    Encode namespace + source ID into a single integer for vector tile features.

    Args:
        namespace: e.g. 'osm', 'ohm', 'm49', 'po', 'clio'
        source_id: integer or string source ID

    Returns:
        Integer fitting within 2^53 - 1

    >>> encode_feature_id('osm', 12345)
    12345
    >>> encode_feature_id('ohm', 12345)
    281474976722809
    >>> decode_feature_id(encode_feature_id('ohm', 99999))
    ('ohm', 99999)
    """
    ns_code = NAMESPACE_CODES.get(namespace, 0)

    if isinstance(source_id, str):
        # Hash string IDs to get a stable numeric ID
        h = hashlib.sha256(source_id.encode('utf-8')).digest()
        numeric_id = int.from_bytes(h[:7], 'big') & _ID_MASK
    else:
        numeric_id = int(source_id) & _ID_MASK

    return (ns_code << _NS_SHIFT) | numeric_id


def decode_feature_id(numeric_id: int) -> tuple[str, int]:
    """
    Decode a numeric feature ID back to (namespace, source_id).

    >>> decode_feature_id(12345)
    ('osm', 12345)
    >>> decode_feature_id(281474976722809)
    ('ohm', 12345)
    """
    ns_code = (numeric_id >> _NS_SHIFT) & 0x1F  # 5 bits
    source_id = numeric_id & _ID_MASK
    namespace = NAMESPACE_NAMES.get(ns_code, f'ns{ns_code}')
    return namespace, source_id


def encode_misc_feature_id(namespace: str, source_id: int) -> int:
    """
    Encode for the OSM/OHM miscellaneous tileset (mixed namespaces).

    Uses 1 high bit: osm=0, ohm=1; remaining 52 bits for source ID.

    >>> encode_misc_feature_id('osm', 12345)
    12345
    >>> encode_misc_feature_id('ohm', 12345)
    4503599627382521
    """
    _MISC_SHIFT = 52
    _MISC_MASK = (1 << 52) - 1
    is_ohm = 1 if namespace == 'ohm' else 0
    return (is_ohm << _MISC_SHIFT) | (int(source_id) & _MISC_MASK)


def decode_misc_feature_id(numeric_id: int) -> tuple[str, int]:
    """
    Decode a miscellaneous tileset feature ID.

    >>> decode_misc_feature_id(4503599627382521)
    ('ohm', 12345)
    """
    _MISC_SHIFT = 52
    _MISC_MASK = (1 << 52) - 1
    is_ohm = (numeric_id >> _MISC_SHIFT) & 1
    source_id = numeric_id & _MISC_MASK
    return ('ohm' if is_ohm else 'osm', source_id)

