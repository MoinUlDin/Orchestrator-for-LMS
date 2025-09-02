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
    poll_until, check_https_up, list_domains_for_compose, validate_domain, get_or_create_project,
    get_compose_one,update_compose, redeploy_compose, create_compose
)

logger = logging.getLogger(__name__)


def patch_compose_env_and_dump(compose_yaml_str, service_env_map):
    """
    compose_yaml_str -> modify each service environment variables based on service_env_map
    service_env_map: { "service_name": {"VAR": "value", ...}, ... }
    returns: updated YAML string
    """
    data = yaml.safe_load(compose_yaml_str)
    services = data.get("services", {})

    for svc_name, env_updates in service_env_map.items():
        svc = services.get(svc_name)
        if not svc:
            # maybe name mismatch, skip
            continue
        # ensure environment exists as mapping
        env_block = svc.get("environment") or {}
        # if environment is a list, convert to dict
        if isinstance(env_block, list):
            # list of "KEY=VAL"
            tmp = {}
            for entry in env_block:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    tmp[k] = v
            env_block = tmp
        # apply updates
        env_block.update(env_updates)
        svc["environment"] = env_block
        services[svc_name] = svc

    data["services"] = services
    return yaml.safe_dump(data, sort_keys=False)


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
                "build": {"context": backend_repo, "dockerfile": "DockerFile"},
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
                "build": {"context": frontend_repo, "dockerfile": "DockerFile"},
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
    Orchestrator task: create project, deploy compose, create domains,
    patch compose envs (frontend -> backend URL), and call internal provision.
    """
    pr = ProvisionRequest.objects.get(id=prov_request_id)
    print("\ngot a request\n")
    try:
        pr.status = "provisioning"
        pr.detail += "\nStarting provisioning..."
        pr.save()

        # 1) generate tenant suffix/ids (do this early)
        tenant_suffix = uuid.uuid4().hex[:8]
        db_name = f"db_{tenant_suffix}"
        db_user = f"user_{tenant_suffix}"
        db_password = payload.get("password") or uuid.uuid4().hex[:12]

        # 2) create or find Dokploy project (idempotent)
        tenant_name = payload.get("client_ref") or f"lms-{tenant_suffix}"
        project_desc = f"LMS tenant for {payload.get('company') or payload.get('email')}"
        pr.detail += f"\nCreating/finding project {tenant_name}..."
        pr.save()
        print(f'\n creating or finding Project\n')
        proj_obj = get_or_create_project(name=tenant_name, description=project_desc)

        proj_id = None
        if isinstance(proj_obj, dict):
            proj_id = proj_obj.get("id") or proj_obj.get("projectId") or proj_obj.get("_id")
        elif isinstance(proj_obj, str):
            proj_id = proj_obj.strip().strip('"')

        print(f'\n project created or not id: {proj_id} \n')
        if not proj_id:
            pr.status = "failed"
            pr.detail += f"\nFailed to create/find Dokploy project: {proj_obj}"
            pr.save()
            return

        pr.project_id = proj_id
        pr.save()

        # 3) prepare compose YAML and deploy (use proj_id)
        backend_repo = payload.get("backend_repo") or settings.BACKEND_REPO
        frontend_repo = payload.get("frontend_repo") or settings.FRONTEND_REPO
        print(f'\n got b: {settings.BACKEND_REPO}')
        print(f'\n got f: {settings.FRONTEND_REPO}')
        compose_yaml, backend_service_name, frontend_service_name, db_service_name = build_compose_yaml(
            backend_repo,
            frontend_repo,
            tenant_suffix,
            db_name,
            db_user,
            db_password
        )
        print(f'Buid Composer')
        pr.detail += "\nDeploying compose..."
        pr.save()

        deploy_resp = create_compose(
        project_id=proj_id,
        name=f"lms-{tenant_suffix}",
        compose_yaml=compose_yaml,
        app_name=f"lms-{tenant_suffix}",
        description=f"Tenant compose for {payload.get('company') or payload.get('email')}"
        )
        
        print(f"\nComposer- Created Successfully\n deploy_Resp: {deploy_resp}")
        deploy_resp = deploy_compose(proj_id)

        # parse compose_id (support multiple shapes)
        
        compose_id = None
        if isinstance(deploy_resp, dict):
            compose_id = deploy_resp.get("id") or deploy_resp.get("composeId") or deploy_resp.get("applicationId")
        elif isinstance(deploy_resp, str):
            # maybe raw id string
            compose_id = deploy_resp.strip().strip('"')

        print(f"\nComposer- Created ID: {compose_id}\n")
        if not compose_id:
            pr.status = "failed"
            pr.detail += f"\ncompose.deploy returned unexpected response: {deploy_resp}"
            pr.save()
            return

        pr.compose_id = compose_id
        pr.detail += f"\nCompose deployed: id={compose_id}"
        pr.save()

        print(f"\nGenerating Domains\n")
        # 4) generate backend domain (try generateDomain, fallback to create)
        backend_domain = None
        try:
            generated = generate_domain_for_app(compose_id)
            if isinstance(generated, str) and generated:
                backend_domain = generated.strip().strip('"')
        except Exception as exc:
            logger.warning("domain.generateDomain failed: %s", exc)

        if not backend_domain:
            host = f"{compose_id}-{tenant_suffix}.traefik.me"
            create_resp = create_domain_for_compose(host, compose_id, backend_service_name, port=8000)
            backend_domain = host
            domain_id = create_resp.get("id") if isinstance(create_resp, dict) else None
            if domain_id:
                try:
                    validate_domain(domain_id)
                except Exception:
                    pass

        pr.backend_domain = backend_domain
        pr.detail += f"\nBackend domain created: {backend_domain}"
        pr.save()

        # 5) wait for backend HTTPS to respond
        poll_until(lambda: (check_https_up(backend_domain), None), timeout=300, interval=5)
        pr.detail += f"\nBackend is up: https://{backend_domain}"
        pr.save()

        # 6) create frontend domain
        frontend_domain = None
        try:
            generated_f = generate_domain_for_app(compose_id)
            if isinstance(generated_f, str) and generated_f:
                frontend_domain = generated_f.strip().strip('"')
        except Exception:
            frontend_domain = f"{compose_id}-fe-{tenant_suffix}.traefik.me"
            create_domain_for_compose(frontend_domain, compose_id, frontend_service_name, port=3000)

        pr.frontend_domain = frontend_domain
        pr.detail += f"\nFrontend domain created: {frontend_domain}"
        pr.save()

        # 7) Patch compose YAML to set ALLOWED_HOSTS on backend and REACT_APP_API_URL for frontend
        pr.detail += "\nPatching compose environment with domains..."
        pr.save()

        # get current compose (compose.one returns current compose YAML in some Dokploy versions)
        try:
            current = get_compose_one(compose_id)
            # assume current contains 'compose' (YAML string) or returns the YAML directly
            existing_compose_yaml = None
            if isinstance(current, dict):
                existing_compose_yaml = current.get("compose") or current.get("data") or yaml.safe_dump(current)
            else:
                existing_compose_yaml = str(current)
        except Exception:
            # fallback to the compose we built earlier
            existing_compose_yaml = compose_yaml

        # build env updates
        backend_api_url = f"https://{backend_domain}"
        service_env_map = {
            backend_service_name: {"ALLOWED_HOSTS": backend_domain},
            frontend_service_name: {"REACT_APP_API_URL": backend_api_url}
        }

        updated_compose_yaml = patch_compose_env_and_dump(existing_compose_yaml, service_env_map)

        # call compose.update + redeploy to apply env changes
        update_compose_resp = update_compose(compose_id, updated_compose_yaml)
        redeploy_compose(compose_id)

        pr.detail += "\nCompose updated with new envs and redeploy triggered."
        pr.save()

        # 8) wait for frontend https (optional)
        try:
            poll_until(lambda: (check_https_up(frontend_domain), None), timeout=240, interval=5)
            pr.detail += f"\nFrontend is up: https://{frontend_domain}"
            pr.save()
        except Exception:
            pr.detail += "\nFrontend not responding yet; continuing."
            pr.save()

        # 9) call internal provision endpoint to create admin
        prov_token = settings.PROVISION_CALLBACK_TOKEN
        admin_payload = {
            "admin_email": payload.get("email"),
            "admin_password": payload.get("password") or db_password,
            "tenant_id": tenant_suffix,
            "company": payload.get("company")
        }
        prov_url = f"https://{backend_domain}/internal/provision"
        headers = {"Content-Type": "application/json", "X-Provision-Token": prov_token}

        resp = requests.post(prov_url, json=admin_payload, headers=headers, timeout=30, verify=False)
        if resp.status_code not in (200, 201):
            pr.status = "failed"
            pr.detail += f"\nProvision call failed: {resp.status_code} {resp.text}"
            pr.save()
            return

        pr.status = "completed"
        pr.detail += f"\nProvisioning complete. backend_url=https://{backend_domain}, frontend_url=https://{frontend_domain}"
        pr.save()

    except Exception as e:
        logger.exception("Provisioning failed")
        pr.status = "failed"
        pr.detail += f"\nError: {str(e)}"
        pr.save()
