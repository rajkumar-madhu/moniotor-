from django.urls import include, path

from app import views as app_views
from notification import views as notification_views
from vault import views as vault_views
from login import views as login_views

urlpatterns = [
    path('', login_views.index, name='index'),
    path('login/verify', app_views.verify, name='verify'),
    path('login2/verify', login_views.verify, name='verify_login_app'),
    path('notification/', include('notification.urls')),
    path('changeStatus', vault_views.changeStatus, name='vault_changeStatus'),
]
