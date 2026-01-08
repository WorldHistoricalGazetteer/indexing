# Elasticsearch Coverage Assessment Queries

These queries help assess the script/language coverage of the new authority configuration (gn, wd, tgn).

## Basic Coverage Query (from original request)

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 1941,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "scripts" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "LATIN",
          "doc_count" : 50068496,
          "distinct_languages" : {
            "value" : 874
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 36646874
              },
              {
                "key" : "gn",
                "doc_count" : 14111277
              },
              {
                "key" : "tgn",
                "doc_count" : 2693589
              },
              {
                "key" : "osm",
                "doc_count" : 2501780
              },
              {
                "key" : "gb",
                "doc_count" : 100961
              },
              {
                "key" : "iv",
                "doc_count" : 18768
              },
              {
                "key" : "pl",
                "doc_count" : 8016
              },
              {
                "key" : "dp",
                "doc_count" : 1022
              },
              {
                "key" : "nl",
                "doc_count" : 513
              },
              {
                "key" : "un",
                "doc_count" : 475
              }
            ]
          }
        },
        {
          "key" : "CYRILLIC",
          "doc_count" : 2903698,
          "distinct_languages" : {
            "value" : 195
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 2577731
              },
              {
                "key" : "gn",
                "doc_count" : 815883
              },
              {
                "key" : "osm",
                "doc_count" : 310937
              },
              {
                "key" : "tgn",
                "doc_count" : 121692
              },
              {
                "key" : "pl",
                "doc_count" : 70
              }
            ]
          }
        },
        {
          "key" : "CJK",
          "doc_count" : 1857832,
          "distinct_languages" : {
            "value" : 86
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 1212549
              },
              {
                "key" : "gn",
                "doc_count" : 752332
              },
              {
                "key" : "tgn",
                "doc_count" : 421055
              },
              {
                "key" : "osm",
                "doc_count" : 239451
              },
              {
                "key" : "pl",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "ARABIC",
          "doc_count" : 1751075,
          "distinct_languages" : {
            "value" : 129
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 1298673
              },
              {
                "key" : "gn",
                "doc_count" : 708090
              },
              {
                "key" : "tgn",
                "doc_count" : 128342
              },
              {
                "key" : "osm",
                "doc_count" : 115849
              },
              {
                "key" : "pl",
                "doc_count" : 286
              },
              {
                "key" : "nl",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "KATAKANA",
          "doc_count" : 310691,
          "distinct_languages" : {
            "value" : 32
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 280380
              },
              {
                "key" : "gn",
                "doc_count" : 63753
              },
              {
                "key" : "tgn",
                "doc_count" : 18143
              },
              {
                "key" : "osm",
                "doc_count" : 7974
              }
            ]
          }
        },
        {
          "key" : "HANGUL",
          "doc_count" : 270407,
          "distinct_languages" : {
            "value" : 36
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 153328
              },
              {
                "key" : "gn",
                "doc_count" : 144305
              },
              {
                "key" : "osm",
                "doc_count" : 34040
              },
              {
                "key" : "tgn",
                "doc_count" : 1331
              }
            ]
          }
        },
        {
          "key" : "OTHER",
          "doc_count" : 243259,
          "distinct_languages" : {
            "value" : 401
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 141986
              },
              {
                "key" : "gn",
                "doc_count" : 110487
              },
              {
                "key" : "osm",
                "doc_count" : 16860
              },
              {
                "key" : "tgn",
                "doc_count" : 2775
              },
              {
                "key" : "pl",
                "doc_count" : 19
              },
              {
                "key" : "gb",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "THAI",
          "doc_count" : 211956,
          "distinct_languages" : {
            "value" : 32
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 166422
              },
              {
                "key" : "wd",
                "doc_count" : 70925
              },
              {
                "key" : "osm",
                "doc_count" : 9225
              },
              {
                "key" : "tgn",
                "doc_count" : 805
              }
            ]
          }
        },
        {
          "key" : "GREEK",
          "doc_count" : 172879,
          "distinct_languages" : {
            "value" : 72
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 155506
              },
              {
                "key" : "gn",
                "doc_count" : 31934
              },
              {
                "key" : "osm",
                "doc_count" : 14849
              },
              {
                "key" : "tgn",
                "doc_count" : 3477
              },
              {
                "key" : "pl",
                "doc_count" : 576
              }
            ]
          }
        },
        {
          "key" : "ARMENIAN",
          "doc_count" : 148266,
          "distinct_languages" : {
            "value" : 39
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 128557
              },
              {
                "key" : "gn",
                "doc_count" : 38195
              },
              {
                "key" : "osm",
                "doc_count" : 9792
              },
              {
                "key" : "tgn",
                "doc_count" : 536
              },
              {
                "key" : "pl",
                "doc_count" : 54
              }
            ]
          }
        },
        {
          "key" : "HEBREW",
          "doc_count" : 133232,
          "distinct_languages" : {
            "value" : 49
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 126477
              },
              {
                "key" : "gn",
                "doc_count" : 21438
              },
              {
                "key" : "osm",
                "doc_count" : 8648
              },
              {
                "key" : "tgn",
                "doc_count" : 2211
              },
              {
                "key" : "pl",
                "doc_count" : 54
              }
            ]
          }
        },
        {
          "key" : "DEVANAGARI",
          "doc_count" : 131785,
          "distinct_languages" : {
            "value" : 58
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 130110
              },
              {
                "key" : "gn",
                "doc_count" : 17676
              },
              {
                "key" : "osm",
                "doc_count" : 6845
              },
              {
                "key" : "tgn",
                "doc_count" : 2035
              },
              {
                "key" : "pl",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "BENGALI",
          "doc_count" : 97931,
          "distinct_languages" : {
            "value" : 28
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 97251
              },
              {
                "key" : "gn",
                "doc_count" : 11079
              },
              {
                "key" : "osm",
                "doc_count" : 2490
              },
              {
                "key" : "tgn",
                "doc_count" : 687
              }
            ]
          }
        },
        {
          "key" : "GEORGIAN",
          "doc_count" : 93334,
          "distinct_languages" : {
            "value" : 33
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 91987
              },
              {
                "key" : "gn",
                "doc_count" : 17374
              },
              {
                "key" : "osm",
                "doc_count" : 7654
              },
              {
                "key" : "tgn",
                "doc_count" : 841
              },
              {
                "key" : "pl",
                "doc_count" : 12
              }
            ]
          }
        },
        {
          "key" : "MALAYALAM",
          "doc_count" : 53570,
          "distinct_languages" : {
            "value" : 8
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 53256
              },
              {
                "key" : "osm",
                "doc_count" : 3403
              },
              {
                "key" : "gn",
                "doc_count" : 2009
              },
              {
                "key" : "tgn",
                "doc_count" : 398
              }
            ]
          }
        },
        {
          "key" : "TAMIL",
          "doc_count" : 47748,
          "distinct_languages" : {
            "value" : 14
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 47291
              },
              {
                "key" : "gn",
                "doc_count" : 5375
              },
              {
                "key" : "osm",
                "doc_count" : 2999
              },
              {
                "key" : "tgn",
                "doc_count" : 493
              },
              {
                "key" : "pl",
                "doc_count" : 3
              }
            ]
          }
        },
        {
          "key" : "HIRAGANA",
          "doc_count" : 47695,
          "distinct_languages" : {
            "value" : 18
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 41008
              },
              {
                "key" : "tgn",
                "doc_count" : 26421
              },
              {
                "key" : "osm",
                "doc_count" : 9715
              },
              {
                "key" : "wd",
                "doc_count" : 7915
              }
            ]
          }
        },
        {
          "key" : "TELUGU",
          "doc_count" : 47646,
          "distinct_languages" : {
            "value" : 14
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 47352
              },
              {
                "key" : "osm",
                "doc_count" : 11989
              },
              {
                "key" : "gn",
                "doc_count" : 3636
              },
              {
                "key" : "tgn",
                "doc_count" : 341
              }
            ]
          }
        },
        {
          "key" : "KANNADA",
          "doc_count" : 21372,
          "distinct_languages" : {
            "value" : 14
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 21135
              },
              {
                "key" : "gn",
                "doc_count" : 3551
              },
              {
                "key" : "osm",
                "doc_count" : 1358
              },
              {
                "key" : "tgn",
                "doc_count" : 364
              }
            ]
          }
        },
        {
          "key" : "GUJARATI",
          "doc_count" : 20340,
          "distinct_languages" : {
            "value" : 6
          },
          "namespace_distribution" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 20145
              },
              {
                "key" : "gn",
                "doc_count" : 3491
              },
              {
                "key" : "osm",
                "doc_count" : 419
              },
              {
                "key" : "tgn",
                "doc_count" : 305
              }
            ]
          }
        }
      ]
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

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 1952,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "by_script" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "LATIN",
          "doc_count" : 50068496,
          "top_languages" : {
            "doc_count_error_upper_bound" : 292889,
            "sum_other_doc_count" : 16938313,
            "buckets" : [
              {
                "key" : "en",
                "doc_count" : 8039735
              },
              {
                "key" : "ceb",
                "doc_count" : 2457885
              },
              {
                "key" : "fr",
                "doc_count" : 2311910
              },
              {
                "key" : "nl",
                "doc_count" : 2292068
              },
              {
                "key" : "de",
                "doc_count" : 2063027
              },
              {
                "key" : "sv",
                "doc_count" : 1715947
              },
              {
                "key" : "es",
                "doc_count" : 1518530
              },
              {
                "key" : "id",
                "doc_count" : 931192
              },
              {
                "key" : "tr",
                "doc_count" : 843744
              },
              {
                "key" : "it",
                "doc_count" : 815139
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 34783569
              },
              {
                "key" : "gn",
                "doc_count" : 14111277
              },
              {
                "key" : "tgn",
                "doc_count" : 1173650
              }
            ]
          },
          "unique_languages" : {
            "value" : 874
          }
        },
        {
          "key" : "CYRILLIC",
          "doc_count" : 2903698,
          "top_languages" : {
            "doc_count_error_upper_bound" : 2585,
            "sum_other_doc_count" : 203761,
            "buckets" : [
              {
                "key" : "ru",
                "doc_count" : 803734
              },
              {
                "key" : "uk",
                "doc_count" : 435644
              },
              {
                "key" : "ce",
                "doc_count" : 289774
              },
              {
                "key" : "tt",
                "doc_count" : 250116
              },
              {
                "key" : "bg",
                "doc_count" : 235749
              },
              {
                "key" : "sr",
                "doc_count" : 235582
              },
              {
                "key" : "be",
                "doc_count" : 138270
              },
              {
                "key" : "kk",
                "doc_count" : 102850
              },
              {
                "key" : "mk",
                "doc_count" : 61607
              },
              {
                "key" : "tg",
                "doc_count" : 35979
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 2086035
              },
              {
                "key" : "gn",
                "doc_count" : 815883
              },
              {
                "key" : "tgn",
                "doc_count" : 1780
              }
            ]
          },
          "unique_languages" : {
            "value" : 195
          }
        },
        {
          "key" : "CJK",
          "doc_count" : 1857832,
          "top_languages" : {
            "doc_count_error_upper_bound" : 28,
            "sum_other_doc_count" : 1755,
            "buckets" : [
              {
                "key" : "zh",
                "doc_count" : 1306961
              },
              {
                "key" : "ja",
                "doc_count" : 337530
              },
              {
                "key" : "wuu",
                "doc_count" : 48883
              },
              {
                "key" : "gan",
                "doc_count" : 37097
              },
              {
                "key" : "yue",
                "doc_count" : 31345
              },
              {
                "key" : "mul",
                "doc_count" : 7576
              },
              {
                "key" : "lzh",
                "doc_count" : 3519
              },
              {
                "key" : "ko",
                "doc_count" : 2060
              },
              {
                "key" : "fr",
                "doc_count" : 2044
              },
              {
                "key" : "nan",
                "doc_count" : 1505
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 1066744
              },
              {
                "key" : "gn",
                "doc_count" : 752332
              },
              {
                "key" : "tgn",
                "doc_count" : 38756
              }
            ]
          },
          "unique_languages" : {
            "value" : 86
          }
        },
        {
          "key" : "ARABIC",
          "doc_count" : 1751075,
          "top_languages" : {
            "doc_count_error_upper_bound" : 203,
            "sum_other_doc_count" : 38016,
            "buckets" : [
              {
                "key" : "fa",
                "doc_count" : 576748
              },
              {
                "key" : "ar",
                "doc_count" : 412316
              },
              {
                "key" : "arz",
                "doc_count" : 266408
              },
              {
                "key" : "azb",
                "doc_count" : 115881
              },
              {
                "key" : "ur",
                "doc_count" : 109700
              },
              {
                "key" : "kk",
                "doc_count" : 46184
              },
              {
                "key" : "glk",
                "doc_count" : 29117
              },
              {
                "key" : "mzn",
                "doc_count" : 27365
              },
              {
                "key" : "pnb",
                "doc_count" : 23202
              },
              {
                "key" : "ckb",
                "doc_count" : 14624
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 1039441
              },
              {
                "key" : "gn",
                "doc_count" : 708090
              },
              {
                "key" : "tgn",
                "doc_count" : 3544
              }
            ]
          },
          "unique_languages" : {
            "value" : 129
          }
        },
        {
          "key" : "KATAKANA",
          "doc_count" : 310691,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 35,
            "buckets" : [
              {
                "key" : "ja",
                "doc_count" : 310414
              },
              {
                "key" : "zh",
                "doc_count" : 69
              },
              {
                "key" : "ryu",
                "doc_count" : 59
              },
              {
                "key" : "mk",
                "doc_count" : 36
              },
              {
                "key" : "en",
                "doc_count" : 32
              },
              {
                "key" : "mul",
                "doc_count" : 11
              },
              {
                "key" : "nl",
                "doc_count" : 10
              },
              {
                "key" : "fr",
                "doc_count" : 7
              },
              {
                "key" : "ceb",
                "doc_count" : 6
              },
              {
                "key" : "ain",
                "doc_count" : 5
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 246486
              },
              {
                "key" : "gn",
                "doc_count" : 63753
              },
              {
                "key" : "tgn",
                "doc_count" : 452
              }
            ]
          },
          "unique_languages" : {
            "value" : 32
          }
        },
        {
          "key" : "HANGUL",
          "doc_count" : 270407,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 52,
            "buckets" : [
              {
                "key" : "ko",
                "doc_count" : 228523
              },
              {
                "key" : "sk",
                "doc_count" : 87
              },
              {
                "key" : "mul",
                "doc_count" : 38
              },
              {
                "key" : "en",
                "doc_count" : 35
              },
              {
                "key" : "mk",
                "doc_count" : 33
              },
              {
                "key" : "fr",
                "doc_count" : 23
              },
              {
                "key" : "de",
                "doc_count" : 14
              },
              {
                "key" : "zh",
                "doc_count" : 14
              },
              {
                "key" : "ceb",
                "doc_count" : 12
              },
              {
                "key" : "hu",
                "doc_count" : 11
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 144305
              },
              {
                "key" : "wd",
                "doc_count" : 125783
              },
              {
                "key" : "tgn",
                "doc_count" : 319
              }
            ]
          },
          "unique_languages" : {
            "value" : 36
          }
        },
        {
          "key" : "OTHER",
          "doc_count" : 243259,
          "top_languages" : {
            "doc_count_error_upper_bound" : 679,
            "sum_other_doc_count" : 32592,
            "buckets" : [
              {
                "key" : "lauc",
                "doc_count" : 69085
              },
              {
                "key" : "my",
                "doc_count" : 39404
              },
              {
                "key" : "en",
                "doc_count" : 25547
              },
              {
                "key" : "pa",
                "doc_count" : 14551
              },
              {
                "key" : "si",
                "doc_count" : 14217
              },
              {
                "key" : "sat",
                "doc_count" : 12626
              },
              {
                "key" : "uicn",
                "doc_count" : 10577
              },
              {
                "key" : "km",
                "doc_count" : 9657
              },
              {
                "key" : "bo",
                "doc_count" : 7998
              },
              {
                "key" : "or",
                "doc_count" : 6907
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 132043
              },
              {
                "key" : "gn",
                "doc_count" : 110487
              },
              {
                "key" : "tgn",
                "doc_count" : 729
              }
            ]
          },
          "unique_languages" : {
            "value" : 401
          }
        },
        {
          "key" : "THAI",
          "doc_count" : 211956,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 41,
            "buckets" : [
              {
                "key" : "th",
                "doc_count" : 210310
              },
              {
                "key" : "mk",
                "doc_count" : 26
              },
              {
                "key" : "en",
                "doc_count" : 20
              },
              {
                "key" : "fr",
                "doc_count" : 16
              },
              {
                "key" : "id",
                "doc_count" : 10
              },
              {
                "key" : "nl",
                "doc_count" : 9
              },
              {
                "key" : "de",
                "doc_count" : 7
              },
              {
                "key" : "lzh",
                "doc_count" : 7
              },
              {
                "key" : "sr",
                "doc_count" : 7
              },
              {
                "key" : "yue",
                "doc_count" : 7
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 166422
              },
              {
                "key" : "wd",
                "doc_count" : 45475
              },
              {
                "key" : "tgn",
                "doc_count" : 59
              }
            ]
          },
          "unique_languages" : {
            "value" : 32
          }
        },
        {
          "key" : "GREEK",
          "doc_count" : 172879,
          "top_languages" : {
            "doc_count_error_upper_bound" : 8,
            "sum_other_doc_count" : 474,
            "buckets" : [
              {
                "key" : "el",
                "doc_count" : 168827
              },
              {
                "key" : "grc",
                "doc_count" : 1246
              },
              {
                "key" : "pnt",
                "doc_count" : 583
              },
              {
                "key" : "es",
                "doc_count" : 339
              },
              {
                "key" : "en",
                "doc_count" : 261
              },
              {
                "key" : "fr",
                "doc_count" : 110
              },
              {
                "key" : "sl",
                "doc_count" : 90
              },
              {
                "key" : "de",
                "doc_count" : 85
              },
              {
                "key" : "ru",
                "doc_count" : 83
              },
              {
                "key" : "sr",
                "doc_count" : 74
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 140141
              },
              {
                "key" : "gn",
                "doc_count" : 31934
              },
              {
                "key" : "tgn",
                "doc_count" : 804
              }
            ]
          },
          "unique_languages" : {
            "value" : 72
          }
        },
        {
          "key" : "ARMENIAN",
          "doc_count" : 148266,
          "top_languages" : {
            "doc_count_error_upper_bound" : 1,
            "sum_other_doc_count" : 69,
            "buckets" : [
              {
                "key" : "hy",
                "doc_count" : 143819
              },
              {
                "key" : "hyw",
                "doc_count" : 4237
              },
              {
                "key" : "en",
                "doc_count" : 30
              },
              {
                "key" : "az",
                "doc_count" : 27
              },
              {
                "key" : "mk",
                "doc_count" : 21
              },
              {
                "key" : "sr",
                "doc_count" : 18
              },
              {
                "key" : "de",
                "doc_count" : 12
              },
              {
                "key" : "eo",
                "doc_count" : 8
              },
              {
                "key" : "fr",
                "doc_count" : 8
              },
              {
                "key" : "sco",
                "doc_count" : 8
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 109993
              },
              {
                "key" : "gn",
                "doc_count" : 38195
              },
              {
                "key" : "tgn",
                "doc_count" : 78
              }
            ]
          },
          "unique_languages" : {
            "value" : 39
          }
        },
        {
          "key" : "HEBREW",
          "doc_count" : 133232,
          "top_languages" : {
            "doc_count_error_upper_bound" : 4,
            "sum_other_doc_count" : 193,
            "buckets" : [
              {
                "key" : "he",
                "doc_count" : 127339
              },
              {
                "key" : "yi",
                "doc_count" : 5362
              },
              {
                "key" : "en",
                "doc_count" : 67
              },
              {
                "key" : "mk",
                "doc_count" : 51
              },
              {
                "key" : "lad",
                "doc_count" : 42
              },
              {
                "key" : "sr",
                "doc_count" : 41
              },
              {
                "key" : "mul",
                "doc_count" : 30
              },
              {
                "key" : "de",
                "doc_count" : 24
              },
              {
                "key" : "es",
                "doc_count" : 24
              },
              {
                "key" : "fr",
                "doc_count" : 21
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 111170
              },
              {
                "key" : "gn",
                "doc_count" : 21438
              },
              {
                "key" : "tgn",
                "doc_count" : 624
              }
            ]
          },
          "unique_languages" : {
            "value" : 49
          }
        },
        {
          "key" : "DEVANAGARI",
          "doc_count" : 131785,
          "top_languages" : {
            "doc_count_error_upper_bound" : 7,
            "sum_other_doc_count" : 2261,
            "buckets" : [
              {
                "key" : "hi",
                "doc_count" : 60800
              },
              {
                "key" : "mr",
                "doc_count" : 24452
              },
              {
                "key" : "new",
                "doc_count" : 17332
              },
              {
                "key" : "ne",
                "doc_count" : 10249
              },
              {
                "key" : "mai",
                "doc_count" : 5313
              },
              {
                "key" : "bho",
                "doc_count" : 5142
              },
              {
                "key" : "sa",
                "doc_count" : 3482
              },
              {
                "key" : "dty",
                "doc_count" : 1100
              },
              {
                "key" : "awa",
                "doc_count" : 927
              },
              {
                "key" : "pi",
                "doc_count" : 727
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 113553
              },
              {
                "key" : "gn",
                "doc_count" : 17676
              },
              {
                "key" : "tgn",
                "doc_count" : 556
              }
            ]
          },
          "unique_languages" : {
            "value" : 58
          }
        },
        {
          "key" : "BENGALI",
          "doc_count" : 97931,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 28,
            "buckets" : [
              {
                "key" : "bn",
                "doc_count" : 77935
              },
              {
                "key" : "bpy",
                "doc_count" : 17586
              },
              {
                "key" : "as",
                "doc_count" : 2302
              },
              {
                "key" : "en",
                "doc_count" : 27
              },
              {
                "key" : "hi",
                "doc_count" : 14
              },
              {
                "key" : "mk",
                "doc_count" : 14
              },
              {
                "key" : "mul",
                "doc_count" : 9
              },
              {
                "key" : "mni",
                "doc_count" : 7
              },
              {
                "key" : "syl",
                "doc_count" : 5
              },
              {
                "key" : "",
                "doc_count" : 3
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 86808
              },
              {
                "key" : "gn",
                "doc_count" : 11079
              },
              {
                "key" : "tgn",
                "doc_count" : 44
              }
            ]
          },
          "unique_languages" : {
            "value" : 28
          }
        },
        {
          "key" : "GEORGIAN",
          "doc_count" : 93334,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 45,
            "buckets" : [
              {
                "key" : "ka",
                "doc_count" : 86021
              },
              {
                "key" : "xmf",
                "doc_count" : 7026
              },
              {
                "key" : "sr",
                "doc_count" : 47
              },
              {
                "key" : "mul",
                "doc_count" : 33
              },
              {
                "key" : "mk",
                "doc_count" : 32
              },
              {
                "key" : "en",
                "doc_count" : 19
              },
              {
                "key" : "fr",
                "doc_count" : 13
              },
              {
                "key" : "ru",
                "doc_count" : 10
              },
              {
                "key" : "uk",
                "doc_count" : 8
              },
              {
                "key" : "hu",
                "doc_count" : 7
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 75909
              },
              {
                "key" : "gn",
                "doc_count" : 17374
              },
              {
                "key" : "tgn",
                "doc_count" : 51
              }
            ]
          },
          "unique_languages" : {
            "value" : 33
          }
        },
        {
          "key" : "MALAYALAM",
          "doc_count" : 53570,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "ml",
                "doc_count" : 53546
              },
              {
                "key" : "mk",
                "doc_count" : 9
              },
              {
                "key" : "en",
                "doc_count" : 6
              },
              {
                "key" : "hi",
                "doc_count" : 4
              },
              {
                "key" : "bn",
                "doc_count" : 2
              },
              {
                "key" : "ar",
                "doc_count" : 1
              },
              {
                "key" : "pt",
                "doc_count" : 1
              },
              {
                "key" : "te",
                "doc_count" : 1
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 51447
              },
              {
                "key" : "gn",
                "doc_count" : 2009
              },
              {
                "key" : "tgn",
                "doc_count" : 114
              }
            ]
          },
          "unique_languages" : {
            "value" : 8
          }
        },
        {
          "key" : "TAMIL",
          "doc_count" : 47748,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 4,
            "buckets" : [
              {
                "key" : "ta",
                "doc_count" : 47700
              },
              {
                "key" : "mk",
                "doc_count" : 14
              },
              {
                "key" : "en",
                "doc_count" : 8
              },
              {
                "key" : "ja",
                "doc_count" : 5
              },
              {
                "key" : "sr",
                "doc_count" : 4
              },
              {
                "key" : "mul",
                "doc_count" : 3
              },
              {
                "key" : "fr",
                "doc_count" : 2
              },
              {
                "key" : "ml",
                "doc_count" : 2
              },
              {
                "key" : "nn",
                "doc_count" : 2
              },
              {
                "key" : "yue",
                "doc_count" : 2
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 42309
              },
              {
                "key" : "gn",
                "doc_count" : 5375
              },
              {
                "key" : "tgn",
                "doc_count" : 64
              }
            ]
          },
          "unique_languages" : {
            "value" : 14
          }
        },
        {
          "key" : "HIRAGANA",
          "doc_count" : 47695,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 8,
            "buckets" : [
              {
                "key" : "ja",
                "doc_count" : 47533
              },
              {
                "key" : "ryu",
                "doc_count" : 40
              },
              {
                "key" : "yue",
                "doc_count" : 15
              },
              {
                "key" : "zh",
                "doc_count" : 14
              },
              {
                "key" : "fr",
                "doc_count" : 8
              },
              {
                "key" : "ceb",
                "doc_count" : 6
              },
              {
                "key" : "sr",
                "doc_count" : 4
              },
              {
                "key" : "",
                "doc_count" : 2
              },
              {
                "key" : "de",
                "doc_count" : 2
              },
              {
                "key" : "id",
                "doc_count" : 2
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 41008
              },
              {
                "key" : "wd",
                "doc_count" : 6079
              },
              {
                "key" : "tgn",
                "doc_count" : 608
              }
            ]
          },
          "unique_languages" : {
            "value" : 18
          }
        },
        {
          "key" : "TELUGU",
          "doc_count" : 47646,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 4,
            "buckets" : [
              {
                "key" : "te",
                "doc_count" : 47617
              },
              {
                "key" : "mul",
                "doc_count" : 6
              },
              {
                "key" : "hi",
                "doc_count" : 5
              },
              {
                "key" : "mk",
                "doc_count" : 5
              },
              {
                "key" : "es",
                "doc_count" : 2
              },
              {
                "key" : "nl",
                "doc_count" : 2
              },
              {
                "key" : "zh",
                "doc_count" : 2
              },
              {
                "key" : "ar",
                "doc_count" : 1
              },
              {
                "key" : "ast",
                "doc_count" : 1
              },
              {
                "key" : "en",
                "doc_count" : 1
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 43912
              },
              {
                "key" : "gn",
                "doc_count" : 3636
              },
              {
                "key" : "tgn",
                "doc_count" : 98
              }
            ]
          },
          "unique_languages" : {
            "value" : 14
          }
        },
        {
          "key" : "KANNADA",
          "doc_count" : 21372,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 4,
            "buckets" : [
              {
                "key" : "kn",
                "doc_count" : 20962
              },
              {
                "key" : "tcy",
                "doc_count" : 356
              },
              {
                "key" : "gom",
                "doc_count" : 25
              },
              {
                "key" : "mk",
                "doc_count" : 9
              },
              {
                "key" : "bn",
                "doc_count" : 5
              },
              {
                "key" : "",
                "doc_count" : 3
              },
              {
                "key" : "en",
                "doc_count" : 3
              },
              {
                "key" : "de",
                "doc_count" : 2
              },
              {
                "key" : "es",
                "doc_count" : 2
              },
              {
                "key" : "hi",
                "doc_count" : 1
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 17758
              },
              {
                "key" : "gn",
                "doc_count" : 3551
              },
              {
                "key" : "tgn",
                "doc_count" : 63
              }
            ]
          },
          "unique_languages" : {
            "value" : 14
          }
        },
        {
          "key" : "GUJARATI",
          "doc_count" : 20340,
          "top_languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gu",
                "doc_count" : 20333
              },
              {
                "key" : "hi",
                "doc_count" : 2
              },
              {
                "key" : "mk",
                "doc_count" : 2
              },
              {
                "key" : "ast",
                "doc_count" : 1
              },
              {
                "key" : "en",
                "doc_count" : 1
              },
              {
                "key" : "nl",
                "doc_count" : 1
              }
            ]
          },
          "by_namespace" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 16809
              },
              {
                "key" : "gn",
                "doc_count" : 3491
              },
              {
                "key" : "tgn",
                "doc_count" : 40
              }
            ]
          },
          "unique_languages" : {
            "value" : 6
          }
        }
      ]
    }
  }
}
```

## Cross-Script Pairs Potential

Identifies scripts with coverage from multiple namespaces (required for generating cross-namespace training pairs):
```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 364,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "scripts_with_multi_ns" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "LATIN",
          "doc_count" : 56707055,
          "namespace_count" : {
            "value" : 10
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 34783569
              },
              {
                "key" : "gn",
                "doc_count" : 14111277
              },
              {
                "key" : "osm",
                "doc_count" : 6021178
              },
              {
                "key" : "tgn",
                "doc_count" : 1173650
              },
              {
                "key" : "gb",
                "doc_count" : 575100
              },
              {
                "key" : "pl",
                "doc_count" : 21173
              },
              {
                "key" : "iv",
                "doc_count" : 16675
              },
              {
                "key" : "nl",
                "doc_count" : 2950
              },
              {
                "key" : "dp",
                "doc_count" : 1245
              },
              {
                "key" : "un",
                "doc_count" : 238
              }
            ]
          }
        },
        {
          "key" : "CYRILLIC",
          "doc_count" : 3614762,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 2086035
              },
              {
                "key" : "gn",
                "doc_count" : 815883
              },
              {
                "key" : "osm",
                "doc_count" : 711004
              },
              {
                "key" : "tgn",
                "doc_count" : 1780
              },
              {
                "key" : "pl",
                "doc_count" : 60
              }
            ]
          }
        },
        {
          "key" : "CJK",
          "doc_count" : 2973525,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "osm",
                "doc_count" : 1115693
              },
              {
                "key" : "wd",
                "doc_count" : 1066744
              },
              {
                "key" : "gn",
                "doc_count" : 752332
              },
              {
                "key" : "tgn",
                "doc_count" : 38756
              }
            ]
          }
        },
        {
          "key" : "ARABIC",
          "doc_count" : 2098089,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 1039441
              },
              {
                "key" : "gn",
                "doc_count" : 708090
              },
              {
                "key" : "osm",
                "doc_count" : 346706
              },
              {
                "key" : "tgn",
                "doc_count" : 3544
              },
              {
                "key" : "pl",
                "doc_count" : 308
              }
            ]
          }
        },
        {
          "key" : "HANGUL",
          "doc_count" : 393996,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 144305
              },
              {
                "key" : "wd",
                "doc_count" : 125783
              },
              {
                "key" : "osm",
                "doc_count" : 123589
              },
              {
                "key" : "tgn",
                "doc_count" : 319
              }
            ]
          }
        },
        {
          "key" : "OTHER",
          "doc_count" : 342642,
          "namespace_count" : {
            "value" : 7
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 132043
              },
              {
                "key" : "gn",
                "doc_count" : 110487
              },
              {
                "key" : "osm",
                "doc_count" : 99335
              },
              {
                "key" : "tgn",
                "doc_count" : 729
              },
              {
                "key" : "pl",
                "doc_count" : 38
              },
              {
                "key" : "gb",
                "doc_count" : 7
              },
              {
                "key" : "nl",
                "doc_count" : 3
              }
            ]
          }
        },
        {
          "key" : "KATAKANA",
          "doc_count" : 340555,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 246486
              },
              {
                "key" : "gn",
                "doc_count" : 63753
              },
              {
                "key" : "osm",
                "doc_count" : 29864
              },
              {
                "key" : "tgn",
                "doc_count" : 452
              }
            ]
          }
        },
        {
          "key" : "THAI",
          "doc_count" : 251458,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gn",
                "doc_count" : 166422
              },
              {
                "key" : "wd",
                "doc_count" : 45475
              },
              {
                "key" : "osm",
                "doc_count" : 39502
              },
              {
                "key" : "tgn",
                "doc_count" : 59
              }
            ]
          }
        },
        {
          "key" : "GREEK",
          "doc_count" : 217997,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 140141
              },
              {
                "key" : "osm",
                "doc_count" : 42975
              },
              {
                "key" : "gn",
                "doc_count" : 31934
              },
              {
                "key" : "pl",
                "doc_count" : 2143
              },
              {
                "key" : "tgn",
                "doc_count" : 804
              }
            ]
          }
        },
        {
          "key" : "DEVANAGARI",
          "doc_count" : 166957,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 113553
              },
              {
                "key" : "osm",
                "doc_count" : 35170
              },
              {
                "key" : "gn",
                "doc_count" : 17676
              },
              {
                "key" : "tgn",
                "doc_count" : 556
              },
              {
                "key" : "pl",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "ARMENIAN",
          "doc_count" : 153467,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 109993
              },
              {
                "key" : "gn",
                "doc_count" : 38195
              },
              {
                "key" : "osm",
                "doc_count" : 5191
              },
              {
                "key" : "tgn",
                "doc_count" : 78
              },
              {
                "key" : "pl",
                "doc_count" : 10
              }
            ]
          }
        },
        {
          "key" : "HIRAGANA",
          "doc_count" : 151980,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "osm",
                "doc_count" : 104285
              },
              {
                "key" : "gn",
                "doc_count" : 41008
              },
              {
                "key" : "wd",
                "doc_count" : 6079
              },
              {
                "key" : "tgn",
                "doc_count" : 608
              }
            ]
          }
        },
        {
          "key" : "HEBREW",
          "doc_count" : 151960,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 111170
              },
              {
                "key" : "gn",
                "doc_count" : 21438
              },
              {
                "key" : "osm",
                "doc_count" : 18696
              },
              {
                "key" : "tgn",
                "doc_count" : 624
              },
              {
                "key" : "pl",
                "doc_count" : 32
              }
            ]
          }
        },
        {
          "key" : "BENGALI",
          "doc_count" : 106896,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 86808
              },
              {
                "key" : "gn",
                "doc_count" : 11079
              },
              {
                "key" : "osm",
                "doc_count" : 8965
              },
              {
                "key" : "tgn",
                "doc_count" : 44
              }
            ]
          }
        },
        {
          "key" : "GEORGIAN",
          "doc_count" : 105902,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 75909
              },
              {
                "key" : "gn",
                "doc_count" : 17374
              },
              {
                "key" : "osm",
                "doc_count" : 12566
              },
              {
                "key" : "tgn",
                "doc_count" : 51
              },
              {
                "key" : "pl",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "MALAYALAM",
          "doc_count" : 68176,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 51447
              },
              {
                "key" : "osm",
                "doc_count" : 14606
              },
              {
                "key" : "gn",
                "doc_count" : 2009
              },
              {
                "key" : "tgn",
                "doc_count" : 114
              }
            ]
          }
        },
        {
          "key" : "TAMIL",
          "doc_count" : 52486,
          "namespace_count" : {
            "value" : 5
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 42309
              },
              {
                "key" : "gn",
                "doc_count" : 5375
              },
              {
                "key" : "osm",
                "doc_count" : 4737
              },
              {
                "key" : "tgn",
                "doc_count" : 64
              },
              {
                "key" : "pl",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "TELUGU",
          "doc_count" : 51440,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 43912
              },
              {
                "key" : "osm",
                "doc_count" : 3794
              },
              {
                "key" : "gn",
                "doc_count" : 3636
              },
              {
                "key" : "tgn",
                "doc_count" : 98
              }
            ]
          }
        },
        {
          "key" : "KANNADA",
          "doc_count" : 43155,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "osm",
                "doc_count" : 21783
              },
              {
                "key" : "wd",
                "doc_count" : 17758
              },
              {
                "key" : "gn",
                "doc_count" : 3551
              },
              {
                "key" : "tgn",
                "doc_count" : 63
              }
            ]
          }
        },
        {
          "key" : "GUJARATI",
          "doc_count" : 21428,
          "namespace_count" : {
            "value" : 4
          },
          "namespaces" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "wd",
                "doc_count" : 16809
              },
              {
                "key" : "gn",
                "doc_count" : 3491
              },
              {
                "key" : "osm",
                "doc_count" : 1088
              },
              {
                "key" : "tgn",
                "doc_count" : 40
              }
            ]
          }
        }
      ]
    }
  }
}
```

## Low-Resource Script Assessment

Identifies scripts that may have insufficient training data:

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 550,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "scripts" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "GUJARATI",
          "doc_count" : 20340,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 20340,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "અંકુશપુર@gu",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "અંકુશપુર",
                    "lang" : "gu",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "અંકોડીયા@gu",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "અંકોડીયા",
                    "lang" : "gu",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "અંકોલડા@gu",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "અંકોલડા",
                    "lang" : "gu",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "અંગકોર વાટ@gu",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "અંગકોર વાટ",
                    "lang" : "gu",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "અંગુઠલા@gu",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "અંગુઠલા",
                    "lang" : "gu",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "KANNADA",
          "doc_count" : 21372,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 21372,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "ಅಂಕರ‍@kn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ಅಂಕರ‍",
                    "lang" : "kn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ಅಂಕಲಕೊಪ್ಪ@kn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ಅಂಕಲಕೊಪ್ಪ",
                    "lang" : "kn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ಅಂಕಲೇಶ್ವರ@kn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ಅಂಕಲೇಶ್ವರ",
                    "lang" : "kn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ಅಂಕೊರೊ@kn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ಅಂಕೊರೊ",
                    "lang" : "kn",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ಅಂಗೋಲ@kn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ಅಂಗೋಲ",
                    "lang" : "kn",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "TELUGU",
          "doc_count" : 47646,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 47646,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : ", ఆల్సస్టర్@te",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : ", ఆల్సస్టర్",
                    "lang" : "te",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : ", ఓర్మేస్బీ సెయింట్ మార్గరెట్ విత్ స్క్రాట్బీ@te",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : ", ఓర్మేస్బీ సెయింట్ మార్గరెట్ విత్ స్క్రాట్బీ",
                    "lang" : "te",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : ", బార్న్బిన్ డన్ విత్ కిర్క్ సాండల్@te",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : ", బార్న్బిన్ డన్ విత్ కిర్క్ సాండల్",
                    "lang" : "te",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : ", బిషప్ టాచ్బ్రూక్@te",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : ", బిషప్ టాచ్బ్రూక్",
                    "lang" : "te",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : ", మార్టిన్ హుస్సింగ్ట్రీ@te",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : ", మార్టిన్ హుస్సింగ్ట్రీ",
                    "lang" : "te",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "HIRAGANA",
          "doc_count" : 47695,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 47695,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "JAあきた北@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "JAあきた北",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "JAとうかつ中央@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "JAとうかつ中央",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "JAなめがたしおさい@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "JAなめがたしおさい",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "JAみどり本店@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "JAみどり本店",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "JA秋田たかのす@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "JA秋田たかのす",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "TAMIL",
          "doc_count" : 47748,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 47748,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "San லோரன்சோ@ta",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "San லோரன்சோ",
                    "lang" : "ta",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ʻஐயா@ta",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ʻஐயா",
                    "lang" : "ta",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "NIT திருச்சி@ta",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "NIT திருச்சி",
                    "lang" : "ta",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "R.புதுப்பட்டி@ta",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "R.புதுப்பட்டி",
                    "lang" : "ta",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Kasai-அஸிடெண்ட்டால்@ta",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Kasai-அஸிடெண்ட்டால்",
                    "lang" : "ta",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "MALAYALAM",
          "doc_count" : 53570,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 53570,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "10 ജൻപഥ്@ml",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "10 ജൻപഥ്",
                    "lang" : "ml",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "1000 ഏക്കർ@ml",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "1000 ഏക്കർ",
                    "lang" : "ml",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "12-ാം മൈൽ വാർഡ്@ml",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "12-ാം മൈൽ വാർഡ്",
                    "lang" : "ml",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "14-ാമത്തെ നിയമസഭാ ജില്ല@ml",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "14-ാമത്തെ നിയമസഭാ ജില്ല",
                    "lang" : "ml",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "1922-ലെ റാംപ ലഹള@ml",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "1922-ലെ റാംപ ലഹള",
                    "lang" : "ml",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "GEORGIAN",
          "doc_count" : 93334,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 93334,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "9 აპრილის სახელობის ბაღი@ka",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "9 აპრილის სახელობის ბაღი",
                    "lang" : "ka",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "V რაიონი@ka",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "V რაიონი",
                    "lang" : "ka",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "VI რაიონი@ka",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "VI რაიონი",
                    "lang" : "ka",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "VII ტურნირი მსოფლიოს პირველობაზე ჭადრაკში ქალთა შორის@ka",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "VII ტურნირი მსოფლიოს პირველობაზე ჭადრაკში ქალთა შორის",
                    "lang" : "ka",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "VIII საზონთაშორისო ტურნირი@ka",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "VIII საზონთაშორისო ტურნირი",
                    "lang" : "ka",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "BENGALI",
          "doc_count" : 97931,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 97931,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "\"খ্রীষ্ট চার্চ@bn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "\"খ্রীষ্ট চার্চ",
                    "lang" : "bn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "BAPS শ্রী স্বামীনারায়ণ মন্দির লন্ডন@bn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "BAPS শ্রী স্বামীনারায়ণ মন্দির লন্ডন",
                    "lang" : "bn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "vরূপসা@bn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "vরূপসা",
                    "lang" : "bn",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "আইজাক@bn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "আইজাক",
                    "lang" : "bn",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "আইজেনস্টাট@bn",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "আইজেনস্টাট",
                    "lang" : "bn",
                    "primary_namespace" : "gn"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "DEVANAGARI",
          "doc_count" : 131785,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 131785,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Mrs. डाउटफायर@hi",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Mrs. डाउटफायर",
                    "lang" : "hi",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "IONIS शिक्षा समूह@hi",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "IONIS शिक्षा समूह",
                    "lang" : "hi",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "PT कॊत्तुरु@hi",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "PT कॊत्तुरु",
                    "lang" : "hi",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "एपोलो बीच@new",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "एपोलो बीच",
                    "lang" : "new",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "एप्पल पार्क@hi",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "एप्पल पार्क",
                    "lang" : "hi",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "HEBREW",
          "doc_count" : 133232,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 133232,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "ַאמאַהא@yi",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ַאמאַהא",
                    "lang" : "yi",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "א דהי@he",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "א דהי",
                    "lang" : "he",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "א טיבה@he",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "א טיבה",
                    "lang" : "he",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "א נבי יוסף@he",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "א נבי יוסף",
                    "lang" : "he",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "א שיח נבהן@he",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "א שיח נבהן",
                    "lang" : "he",
                    "primary_namespace" : "gn"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "ARMENIAN",
          "doc_count" : 148266,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 148266,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Վոլնովախայի մոտ ավտոբուսի գնդակոծում@hy",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Վոլնովախայի մոտ ավտոբուսի գնդակոծում",
                    "lang" : "hy",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Վոլշայմ@hy",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Վոլշայմ",
                    "lang" : "hy",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Վոլոգդա@hy",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Վոլոգդա",
                    "lang" : "hy",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Վոլոգդայի նահանգ@hy",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Վոլոգդայի նահանգ",
                    "lang" : "hy",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Վոլոժկա@hy",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Վոլոժկա",
                    "lang" : "hy",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "GREEK",
          "doc_count" : 172879,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 172879,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Möja υπαίθρια@el",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Möja υπαίθρια",
                    "lang" : "el",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Mάρλοου@el",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Mάρλοου",
                    "lang" : "el",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Mονή της Παναγίας Σκριπούς@el",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Mονή της Παναγίας Σκριπούς",
                    "lang" : "el",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Modigliani Παραδοσιακό Καφενείο@el",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Modigliani Παραδοσιακό Καφενείο",
                    "lang" : "el",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "68o Διεθνές Φεστιβάλ Κινηματογράφου Βενετίας@el",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "68o Διεθνές Φεστιβάλ Κινηματογράφου Βενετίας",
                    "lang" : "el",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "THAI",
          "doc_count" : 211956,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 211956,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Myrudden พื้นที่บาร์บีคิว@th",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Myrudden พื้นที่บาร์บีคิว",
                    "lang" : "th",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "9 เดอคาลบ์อเวนิว@th",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "9 เดอคาลบ์อเวนิว",
                    "lang" : "th",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "901 แลนด์@th",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "901 แลนด์",
                    "lang" : "th",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "111 ซัมเมอร์เซต@th",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "111 ซัมเมอร์เซต",
                    "lang" : "th",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "23 เขตในโตเกียว@th",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "23 เขตในโตเกียว",
                    "lang" : "th",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "OTHER",
          "doc_count" : 243259,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 243259,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "۵@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "۵",
                    "lang" : "ar",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ܐܘܙܒܩܣܛܐܢ@arc",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ܐܘܙܒܩܣܛܐܢ",
                    "lang" : "arc",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ܐܘܚܕܢܐ ܦܪܢܣܝܐ ܩܕܡܝܐ@arc",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ܐܘܚܕܢܐ ܦܪܢܣܝܐ ܩܕܡܝܐ",
                    "lang" : "arc",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ܐܘܛܪܝܟܛ@arc",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ܐܘܛܪܝܟܛ",
                    "lang" : "arc",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "ܐܘܣܠܘ@arc",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "ܐܘܣܠܘ",
                    "lang" : "arc",
                    "primary_namespace" : "gn"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "HANGUL",
          "doc_count" : 270407,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 270407,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Lidö 자연 보호 구역@ko",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Lidö 자연 보호 구역",
                    "lang" : "ko",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "M6 모터웨이@ko",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "M6 모터웨이",
                    "lang" : "ko",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "M2 모터웨이@ko",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "M2 모터웨이",
                    "lang" : "ko",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "MBC 여의도 방송센터@ko",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "MBC 여의도 방송센터",
                    "lang" : "ko",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "MS 레드몬드 캠퍼스@ko",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "MS 레드몬드 캠퍼스",
                    "lang" : "ko",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "KATAKANA",
          "doc_count" : 310691,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 310691,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "M・K・チュルリョーニス美術館@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "M・K・チュルリョーニス美術館",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "N19（アイルランド）@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "N19（アイルランド）",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "NATOガイレンキルヒェン航空基地@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "NATOガイレンキルヒェン航空基地",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "NAVER グリーンファクトリー@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "NAVER グリーンファクトリー",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "NEオクラホマA&M大学@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "NEオクラホマA&M大学",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "ARABIC",
          "doc_count" : 1751075,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 1751075,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Kålgårdsöns المحمية الطبيعية ، منطقة الشواء@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Kålgårdsöns المحمية الطبيعية ، منطقة الشواء",
                    "lang" : "ar",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Kålgårdsöns المحمية الطبيعية@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Kålgårdsöns المحمية الطبيعية",
                    "lang" : "ar",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "\"المسجد الجديد\"@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "\"المسجد الجديد\"",
                    "lang" : "ar",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "\"مسجد النافورة السفلى\"@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "\"مسجد النافورة السفلى\"",
                    "lang" : "ar",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "\"همدراشا\"@ar",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "\"همدراشا\"",
                    "lang" : "ar",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "CJK",
          "doc_count" : 1857832,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 1857832,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "SNO地下实验室@zh",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "SNO地下实验室",
                    "lang" : "zh",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "任家里村@zh",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "任家里村",
                    "lang" : "zh",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "任寡妇湾@zh",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "任寡妇湾",
                    "lang" : "zh",
                    "primary_namespace" : "gn"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "任實駅@ja",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "任實駅",
                    "lang" : "ja",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "任寨新村@zh",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "任寨新村",
                    "lang" : "zh",
                    "primary_namespace" : "gn"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "CYRILLIC",
          "doc_count" : 2903698,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 2903698,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Дæллаг Серги@os",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Дæллаг Серги",
                    "lang" : "os",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Дæллаг Тунгускæ@os",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Дæллаг Тунгускæ",
                    "lang" : "os",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Дæллаг Франкони@os",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Дæллаг Франкони",
                    "lang" : "os",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Дæллаг Чъала@os",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Дæллаг Чъала",
                    "lang" : "os",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Дæлмандатон Палестинæ@os",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Дæлмандатон Палестинæ",
                    "lang" : "os",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        },
        {
          "key" : "LATIN",
          "doc_count" : 50068496,
          "sample_toponyms" : {
            "hits" : {
              "total" : {
                "value" : 50068496,
                "relation" : "eq"
              },
              "max_score" : 0.0,
              "hits" : [
                {
                  "_index" : "toponyms",
                  "_id" : "Oravsky Biely Potok@it",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Oravsky Biely Potok",
                    "lang" : "it",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Oravsky Podzamok@it",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Oravsky Podzamok",
                    "lang" : "it",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Oravsky Podzamok@nl",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Oravsky Podzamok",
                    "lang" : "nl",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Oravská Jasenica@de",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Oravská Jasenica",
                    "lang" : "de",
                    "primary_namespace" : "wd"
                  }
                },
                {
                  "_index" : "toponyms",
                  "_id" : "Oravská Jasenica@ro",
                  "_score" : 0.0,
                  "_source" : {
                    "name" : "Oravská Jasenica",
                    "lang" : "ro",
                    "primary_namespace" : "wd"
                  }
                }
              ]
            }
          }
        }
      ]
    }
  }
}
```

