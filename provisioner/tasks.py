# provisioner/tasks.py
import logging
import uuid
import yaml
import requests

from django.conf import settings
from django.utils import timezone

from .models import ProvisionRequest
from .dokploy_client import (
    deploy_compose, generate_domain_for_app, create_domain_for_compose,
    poll_until, check_https_up, list_domains_for_compose, validate_domain
)

logger = logging.getLogger(__name__)

def build_compose_yaml(backend_repo, frontend_repo, tenant_suffix, db_name, db_user, db_password):
    """
    Build a docker-compose YAML string with concrete env values.
    Using Git build contexts for backend & frontend.
    """
    # use tenant_suffix to make service names unique if you plan multiple tenants on same host
    backend_service = f"backend_{tenant_suffix}"
    frontend_service = f"frontend_{tenant_suffix}"
    db_service = f"db_{tenant_suffix}"

    compose = {
        "version": "3.8",
        "services": {
            backend_service: {
                "build": {"context": backend_repo, "dockerfile": "Dockerfile"},
                "environment": {
                    "DB_NAME": db_name,
                    "DB_USER": db_user,
                    "DB_PASSWORD": db_password,
                    "DB_HOST": db_service,
                    "DB_PORT": "5432",
                    "DJANGO_SECRET_KEY": uuid.uuid4().hex,
                    "ALLOWED_HOSTS": ""  # will update after domain generation
                },
                "depends_on": [db_service],
                "expose": ["8000"]
            },
            frontend_service: {
                "build": {"context": frontend_repo, "dockerfile": "Dockerfile"},
                "environment": {
                    "REACT_APP_API_URL": "",  # will update after backend domain is available
                },
                "expose": ["3000"]
            },
            db_service: {
                "image": "postgres:15",
                "environment": {
                    "POSTGRES_DB": db_name,
                    "POSTGRES_USER": db_user,
                    "POSTGRES_PASSWORD": db_password
                },
                "volumes": [f"{db_service}-data:/var/lib/postgresql/data"],
                "expose": ["5432"]
            }
        },
        "volumes": {
            f"{db_service}-data": None
        }
    }
    # Convert to YAML with safe_dump
    compose_yaml = yaml.safe_dump(compose, sort_keys=False)
    return compose_yaml, backend_service, frontend_service, db_service

def provision_tenant_task(prov_request_id, payload):
    """
    Heavy lifting task. Run this in background (Celery or thread).
    payload contains: email, company, subdomain, password, secrets...
    """
    pr = ProvisionRequest.objects.get(id=prov_request_id)
    try:
        pr.status = "provisioning"
        pr.save()

        # settings
        project_id = settings.DOKPLOY_PROJECT_ID
        server_id = settings.DOKPLOY_SERVER_ID
        backend_repo = payload.get("backend_repo", "https://github.com/vista/schoolcare-backend.git")
        frontend_repo = payload.get("frontend_repo", "https://github.com/vista/schoolcare-frontend.git")

        # generate tenant suffix/ids
        tenant_suffix = uuid.uuid4().hex[:8]
        db_name = f"db_{tenant_suffix}"
        db_user = f"user_{tenant_suffix}"
        db_password = payload.get("password") or uuid.uuid4().hex[:12]

        # 1) create & deploy compose
        compose_yaml, backend_service_name, frontend_service_name, db_service_name = build_compose_yaml(
            backend_repo,
            frontend_repo,
            tenant_suffix,
            db_name,
            db_user,
            db_password
        )

        pr.detail += "\nDeploying compose..."
        pr.save()

        deploy_resp = deploy_compose(project_id, server_id, f"lms-{tenant_suffix}", compose_yaml)
        # deploy_resp might include 'id' or 'composeId' or 'applicationId'
        compose_id = deploy_resp.get("id") or deploy_resp.get("composeId") or deploy_resp.get("applicationId")
        if not compose_id:
            # If deploy returned raw text or different structure, capture whole response for debugging
            pr.status = "failed"
            pr.detail += f"\ncompose.deploy returned unexpected response: {deploy_resp}"
            pr.save()
            return

        pr.detail += f"\nCompose deployed: id={compose_id}"
        pr.save()

        # 2) Generate (or create) backend domain and attach it to the backend service
        backend_domain = None
        try:
            # Try Dokploy domain.generateDomain using the compose/application id
            generated = generate_domain_for_app(compose_id)
            # if function returned a string domain use it; else continue
            if isinstance(generated, str) and generated:
                backend_domain = generated.strip().strip('"')
        except Exception as exc:
            # fallback: forge a host and call domain.create with composeId + serviceName
            logger.warning("domain.generateDomain failed: %s", exc)

        if not backend_domain:
            # fallback host
            host = f"{compose_id}-{tenant_suffix}.traefik.me"
            # create domain targeted at compose & service
            create_resp = create_domain_for_compose(host, compose_id, backend_service_name, port=8000)
            # create_resp may return object with id; the actual host is what we passed
            backend_domain = host
            # optionally extract domain id:
            domain_id = create_resp.get("id") if isinstance(create_resp, dict) else None
            if domain_id:
                validate_domain(domain_id)

        pr.detail += f"\nBackend domain created: {backend_domain}"
        pr.save()

        # 3) Wait until backend https responds (Let's Encrypt may take a short while)
        poll_until(lambda: (check_https_up(backend_domain), None), timeout=300, interval=5)
        pr.detail += f"\nBackend domain is up: https://{backend_domain}"
        pr.save()

        # 4) Update frontend env to point to backend domain.
        # Approach: call domain.generate/create for frontend and then call compose.update or redeploy.
        # Simpler: create frontend domain now, get host, then instruct Dokploy to update compose env for frontend.
        frontend_domain = None
        try:
            generated_f = generate_domain_for_app(compose_id)
            if isinstance(generated_f, str) and generated_f:
                frontend_domain = generated_f.strip().strip('"')
        except Exception:
            frontend_domain = f"{compose_id}-fe-{tenant_suffix}.traefik.me"
            create_domain_for_compose(frontend_domain, compose_id, frontend_service_name, port=3000)

        pr.detail += f"\nFrontend domain created: {frontend_domain}"
        pr.save()

        # Wait until frontend https responds (optional)
        try:
            poll_until(lambda: (check_https_up(frontend_domain), None), timeout=240, interval=5)
        except Exception:
            # not fatal — continue
            pr.detail += f"\nFrontend https not responding yet; continue."
            pr.save()

        # 5) Call internal provision endpoint on backend to create admin user
        prov_token = settings.PROVISION_CALLBACK_TOKEN
        admin_payload = {
            "admin_email": payload.get("email"),
            "admin_password": payload.get("password") or db_password,
            "tenant_id": tenant_suffix,
            "company": payload.get("company")
        }
        prov_url = f"https://{backend_domain}/internal/provision"
        headers = {"Content-Type": "application/json", "X-Provision-Token": prov_token}
        # call once (could implement retries)
        resp = requests.post(prov_url, json=admin_payload, headers=headers, timeout=30, verify=False)
        if resp.status_code not in (200, 201):
            pr.status = "failed"
            pr.detail += f"\nProvision call failed: {resp.status_code} {resp.text}"
            pr.save()
            return

        # 6) Success
        pr.status = "completed"
        pr.detail += f"\nProvisioning complete. backend_url=https://{backend_domain}, frontend_url=https://{frontend_domain}"
        pr.save()

    except Exception as e:
        logger.exception("Provisioning failed")
        pr.status = "failed"
        pr.detail += f"\nError: {str(e)}"
        pr.save()
