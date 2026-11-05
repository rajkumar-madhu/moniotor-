from django.urls import path
from . import views

app_name = 'incidents'

urlpatterns = [
    path('', views.incidents, name='incidents'),
    path('api/incidents', views.get_incidents_api, name='incidents_api'),
    path('api/incidents/', views.get_incidents_api, name='incidents_api_slash'),
    path('api/chart-data', views.get_incidents_chart_data, name='incidents_chart_data'),
    path('api/chart-data/', views.get_incidents_chart_data, name='incidents_chart_data_slash'),
    path('api/per-site', views.get_incidents_per_site, name='incidents_per_site'),
    path('api/per-site/', views.get_incidents_per_site, name='incidents_per_site_slash'),
    path('create/', views.create_incident, name='create_incident'),
    path('<str:incident_id>/', views.incident_detail, name='incident_detail'),
]
