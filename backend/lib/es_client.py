"""
Elasticsearch Singleton Client
HIGH FIX #10: Singleton client with connection pooling and sniffing
"""
import os
import logging
from elasticsearch import Elasticsearch
import threading

logger = logging.getLogger('linkedeye')

# ─────────────────────────────────────────────────
# ELASTICSEARCH SINGLETON CLIENT
# HIGH FIX #10: Prevents connection leak per request
# ─────────────────────────────────────────────────
_es_lock = threading.Lock()
_es_client = None


# ─────────────────────────────────────────────────
# SECURE (oauth2-proxy) CLIENT — public ES hosts only
# The internal singleton above is unaffected: it never calls this.
# ─────────────────────────────────────────────────

def get_secure_es_client(host, port=443):
    """
    Build a fresh (non-singleton, non-cached) client for a public,
    oauth2-proxy-gated ES host, attaching a Keycloak client-credentials
    bearer token. Callers should create one per use rather than reuse across
    unrelated hosts, since the token is short-lived and host-specific.

    Returns None (never raises) when the Keycloak client isn't configured or
    the token fetch fails, so callers can fall back or surface a clear error.
    """
    try:
        from lib.LinkedEyeMonitoring.token import get_monitoring_token
        token = get_monitoring_token('elastic')
    except Exception as e:
        logger.error(f"Elastic bearer token fetch failed: {e}")
        token = None
    if not token:
        logger.error("Elastic secure client requested but no bearer token available")
        return None
    return Elasticsearch(
        [f"https://{host}:{port}"],
        bearer_auth=token,
        request_timeout=30,
    )


def get_es_client(host=None, port=None):
    """
    Get or create singleton Elasticsearch client with connection pool.
    
    HIGH FIX #10: Singleton pattern with:
    - Connection pooling (maxsize=10)
    - Sniffing for node discovery
    - Retry on timeout
    - 30s timeout
    """
    global _es_client
    
    if _es_client is None:
        with _es_lock:
            if _es_client is None:
                # Get config from settings or env
                if not host:
                    try:
                        from django.conf import settings
                        elastic_url = settings.ELASTIC_URL
                        # Parse host:port from ELASTIC_URL
                        if ':' in elastic_url:
                            host, port = elastic_url.rsplit(':', 1)
                        else:
                            host = elastic_url
                            port = 9200
                    except Exception:
                        host = os.getenv('ELASTIC_HOST', 'elasticsearch.fs-linkedeye')
                        port = os.getenv('ELASTIC_PORT', '9200')
                
                if not port:
                    port = 9200
                
                # Build connection URL
                es_url = f"http://{host}:{port}"
                
                logger.info(f"Creating Elasticsearch singleton client: {es_url}")
                
                # Create client with connection pooling
                _es_client = Elasticsearch(
                    [es_url],
                    # Connection pool settings
                    maxsize=10,  # Max connections in pool
                    # Sniffing for node discovery
                    sniff_on_start=False,  # Don't sniff on startup (can be slow)
                    sniff_on_connection_fail=True,  # Sniff when connection fails
                    sniffer_timeout=60,  # Sniff every 60s
                    # Retry settings
                    retry_on_timeout=True,
                    max_retries=3,
                    # Timeout settings
                    timeout=30,  # 30s timeout
                    # Keep-alive
                    http_compress=True,
                )
                
                # Test connection
                try:
                    info = _es_client.info()
                    logger.info(f"Elasticsearch connected: {info['version']['number']}")
                except Exception as e:
                    logger.error(f"Elasticsearch connection test failed: {e}")
    
    return _es_client


def close_es_client():
    """Close Elasticsearch client (for cleanup)."""
    global _es_client
    if _es_client:
        try:
            _es_client.close()
        except Exception as e:
            logger.error(f"Error closing Elasticsearch client: {e}")
        finally:
            _es_client = None


class ESClientContext:
    """
    Context manager for Elasticsearch operations.
    Ensures client is available and handles errors gracefully.
    """
    def __init__(self, host=None, port=None):
        self.host = host
        self.port = port
        self.client = None
    
    def __enter__(self):
        self.client = get_es_client(self.host, self.port)
        return self.client
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Don't close the singleton client - it's reused
        # Just log errors if any
        if exc_type:
            logger.error(f"Elasticsearch operation error: {exc_val}")
        return False  # Don't suppress exceptions


# Convenience functions
def search(index, body, **kwargs):
    """
    Execute Elasticsearch search with singleton client.
    
    Args:
        index: Index name or pattern
        body: Query DSL body
        **kwargs: Additional search parameters
    
    Returns:
        Search results
    """
    client = get_es_client()
    return client.search(index=index, body=body, **kwargs)


def index_document(index, doc_type, body, doc_id=None, **kwargs):
    """
    Index a document with singleton client.
    
    Args:
        index: Index name
        doc_type: Document type (deprecated in ES 7+)
        body: Document body
        doc_id: Optional document ID
        **kwargs: Additional index parameters
    
    Returns:
        Index response
    """
    client = get_es_client()
    if doc_id:
        return client.index(index=index, doc_type=doc_type, id=doc_id, body=body, **kwargs)
    else:
        return client.index(index=index, doc_type=doc_type, body=body, **kwargs)


def bulk_index(body, **kwargs):
    """
    Bulk index documents with singleton client.
    
    Args:
        body: Bulk request body
        **kwargs: Additional bulk parameters
    
    Returns:
        Bulk response
    """
    client = get_es_client()
    return client.bulk(body=body, **kwargs)


def delete_by_query(index, body, **kwargs):
    """
    Delete documents by query with singleton client.
    
    Args:
        index: Index name or pattern
        body: Query DSL body
        **kwargs: Additional parameters
    
    Returns:
        Delete response
    """
    client = get_es_client()
    return client.delete_by_query(index=index, body=body, **kwargs)


def get_document(index, doc_type, doc_id, **kwargs):
    """
    Get a document by ID with singleton client.
    
    Args:
        index: Index name
        doc_type: Document type
        doc_id: Document ID
        **kwargs: Additional parameters
    
    Returns:
        Document
    """
    client = get_es_client()
    return client.get(index=index, doc_type=doc_type, id=doc_id, **kwargs)
