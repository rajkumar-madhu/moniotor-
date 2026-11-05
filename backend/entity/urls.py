from django.urls import path

from . import views

#print('entity-------->')
urlpatterns = [
    path('', views.index, name='Entities'),
    path('addrelationships', views.addrelationships, name='Add Relationship'),
    path('setneo4jrelationships', views.setneo4jrelationships),
    path('deleterelationship', views.delete_relationship),
    path('getneo4jspecificelement', views.getneo4jspecificelement),
    path('portconnection', views.portconnection),
    path('connporttable', views.connporttable),
    path('deleteconnection', views.deleteconnection),
    path('editportconnection', views.editportconnection),
    path('uploadportconn', views.uploadportconn),
    path('postentprocessdata', views.postentprocessdata, name='postentprocessdata'),
    #path('getentprocesskeys', views.getentprocesskeys, name='getentprocesskeys'),
    path('download_port_file', views.download_port_file, name='download_port_file')
]