## Wikidata's Contribution to Non-Latin Scripts

Critical for assessing whether Wikidata provides the necessary coverage for South/Southeast Asian scripts:

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 322,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "non_latin_scripts" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "CYRILLIC",
          "doc_count" : 2086035,
          "languages" : {
            "doc_count_error_upper_bound" : 461,
            "sum_other_doc_count" : 29342,
            "buckets" : [
              {
                "key" : "ru",
                "doc_count" : 432212
              },
              {
                "key" : "uk",
                "doc_count" : 344426
              },
              {
                "key" : "ce",
                "doc_count" : 263504
              },
              {
                "key" : "tt",
                "doc_count" : 224916
              },
              {
                "key" : "bg",
                "doc_count" : 204826
              },
              {
                "key" : "sr",
                "doc_count" : 190530
              },
              {
                "key" : "be",
                "doc_count" : 122688
              },
              {
                "key" : "kk",
                "doc_count" : 61264
              },
              {
                "key" : "mk",
                "doc_count" : 34868
              },
              {
                "key" : "uz",
                "doc_count" : 34774
              },
              {
                "key" : "ba",
                "doc_count" : 32772
              },
              {
                "key" : "tg",
                "doc_count" : 30230
              },
              {
                "key" : "cv",
                "doc_count" : 23066
              },
              {
                "key" : "ky",
                "doc_count" : 21582
              },
              {
                "key" : "os",
                "doc_count" : 10600
              },
              {
                "key" : "ceb",
                "doc_count" : 8297
              },
              {
                "key" : "mdf",
                "doc_count" : 4744
              },
              {
                "key" : "mn",
                "doc_count" : 3832
              },
              {
                "key" : "myv",
                "doc_count" : 3787
              },
              {
                "key" : "mhr",
                "doc_count" : 3775
              }
            ]
          }
        },
        {
          "key" : "CJK",
          "doc_count" : 1066744,
          "languages" : {
            "doc_count_error_upper_bound" : 7,
            "sum_other_doc_count" : 411,
            "buckets" : [
              {
                "key" : "zh",
                "doc_count" : 664842
              },
              {
                "key" : "ja",
                "doc_count" : 271653
              },
              {
                "key" : "wuu",
                "doc_count" : 46735
              },
              {
                "key" : "gan",
                "doc_count" : 36948
              },
              {
                "key" : "yue",
                "doc_count" : 28749
              },
              {
                "key" : "mul",
                "doc_count" : 7576
              },
              {
                "key" : "lzh",
                "doc_count" : 3189
              },
              {
                "key" : "fr",
                "doc_count" : 2044
              },
              {
                "key" : "ko",
                "doc_count" : 1861
              },
              {
                "key" : "nan",
                "doc_count" : 1504
              },
              {
                "key" : "en",
                "doc_count" : 366
              },
              {
                "key" : "sr",
                "doc_count" : 243
              },
              {
                "key" : "ryu",
                "doc_count" : 204
              },
              {
                "key" : "de",
                "doc_count" : 107
              },
              {
                "key" : "sk",
                "doc_count" : 64
              },
              {
                "key" : "mk",
                "doc_count" : 61
              },
              {
                "key" : "ca",
                "doc_count" : 50
              },
              {
                "key" : "id",
                "doc_count" : 49
              },
              {
                "key" : "es",
                "doc_count" : 45
              },
              {
                "key" : "cs",
                "doc_count" : 43
              }
            ]
          }
        },
        {
          "key" : "ARABIC",
          "doc_count" : 1039441,
          "languages" : {
            "doc_count_error_upper_bound" : 17,
            "sum_other_doc_count" : 2467,
            "buckets" : [
              {
                "key" : "fa",
                "doc_count" : 274191
              },
              {
                "key" : "arz",
                "doc_count" : 249443
              },
              {
                "key" : "ar",
                "doc_count" : 183388
              },
              {
                "key" : "azb",
                "doc_count" : 95694
              },
              {
                "key" : "ur",
                "doc_count" : 82006
              },
              {
                "key" : "kk",
                "doc_count" : 46076
              },
              {
                "key" : "glk",
                "doc_count" : 29058
              },
              {
                "key" : "mzn",
                "doc_count" : 25699
              },
              {
                "key" : "pnb",
                "doc_count" : 15736
              },
              {
                "key" : "ckb",
                "doc_count" : 13054
              },
              {
                "key" : "ps",
                "doc_count" : 4182
              },
              {
                "key" : "ary",
                "doc_count" : 4006
              },
              {
                "key" : "ks",
                "doc_count" : 3408
              },
              {
                "key" : "ku",
                "doc_count" : 3005
              },
              {
                "key" : "sd",
                "doc_count" : 2875
              },
              {
                "key" : "ms",
                "doc_count" : 1917
              },
              {
                "key" : "ug",
                "doc_count" : 1821
              },
              {
                "key" : "lrc",
                "doc_count" : 522
              },
              {
                "key" : "skr",
                "doc_count" : 481
              },
              {
                "key" : "az",
                "doc_count" : 412
              }
            ]
          }
        },
        {
          "key" : "KATAKANA",
          "doc_count" : 246486,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 7,
            "buckets" : [
              {
                "key" : "ja",
                "doc_count" : 246229
              },
              {
                "key" : "zh",
                "doc_count" : 69
              },
              {
                "key" : "ryu",
                "doc_count" : 57
              },
              {
                "key" : "mk",
                "doc_count" : 36
              },
              {
                "key" : "en",
                "doc_count" : 32
              },
              {
                "key" : "mul",
                "doc_count" : 11
              },
              {
                "key" : "nl",
                "doc_count" : 10
              },
              {
                "key" : "fr",
                "doc_count" : 7
              },
              {
                "key" : "ceb",
                "doc_count" : 6
              },
              {
                "key" : "ko",
                "doc_count" : 4
              },
              {
                "key" : "de",
                "doc_count" : 3
              },
              {
                "key" : "es",
                "doc_count" : 3
              },
              {
                "key" : "yue",
                "doc_count" : 3
              },
              {
                "key" : "pt",
                "doc_count" : 2
              },
              {
                "key" : "tl",
                "doc_count" : 2
              },
              {
                "key" : "be",
                "doc_count" : 1
              },
              {
                "key" : "cs",
                "doc_count" : 1
              },
              {
                "key" : "da",
                "doc_count" : 1
              },
              {
                "key" : "id",
                "doc_count" : 1
              },
              {
                "key" : "km",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "GREEK",
          "doc_count" : 140141,
          "languages" : {
            "doc_count_error_upper_bound" : 3,
            "sum_other_doc_count" : 122,
            "buckets" : [
              {
                "key" : "el",
                "doc_count" : 137165
              },
              {
                "key" : "grc",
                "doc_count" : 1042
              },
              {
                "key" : "pnt",
                "doc_count" : 540
              },
              {
                "key" : "es",
                "doc_count" : 339
              },
              {
                "key" : "en",
                "doc_count" : 247
              },
              {
                "key" : "fr",
                "doc_count" : 110
              },
              {
                "key" : "sl",
                "doc_count" : 90
              },
              {
                "key" : "de",
                "doc_count" : 85
              },
              {
                "key" : "ru",
                "doc_count" : 83
              },
              {
                "key" : "sr",
                "doc_count" : 74
              },
              {
                "key" : "cs",
                "doc_count" : 48
              },
              {
                "key" : "tr",
                "doc_count" : 38
              },
              {
                "key" : "mk",
                "doc_count" : 33
              },
              {
                "key" : "mul",
                "doc_count" : 33
              },
              {
                "key" : "hu",
                "doc_count" : 18
              },
              {
                "key" : "nn",
                "doc_count" : 16
              },
              {
                "key" : "eo",
                "doc_count" : 15
              },
              {
                "key" : "it",
                "doc_count" : 15
              },
              {
                "key" : "ca",
                "doc_count" : 14
              },
              {
                "key" : "gl",
                "doc_count" : 14
              }
            ]
          }
        },
        {
          "key" : "OTHER",
          "doc_count" : 132043,
          "languages" : {
            "doc_count_error_upper_bound" : 93,
            "sum_other_doc_count" : 7954,
            "buckets" : [
              {
                "key" : "my",
                "doc_count" : 28017
              },
              {
                "key" : "en",
                "doc_count" : 25546
              },
              {
                "key" : "pa",
                "doc_count" : 13109
              },
              {
                "key" : "sat",
                "doc_count" : 12390
              },
              {
                "key" : "si",
                "doc_count" : 11139
              },
              {
                "key" : "bo",
                "doc_count" : 6489
              },
              {
                "key" : "or",
                "doc_count" : 6101
              },
              {
                "key" : "shn",
                "doc_count" : 5797
              },
              {
                "key" : "km",
                "doc_count" : 2496
              },
              {
                "key" : "fr",
                "doc_count" : 2059
              },
              {
                "key" : "am",
                "doc_count" : 1810
              },
              {
                "key" : "mn",
                "doc_count" : 1548
              },
              {
                "key" : "lo",
                "doc_count" : 1235
              },
              {
                "key" : "tr",
                "doc_count" : 1226
              },
              {
                "key" : "nqo",
                "doc_count" : 1087
              },
              {
                "key" : "de",
                "doc_count" : 968
              },
              {
                "key" : "pt",
                "doc_count" : 887
              },
              {
                "key" : "blk",
                "doc_count" : 751
              },
              {
                "key" : "zgh",
                "doc_count" : 746
              },
              {
                "key" : "mni",
                "doc_count" : 688
              }
            ]
          }
        },
        {
          "key" : "HANGUL",
          "doc_count" : 125783,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 13,
            "buckets" : [
              {
                "key" : "ko",
                "doc_count" : 125475
              },
              {
                "key" : "sk",
                "doc_count" : 87
              },
              {
                "key" : "mul",
                "doc_count" : 38
              },
              {
                "key" : "en",
                "doc_count" : 35
              },
              {
                "key" : "mk",
                "doc_count" : 33
              },
              {
                "key" : "fr",
                "doc_count" : 23
              },
              {
                "key" : "de",
                "doc_count" : 14
              },
              {
                "key" : "zh",
                "doc_count" : 14
              },
              {
                "key" : "ceb",
                "doc_count" : 12
              },
              {
                "key" : "hu",
                "doc_count" : 11
              },
              {
                "key" : "cs",
                "doc_count" : 6
              },
              {
                "key" : "ar",
                "doc_count" : 3
              },
              {
                "key" : "ca",
                "doc_count" : 3
              },
              {
                "key" : "es",
                "doc_count" : 3
              },
              {
                "key" : "ja",
                "doc_count" : 3
              },
              {
                "key" : "nb",
                "doc_count" : 3
              },
              {
                "key" : "bn",
                "doc_count" : 2
              },
              {
                "key" : "id",
                "doc_count" : 2
              },
              {
                "key" : "yue",
                "doc_count" : 2
              },
              {
                "key" : "ast",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "DEVANAGARI",
          "doc_count" : 113553,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 46,
            "buckets" : [
              {
                "key" : "hi",
                "doc_count" : 54928
              },
              {
                "key" : "mr",
                "doc_count" : 19515
              },
              {
                "key" : "new",
                "doc_count" : 13099
              },
              {
                "key" : "ne",
                "doc_count" : 9201
              },
              {
                "key" : "mai",
                "doc_count" : 5109
              },
              {
                "key" : "bho",
                "doc_count" : 4924
              },
              {
                "key" : "sa",
                "doc_count" : 2692
              },
              {
                "key" : "dty",
                "doc_count" : 1079
              },
              {
                "key" : "awa",
                "doc_count" : 887
              },
              {
                "key" : "pi",
                "doc_count" : 591
              },
              {
                "key" : "anp",
                "doc_count" : 478
              },
              {
                "key" : "gom",
                "doc_count" : 311
              },
              {
                "key" : "fr",
                "doc_count" : 289
              },
              {
                "key" : "mag",
                "doc_count" : 279
              },
              {
                "key" : "en",
                "doc_count" : 43
              },
              {
                "key" : "mk",
                "doc_count" : 39
              },
              {
                "key" : "bn",
                "doc_count" : 20
              },
              {
                "key" : "sr",
                "doc_count" : 10
              },
              {
                "key" : "de",
                "doc_count" : 7
              },
              {
                "key" : "ks",
                "doc_count" : 6
              }
            ]
          }
        },
        {
          "key" : "HEBREW",
          "doc_count" : 111170,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 57,
            "buckets" : [
              {
                "key" : "he",
                "doc_count" : 106767
              },
              {
                "key" : "yi",
                "doc_count" : 3929
              },
              {
                "key" : "en",
                "doc_count" : 63
              },
              {
                "key" : "mk",
                "doc_count" : 51
              },
              {
                "key" : "sr",
                "doc_count" : 41
              },
              {
                "key" : "lad",
                "doc_count" : 40
              },
              {
                "key" : "mul",
                "doc_count" : 30
              },
              {
                "key" : "de",
                "doc_count" : 24
              },
              {
                "key" : "es",
                "doc_count" : 24
              },
              {
                "key" : "fr",
                "doc_count" : 21
              },
              {
                "key" : "cs",
                "doc_count" : 19
              },
              {
                "key" : "eo",
                "doc_count" : 19
              },
              {
                "key" : "sco",
                "doc_count" : 13
              },
              {
                "key" : "pt",
                "doc_count" : 12
              },
              {
                "key" : "ar",
                "doc_count" : 11
              },
              {
                "key" : "it",
                "doc_count" : 11
              },
              {
                "key" : "nl",
                "doc_count" : 11
              },
              {
                "key" : "hu",
                "doc_count" : 10
              },
              {
                "key" : "ro",
                "doc_count" : 9
              },
              {
                "key" : "ca",
                "doc_count" : 8
              }
            ]
          }
        },
        {
          "key" : "ARMENIAN",
          "doc_count" : 109993,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 23,
            "buckets" : [
              {
                "key" : "hy",
                "doc_count" : 105935
              },
              {
                "key" : "hyw",
                "doc_count" : 3866
              },
              {
                "key" : "en",
                "doc_count" : 29
              },
              {
                "key" : "az",
                "doc_count" : 26
              },
              {
                "key" : "mk",
                "doc_count" : 21
              },
              {
                "key" : "sr",
                "doc_count" : 18
              },
              {
                "key" : "de",
                "doc_count" : 12
              },
              {
                "key" : "eo",
                "doc_count" : 8
              },
              {
                "key" : "fr",
                "doc_count" : 8
              },
              {
                "key" : "sco",
                "doc_count" : 8
              },
              {
                "key" : "ku",
                "doc_count" : 7
              },
              {
                "key" : "nn",
                "doc_count" : 5
              },
              {
                "key" : "ru",
                "doc_count" : 5
              },
              {
                "key" : "cs",
                "doc_count" : 4
              },
              {
                "key" : "ka",
                "doc_count" : 4
              },
              {
                "key" : "mul",
                "doc_count" : 4
              },
              {
                "key" : "pl",
                "doc_count" : 3
              },
              {
                "key" : "tr",
                "doc_count" : 3
              },
              {
                "key" : "ar",
                "doc_count" : 2
              },
              {
                "key" : "ast",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "BENGALI",
          "doc_count" : 86808,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 4,
            "buckets" : [
              {
                "key" : "bn",
                "doc_count" : 72558
              },
              {
                "key" : "bpy",
                "doc_count" : 12220
              },
              {
                "key" : "as",
                "doc_count" : 1931
              },
              {
                "key" : "en",
                "doc_count" : 27
              },
              {
                "key" : "mk",
                "doc_count" : 14
              },
              {
                "key" : "hi",
                "doc_count" : 13
              },
              {
                "key" : "mul",
                "doc_count" : 9
              },
              {
                "key" : "mni",
                "doc_count" : 7
              },
              {
                "key" : "syl",
                "doc_count" : 5
              },
              {
                "key" : "ar",
                "doc_count" : 3
              },
              {
                "key" : "es",
                "doc_count" : 3
              },
              {
                "key" : "yue",
                "doc_count" : 3
              },
              {
                "key" : "de",
                "doc_count" : 2
              },
              {
                "key" : "fr",
                "doc_count" : 2
              },
              {
                "key" : "my",
                "doc_count" : 2
              },
              {
                "key" : "bg",
                "doc_count" : 1
              },
              {
                "key" : "bo",
                "doc_count" : 1
              },
              {
                "key" : "ca",
                "doc_count" : 1
              },
              {
                "key" : "eo",
                "doc_count" : 1
              },
              {
                "key" : "or",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "GEORGIAN",
          "doc_count" : 75909,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 7,
            "buckets" : [
              {
                "key" : "ka",
                "doc_count" : 69602
              },
              {
                "key" : "xmf",
                "doc_count" : 6106
              },
              {
                "key" : "sr",
                "doc_count" : 47
              },
              {
                "key" : "mul",
                "doc_count" : 33
              },
              {
                "key" : "mk",
                "doc_count" : 32
              },
              {
                "key" : "en",
                "doc_count" : 19
              },
              {
                "key" : "fr",
                "doc_count" : 13
              },
              {
                "key" : "uk",
                "doc_count" : 8
              },
              {
                "key" : "hu",
                "doc_count" : 7
              },
              {
                "key" : "ru",
                "doc_count" : 7
              },
              {
                "key" : "de",
                "doc_count" : 6
              },
              {
                "key" : "ca",
                "doc_count" : 5
              },
              {
                "key" : "az",
                "doc_count" : 3
              },
              {
                "key" : "sl",
                "doc_count" : 3
              },
              {
                "key" : "dty",
                "doc_count" : 2
              },
              {
                "key" : "id",
                "doc_count" : 2
              },
              {
                "key" : "nn",
                "doc_count" : 2
              },
              {
                "key" : "pl",
                "doc_count" : 2
              },
              {
                "key" : "sco",
                "doc_count" : 2
              },
              {
                "key" : "ab",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "MALAYALAM",
          "doc_count" : 51447,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "ml",
                "doc_count" : 51423
              },
              {
                "key" : "mk",
                "doc_count" : 9
              },
              {
                "key" : "en",
                "doc_count" : 6
              },
              {
                "key" : "hi",
                "doc_count" : 4
              },
              {
                "key" : "bn",
                "doc_count" : 2
              },
              {
                "key" : "ar",
                "doc_count" : 1
              },
              {
                "key" : "pt",
                "doc_count" : 1
              },
              {
                "key" : "te",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "THAI",
          "doc_count" : 45475,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 7,
            "buckets" : [
              {
                "key" : "th",
                "doc_count" : 45332
              },
              {
                "key" : "mk",
                "doc_count" : 26
              },
              {
                "key" : "en",
                "doc_count" : 20
              },
              {
                "key" : "fr",
                "doc_count" : 16
              },
              {
                "key" : "id",
                "doc_count" : 10
              },
              {
                "key" : "nl",
                "doc_count" : 9
              },
              {
                "key" : "de",
                "doc_count" : 7
              },
              {
                "key" : "lzh",
                "doc_count" : 7
              },
              {
                "key" : "sr",
                "doc_count" : 7
              },
              {
                "key" : "yue",
                "doc_count" : 7
              },
              {
                "key" : "zh",
                "doc_count" : 7
              },
              {
                "key" : "nod",
                "doc_count" : 6
              },
              {
                "key" : "wuu",
                "doc_count" : 3
              },
              {
                "key" : "hi",
                "doc_count" : 2
              },
              {
                "key" : "la",
                "doc_count" : 2
              },
              {
                "key" : "lo",
                "doc_count" : 2
              },
              {
                "key" : "nan",
                "doc_count" : 2
              },
              {
                "key" : "bn",
                "doc_count" : 1
              },
              {
                "key" : "cs",
                "doc_count" : 1
              },
              {
                "key" : "it",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "TELUGU",
          "doc_count" : 43912,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "te",
                "doc_count" : 43883
              },
              {
                "key" : "mul",
                "doc_count" : 6
              },
              {
                "key" : "hi",
                "doc_count" : 5
              },
              {
                "key" : "mk",
                "doc_count" : 5
              },
              {
                "key" : "es",
                "doc_count" : 2
              },
              {
                "key" : "nl",
                "doc_count" : 2
              },
              {
                "key" : "zh",
                "doc_count" : 2
              },
              {
                "key" : "ar",
                "doc_count" : 1
              },
              {
                "key" : "ast",
                "doc_count" : 1
              },
              {
                "key" : "en",
                "doc_count" : 1
              },
              {
                "key" : "fr",
                "doc_count" : 1
              },
              {
                "key" : "lld",
                "doc_count" : 1
              },
              {
                "key" : "or",
                "doc_count" : 1
              },
              {
                "key" : "sr",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "TAMIL",
          "doc_count" : 42309,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "ta",
                "doc_count" : 42265
              },
              {
                "key" : "mk",
                "doc_count" : 14
              },
              {
                "key" : "en",
                "doc_count" : 8
              },
              {
                "key" : "ja",
                "doc_count" : 5
              },
              {
                "key" : "sr",
                "doc_count" : 4
              },
              {
                "key" : "mul",
                "doc_count" : 3
              },
              {
                "key" : "fr",
                "doc_count" : 2
              },
              {
                "key" : "nn",
                "doc_count" : 2
              },
              {
                "key" : "yue",
                "doc_count" : 2
              },
              {
                "key" : "hi",
                "doc_count" : 1
              },
              {
                "key" : "ml",
                "doc_count" : 1
              },
              {
                "key" : "te",
                "doc_count" : 1
              },
              {
                "key" : "zh",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "KANNADA",
          "doc_count" : 17758,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "kn",
                "doc_count" : 17366
              },
              {
                "key" : "tcy",
                "doc_count" : 344
              },
              {
                "key" : "gom",
                "doc_count" : 24
              },
              {
                "key" : "mk",
                "doc_count" : 9
              },
              {
                "key" : "bn",
                "doc_count" : 5
              },
              {
                "key" : "en",
                "doc_count" : 3
              },
              {
                "key" : "de",
                "doc_count" : 2
              },
              {
                "key" : "es",
                "doc_count" : 2
              },
              {
                "key" : "hi",
                "doc_count" : 1
              },
              {
                "key" : "km",
                "doc_count" : 1
              },
              {
                "key" : "te",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "GUJARATI",
          "doc_count" : 16809,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "gu",
                "doc_count" : 16803
              },
              {
                "key" : "mk",
                "doc_count" : 2
              },
              {
                "key" : "ast",
                "doc_count" : 1
              },
              {
                "key" : "en",
                "doc_count" : 1
              },
              {
                "key" : "hi",
                "doc_count" : 1
              },
              {
                "key" : "nl",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "HIRAGANA",
          "doc_count" : 6079,
          "languages" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "ja",
                "doc_count" : 5981
              },
              {
                "key" : "ryu",
                "doc_count" : 40
              },
              {
                "key" : "yue",
                "doc_count" : 15
              },
              {
                "key" : "zh",
                "doc_count" : 14
              },
              {
                "key" : "fr",
                "doc_count" : 8
              },
              {
                "key" : "ceb",
                "doc_count" : 6
              },
              {
                "key" : "sr",
                "doc_count" : 4
              },
              {
                "key" : "de",
                "doc_count" : 2
              },
              {
                "key" : "id",
                "doc_count" : 2
              },
              {
                "key" : "ca",
                "doc_count" : 1
              },
              {
                "key" : "lt",
                "doc_count" : 1
              },
              {
                "key" : "lzh",
                "doc_count" : 1
              },
              {
                "key" : "pl",
                "doc_count" : 1
              },
              {
                "key" : "ru",
                "doc_count" : 1
              },
              {
                "key" : "sgs",
                "doc_count" : 1
              },
              {
                "key" : "vi",
                "doc_count" : 1
              }
            ]
          }
        }
      ]
    }
  }
}
```

## Cross-Script Place Linkage Potential

Estimates how many places have toponyms in multiple scripts (required for training pairs):

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/places/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
GET /places/_search
{
  "took" : 402,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "script_diversity" : {
      "doc_count" : 88115002,
      "scripts_per_place" : {
        "value" : 0
      }
    }
  }
}
```

## Language Coverage by Authority

Detailed breakdown of which authorities contribute which languages:

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
}'
```
```json
{
  "took" : 78,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "by_namespace" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "wd",
          "doc_count" : 40341464,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 34783569
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 2086035
              },
              {
                "key" : "CJK",
                "doc_count" : 1066744
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1039441
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 246486
              },
              {
                "key" : "GREEK",
                "doc_count" : 140141
              },
              {
                "key" : "OTHER",
                "doc_count" : 132043
              },
              {
                "key" : "HANGUL",
                "doc_count" : 125783
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 113553
              },
              {
                "key" : "HEBREW",
                "doc_count" : 111170
              },
              {
                "key" : "ARMENIAN",
                "doc_count" : 109993
              },
              {
                "key" : "BENGALI",
                "doc_count" : 86808
              },
              {
                "key" : "GEORGIAN",
                "doc_count" : 75909
              },
              {
                "key" : "MALAYALAM",
                "doc_count" : 51447
              },
              {
                "key" : "THAI",
                "doc_count" : 45475
              },
              {
                "key" : "TELUGU",
                "doc_count" : 43912
              },
              {
                "key" : "TAMIL",
                "doc_count" : 42309
              },
              {
                "key" : "KANNADA",
                "doc_count" : 17758
              },
              {
                "key" : "GUJARATI",
                "doc_count" : 16809
              },
              {
                "key" : "HIRAGANA",
                "doc_count" : 6079
              }
            ]
          },
          "languages" : {
            "doc_count_error_upper_bound" : 83734,
            "sum_other_doc_count" : 6973175,
            "buckets" : [
              {
                "key" : "en",
                "doc_count" : 7223128
              },
              {
                "key" : "ceb",
                "doc_count" : 2418737
              },
              {
                "key" : "nl",
                "doc_count" : 2209474
              },
              {
                "key" : "fr",
                "doc_count" : 2137282
              },
              {
                "key" : "de",
                "doc_count" : 1947515
              },
              {
                "key" : "sv",
                "doc_count" : 1648496
              },
              {
                "key" : "es",
                "doc_count" : 1169708
              },
              {
                "key" : "tr",
                "doc_count" : 782750
              },
              {
                "key" : "it",
                "doc_count" : 745932
              },
              {
                "key" : "zh",
                "doc_count" : 676908
              },
              {
                "key" : "ga",
                "doc_count" : 650181
              },
              {
                "key" : "id",
                "doc_count" : 649566
              },
              {
                "key" : "pl",
                "doc_count" : 602737
              },
              {
                "key" : "cs",
                "doc_count" : 582395
              },
              {
                "key" : "pt",
                "doc_count" : 558954
              },
              {
                "key" : "ca",
                "doc_count" : 543287
              },
              {
                "key" : "ja",
                "doc_count" : 534190
              },
              {
                "key" : "ru",
                "doc_count" : 444237
              },
              {
                "key" : "nb",
                "doc_count" : 421663
              },
              {
                "key" : "uk",
                "doc_count" : 353117
              },
              {
                "key" : "sr",
                "doc_count" : 351088
              },
              {
                "key" : "ro",
                "doc_count" : 322094
              },
              {
                "key" : "eu",
                "doc_count" : 321678
              },
              {
                "key" : "nn",
                "doc_count" : 303882
              },
              {
                "key" : "ast",
                "doc_count" : 298103
              },
              {
                "key" : "fi",
                "doc_count" : 291526
              },
              {
                "key" : "fa",
                "doc_count" : 281461
              },
              {
                "key" : "da",
                "doc_count" : 280206
              },
              {
                "key" : "nan",
                "doc_count" : 270867
              },
              {
                "key" : "ce",
                "doc_count" : 264234
              },
              {
                "key" : "eo",
                "doc_count" : 260002
              },
              {
                "key" : "arz",
                "doc_count" : 249609
              },
              {
                "key" : "sl",
                "doc_count" : 242288
              },
              {
                "key" : "ms",
                "doc_count" : 236934
              },
              {
                "key" : "hu",
                "doc_count" : 228303
              },
              {
                "key" : "tt",
                "doc_count" : 227550
              },
              {
                "key" : "vi",
                "doc_count" : 219436
              },
              {
                "key" : "sh",
                "doc_count" : 218217
              },
              {
                "key" : "cy",
                "doc_count" : 217471
              },
              {
                "key" : "sk",
                "doc_count" : 206439
              },
              {
                "key" : "bg",
                "doc_count" : 205741
              },
              {
                "key" : "mul",
                "doc_count" : 200936
              },
              {
                "key" : "oc",
                "doc_count" : 190851
              },
              {
                "key" : "ar",
                "doc_count" : 185286
              },
              {
                "key" : "uz",
                "doc_count" : 182003
              },
              {
                "key" : "gl",
                "doc_count" : 180355
              },
              {
                "key" : "vec",
                "doc_count" : 171206
              },
              {
                "key" : "kk",
                "doc_count" : 162599
              },
              {
                "key" : "et",
                "doc_count" : 153430
              },
              {
                "key" : "lld",
                "doc_count" : 144237
              }
            ]
          },
          "unique_langs" : {
            "value" : 456
          }
        },
        {
          "key" : "gn",
          "doc_count" : 17069315,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 14111277
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 815883
              },
              {
                "key" : "CJK",
                "doc_count" : 752332
              },
              {
                "key" : "ARABIC",
                "doc_count" : 708090
              },
              {
                "key" : "THAI",
                "doc_count" : 166422
              },
              {
                "key" : "HANGUL",
                "doc_count" : 144305
              },
              {
                "key" : "OTHER",
                "doc_count" : 110487
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 63753
              },
              {
                "key" : "HIRAGANA",
                "doc_count" : 41008
              },
              {
                "key" : "ARMENIAN",
                "doc_count" : 38195
              },
              {
                "key" : "GREEK",
                "doc_count" : 31934
              },
              {
                "key" : "HEBREW",
                "doc_count" : 21438
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 17676
              },
              {
                "key" : "GEORGIAN",
                "doc_count" : 17374
              },
              {
                "key" : "BENGALI",
                "doc_count" : 11079
              },
              {
                "key" : "TAMIL",
                "doc_count" : 5375
              },
              {
                "key" : "TELUGU",
                "doc_count" : 3636
              },
              {
                "key" : "KANNADA",
                "doc_count" : 3551
              },
              {
                "key" : "GUJARATI",
                "doc_count" : 3491
              },
              {
                "key" : "MALAYALAM",
                "doc_count" : 2009
              }
            ]
          },
          "languages" : {
            "doc_count_error_upper_bound" : 7177,
            "sum_other_doc_count" : 752767,
            "buckets" : [
              {
                "key" : "en",
                "doc_count" : 613156
              },
              {
                "key" : "zh",
                "doc_count" : 603857
              },
              {
                "key" : "no",
                "doc_count" : 427089
              },
              {
                "key" : "ru",
                "doc_count" : 398200
              },
              {
                "key" : "es",
                "doc_count" : 348445
              },
              {
                "key" : "fa",
                "doc_count" : 306179
              },
              {
                "key" : "id",
                "doc_count" : 281754
              },
              {
                "key" : "fi",
                "doc_count" : 240659
              },
              {
                "key" : "ar",
                "doc_count" : 230655
              },
              {
                "key" : "fr",
                "doc_count" : 178687
              },
              {
                "key" : "th",
                "doc_count" : 176253
              },
              {
                "key" : "ja",
                "doc_count" : 171057
              },
              {
                "key" : "pt",
                "doc_count" : 146650
              },
              {
                "key" : "uk",
                "doc_count" : 117296
              },
              {
                "key" : "de",
                "doc_count" : 116799
              },
              {
                "key" : "ko",
                "doc_count" : 113300
              },
              {
                "key" : "nl",
                "doc_count" : 73833
              },
              {
                "key" : "lauc",
                "doc_count" : 71605
              },
              {
                "key" : "it",
                "doc_count" : 68879
              },
              {
                "key" : "sv",
                "doc_count" : 68082
              },
              {
                "key" : "sr",
                "doc_count" : 65446
              },
              {
                "key" : "tr",
                "doc_count" : 58850
              },
              {
                "key" : "hy",
                "doc_count" : 58539
              },
              {
                "key" : "ro",
                "doc_count" : 53347
              },
              {
                "key" : "pl",
                "doc_count" : 53235
              },
              {
                "key" : "ms",
                "doc_count" : 50587
              },
              {
                "key" : "kk",
                "doc_count" : 49722
              },
              {
                "key" : "vi",
                "doc_count" : 47998
              },
              {
                "key" : "ceb",
                "doc_count" : 47491
              },
              {
                "key" : "el",
                "doc_count" : 43367
              },
              {
                "key" : "fil",
                "doc_count" : 43365
              },
              {
                "key" : "mk",
                "doc_count" : 43314
              },
              {
                "key" : "bg",
                "doc_count" : 36599
              },
              {
                "key" : "ca",
                "doc_count" : 34825
              },
              {
                "key" : "lt",
                "doc_count" : 34341
              },
              {
                "key" : "se",
                "doc_count" : 31193
              },
              {
                "key" : "bn",
                "doc_count" : 30823
              },
              {
                "key" : "my",
                "doc_count" : 28404
              },
              {
                "key" : "ur",
                "doc_count" : 27959
              },
              {
                "key" : "he",
                "doc_count" : 27578
              },
              {
                "key" : "lv",
                "doc_count" : 27196
              },
              {
                "key" : "ce",
                "doc_count" : 26866
              },
              {
                "key" : "eu",
                "doc_count" : 26615
              },
              {
                "key" : "tt",
                "doc_count" : 25377
              },
              {
                "key" : "ne",
                "doc_count" : 23582
              },
              {
                "key" : "nan",
                "doc_count" : 23522
              },
              {
                "key" : "eo",
                "doc_count" : 22366
              },
              {
                "key" : "nb",
                "doc_count" : 21489
              },
              {
                "key" : "ga",
                "doc_count" : 21333
              },
              {
                "key" : "hbs",
                "doc_count" : 21268
              }
            ]
          },
          "unique_langs" : {
            "value" : 809
          }
        },
        {
          "key" : "tgn",
          "doc_count" : 1222433,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 1173650
              },
              {
                "key" : "CJK",
                "doc_count" : 38756
              },
              {
                "key" : "ARABIC",
                "doc_count" : 3544
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 1780
              },
              {
                "key" : "GREEK",
                "doc_count" : 804
              },
              {
                "key" : "OTHER",
                "doc_count" : 729
              },
              {
                "key" : "HEBREW",
                "doc_count" : 624
              },
              {
                "key" : "HIRAGANA",
                "doc_count" : 608
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 556
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 452
              },
              {
                "key" : "HANGUL",
                "doc_count" : 319
              },
              {
                "key" : "MALAYALAM",
                "doc_count" : 114
              },
              {
                "key" : "TELUGU",
                "doc_count" : 98
              },
              {
                "key" : "ARMENIAN",
                "doc_count" : 78
              },
              {
                "key" : "TAMIL",
                "doc_count" : 64
              },
              {
                "key" : "KANNADA",
                "doc_count" : 63
              },
              {
                "key" : "THAI",
                "doc_count" : 59
              },
              {
                "key" : "GEORGIAN",
                "doc_count" : 51
              },
              {
                "key" : "BENGALI",
                "doc_count" : 44
              },
              {
                "key" : "GUJARATI",
                "doc_count" : 40
              }
            ]
          },
          "languages" : {
            "doc_count_error_upper_bound" : 52,
            "sum_other_doc_count" : 5020,
            "buckets" : [
              {
                "key" : "zh",
                "doc_count" : 455803
              },
              {
                "key" : "en",
                "doc_count" : 230988
              },
              {
                "key" : "",
                "doc_count" : 204612
              },
              {
                "key" : "fa",
                "doc_count" : 131093
              },
              {
                "key" : "ja",
                "doc_count" : 60483
              },
              {
                "key" : "el",
                "doc_count" : 40148
              },
              {
                "key" : "ru",
                "doc_count" : 38584
              },
              {
                "key" : "ar",
                "doc_count" : 11365
              },
              {
                "key" : "nl",
                "doc_count" : 9057
              },
              {
                "key" : "pt",
                "doc_count" : 6702
              },
              {
                "key" : "tr",
                "doc_count" : 3462
              },
              {
                "key" : "ang",
                "doc_count" : 2772
              },
              {
                "key" : "fr",
                "doc_count" : 1256
              },
              {
                "key" : "uig",
                "doc_count" : 1117
              },
              {
                "key" : "es",
                "doc_count" : 1071
              },
              {
                "key" : "bo",
                "doc_count" : 971
              },
              {
                "key" : "he",
                "doc_count" : 641
              },
              {
                "key" : "nor",
                "doc_count" : 635
              },
              {
                "key" : "it",
                "doc_count" : 536
              },
              {
                "key" : "mly",
                "doc_count" : 526
              },
              {
                "key" : "swa",
                "doc_count" : 513
              },
              {
                "key" : "ko",
                "doc_count" : 439
              },
              {
                "key" : "nep",
                "doc_count" : 424
              },
              {
                "key" : "de",
                "doc_count" : 420
              },
              {
                "key" : "slv",
                "doc_count" : 411
              },
              {
                "key" : "ug",
                "doc_count" : 382
              },
              {
                "key" : "oci",
                "doc_count" : 363
              },
              {
                "key" : "sme",
                "doc_count" : 363
              },
              {
                "key" : "mn",
                "doc_count" : 354
              },
              {
                "key" : "ory",
                "doc_count" : 304
              },
              {
                "key" : "mk",
                "doc_count" : 275
              },
              {
                "key" : "qwe",
                "doc_count" : 268
              },
              {
                "key" : "ga",
                "doc_count" : 256
              },
              {
                "key" : "ton",
                "doc_count" : 255
              },
              {
                "key" : "fil",
                "doc_count" : 253
              },
              {
                "key" : "ido",
                "doc_count" : 251
              },
              {
                "key" : "sag",
                "doc_count" : 237
              },
              {
                "key" : "ina",
                "doc_count" : 232
              },
              {
                "key" : "kik",
                "doc_count" : 225
              },
              {
                "key" : "th",
                "doc_count" : 225
              },
              {
                "key" : "lub",
                "doc_count" : 224
              },
              {
                "key" : "sna",
                "doc_count" : 224
              },
              {
                "key" : "vol",
                "doc_count" : 210
              },
              {
                "key" : "yo",
                "doc_count" : 203
              },
              {
                "key" : "fry",
                "doc_count" : 198
              },
              {
                "key" : "az",
                "doc_count" : 197
              },
              {
                "key" : "la",
                "doc_count" : 170
              },
              {
                "key" : "gla",
                "doc_count" : 161
              },
              {
                "key" : "rm",
                "doc_count" : 159
              },
              {
                "key" : "lim",
                "doc_count" : 145
              }
            ]
          },
          "unique_langs" : {
            "value" : 285
          }
        }
      ]
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

