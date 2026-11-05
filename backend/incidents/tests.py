from django.test import TestCase
from .models import IncidentModel

class IncidentModelTest(TestCase):
    def setUp(self):
        IncidentModel.objects.create(
            number='TEST001',
            short_description='Test incident',
            description='Test description',
            priority='P3',
            state='NEW',
            site_name='test'
        )
    
    def test_incident_creation(self):
        incident = IncidentModel.objects.get(number='TEST001')
        self.assertEqual(incident.short_description, 'Test incident')
        self.assertEqual(incident.priority, 'P3')
        self.assertEqual(incident.state, 'NEW')
