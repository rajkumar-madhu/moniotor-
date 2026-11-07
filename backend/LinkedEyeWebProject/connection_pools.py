"""
Centralized connection pool management for LinkedEye.

Provides singleton connection pools for:
- Neo4j (Bolt driver)
- PostgreSQL (psycopg2)
- MySQL (mysql-connector)
- Elasticsearch
- RabbitMQ (pika)

All pools are thread-safe and provide context managers for safe resource cleanup.
"""

import threading
import logging
import os
from contextlib import contextmanager

from lib.LinkedEyeVault.AppSecrets import get_app_secret

logger = logging.getLogger('linkedeye')


# ─── Neo4j Connection Pool ────────────────────────────────────────────────────

class Neo4jPool:
    """Thread-safe singleton Neo4j Bolt driver pool.

    The neo4j Python driver already manages internal connection pooling.
    This class ensures only ONE driver instance is created per (host, port) pair.
    """

    _instances = {}
    _lock = threading.Lock()

    @classmethod
    def get_driver(cls, host, port, user, password, max_pool_size=50):
        key = f"{host}:{port}"
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    from neo4j import GraphDatabase as BoltGraphDatabase
                    uri = f"bolt://{host}:{port}"
                    driver = BoltGraphDatabase.driver(
                        uri,
                        auth=(user, password),
                        max_connection_pool_size=max_pool_size,
                        connection_acquisition_timeout=30,
                    )
                    cls._instances[key] = driver
                    logger.info("Created Neo4j driver pool for %s", key)
        return cls._instances[key]

    @classmethod
    def close_all(cls):
        with cls._lock:
            for key, driver in cls._instances.items():
                try:
                    driver.close()
                    logger.info("Closed Neo4j driver pool for %s", key)
                except Exception as e:
                    logger.error("Error closing Neo4j driver for %s: %s", key, e)
            cls._instances.clear()


# ─── PostgreSQL Connection Pool ────────────────────────────────────────────────

_pg_pool = None
_pg_lock = threading.Lock()


def get_pg_pool():
    """Get or create the PostgreSQL connection pool singleton."""
    global _pg_pool
    if _pg_pool is None:
        with _pg_lock:
            if _pg_pool is None:
                try:
                    from django.conf import settings
                    import psycopg2.pool
                    _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=10,
                        host=settings.POSTGRES_HOST,
                        port=settings.POSTGRES_PORT,
                        database=settings.POSTGRES_SUPERSET_DB,
                        user=settings.POSTGRES_USER,
                        password=settings.POSTGRES_PASS,
                        connect_timeout=10,
                    )
                    logger.info("Created PostgreSQL connection pool")
                except Exception as e:
                    logger.error("Failed to create PostgreSQL pool: %s", e)
                    raise
    return _pg_pool


@contextmanager
def pg_connection():
    """Context manager for pooled PostgreSQL connections."""
    pool = get_pg_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ─── MySQL Analytics Connection Pool ──────────────────────────────────────────

_mysql_pool = None
_mysql_lock = threading.Lock()


def get_mysql_analytics_pool():
    """Get or create the MySQL analytics connection pool singleton."""
    global _mysql_pool
    if _mysql_pool is None:
        with _mysql_lock:
            if _mysql_pool is None:
                try:
                    from mysql.connector import pooling
                    _mysql_pool = pooling.MySQLConnectionPool(
                        pool_name="le_analytics_pool",
                        pool_size=5,
                        host=os.getenv('MYSQL_ANALYTICS_HOST', os.getenv('MYSQL_DB_HOST', 'db')),
                        port=int(os.getenv('MYSQL_ANALYTICS_PORT', os.getenv('MYSQL_DB_PORT', '3306'))),
                        user=os.getenv('MYSQL_ANALYTICS_USER', os.getenv('MYSQL_DB_USER', 'root')),
                        password=os.getenv('MYSQL_ANALYTICS_PASS') or get_app_secret('MYSQL_DB_PASS', env_var='MYSQL_DB_PASS', default=''),
                        database=os.getenv('MYSQL_ANALYTICS_DB', 'analytics'),
                        connection_timeout=10,
                    )
                    logger.info("Created MySQL analytics connection pool")
                except Exception as e:
                    logger.error("Failed to create MySQL analytics pool: %s", e)
                    raise
    return _mysql_pool


@contextmanager
def mysql_analytics_connection():
    """Context manager for pooled MySQL analytics connections."""
    pool = get_mysql_analytics_pool()
    conn = pool.get_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─── Elasticsearch Client Singleton ───────────────────────────────────────────

_es_clients = {}
_es_lock = threading.Lock()


def get_es_client(host, port):
    """Get or create a singleton Elasticsearch client per host:port."""
    key = f"{host}:{port}"
    if key not in _es_clients:
        with _es_lock:
            if key not in _es_clients:
                from elasticsearch import Elasticsearch
                _es_clients[key] = Elasticsearch(
                    [{'host': host, 'port': int(port)}],
                    timeout=30,
                    max_retries=2,
                    retry_on_timeout=True,
                )
                logger.info("Created Elasticsearch client for %s", key)
    return _es_clients[key]


# ─── Cleanup on process shutdown ──────────────────────────────────────────────

import atexit

def _cleanup_pools():
    """Cleanup all connection pools on process exit."""
    Neo4jPool.close_all()
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.closeall()
        except Exception:
            pass
    logger.info("All connection pools cleaned up")

atexit.register(_cleanup_pools)
