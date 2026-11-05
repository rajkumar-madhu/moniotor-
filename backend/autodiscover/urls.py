from django.urls import path
from . import views

#print('auto-discover---------->')
urlpatterns = [
    path('', views.index, name='Autodiscover'),
    path('getdiscoverydetails', views.getdiscoverydetails, name='getdiscoverydetails'),
    path('savedata', views.savedata, name='savedata')
]