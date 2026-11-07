from django.urls import path
from . import views

#print('analytics------->')
urlpatterns = [
    path('dashboard', views.dashboard, name='dashboard'),
    path('saveSettings', views.saveSettings),
    path('getprefixurlData', views.getprefixurlData),
    path('getpermalink', views.getpermalink),
    path('getaccesstoken', views.getaccesstoken),
    path('getUID', views.getUID),
    path('monitorgraph', views.monitorgraph),
    path('getTableIndex', views.getTableIndex),
    path('search_elasticsearch', views.search_elasticsearch),
    path('export_to_excel', views.export_to_excel),
    path('export_to_pdf', views.export_to_pdf),
]
