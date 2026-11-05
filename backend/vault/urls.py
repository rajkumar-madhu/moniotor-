from django.urls import path
from . import views

#print('valut------>')
urlpatterns = [
    path('vault/', views.vault, name='Vault'),
    path('vaultOperation', views.vaultOperation, name='vaultOperation'),
    path('getfilenames', views.getfilenames, name='getfilenames'),
    path('changeStatus', views.changeStatus, name='changeStatus'),
    path('getallsecrets', views.getallsecrets, name='getallsecrets')
]