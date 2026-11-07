from django.urls import path
from . import views

#print('site-health---------->')
urlpatterns = [
    path('overall', views.sitehealth),
]