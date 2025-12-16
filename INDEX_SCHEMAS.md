# Index Schemas

This document describes the Elasticsearch index schemas for the WHG places and toponyms indices.

## Overview

| Index | Purpose | Documents | Size (approx) |
|-------|---------|-----------|---------------|
| `places` | Geographic entities with locations, types, relations | ~25-30 million | 60-90 GB |
| `toponyms` | Place names with embeddings for phonetic search | ~80 million | 120-180 GB |

## Settings: Staging vs Production

Indices are created with settings optimised for bulk indexing, then reconfigured for production queries.

| Setting | Staging (Indexing) | Production (Queries) | Purpose |
|---------|-------------------|---------------------|---------|
| `refresh_interval` | `"-1"` (disabled) | `"1s"` | Staging: skip refresh overhead. Production: near real-time search |
| `translog.durability` | `"async"` | `"request"` | Staging: batch writes. Production: data safety |
| `translog.flush_threshold_size` | `"1gb"` | `"512mb"` | Staging: fewer flushes. Production: bounded recovery time |
| `number_of_replicas` | `0` | `0` | Single-node deployment |
| `number_of_shards` | `4` | `4` | Fixed at creation |

The `deploy_to_production.py` script handles the settings transition and runs a force merge to optimise segment layout for queries.

---

## Places Index

Stores geographic entities from multiple authority sources.

### Schema

