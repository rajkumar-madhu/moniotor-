from django.urls import path

from . import views

#print('login----------->')
urlpatterns = [
    path('', views.index, name='index'),
    path('verify', views.verify, name='verify'),
    path('logout', views.logout, name='logout'),
    path('forgotPassword', views.forgot_password, name='forgotPassword'),
]
