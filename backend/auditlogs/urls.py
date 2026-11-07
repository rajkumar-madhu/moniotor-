from django.urls import path
from . import views

#print('audi-log------------>')
urlpatterns = [
    path('getAuditlogs', views.get_auditlogs),
    path('get_logs/', views.get_logs, name='get_logs'),
    path('view_logs/', views.view_logs, name='view_logs'),
    path('get_log_file/<str:filename>/', views.get_log_file, name='get_log_file'),
]