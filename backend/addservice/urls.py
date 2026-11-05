from django.urls import path
from . import views

#print('addservice------->')
urlpatterns = [
    path('serviceOperation', views.serviceOperation, name='serviceOperation'),
    path('getfilenames', views.getfilenames, name='getfilenames'),
    path('changeStatus', views.changeStatus, name='changeStatus'),
    path('getallsecrets', views.getallsecrets, name='getallsecrets'),
    path('getServersType', views.getServersType, name='getServersType'),
    path('getServersSoftware', views.getServersSoftware, name='getServersSoftware'),
    path('getSwitchesLayers', views.getSwitchesLayers, name='getSwitchesLayers'),
    path('getSwitchesModel', views.getSwitchesModel, name='getSwitchesModel'),
    path('getFirewallModel', views.getFirewallModel, name='getFirewallModel'),
    path('getalldropdowndata', views.getalldropdowndata, name='getalldropdowndata'),
]