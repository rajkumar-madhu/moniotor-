from django.urls import path

from . import views

app_name = "dashboard"
#print('dashboard--------->')
urlpatterns = [
    path('', views.index, name='Dashboard'),
    path('api/summary', views.backend_summary_api, name='backend_summary_api'),
    path('getneo4jnodes', views.getneo4jnodes, name='getneo4jnodes'),
    path('getHostOrIconnodes', views.getHostOrIconnodes, name='getHostOrIconnodes'),
    path('getHostnodes', views.getHostnodes, name='getHostnodes'),
    path('getIconspecificnodes', views.getIconspecificnodes, name='getIconspecificnodes'),
    path('getNicConnectnodes', views.getNicConnectnodes, name='getNicConnectnodes'),
    path('getstatusAll', views.getstatusAll, name='getstatusAll'),
    path('gethostandservicedetails', views.gethostandservicedetails, name='gethostandservicedetails'),
    path('getnodespecificdetails', views.getnodespecificdetails, name="getnodespecificdetails"),
    path('getwebsocupdate', views.getwebsocupdate, name="getwebsocupdate"),
    path('getchartspecificdetails', views.getchartspecificdetails, name="getchartspecificdetails"),
    path('getoverallchartdetails', views.getoverallchartdetails, name="getoverallchartdetails"),
    path('savedatabase', views.savedatabase, name='savedatabase'),
    path('snmpnewtable', views.snmpnewtable, name='snmpnewtable'),
    path('deleteactions', views.deleteactions, name='deleteactions'),
    path('snmpvalidation', views.snmpvalidation, name='snmpvalidation'),
    path('gethostandservicecount', views.gethostandservicecount, name="gethostandservicecount"),
    path('getspecificrelation', views.getspecificrelation, name="getspecificrelation"),
    path('getSwitches', views.getSwitches, name="getSwitches"),
]
