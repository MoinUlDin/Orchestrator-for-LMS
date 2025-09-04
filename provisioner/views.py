# provisioner/views.py
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from .models import ProvisionRequest
from .tasks import provision_tenant_task

from .scheduler import schedule_provision_job
import uuid

@api_view(['POST'])
@permission_classes([AllowAny])
def provision_request_view(request):
    """
    Entry endpoint for creating a provisioning request.
    Expects JSON:
    {
      "client_ref": "optional-id",
      "secret1": "...",         # Required
      "secret2": "...",         # Required
      "email": "...",           # Required
      "client_name": "..."      # Required
      "subdomain": "...",       # Required
      "company": "...",      # optional
      "password": "..."      # admin password for internal provision
    }
    """
    data = request.data
    s1 = data.get("secret1")
    s2 = data.get("secret2")
    
    if s1 != settings.PROVISION_SECRET_1 or s2 != settings.PROVISION_SECRET_2:
        return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

    client_ref = data.get("client_ref")
    client_name = data.get("client_name")  # required
    subdomain = data.get("subdomain")
    if not client_name:
        return Response({"detail": "client_name is required"}, status=status.HTTP_400_BAD_REQUEST)

    if not subdomain:
        return Response({"detail": "subdomain is required"}, status=status.HTTP_400_BAD_REQUEST)
    
    if subdomain:
        try:
            existing = ProvisionRequest.objects.get(subdomain=subdomain)
            if existing:
                return Response({"detail": "Given subdomain already exist choose another one"}, status=status.HTTP_400_BAD_REQUEST)
        except ProvisionRequest.DoesNotExist:
            pass
    # Idempotency: if client_ref exists
    if client_ref:
        try:
            existing = ProvisionRequest.objects.get(client_ref=client_ref)
            if existing.status == "completed":
                return Response({
                    "detail": "already_provisioned",
                    "id": existing.id,
                    "status": existing.status,
                    "detail_text": existing.detail
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "detail": "already_exists",
                    "id": existing.id,
                    "status": existing.status,
                    "detail_text": existing.detail
                }, status=status.HTTP_202_ACCEPTED)
        except ProvisionRequest.DoesNotExist:
            pass

    # Create ProvisionRequest
    pr = ProvisionRequest.objects.create(
        client_ref=client_ref,
        client_name=client_name,
        email=data.get("email"),
        company=data.get("company", ""),
        subdomain=data.get("subdomain", ""),
        status="pending"
    )

    payload = {
        "email": data.get("email"),
        "company": data.get("company"),
        "subdomain": data.get("subdomain"),
        "password": data.get("password"),
        "backend_repo": data.get("backend_repo"),
        "frontend_repo": data.get("frontend_repo"),
    }

    # Schedule project creation task
    schedule_provision_job(pr.id, payload, run_in_seconds=1)

    return Response({"detail": "accepted", "id": pr.id}, status=status.HTTP_202_ACCEPTED)

@api_view(['POST'])
@permission_classes([AllowAny])
def executeme(request):
    fun = request.data["fun"]
    print(request.data)
    r = None
    if fun=='list_projects':
        print('\n Getting project list\n')
    
    return Response({"ok": "got you"}, status=status.HTTP_200_OK)
