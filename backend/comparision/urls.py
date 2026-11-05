from django.urls import path

from . import views

app_name = "comparision"
#print('dashboard--------->')
urlpatterns = [
    path('configmapCompare', views.configmapCompare, name='configmapCompare'),
    path('versionCompare', views.versionCompare, name='versionCompare'),
]
