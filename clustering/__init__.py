"""Batch 12 hard-link harvest + SQLite overlay.

The legacy 4-phase ES-based clustering pipeline has been retired in favour
of a per-place SQLite overlay shipped to the gateway and queried at
search-time. The surviving modules:

* ``clustering.sqlite_overlay`` — schema, builder, atomic ship-to-Pitt.
* ``clustering.harvest.hard_links_staged`` — authority harvest from staged
  ``final/places.parquet`` snapshots.
* ``clustering.harvest.loc_links`` — LOC NDJSON → transitive pairs.
* ``clustering.harvest.contributor_replay`` — DO PG → SQLite replay.
* ``clustering.config`` — surviving constants.
* ``clustering.pg_client`` — SSH-tunnelled asyncpg client used by the
  contributor replay.
"""
