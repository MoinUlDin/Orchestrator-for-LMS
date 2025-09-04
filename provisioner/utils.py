import re
from django.db.models import Max
from .models import ProvisionRequest

def generate_project_name(client_name):
    # Normalize: strip, collapse spaces, capitalize
    normalized = " ".join(client_name.strip().split())
    normalized = "-".join(word.capitalize() for word in normalized.split())
    
    # Count existing projects with same base name to generate suffix
    base_name = normalized
    last_project = ProvisionRequest.objects.filter(project_name__startswith=base_name).aggregate(Max('project_name'))
    last_name = last_project['project_name__max']
    
    if last_name:
        # Extract last suffix
        match = re.search(r'(\d+)$', last_name)
        suffix = int(match.group(1)) + 1 if match else 1
    else:
        suffix = 1

    return f"{base_name}-{suffix:03d}"
