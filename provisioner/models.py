from django.db import models

class ProvisionRequest(models.Model):
    client_ref = models.CharField(max_length=128, unique=True, null=True, blank=True)
    email = models.EmailField()
    company = models.CharField(max_length=255, blank=True)
    subdomain = models.CharField(max_length=255, blank=True)
    tenant_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=50, default="pending")  # pending, provisioning, failed, completed
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # new fields
    project_id = models.CharField(max_length=128, blank=True, null=True)
    compose_id = models.CharField(max_length=128, blank=True, null=True)
    backend_domain = models.CharField(max_length=255, blank=True, null=True)
    frontend_domain = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"{self.email} - {self.status}"
