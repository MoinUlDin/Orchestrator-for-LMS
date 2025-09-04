from django.db import models

class ProvisionRequest(models.Model):
    client_ref = models.CharField(max_length=128, unique=True, null=True, blank=True)
    email = models.EmailField()
    company = models.CharField(max_length=255, blank=True)
    subdomain = models.CharField(unique=True, max_length=255, blank=True)
    tenant_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=50, default="pending")  # pending, provisioning, failed, completed
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # new fields
    client_name = models.CharField(max_length=255)  # Required for project naming
    project_name = models.CharField(max_length=255, blank=True, null=True)
    project_id = models.CharField(max_length=128, blank=True, null=True)
    backend_id = models.CharField(max_length=128, blank=True, null=True) #to store backend service id
    frontend_id = models.CharField(max_length=128, blank=True, null=True)#to store frontend service id
    db_id = models.CharField(max_length=128, blank=True, null=True)#to store db id
    backend_domain = models.CharField(max_length=255, blank=True, null=True)
    frontend_domain = models.CharField(max_length=255, blank=True, null=True)
    
    db_id = models.CharField(max_length=128, blank=True, null=True)
    db_app_name = models.CharField(max_length=255, blank=True, null=True)
    db_name = models.CharField(max_length=255, blank=True, null=True)
    db_user = models.CharField(max_length=255, blank=True, null=True)
    db_password = models.CharField(max_length=255, blank=True, null=True)
    db_port = models.CharField(max_length=10, blank=True, null=True)

    # Progress tracking fields
    project_created = models.BooleanField(default=False)
    backend_created = models.BooleanField(default=False)
    backend_git_attached = models.BooleanField(default=False)
    backend_build_configured = models.BooleanField(default=False)
    db_created = models.BooleanField(default=False)
    backend_env_configured = models.BooleanField(default=False)
    postgres_deploy_triggered = models.BooleanField(default=False)
    backend_deploy_triggered = models.BooleanField(default=False)
    frontend_created = models.BooleanField(default=False)
    frontend_git_attached = models.BooleanField(default=False)
    frontend_build_configured = models.BooleanField(default=False)
    frontend_deploy_triggered = models.BooleanField(default=False)
    domains_configured = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    failed = models.BooleanField(default=False)

    
    def __str__(self):
        return f"{self.email} - {self.status}"