```bash
curl -s -X GET "http://$ES_NODE:$ES_PORT/toponyms/_search?pretty" -H 'Content-Type: application/json' -d'
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
            "lang": ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "ro", "hu", "fi", "sv", "no", "da", "tr", "vi", "id", "ms", "sw", "la", "ru", "uk", "bg", "sr", "mk", "el", "ar", "fa", "ur", "he", "hi", "mr", "ne", "sa", "bn", "ta", "te", "ml", "kn", "gu", "th", "ka", "hy", "ko", "zh", "ja"]
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
}' 
```

```json
{
  "took" : 1501,
  "timed_out" : false,
  "_shards" : {
    "total" : 4,
    "successful" : 4,
    "skipped" : 0,
    "failed" : 0
  },
  "hits" : {
    "total" : {
      "value" : 10000,
      "relation" : "gte"
    },
    "max_score" : null,
    "hits" : [ ]
  },
  "aggregations" : {
    "total_epitran_supported" : {
      "value" : 32513010
    },
    "epitran_supported" : {
      "doc_count_error_upper_bound" : 0,
      "sum_other_doc_count" : 0,
      "buckets" : [
        {
          "key" : "en",
          "doc_count" : 8067272,
          "scripts" : {
            "doc_count_error_upper_bound" : 3,
            "sum_other_doc_count" : 553,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 8039735
              },
              {
                "key" : "OTHER",
                "doc_count" : 25547
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 685
              },
              {
                "key" : "ARABIC",
                "doc_count" : 385
              },
              {
                "key" : "CJK",
                "doc_count" : 367
              }
            ]
          }
        },
        {
          "key" : "fr",
          "doc_count" : 2317225,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 369,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 2311910
              },
              {
                "key" : "OTHER",
                "doc_count" : 2064
              },
              {
                "key" : "CJK",
                "doc_count" : 2044
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 549
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 289
              }
            ]
          }
        },
        {
          "key" : "nl",
          "doc_count" : 2292364,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 46,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 2292068
              },
              {
                "key" : "OTHER",
                "doc_count" : 140
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 82
              },
              {
                "key" : "CJK",
                "doc_count" : 16
              },
              {
                "key" : "GREEK",
                "doc_count" : 12
              }
            ]
          }
        },
        {
          "key" : "de",
          "doc_count" : 2064734,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 130,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 2063027
              },
              {
                "key" : "OTHER",
                "doc_count" : 968
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 417
              },
              {
                "key" : "CJK",
                "doc_count" : 107
              },
              {
                "key" : "GREEK",
                "doc_count" : 85
              }
            ]
          }
        },
        {
          "key" : "zh",
          "doc_count" : 1736568,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 64,
            "buckets" : [
              {
                "key" : "CJK",
                "doc_count" : 1306961
              },
              {
                "key" : "LATIN",
                "doc_count" : 429370
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 69
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 53
              },
              {
                "key" : "OTHER",
                "doc_count" : 51
              }
            ]
          }
        },
        {
          "key" : "sv",
          "doc_count" : 1716601,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 10,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 1715947
              },
              {
                "key" : "OTHER",
                "doc_count" : 590
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 37
              },
              {
                "key" : "CJK",
                "doc_count" : 9
              },
              {
                "key" : "GREEK",
                "doc_count" : 8
              }
            ]
          }
        },
        {
          "key" : "es",
          "doc_count" : 1519224,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 74,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 1518530
              },
              {
                "key" : "GREEK",
                "doc_count" : 339
              },
              {
                "key" : "OTHER",
                "doc_count" : 171
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 64
              },
              {
                "key" : "CJK",
                "doc_count" : 46
              }
            ]
          }
        },
        {
          "key" : "id",
          "doc_count" : 931341,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 16,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 931192
              },
              {
                "key" : "OTHER",
                "doc_count" : 53
              },
              {
                "key" : "CJK",
                "doc_count" : 49
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 21
              },
              {
                "key" : "THAI",
                "doc_count" : 10
              }
            ]
          }
        },
        {
          "key" : "ru",
          "doc_count" : 881021,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 28,
            "buckets" : [
              {
                "key" : "CYRILLIC",
                "doc_count" : 803734
              },
              {
                "key" : "LATIN",
                "doc_count" : 77107
              },
              {
                "key" : "GREEK",
                "doc_count" : 83
              },
              {
                "key" : "OTHER",
                "doc_count" : 43
              },
              {
                "key" : "CJK",
                "doc_count" : 26
              }
            ]
          }
        },
        {
          "key" : "tr",
          "doc_count" : 845062,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 13,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 843744
              },
              {
                "key" : "OTHER",
                "doc_count" : 1226
              },
              {
                "key" : "GREEK",
                "doc_count" : 38
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 26
              },
              {
                "key" : "ARABIC",
                "doc_count" : 15
              }
            ]
          }
        },
        {
          "key" : "it",
          "doc_count" : 815347,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 21,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 815139
              },
              {
                "key" : "ARABIC",
                "doc_count" : 60
              },
              {
                "key" : "OTHER",
                "doc_count" : 59
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 53
              },
              {
                "key" : "GREEK",
                "doc_count" : 15
              }
            ]
          }
        },
        {
          "key" : "ja",
          "doc_count" : 765730,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 55,
            "buckets" : [
              {
                "key" : "CJK",
                "doc_count" : 337530
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 310414
              },
              {
                "key" : "LATIN",
                "doc_count" : 70158
              },
              {
                "key" : "HIRAGANA",
                "doc_count" : 47533
              },
              {
                "key" : "OTHER",
                "doc_count" : 40
              }
            ]
          }
        },
        {
          "key" : "fa",
          "doc_count" : 718733,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "ARABIC",
                "doc_count" : 576748
              },
              {
                "key" : "LATIN",
                "doc_count" : 141966
              },
              {
                "key" : "OTHER",
                "doc_count" : 13
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 3
              },
              {
                "key" : "GREEK",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "pt",
          "doc_count" : 712306,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 16,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 711350
              },
              {
                "key" : "OTHER",
                "doc_count" : 887
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 35
              },
              {
                "key" : "HEBREW",
                "doc_count" : 12
              },
              {
                "key" : "CJK",
                "doc_count" : 6
              }
            ]
          }
        },
        {
          "key" : "pl",
          "doc_count" : 656086,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 17,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 655880
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 120
              },
              {
                "key" : "CJK",
                "doc_count" : 38
              },
              {
                "key" : "OTHER",
                "doc_count" : 24
              },
              {
                "key" : "GREEK",
                "doc_count" : 7
              }
            ]
          }
        },
        {
          "key" : "cs",
          "doc_count" : 593931,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 46,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 593385
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 383
              },
              {
                "key" : "GREEK",
                "doc_count" : 48
              },
              {
                "key" : "CJK",
                "doc_count" : 43
              },
              {
                "key" : "OTHER",
                "doc_count" : 26
              }
            ]
          }
        },
        {
          "key" : "fi",
          "doc_count" : 532204,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 532011
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 171
              },
              {
                "key" : "OTHER",
                "doc_count" : 11
              },
              {
                "key" : "GREEK",
                "doc_count" : 9
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "uk",
          "doc_count" : 470511,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 8,
            "buckets" : [
              {
                "key" : "CYRILLIC",
                "doc_count" : 435644
              },
              {
                "key" : "LATIN",
                "doc_count" : 34817
              },
              {
                "key" : "OTHER",
                "doc_count" : 26
              },
              {
                "key" : "GEORGIAN",
                "doc_count" : 8
              },
              {
                "key" : "HEBREW",
                "doc_count" : 8
              }
            ]
          }
        },
        {
          "key" : "no",
          "doc_count" : 428240,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 428240
              }
            ]
          }
        },
        {
          "key" : "ar",
          "doc_count" : 427306,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 18,
            "buckets" : [
              {
                "key" : "ARABIC",
                "doc_count" : 412316
              },
              {
                "key" : "LATIN",
                "doc_count" : 14889
              },
              {
                "key" : "OTHER",
                "doc_count" : 63
              },
              {
                "key" : "HEBREW",
                "doc_count" : 12
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 8
              }
            ]
          }
        },
        {
          "key" : "sr",
          "doc_count" : 416628,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 159,
            "buckets" : [
              {
                "key" : "CYRILLIC",
                "doc_count" : 235582
              },
              {
                "key" : "LATIN",
                "doc_count" : 180462
              },
              {
                "key" : "CJK",
                "doc_count" : 243
              },
              {
                "key" : "ARABIC",
                "doc_count" : 108
              },
              {
                "key" : "GREEK",
                "doc_count" : 74
              }
            ]
          }
        },
        {
          "key" : "ro",
          "doc_count" : 375517,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 9,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 375292
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 156
              },
              {
                "key" : "OTHER",
                "doc_count" : 45
              },
              {
                "key" : "HEBREW",
                "doc_count" : 9
              },
              {
                "key" : "GREEK",
                "doc_count" : 6
              }
            ]
          }
        },
        {
          "key" : "da",
          "doc_count" : 297282,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 9,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 297142
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 66
              },
              {
                "key" : "OTHER",
                "doc_count" : 47
              },
              {
                "key" : "ARABIC",
                "doc_count" : 10
              },
              {
                "key" : "CJK",
                "doc_count" : 8
              }
            ]
          }
        },
        {
          "key" : "ms",
          "doc_count" : 287522,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 285578
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1918
              },
              {
                "key" : "OTHER",
                "doc_count" : 20
              },
              {
                "key" : "CJK",
                "doc_count" : 3
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "vi",
          "doc_count" : 267473,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 267432
              },
              {
                "key" : "CJK",
                "doc_count" : 19
              },
              {
                "key" : "OTHER",
                "doc_count" : 16
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 4
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "hu",
          "doc_count" : 247461,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 34,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 247134
              },
              {
                "key" : "ARABIC",
                "doc_count" : 136
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 128
              },
              {
                "key" : "GREEK",
                "doc_count" : 18
              },
              {
                "key" : "HANGUL",
                "doc_count" : 11
              }
            ]
          }
        },
        {
          "key" : "ko",
          "doc_count" : 243198,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 5,
            "buckets" : [
              {
                "key" : "HANGUL",
                "doc_count" : 228523
              },
              {
                "key" : "LATIN",
                "doc_count" : 12585
              },
              {
                "key" : "CJK",
                "doc_count" : 2060
              },
              {
                "key" : "OTHER",
                "doc_count" : 21
              },
              {
                "key" : "KATAKANA",
                "doc_count" : 4
              }
            ]
          }
        },
        {
          "key" : "bg",
          "doc_count" : 242454,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "CYRILLIC",
                "doc_count" : 235749
              },
              {
                "key" : "LATIN",
                "doc_count" : 6698
              },
              {
                "key" : "OTHER",
                "doc_count" : 3
              },
              {
                "key" : "GREEK",
                "doc_count" : 2
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "th",
          "doc_count" : 224637,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 2,
            "buckets" : [
              {
                "key" : "THAI",
                "doc_count" : 210310
              },
              {
                "key" : "LATIN",
                "doc_count" : 14299
              },
              {
                "key" : "OTHER",
                "doc_count" : 14
              },
              {
                "key" : "CJK",
                "doc_count" : 10
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "el",
          "doc_count" : 224074,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 2,
            "buckets" : [
              {
                "key" : "GREEK",
                "doc_count" : 168827
              },
              {
                "key" : "LATIN",
                "doc_count" : 55232
              },
              {
                "key" : "OTHER",
                "doc_count" : 8
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 4
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "hy",
          "doc_count" : 165308,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "ARMENIAN",
                "doc_count" : 143819
              },
              {
                "key" : "LATIN",
                "doc_count" : 21464
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 18
              },
              {
                "key" : "OTHER",
                "doc_count" : 4
              },
              {
                "key" : "ARABIC",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "he",
          "doc_count" : 136592,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 3,
            "buckets" : [
              {
                "key" : "HEBREW",
                "doc_count" : 127339
              },
              {
                "key" : "LATIN",
                "doc_count" : 9225
              },
              {
                "key" : "OTHER",
                "doc_count" : 12
              },
              {
                "key" : "ARABIC",
                "doc_count" : 7
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 6
              }
            ]
          }
        },
        {
          "key" : "sw",
          "doc_count" : 113145,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 113131
              },
              {
                "key" : "OTHER",
                "doc_count" : 10
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 4
              }
            ]
          }
        },
        {
          "key" : "ur",
          "doc_count" : 110627,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 3,
            "buckets" : [
              {
                "key" : "ARABIC",
                "doc_count" : 109700
              },
              {
                "key" : "LATIN",
                "doc_count" : 919
              },
              {
                "key" : "OTHER",
                "doc_count" : 3
              },
              {
                "key" : "BENGALI",
                "doc_count" : 1
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "bn",
          "doc_count" : 103968,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 32,
            "buckets" : [
              {
                "key" : "BENGALI",
                "doc_count" : 77935
              },
              {
                "key" : "LATIN",
                "doc_count" : 25925
              },
              {
                "key" : "OTHER",
                "doc_count" : 37
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 21
              },
              {
                "key" : "ARABIC",
                "doc_count" : 18
              }
            ]
          }
        },
        {
          "key" : "ka",
          "doc_count" : 90915,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "GEORGIAN",
                "doc_count" : 86021
              },
              {
                "key" : "LATIN",
                "doc_count" : 4884
              },
              {
                "key" : "ARMENIAN",
                "doc_count" : 5
              },
              {
                "key" : "OTHER",
                "doc_count" : 4
              },
              {
                "key" : "GREEK",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "mk",
          "doc_count" : 80066,
          "scripts" : {
            "doc_count_error_upper_bound" : 7,
            "sum_other_doc_count" : 310,
            "buckets" : [
              {
                "key" : "CYRILLIC",
                "doc_count" : 61607
              },
              {
                "key" : "LATIN",
                "doc_count" : 17948
              },
              {
                "key" : "ARABIC",
                "doc_count" : 89
              },
              {
                "key" : "CJK",
                "doc_count" : 61
              },
              {
                "key" : "HEBREW",
                "doc_count" : 51
              }
            ]
          }
        },
        {
          "key" : "la",
          "doc_count" : 77720,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 3,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 77703
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 6
              },
              {
                "key" : "CJK",
                "doc_count" : 4
              },
              {
                "key" : "GREEK",
                "doc_count" : 2
              },
              {
                "key" : "OTHER",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "hi",
          "doc_count" : 61549,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 11,
            "buckets" : [
              {
                "key" : "DEVANAGARI",
                "doc_count" : 60800
              },
              {
                "key" : "LATIN",
                "doc_count" : 714
              },
              {
                "key" : "BENGALI",
                "doc_count" : 14
              },
              {
                "key" : "OTHER",
                "doc_count" : 5
              },
              {
                "key" : "TELUGU",
                "doc_count" : 5
              }
            ]
          }
        },
        {
          "key" : "ml",
          "doc_count" : 56596,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 1,
            "buckets" : [
              {
                "key" : "MALAYALAM",
                "doc_count" : 53546
              },
              {
                "key" : "LATIN",
                "doc_count" : 3040
              },
              {
                "key" : "OTHER",
                "doc_count" : 6
              },
              {
                "key" : "TAMIL",
                "doc_count" : 2
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "ta",
          "doc_count" : 48017,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "TAMIL",
                "doc_count" : 47700
              },
              {
                "key" : "LATIN",
                "doc_count" : 309
              },
              {
                "key" : "OTHER",
                "doc_count" : 6
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 1
              },
              {
                "key" : "GREEK",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "te",
          "doc_count" : 47842,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 4,
            "buckets" : [
              {
                "key" : "TELUGU",
                "doc_count" : 47617
              },
              {
                "key" : "LATIN",
                "doc_count" : 215
              },
              {
                "key" : "OTHER",
                "doc_count" : 4
              },
              {
                "key" : "BENGALI",
                "doc_count" : 1
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "ne",
          "doc_count" : 32865,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "LATIN",
                "doc_count" : 22611
              },
              {
                "key" : "DEVANAGARI",
                "doc_count" : 10249
              },
              {
                "key" : "OTHER",
                "doc_count" : 4
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "mr",
          "doc_count" : 24586,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "DEVANAGARI",
                "doc_count" : 24452
              },
              {
                "key" : "LATIN",
                "doc_count" : 132
              },
              {
                "key" : "OTHER",
                "doc_count" : 2
              }
            ]
          }
        },
        {
          "key" : "kn",
          "doc_count" : 21110,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "KANNADA",
                "doc_count" : 20962
              },
              {
                "key" : "LATIN",
                "doc_count" : 147
              },
              {
                "key" : "OTHER",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "gu",
          "doc_count" : 20466,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "GUJARATI",
                "doc_count" : 20333
              },
              {
                "key" : "LATIN",
                "doc_count" : 132
              },
              {
                "key" : "OTHER",
                "doc_count" : 1
              }
            ]
          }
        },
        {
          "key" : "sa",
          "doc_count" : 3586,
          "scripts" : {
            "doc_count_error_upper_bound" : 0,
            "sum_other_doc_count" : 0,
            "buckets" : [
              {
                "key" : "DEVANAGARI",
                "doc_count" : 3482
              },
              {
                "key" : "LATIN",
                "doc_count" : 97
              },
              {
                "key" : "OTHER",
                "doc_count" : 4
              },
              {
                "key" : "CYRILLIC",
                "doc_count" : 2
              },
              {
                "key" : "ARABIC",
                "doc_count" : 1
              }
            ]
          }
        }
      ]
    }
  }
}
```
