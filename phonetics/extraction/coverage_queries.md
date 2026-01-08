# Elasticsearch Coverage Assessment Queries

These queries help assess the script/language coverage of the new authority configuration (gn, wd, tgn).

## Basic Coverage Query (from original request)

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "namespaces": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "scripts": {
      "terms": {
        "field": "script",
        "size": 50 
      },
      "aggs": {
        "namespace_distribution": {
          "terms": {
            "field": "namespaces",
            "size": 10
          }
        },
        "distinct_languages": {
          "cardinality": {
            "field": "lang"
          }
        }
      }
    }
  }
}
```

## Enhanced Query: Script × Namespace Matrix with Language Depth

This query provides a more detailed breakdown including:
- Per-script toponym counts
- Which namespaces contribute to each script
- Language diversity within each script
- Sample languages for each script

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "by_script": {
      "terms": {
        "field": "script",
        "size": 30,
        "order": { "_count": "desc" }
      },
      "aggs": {
        "by_namespace": {
          "terms": {
            "field": "primary_namespace",
            "size": 5
          }
        },
        "unique_languages": {
          "cardinality": {
            "field": "lang"
          }
        },
        "top_languages": {
          "terms": {
            "field": "lang",
            "size": 10
          }
        }
      }
    }
  }
}
```

## Cross-Script Pairs Potential

Identifies scripts with coverage from multiple namespaces (required for generating cross-namespace training pairs):

```json
GET /toponyms/_search
{
  "size": 0,
  "aggs": {
    "scripts_with_multi_ns": {
      "terms": {
        "field": "script",
        "size": 30
      },
      "aggs": {
        "namespace_count": {
          "cardinality": {
            "field": "primary_namespace"
          }
        },
        "namespaces": {
          "terms": {
            "field": "primary_namespace",
            "size": 10
          }
        }
      }
    }
  }
}
```

## Low-Resource Script Assessment

Identifies scripts that may have insufficient training data:

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "scripts": {
      "terms": {
        "field": "script",
        "size": 50,
        "order": { "_count": "asc" }
      },
      "aggs": {
        "sample_toponyms": {
          "top_hits": {
            "size": 5,
            "_source": ["name", "lang", "primary_namespace"]
          }
        }
      }
    }
  }
}
```

## Wikidata's Contribution to Non-Latin Scripts

Critical for assessing whether Wikidata provides the necessary coverage for South/Southeast Asian scripts:

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "term": {
            "primary_namespace": "wd"
          }
        },
        {
          "bool": {
            "must_not": {
              "term": {
                "script": "LATIN"
              }
            }
          }
        }
      ]
    }
  },
  "aggs": {
    "non_latin_scripts": {
      "terms": {
        "field": "script",
        "size": 30,
        "order": { "_count": "desc" }
      },
      "aggs": {
        "languages": {
          "terms": {
            "field": "lang",
            "size": 20
          }
        }
      }
    }
  }
}
```

## Cross-Script Place Linkage Potential

Estimates how many places have toponyms in multiple scripts (required for training pairs):

```json
GET /places/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "namespace": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "script_diversity": {
      "nested": {
        "path": "toponyms"
      },
      "aggs": {
        "scripts_per_place": {
          "cardinality": {
            "field": "toponyms.script"
          }
        }
      }
    }
  }
}
```

## Language Coverage by Authority

Detailed breakdown of which authorities contribute which languages:

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "by_namespace": {
      "terms": {
        "field": "primary_namespace",
        "size": 5
      },
      "aggs": {
        "languages": {
          "terms": {
            "field": "lang",
            "size": 50,
            "order": { "_count": "desc" }
          }
        },
        "scripts": {
          "terms": {
            "field": "script",
            "size": 25
          }
        },
        "unique_langs": {
          "cardinality": {
            "field": "lang"
          }
        }
      }
    }
  }
}
```

## Epitran-Supported Language Coverage

Check coverage of languages that have Epitran support (required for Teacher training):

The following language-script combinations have Epitran support:
- Latin: en, de, fr, es, it, pt, nl, pl, cs, ro, hu, fi, sv, no, da, tr, vi, id, ms, sw, la
- Cyrillic: ru, uk, bg, sr, mk
- Greek: el
- Arabic: ar, fa, ur
- Hebrew: he
- Devanagari: hi, mr, ne, sa
- Bengali: bn
- Tamil: ta
- Telugu: te
- Malayalam: ml
- Kannada: kn
- Gujarati: gu
- Thai: th
- Georgian: ka
- Armenian: hy
- Hangul: ko
- CJK: zh (Mandarin)
- Hiragana: ja

```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "wd", "tgn"]
          }
        },
        {
          "terms": {
            "lang": ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "ro", 
                     "hu", "fi", "sv", "no", "da", "tr", "vi", "id", "ms", "sw", 
                     "la", "ru", "uk", "bg", "sr", "mk", "el", "ar", "fa", "ur", 
                     "he", "hi", "mr", "ne", "sa", "bn", "ta", "te", "ml", "kn", 
                     "gu", "th", "ka", "hy", "ko", "zh", "ja"]
          }
        }
      ]
    }
  },
  "aggs": {
    "epitran_supported": {
      "terms": {
        "field": "lang",
        "size": 50,
        "order": { "_count": "desc" }
      },
      "aggs": {
        "scripts": {
          "terms": {
            "field": "script",
            "size": 5
          }
        }
      }
    },
    "total_epitran_supported": {
      "value_count": {
        "field": "toponym_id"
      }
    }
  }
}
```

## Comparison: Old vs New Authorities

Compare the script/language coverage between old (gn, pl, iv) and new (gn, wd, tgn) configurations:

### Old Configuration (for reference)
```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "pl", "iv"]
          }
        }
      ]
    }
  },
  "aggs": {
    "total": { "value_count": { "field": "toponym_id" } },
    "scripts": { "terms": { "field": "script", "size": 30 } },
    "languages": { "cardinality": { "field": "lang" } }
  }
}
```

### New Configuration
```json
GET /toponyms/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "primary_namespace": ["gn", "wd", "tgn"]
          }
        }
      ]
    }
  },
  "aggs": {
    "total": { "value_count": { "field": "toponym_id" } },
    "scripts": { "terms": { "field": "script", "size": 30 } },
    "languages": { "cardinality": { "field": "lang" } }
  }
}
```

