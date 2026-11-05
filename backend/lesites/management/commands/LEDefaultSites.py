import pandas as pd
from django.core.management.base import BaseCommand, no_translations
from lesites.models import SiteModel, LocationModel, CountryModel, StateModel

class Command(BaseCommand):
    help = 'Help text for your command'

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **kwargs):
        # adding list of CountryModel #########################
        CountryModel.objects.all().delete()
        filename = './jvectormap/country.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'countryid' : row['countryid'], 'countryshortname' : row['countryshortname'], 'countryname' : row['countryname']}
            obj , created = CountryModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.countryid = row['countryid']
                obj.countryshortname = row['countryshortname']
                obj.countryname = row['countryname']
                obj.save()
        # adding list of lecountry #########################

        # adding list of StateModel #########################
        StateModel.objects.all().delete()
        filename = './jvectormap/IN.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'stateid' : row['stateid'], 'countryid' : row['countryid'], 'stateshortname' : row['stateshortname'], 'stateshortname' : row['stateshortname'], 'statename' : row['statename'], 'lat' : row['lat'], 'lng' : row['lng']}
            obj , created = StateModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.stateid = row['stateid']
                obj.countryid = row['countryid']
                obj.stateshortname = row['stateshortname']
                obj.statename = row['statename']
                obj.lat = row['lat']
                obj.lng = row['lng']
                obj.save()
        # adding list of switchlayers #########################

        