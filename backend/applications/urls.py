from django.urls import path
from . import views

#print('applications--------->')
urlpatterns = [
    path('', views.application, name='Application'),
    path('getallapplicationnames', views.getallapplicationnames),
    path('applicationactions', views.applicationactions),
    path('exportsnmp', views.exportsnmp, name='exportsnmp'),
]