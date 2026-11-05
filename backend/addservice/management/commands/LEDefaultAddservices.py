import pandas as pd
from django.core.management.base import BaseCommand, no_translations
from addservice.models import ServerstypesModel, ServersosModel, ServerssoftwareModel, ServersnicModel, SwitcheslayersModel, SwitchesmodelModel, FirewalltypeModel, FirewallmodelModel, RoutertypeModel, RoutermodelModel
from applications.models import ApplicationModel

class Command(BaseCommand):
    help = 'Help text for your command'

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **kwargs):
        # adding list of switchmodel #########################
        SwitchesmodelModel.objects.all().delete()
        filename = './onboardOptions/switchs.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'model' : row['model']}
            obj , created = SwitchesmodelModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.model = row['model']
                obj.save()
        # adding list of switchmodel #########################

        # adding list of switchlayers #########################
        SwitcheslayersModel.objects.all().delete()
        filename = './onboardOptions/switchlayers.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'layers' : row['layers']}
            obj , created = SwitcheslayersModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.layers = row['layers']
                obj.save()
        # adding list of switchlayers #########################

        # adding list of servertypes #########################
        ServerstypesModel.objects.all().delete()
        filename = './onboardOptions/servertypes.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'types' : row['types']}
            obj , created = ServerstypesModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.types = row['types']
                obj.save()
        # adding list of servertypes #########################

        # adding list of firewallmodal #########################
        FirewalltypeModel.objects.all().delete()
        filename = './onboardOptions/firewallmodels.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'types' : row['types']}
            obj , created = FirewalltypeModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.types = row['types']
                obj.save()
        # adding list of firewallmodal #########################

        # adding list of firewallmake #########################
        FirewallmodelModel.objects.all().delete()
        filename = './onboardOptions/firewallmake.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'model' : row['model']}
            obj , created = FirewallmodelModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.model = row['model']
                obj.save()
        # adding list of firewallmake #########################

        # adding list of firewallmodal #########################
        RoutertypeModel.objects.all().delete()
        filename = './onboardOptions/routertypes.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'types' : row['types']}
            obj , created = RoutertypeModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.types = row['types']
                obj.save()
        # adding list of firewallmodal #########################

        # adding list of firewallmake #########################
        RoutermodelModel.objects.all().delete()
        filename = './onboardOptions/routermodel.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'model' : row['model']}
            obj , created = RoutermodelModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.model = row['model']
                obj.save()
        # adding list of firewallmake #########################

        # adding list of servertypes #########################
        ApplicationModel.objects.all().delete()
        filename = './onboardOptions/applicationname.csv'
        data = pd.read_csv(filename)
        for index, row in data.iterrows():
            query = {'applicationname' : row['applicationname']}
            obj , created = ApplicationModel.objects.get_or_create(defaults=query, **query)
            if created:
                obj.applicationname = row['applicationname']
                obj.save()
        # adding list of servertypes #########################