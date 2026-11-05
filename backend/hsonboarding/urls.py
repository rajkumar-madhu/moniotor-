from django.urls import path
from . import views

#print('hsonboard---------->')
urlpatterns = [
    path('', views.index, name='HyperSync Onboard'),
    path('gethsconfig', views.gethsconfig, name='gethsconfig')
]