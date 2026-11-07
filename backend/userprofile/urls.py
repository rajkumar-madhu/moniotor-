from django.urls import path

from . import views

app_name = "userprofile"
#print('user-profile-------->')
urlpatterns = [
    path('', views.userprofile, name='General Settings')
]
