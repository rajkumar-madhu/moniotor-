from django.apps import AppConfig
from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from argparse import ArgumentParser

#def register_commands(parser: ArgumentParser):
 #   parser.add_argument('LEDefaultTableValues', nargs='?', type=str, help='Help text for your command')

class addserviceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'addservice'
    
    def ready(self):
        self._commands = {}
        parser = ArgumentParser()
        #register_commands(parser)
        self._commands['LEDefaultAddservices'] = parser
