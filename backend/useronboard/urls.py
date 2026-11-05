from django.urls import path
from . import views

#print('useronboard------->')
urlpatterns = [
    path('', views.useronboard, name='Useronboard'),
    path('useroperations/', views.useroperations),
    path('getallPermissions', views.getallPermissions),
    path('addRoles', views.addRoles),
    path('get_all_groups', views.get_all_groups),
    path('getuserlist',views.get_userlist),
    path('changePassword', views.change_password),
    path('getcurrentuser', views.getcurrentuser),
    path('getsubsitedata', views.getsubsitedata)
]
