# clustering/pg_client.py
"""
PostgreSQL client via SSH tunnel to the WHG DigitalOcean VM.

Uses ``sshtunnel`` to forward a local port to the PostgreSQL service on
the remote VM, then connects with ``asyncpg``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from sshtunnel import SSHTunnelForwarder

from .config import (
    PG_SSH_HOST, PG_DB_NAME, PG_DB_USER, PG_DB_HOST, PG_DB_PORT, PG_DB_PASSWORD,
)

logger = logging.getLogger("clustering.pg_client")

# SSH tunnel configuration — reads from ~/.ssh/config via host alias
_SSH_CONFIG_HOST = PG_SSH_HOST


def _start_ssh_tunnel() -> SSHTunnelForwarder:
    """Open an SSH tunnel forwarding a local port to PG on the remote VM."""
    tunnel = SSHTunnelForwarder(
        ssh_address_or_host=_SSH_CONFIG_HOST,
        ssh_config_file="~/.ssh/config",
        remote_bind_address=(PG_DB_HOST, PG_DB_PORT),
    )
    tunnel.start()
    logger.info(
        "SSH tunnel opened: local port %d → %s:%d on %s",
        tunnel.local_bind_port,
        PG_DB_HOST,
        PG_DB_PORT,
        _SSH_CONFIG_HOST,
    )
    return tunnel


@asynccontextmanager
async def pg_connection() -> AsyncIterator[asyncpg.Connection]:
    """
    Yield an asyncpg Connection to the WHG PostgreSQL database,
    tunnelled over SSH.
    """
    tunnel = _start_ssh_tunnel()
    try:
        conn = await asyncpg.connect(
            host="127.0.0.1",
            port=tunnel.local_bind_port,
            database=PG_DB_NAME,
            user=PG_DB_USER,
            password=PG_DB_PASSWORD,  # None → asyncpg consults ~/.pgpass
        )
        try:
            yield conn
        finally:
            await conn.close()
    finally:
        tunnel.stop()
        logger.info("SSH tunnel closed")

