# provisioner/apps.py
from django.apps import AppConfig

class ProvisionerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'provisioner'

    def ready(self):
        from .scheduler import start_scheduler
        start_scheduler()
