# provisioner/dokploy_client.py
import time
import requests
import yaml
import uuid

from django.conf import settings

API_BASE = getattr(settings, "DOKPLOY_API", "https://dokploy.thevista.one/api")
API_KEY = getattr(settings, "DOKPLOY_API_KEY", None)
HEADERS = {
    "Accept": "application/json",
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
}

class DokployError(Exception):
    pass

def _post(path, json=None, timeout=30):
    url = f"{API_BASE}{path}"
    r = requests.post(url, json=json or {}, headers=HEADERS, timeout=timeout)
    if not r.ok:
        raise DokployError(f"POST {path} failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except ValueError:
        # some endpoints return raw strings (eg. domain.generateDomain returns quoted string)
        return r.text.strip().strip('"')

def _get(path, params=None, timeout=30):
    url = f"{API_BASE}{path}"
    r = requests.get(url, headers=HEADERS, params=params or {}, timeout=timeout)
    if not r.ok:
        raise DokployError(f"GET {path} failed: {r.status_code} {r.text}")
    return r.json()

def deploy_compose(project_id, name, compose_yaml):
    """
    Deploy a single compose YAML to Dokploy.
    Returns response JSON (dokploy returns some object with id keys).
    """
    body = {
        "projectId": project_id,
        "name": name,
        "compose": compose_yaml,
        "restart": "always"
    }
    return _post("/compose.deploy", json=body, timeout=120)

def get_compose_one(compose_id):
    return _get("/compose.one", params={"id": compose_id})

def generate_domain_for_app(app_id):
    # returns domain string or raise
    return _post("/domain.generateDomain", json={"appName": app_id})

def create_domain_for_compose(host, compose_id, service_name, port=8000):
    body = {
        "host": host,
        "port": port,
        "https": True,
        "composeId": compose_id,
        "serviceName": service_name,
        "certificateType": "letsencrypt",
        "domainType": "compose"
    }
    return _post("/domain.create", json=body)

def list_domains_for_compose(compose_id):
    return _get("/domain.byComposeId", params={"composeId": compose_id})

def validate_domain(domain_id):
    return _post("/domain.validateDomain", json={"id": domain_id})

def redeploy_compose(compose_id):
    return _post("/compose.redeploy", json={"composeId": compose_id})

def poll_until(predicate_fn, timeout=300, interval=5):
    start = time.time()
    while True:
        ok, data = predicate_fn()
        if ok:
            return data
        if time.time() - start > timeout:
            raise DokployError("Timed out waiting for condition")
        time.sleep(interval)

def check_https_up(domain, timeout=3):
    try:
        r = requests.get(f"https://{domain}", timeout=timeout)
        return r.status_code in (200, 301, 302)
    except Exception:
        return False


def list_projects():
    return _get("/project.all")

def create_project(name, description=""):
    body = {"name": name, "description": description, "env": ""}
    return _post("/project.create", json=body)

def get_or_create_project(name, description=""):
    # Try to find existing project by name
    projects = list_projects()
    if isinstance(projects, dict):
        # some Dokploy versions return {"data": [...]} -- try common patterns
        candidates = projects.get("data") or projects.get("projects") or []
    else:
        candidates = projects

    for p in candidates:
        # project object often has 'id' and 'name'
        if p.get("name") == name:
            return p  # return full project object

    # not found -> create
    created = create_project(name=name, description=description)
    # created usually includes id, but shape may vary; return created object
    return created


def update_compose(compose_id, compose_yaml):
    """Update an existing compose stack (replace its compose)."""
    body = {
        "id": compose_id,
        "compose": compose_yaml
    }
    return _post("/compose.update", json=body, timeout=120)