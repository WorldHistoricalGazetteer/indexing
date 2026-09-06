"""
phonetics.ipa - incremental IPA computation for the toponym corpus.

Deliberately dependency-light at import time: the planner and merger run on
hosts with no torch and no epitran, and only `compute` needs a G2P backend.
Import submodules directly rather than re-exporting them here.
"""

__all__ = []
