from django.urls import path
from . import views

#print('onboarding--------->')
urlpatterns = [
    path('', views.index, name='Onboard'),
    path('getfilenames', views.getfilenames, name='getfilenames'),
    path('getfilecontentdata', views.getfilecontentdata, name='getfilecontentdata'),
    path('createcfg', views.createcfg, name='createcfg'),
    path('getiplist/', views.getiplist, name='getiplist'),
    path('gethostservicedata/', views.gethostservicedata, name='gethostservicedata'),
    path('edithostdetails/', views.edithostdetails, name='edithostdetails'),
    path('deletehost', views.deletehost),
]
