# authorities/vob_cty-places.py
"""Stage the GBHGIS **vob_cty** boundary level (see authorities.vob_common).

One authority per Vision of Britain / GB Historical GIS administrative level
(place#135). All logic lives in the shared :mod:`authorities.vob_common`
builder; this thin wrapper just selects the level. Run standalone:

    python -m authorities.vob_cty-places
"""
from authorities.vob_common import stage_level

NAMESPACE = "vob_cty"

if __name__ == "__main__":
    stage_level(NAMESPACE)
