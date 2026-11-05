from django.urls import path

from . import views
from . import messagequeue_views as mq_views
from . import bandwidth_views as bw_views

app_name = "bod-eodstatus"
#print('bod-eodstatus----------->')
urlpatterns = [
    path('', views.bodeodstatus, name='BOD-EOD Status'),
    path('adp', views.adpstatus, name='ADP Status'),
    path('eod', views.eodstatus, name='EOD Status'),
    path('le-adp-status', views.leadp_status, name='LE ADP Status'), #new LE_ADP site
    path('getbodeodkeys', views.getbodeodkeys, name='getbodeodkeys'),
    path('updatekeys', views.updatekeys, name='updatekeys'),
    path('readfile', views.read_file, name='readfile'),

    # MessageQueue (LinkedEye DB)
    path('messagequeue', mq_views.messagequeue_dashboard, name='messagequeue_dashboard'),
    path('messagequeue-data', mq_views.messagequeue_data, name='messagequeue_data'),
    path('messagequeue-stats', mq_views.messagequeue_stats, name='messagequeue_stats'),
    path('messagequeue-latency', mq_views.messagequeue_latency_data, name='messagequeue_latency'),
    path('messagequeue-dates', mq_views.messagequeue_dates, name='messagequeue_dates'),
    path('messagequeue-csv-files', mq_views.messagequeue_csv_files, name='messagequeue_csv_files'),
    path('messagequeue-order-latency', mq_views.get_order_latency, name='get_order_latency'),
    path('messagequeue-queue-line1',   mq_views.get_queue_line1,   name='get_queue_line1'),
    path('messagequeue-queue-line2',   mq_views.get_queue_line2,   name='get_queue_line2'),
    path('messagequeue-sites',           mq_views.messagequeue_sites, name='messagequeue_sites'),

    # Bandwidth (LinkedEye DB)
    path('bandwidth-summary', bw_views.get_bandwidth_summary, name='bandwidth_summary'),
    path('bandwidth-trends',  bw_views.get_bandwidth_trends,  name='bandwidth_trends'),
    path('bandwidth-status',  bw_views.get_interface_status,  name='bandwidth_status'),
    path('bandwidth-sync',    bw_views.sync_prometheus_bandwidth, name='bandwidth_sync'),
]
