"""Symphonym evaluation: geometry gate, discrimination, retrieval, baselines.

A TOP-LEVEL PACKAGE, NOT `evaluation`, and the reason is not filing.
`testing/__init__.py` imports `mehdie_benchmark`, which imports **torch**, so
`import testing.anything` fails on **pitt** — the only host that can reach
production Elasticsearch, and therefore the only host that can build the corpus.
`processing/reembed.py` carries the same note about `phonetics/inference/` for
the same reason. Nothing is imported here, so this package stays importable
wherever any part of it needs to run.
"""
