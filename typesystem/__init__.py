# typesystem/__init__.py

"""
WHG Place Type System.

This package manages the AAT (Art & Architecture Thesaurus) based place
type hierarchy and cross-vocabulary mappings for the WHG search infrastructure.

Note: this package is named 'typesystem' (not 'types') to avoid shadowing
Python's built-in 'types' module.

Key modules:
    aat_config       — AAT configuration (entry points, fclass map, exclusions)
    sync_aat_types   — Download/parse AAT hierarchy → ES types index
    aat_mapper       — Augment typesystem/data/*.json with AAT mappings
    merge_mappings   — Write cross-vocab fields from data files into ES
    tree_api         — FastAPI endpoints for the type-tree widget
    build_*_types    — Scripts to create typesystem/data/ vocabulary files
"""


