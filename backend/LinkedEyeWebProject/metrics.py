"""
Prometheus Metrics Endpoint for LinkedEye
MEDIUM FIX #25: Expose process metrics for Prometheus scraping

URL: /metrics/
Format: Prometheus text format 0.0.4

Add to urls.py:
    path('metrics/', metrics_view, name='metrics'),

Add to requirements.txt:
    prometheus-client==0.19.0
"""

import os
import time
import threading
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
    )
    
    # Define metrics
    REQUEST_COUNT = Counter(
        'linkedeye_requests_total',
        'Total number of requests',
        ['method', 'endpoint', 'status']
    )
    
    REQUEST_LATENCY = Histogram(
        'linkedeye_request_latency_seconds',
        'Request latency in seconds',
        ['method', 'endpoint'],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    )
    
    RSS_BYTES = Gauge(
        'linkedeye_process_rss_bytes',
        'Resident Set Size memory in bytes'
    )
    
    VMS_BYTES = Gauge(
        'linkedeye_process_vms_bytes',
        'Virtual Memory Size in bytes'
    )
    
    CPU_PERCENT = Gauge(
        'linkedeye_process_cpu_percent',
        'CPU utilization percentage'
    )
    
    THREAD_COUNT = Gauge(
        'linkedeye_process_threads',
        'Number of active threads'
    )
    
    CONNECTION_COUNT = Gauge(
        'linkedeye_process_connections',
        'Number of open TCP connections'
    )
    
    NEO4J_POOL_SIZE = Gauge(
        'linkedeye_neo4j_connection_pool_size',
        'Neo4j connection pool size'
    )
    
    GC_COUNT = Counter(
        'linkedeye_gc_collections_total',
        'Total garbage collections',
        ['generation']
    )
    
    PROMETHEUS_AVAILABLE = True
    
except ImportError:
    logger.warning("prometheus-client not installed - /metrics endpoint disabled")
    PROMETHEUS_AVAILABLE = False


@csrf_exempt
def health_view(request):
    """Lightweight health check for K8s liveness/readiness probes. CRITICAL FIX #3."""
    import json
    data = {"status": "ok"}
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        data["rss_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
        data["threads"] = threading.active_count()
        try:
            data["connections"] = len(proc.connections())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            data["connections"] = 0
    except ImportError:
        pass
    return HttpResponse(json.dumps(data), content_type="application/json")


@csrf_exempt
def metrics_view(request):
    """
    Prometheus metrics endpoint.
    Returns metrics in Prometheus text format.
    """
    if not PROMETHEUS_AVAILABLE:
        return HttpResponse(
            "# prometheus-client not installed\n",
            content_type='text/plain'
        )
    
    try:
        import psutil
        
        # Update process metrics
        proc = psutil.Process(os.getpid())
        
        # Memory
        mem_info = proc.memory_info()
        RSS_BYTES.set(mem_info.rss)
        VMS_BYTES.set(mem_info.vms)
        
        # CPU
        CPU_PERCENT.set(proc.cpu_percent(interval=0.1))
        
        # Threads
        THREAD_COUNT.set(threading.active_count())
        
        # Connections
        try:
            CONNECTION_COUNT.set(len(proc.connections()))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            CONNECTION_COUNT.set(0)
        
        # Neo4j pool size (from singleton driver)
        try:
            from lib.LinkedEyeEntity.Node import _bolt_driver
            if _bolt_driver:
                # Neo4j driver doesn't expose pool size directly
                # This is an estimate based on configuration
                NEO4J_POOL_SIZE.set(25)  # Configured max pool size
        except Exception:
            NEO4J_POOL_SIZE.set(0)
        
        # Generate Prometheus format
        output = generate_latest()
        
        return HttpResponse(output, content_type=CONTENT_TYPE_LATEST)
    
    except Exception as e:
        logger.error(f"Metrics collection error: {e}")
        return HttpResponse(
            f"# Error collecting metrics: {e}\n",
            content_type='text/plain',
            status=500
        )


def record_request_metrics(method, endpoint, status, latency):
    """
    Record request metrics.
    Call this at the end of request processing.
    
    Usage in middleware:
        from LinkedEyeWebProject.metrics import record_request_metrics
        record_request_metrics(request.method, request.path, response.status_code, latency)
    """
    if not PROMETHEUS_AVAILABLE:
        return
    
    try:
        REQUEST_COUNT.labels(
            method=method,
            endpoint=endpoint,
            status=str(status)
        ).inc()
        
        REQUEST_LATENCY.labels(
            method=method,
            endpoint=endpoint
        ).observe(latency)
    
    except Exception as e:
        logger.error(f"Error recording request metrics: {e}")


def record_gc_count(generation):
    """
    Record garbage collection count.
    Call this after gc.collect().
    """
    if not PROMETHEUS_AVAILABLE:
        return
    
    try:
        GC_COUNT.labels(generation=str(generation)).inc()
    except Exception as e:
        logger.error(f"Error recording GC count: {e}")