```json
{
  "settings": {
    "index": {
      "number_of_shards": 4,
      "number_of_replicas": 0,
      "refresh_interval": "-1",
      "blocks.read_only_allow_delete": false,
      "translog": {
        "durability": "async",
        "flush_threshold_size": "1gb"
      }
    }
  },
  "mappings": {
    "properties": {
      "place_id": {
        "type": "keyword"
      },
      "namespace": {
        "type": "keyword"
      },
      "label": {
        "type": "text",
        "fields": {
          "keyword": {
            "type": "keyword"
          }
        }
      },
      "toponyms": {
        "type": "keyword"
      },
      "ccodes": {
        "type": "keyword"
      },
      "locations": {
        "type": "nested",
        "properties": {
          "geometry": {
            "type": "geo_shape",
            "ignore_malformed": true,
            "coerce": true
          },
          "rep_point": {
            "type": "geo_point",
            "ignore_malformed": true
          },
          "timespans": {
            "type": "nested",
            "properties": {
              "start": { "type": "integer" },
              "end": { "type": "integer" }
            }
          }
        }
      },
      "elevation": {
        "type": "integer"
      },
      "minmax": {
        "type": "integer_range"
      },
      "types": {
        "type": "nested",
        "properties": {
          "identifier": { "type": "keyword" },
          "label": { "type": "keyword" },
          "sourceLabel": { "type": "keyword" }
        }
      },
      "relations": {
        "type": "nested",
        "properties": {
          "relationType": { "type": "keyword" },
          "relationTo": { "type": "keyword" },
          "label": { "type": "text" },
          "source": { "type": "keyword" },
          "certainty": { "type": "float" },
          "method": { "type": "keyword" }
        }
      },
      "clusters": {
        "type": "nested",
        "properties": {
          "cluster_id": { "type": "keyword" },
          "level": { "type": "keyword" },
          "certainty": { "type": "float" },
          "is_canonical": { "type": "boolean" },
          "cluster_size": { "type": "integer" }
        }
      },
      "canonical_ids": {
        "type": "object",
        "properties": {
          "strict": { "type": "keyword" },
          "moderate": { "type": "keyword" },
          "loose": { "type": "keyword" }
        }
      },
      "whg_published": {
        "type": "boolean"
      },
      "indexed_at": {
        "type": "date"
      }
    }
  }
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `place_id` | keyword | Unique identifier in `namespace:id` format (e.g., `gn:2643743`, `wd:Q84`) |
| `namespace` | keyword | Authority source prefix (`gn`, `wd`, `tgn`, `pl`, `gb`, `un`, etc.) |
| `label` | text | Primary display name |
| `toponyms` | keyword[] | All name variants in `name@lang` format |
| `ccodes` | keyword[] | ISO 3166-1 alpha-2 country codes |
| `locations` | nested | Array of geometries with optional timespans |
| `locations.geometry` | geo_shape | GeoJSON geometry (Point, Polygon, etc.) |
| `locations.rep_point` | geo_point | Representative point for the location |
| `locations.timespans` | nested | When this location was valid |
| `elevation` | integer | Elevation in metres |
| `minmax` | integer_range | Temporal range spanning all timespans (for range queries) |
| `types` | nested | Place type classifications |
| `relations` | nested | Links to other places (sameAs, partOf, etc.) |
| `clusters` | nested | Reconciliation cluster memberships |
| `canonical_ids` | object | Canonical place IDs at different confidence levels |
| `whg_published` | boolean | Whether published in WHG |
| `indexed_at` | date | Indexing timestamp |

### Example Document

```json
{
  "place_id": "gn:2643743",
  "namespace": "gn",
  "label": "London",
  "toponyms": ["London@en", "Londres@fr", "Londra@it", "ロンドン@ja", "Лондон@ru"],
  "ccodes": ["GB"],
  "locations": [{
    "geometry": {
      "type": "Point",
      "coordinates": [-0.1276, 51.5074]
    },
    "rep_point": {
      "lon": -0.1276,
      "lat": 51.5074
    }
  }],
  "types": [{
    "identifier": "PPLC",
    "label": "P",
    "sourceLabel": "P.PPLC"
  }],
  "relations": [{
    "relationType": "sameAs",
    "relationTo": "wd:Q84",
    "source": "geonames",
    "method": "curated",
    "certainty": 1.0
  }],
  "indexed_at": "2024-12-16T10:30:00Z"
}
```

---

## Toponyms Index

Stores individual place names with phonetic embeddings for similarity search.

### Schema

```json
{
  "settings": {
    "index": {
      "number_of_shards": 4,
      "number_of_replicas": 0,
      "refresh_interval": "-1",
      "blocks.read_only_allow_delete": false,
      "translog": {
        "durability": "async",
        "flush_threshold_size": "1gb"
      }
    },
    "analysis": {
      "analyzer": {
        "edge_ngram_analyzer": {
          "tokenizer": "edge_ngram_tokenizer",
          "filter": ["lowercase"]
        },
        "asciifolding_analyzer": {
          "tokenizer": "standard",
          "filter": ["asciifolding", "lowercase"]
        }
      },
      "tokenizer": {
        "edge_ngram_tokenizer": {
          "type": "edge_ngram",
          "min_gram": 2,
          "max_gram": 20,
          "token_chars": ["letter", "digit"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "place_id": {
        "type": "keyword"
      },
      "name": {
        "type": "text",
        "fields": {
          "keyword": { "type": "keyword" }
        }
      },
      "name_lower": {
        "type": "keyword"
      },
      "embedding_bilstm": {
        "type": "dense_vector",
        "dims": 128,
        "index": true,
        "similarity": "cosine"
      },
      "lang": {
        "type": "keyword"
      },
      "lang_variant": {
        "type": "keyword"
      },
      "timespans": {
        "type": "nested",
        "properties": {
          "start": { "type": "integer" },
          "end": { "type": "integer" }
        }
      },
      "suggest": {
        "type": "completion",
        "analyzer": "simple",
        "preserve_separators": true,
        "preserve_position_increments": true,
        "max_input_length": 50,
        "contexts": [{
          "name": "lang",
          "type": "category"
        }]
      }
    }
  }
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `place_id` | keyword | Reference to parent place document |
| `name` | text | The toponym text (extracted from `toponym@lang` by ingest pipeline) |
| `name_lower` | keyword | Lowercase version for exact matching |
| `embedding_bilstm` | dense_vector | 128-dimensional phonetic embedding from Siamese BiLSTM model |
| `lang` | keyword | ISO 639 language code (extracted from `toponym@lang`) |
| `lang_variant` | keyword | Language variant if present (e.g., `zh-Hans`, `pt-BR`) |
| `timespans` | nested | When this name was in use |
| `suggest` | completion | Autocomplete suggester with language context |

### Analyzers

| Analyzer | Purpose |
|----------|---------|
| `edge_ngram_analyzer` | Prefix matching for autocomplete (2-20 character prefixes) |
| `asciifolding_analyzer` | Diacritic-insensitive search (café → cafe) |

### Example Document

```json
{
  "place_id": "gn:2643743",
  "name": "Londres",
  "name_lower": "londres",
  "embedding_bilstm": [0.123, -0.456, 0.789, ...],
  "lang": "fr",
  "suggest": {
    "input": ["Londres"],
    "contexts": {
      "lang": ["fr"]
    }
  }
}
```

### Ingest Pipeline

The `toponyms_pipeline` processes incoming documents:

1. Splits `toponym@lang` format into separate `name` and `lang` fields
2. Creates `name_lower` for case-insensitive exact matching
3. Handles language variants (e.g., `zh-Hans` → `lang: zh`, `lang_variant: zh-Hans`)

---

## Phonetic Search with Embeddings

The `embedding_bilstm` field enables phonetic similarity search using Elasticsearch's kNN functionality.

### How it works

1. **Training**: A Siamese BiLSTM model learns phonetic similarity from pairs of equivalent toponyms
2. **Indexing**: Each toponym is encoded to a 128-dimensional vector
3. **Querying**: Search queries are encoded with the same model, then matched using cosine similarity

### Query Example

```json
{
  "knn": {
    "field": "embedding_bilstm",
    "query_vector": [0.123, -0.456, 0.789, ...],
    "k": 10,
    "num_candidates": 100
  }
}
```

This finds the 10 toponyms most phonetically similar to the query, regardless of spelling or script.

### Use Cases

- Finding variant spellings: "Muhammad" ↔ "Mohammed" ↔ "Mohamed"
- Cross-script matching: "Москва" ↔ "Moskva" ↔ "Moscow"
- Historical variants: "Lundenwic" ↔ "London"
- Transliteration differences: "北京" ↔ "Beijing" ↔ "Peking"

---

## Index Lifecycle

### Creation

```bash
python -m processing.create_indices
```

Creates both indices with staging settings. Destroys existing indices if present.

### Bulk Indexing (Staging)

Indices are configured for maximum throughput:
- `refresh_interval: -1` — no automatic refresh
- `translog.durability: async` — batch commits
- `replicas: 0` — no replication overhead

### Production Deployment

```bash
python -m processing.deploy_to_production
```

1. Restores snapshot to new timestamped indices
2. Reconfigures settings for queries
3. Runs force merge to 1 segment per shard
4. Switches aliases atomically

### Settings Transition

```python
es.indices.put_settings(
    index=index_name,
    body={
        "index": {
            "refresh_interval": "1s",
            "translog": {
                "durability": "request",
                "flush_threshold_size": "512mb"
            }
        }
    }
)
```

### Force Merge

```python
# Merge to 1 segment per shard for optimal query performance
# Takes 30-60 minutes but worthwhile for read-heavy workload
es.indices.forcemerge(index=index_name, max_num_segments=1)
```

This consolidates all segments into one per shard, eliminating the overhead of searching multiple segments. With 4 shards, the final index has exactly 4 segments.