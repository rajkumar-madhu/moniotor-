from django.db import models
from django.conf import settings

class OrderLatencyModel(models.Model):
    """
    Model for order_latency table
    Stores order latency metrics for OMS and exchange confirmations
    """
    id = models.AutoField(primary_key=True)
    file_date = models.DateField(null=True, blank=True)
    noren_ord_num = models.CharField(max_length=100, null=True, blank=True)
    exch_seg = models.CharField(max_length=50, null=True, blank=True)
    token = models.CharField(max_length=100, null=True, blank=True)
    oms_latency = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    oms_exch_confirmation = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    oms_update_time = models.CharField(max_length=50, null=True, blank=True)
    exch_update_time = models.CharField(max_length=50, null=True, blank=True)
    oms_update_time_conv = models.DateTimeField(null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "order_latency"
        managed = True


class QueueLine1Model(models.Model):
    """
    Model for queue_line1 table
    Stores queue data for Line-1 segments (BSE, BFO)
    """
    id = models.AutoField(primary_key=True)
    file_date = models.DateField(null=True, blank=True)
    segment = models.CharField(max_length=50, null=True, blank=True)
    time = models.DateTimeField(null=True, blank=True)
    seq_no = models.IntegerField(null=True, blank=True)
    erf = models.IntegerField(null=True, blank=True)
    queue_size = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "queue_line1"
        managed = True


class QueueLine2Model(models.Model):
    """
    Model for queue_line2 table
    Stores queue data for Line-2 segments (NSE, NFO)
    """
    id = models.AutoField(primary_key=True)
    file_date = models.DateField(null=True, blank=True)
    segment = models.CharField(max_length=50, null=True, blank=True)
    time = models.DateTimeField(null=True, blank=True)
    seq_no = models.IntegerField(null=True, blank=True)
    erf = models.IntegerField(null=True, blank=True)
    queue_size = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "queue_line2"
        managed = True


class OrderLatencyDailySummaryModel(models.Model):
    """
    Per-day, per-segment latency rollup for order_latency, written by the
    latency.py ETL (in-memory, from the same DataFrame it already loads to
    insert raw rows -- no extra table scan). exch_seg='ALL' holds the
    whole-day aggregate across all segments.

    The APM page and Grafana both read this instead of running percentile
    queries against the multi-million-row order_latency table live.
    """
    id = models.AutoField(primary_key=True)
    file_date = models.DateField()
    exch_seg = models.CharField(max_length=50)
    order_count = models.IntegerField(default=0)
    avg_oms_latency = models.FloatField(null=True, blank=True)
    max_oms_latency = models.FloatField(null=True, blank=True)
    p50_oms_latency = models.FloatField(null=True, blank=True)
    p95_oms_latency = models.FloatField(null=True, blank=True)
    p99_oms_latency = models.FloatField(null=True, blank=True)
    avg_exch_confirmation = models.FloatField(null=True, blank=True)
    max_exch_confirmation = models.FloatField(null=True, blank=True)
    p50_exch_confirmation = models.FloatField(null=True, blank=True)
    histogram_labels = models.JSONField(null=True, blank=True)
    histogram_pct = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "order_latency_daily_summary"
        managed = True
        unique_together = (('file_date', 'exch_seg'),)


class OrderLatencyMinuteSummaryModel(models.Model):
    """
    Per-minute-bucket latency rollup for order_latency (all segments
    combined), written by the same latency.py ETL pass. Backs the per-minute
    latency chart without scanning the raw table.
    """
    id = models.AutoField(primary_key=True)
    file_date = models.DateField()
    minute_bucket = models.CharField(max_length=5)  # 'HH:MM'
    order_count = models.IntegerField(default=0)
    avg_oms_latency = models.FloatField(null=True, blank=True)
    max_oms_latency = models.FloatField(null=True, blank=True)
    avg_exch_confirmation = models.FloatField(null=True, blank=True)
    max_exch_confirmation = models.FloatField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "order_latency_minute_summary"
        managed = True
        unique_together = (('file_date', 'minute_bucket'),)


class BandwidthBaseModel(models.Model):
    """
    Base model for bandwidth metrics
    """
    id = models.AutoField(primary_key=True)
    site = models.CharField(max_length=100, null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)
    interface = models.CharField(max_length=100, null=True, blank=True)
    instance = models.CharField(max_length=100, null=True, blank=True)
    rx_bytes_per_sec = models.FloatField(null=True, blank=True)
    tx_bytes_per_sec = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class ExchangeBandwidthModel(BandwidthBaseModel):
    class Meta:
        db_table = "lama_exchange_bandwidth"
        managed = True
        unique_together = (('site', 'timestamp', 'interface', 'instance'),)


class DeviceBandwidthModel(BandwidthBaseModel):
    model = models.CharField(max_length=100, null=True, blank=True) # "cisco", "fortigate", "huawei", etc.

    class Meta:
        db_table = "lama_device_bandwidth"
        managed = True
        unique_together = (('site', 'timestamp', 'interface', 'instance', 'model'),)


class AwsDirectBandwidthModel(models.Model):
    id = models.AutoField(primary_key=True)
    site = models.CharField(max_length=100, null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)
    interface = models.CharField(max_length=100, null=True, blank=True)
    vdom = models.CharField(max_length=100, default='root')
    rx_bytes_per_sec = models.FloatField(null=True, blank=True)
    tx_bytes_per_sec = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lama_aws_direct_bandwidth"
        managed = True
        unique_together = (('site', 'timestamp', 'interface', 'vdom'),)


class VpnBandwidthModel(BandwidthBaseModel):
    class Meta:
        db_table = "lama_vpn_bandwidth"
        managed = True
        unique_together = (('site', 'timestamp', 'interface', 'instance'),)


class WanBandwidthModel(BandwidthBaseModel):
    class Meta:
        db_table = "lama_wan_bandwidth"
        managed = True
        unique_together = (('site', 'timestamp', 'interface', 'instance'),)


class InterfaceStatusModel(models.Model):
    id = models.AutoField(primary_key=True)
    site = models.CharField(max_length=100, null=True, blank=True)
    snapshot_time = models.DateTimeField(null=True, blank=True)
    source_type = models.CharField(max_length=50, null=True, blank=True)
    interface = models.CharField(max_length=100, null=True, blank=True)
    instance = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(max_length=20, default='UNKNOWN')
    speed_mbps = models.FloatField(default=0)
    rx_bytes_total = models.BigIntegerField(default=0)
    tx_bytes_total = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lama_interface_status"
        managed = True
        unique_together = (('site', 'snapshot_time', 'source_type', 'interface', 'instance'),)


class BandwidthDailySummaryModel(models.Model):
    id = models.AutoField(primary_key=True)
    site = models.CharField(max_length=100, null=True, blank=True)
    report_date = models.DateField()
    source_type = models.CharField(max_length=50)
    interface = models.CharField(max_length=100)
    total_rx_bytes = models.BigIntegerField(default=0)
    total_tx_bytes = models.BigIntegerField(default=0)
    avg_rx_mbps = models.FloatField(default=0)
    avg_tx_mbps = models.FloatField(default=0)
    max_rx_mbps = models.FloatField(default=0)
    max_tx_mbps = models.FloatField(default=0)
    sample_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lama_daily_summary"
        managed = True
        unique_together = (('site', 'report_date', 'source_type', 'interface'),)
