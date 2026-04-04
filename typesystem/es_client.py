# typesystem/es_client.py

"""
Shared Elasticsearch client factory for the type system.

Handles authentication by reading the elastic password file when present.
The password file location follows the same convention as scripts/es.sh:
  ${IX1_BASE}/es/config/elastic.password
"""

import os
from pathlib import Path

from elasticsearch import Elasticsearch

IX1_BASE = os.getenv("IX1_BASE", "/ix1/ishi")
PASSWORD_FILE = Path(IX1_BASE) / "es" / "config" / "elastic.password"


def create_client(es_host: str, **kwargs) -> Elasticsearch:
    """
    Create an Elasticsearch client with optional authentication.

    If the password file exists, uses basic auth with user 'elastic'.
    This allows the same --es-host URL to work against both staging
    (no auth) and production (auth required).
    """
    kwargs.setdefault("request_timeout", 120)

    if PASSWORD_FILE.is_file():
        password = PASSWORD_FILE.read_text().strip()
        kwargs["basic_auth"] = ("elastic", password)

    return Elasticsearch(es_host, **kwargs)

