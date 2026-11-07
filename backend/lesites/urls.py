from django.urls import path
from . import views

#print('sites------->')
urlpatterns = [
    path('getallsitenames', views.getallsitenames),
    path('getsiteinfo', views.getallsitenames,name='getsiteinfo'),
    path('siteactions', views.siteactions),
    path('locationactions', views.locationactions),
    path('countryactions', views.countryactions),
    path('stateactions', views.stateactions),
    path('getsitelocations', views.get_site_locations),
    path('getsitecountry', views.get_site_country),
    path('getsitestate', views.get_site_state),
    
]