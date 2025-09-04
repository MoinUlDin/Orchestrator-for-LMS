# provisioner/dokploy_client.py
import time
import logging
import requests
from typing import Optional, Dict, Any, List
from requests.exceptions import RequestException
from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE = getattr(settings, "DOKPLOY_API", "https://dokploy.thevista.one/api")
API_KEY = getattr(settings, "DOKPLOY_TOKEN", None)

# Default retry settings (can override from Django settings)
DEFAULT_MAX_RETRIES = getattr(settings, "DOKPLOY_MAX_RETRIES", 5)
DEFAULT_RETRY_DELAY = getattr(settings, "DOKPLOY_RETRY_DELAY", 3)  # seconds, base delay
DEFAULT_BACKOFF_FACTOR = getattr(settings, "DOKPLOY_BACKOFF_FACTOR", 2.0)

def _headers():
    return {
        "Accept": "application/json",
        "x-api-key": API_KEY,
        "Content-Type": "application/json",
    }

class DokployError(Exception):
    pass


def _sleep_with_backoff(attempt: int, base_delay: float):
    # exponential backoff: base_delay * 2^(attempt-1)
    delay = base_delay * (2 ** (attempt - 1))
    # small safety cap
    max_cap = getattr(settings, "DOKPLOY_MAX_RETRY_DELAY_CAP", 60)
    if delay > max_cap:
        delay = max_cap
    time.sleep(delay)


def _post(path: str, json: dict = None, timeout: int = 40, retry: bool = True,
          max_retries: int = None, base_delay: float = None):
    """
    POST wrapper with optional retries.
    - path: path appended to API_BASE (e.g. "/project/create" or "/api/application.create/")
    - json: payload
    - retry: True/False (default True)
    - max_retries: override default
    - base_delay: initial retry delay in seconds (exponential backoff)
    Returns parsed JSON (or raw text when response not JSON).
    Raises DokployError on permanent failure.
    """
    url = f"{API_BASE}{path}"
    headers = _headers()

    if not retry:
        try:
            r = requests.post(url, json=json or {}, headers=headers, timeout=timeout)
        except RequestException as e:
            raise DokployError(f"POST {path} failed: {e}")
        if not r.ok:
            raise DokployError(f"POST {path} failed: {r.status_code} {r.text}")
        try:
            return r.json()
        except ValueError:
            return r.text.strip().strip('"')

    # retry mode
    max_retries = max_retries if max_retries is not None else DEFAULT_MAX_RETRIES
    base_delay = base_delay if base_delay is not None else DEFAULT_RETRY_DELAY

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug("POST %s attempt %d/%d payload=%s", path, attempt, max_retries, json)
            print(f"\n       ==Trying #{attempt} to: {url} with: POST == \n")
            r = requests.post(url, json=json or {}, headers=headers, timeout=timeout)
            if not r.ok:
                # treat non-2xx as failure to retry
                last_exc = DokployError(f"POST {path} failed: {r.status_code} {r.text}")
                logger.warning("POST %s returned non-OK status on attempt %d: %s", path, attempt, last_exc)
                raise last_exc
            try:
                return r.json()
            except ValueError:
                return r.text.strip().strip('"')
        except (RequestException, DokployError) as e:
            last_exc = e
            logger.exception("POST %s attempt %d failed: %s", path, attempt, e)
            if attempt < max_retries:
                _sleep_with_backoff(attempt, base_delay)
            else:
                logger.error("POST %s all %d attempts failed", path, max_retries)
                raise DokployError(f"POST {path} failed after {max_retries} attempts: {e}") from e


def _get(path: str, params: dict = None, timeout: int = 30, retry: bool = True,
         max_retries: int = None, base_delay: float = None):
    """
    GET wrapper with optional retries.
    - retry True by default
    """
    url = f"{API_BASE}{path}"
    headers = _headers()

    if not retry:
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
        except RequestException as e:
            raise DokployError(f"GET {path} failed: {e}")
        if not r.ok:
            raise DokployError(f"GET {path} failed: {r.status_code} {r.text}")
        try:
            return r.json()
        except ValueError:
            return r.text.strip().strip('"')

    max_retries = max_retries if max_retries is not None else DEFAULT_MAX_RETRIES
    base_delay = base_delay if base_delay is not None else DEFAULT_RETRY_DELAY

    last_exc = None
    for attempt in range(1, max_retries + 1):
        print(f"\n       ==Trying #{attempt} to: {url} with: GET == \n")
        try:
            logger.debug("GET %s attempt %d/%d params=%s", path, attempt, max_retries, params)
            r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
            if not r.ok:
                last_exc = DokployError(f"GET {path} failed: {r.status_code} {r.text}")
                logger.warning("GET %s returned non-OK status on attempt %d: %s", path, attempt, last_exc)
                raise last_exc
            try:
                return r.json()
            except ValueError:
                return r.text.strip().strip('"')
        except (RequestException, DokployError) as e:
            last_exc = e
            logger.exception("GET %s attempt %d failed: %s", path, attempt, e)
            if attempt < max_retries:
                _sleep_with_backoff(attempt, base_delay)
            else:
                logger.error("GET %s all %d attempts failed", path, max_retries)
                raise DokployError(f"GET {path} failed after {max_retries} attempts: {e}") from e


# Helpers
def create_application(project_id: str, name: str, description: str, timeout: int = 40) -> Dict[str, Any]:
    """
    POST /application.create/ -> returns response dict (may contain applicationId or id)
    """
    payload = {"name": name, "description": description, "projectId": project_id}
    return _post("/application.create/", json=payload, timeout=timeout)

def save_git_provider(application_id: str, custom_git_url: str, branch: str = "main", build_path: str = "/") -> Dict[str, Any]:
    """
    POST /application.saveGitProdiver
    """
    payload = {
        "customGitBranch": branch,
        "applicationId": application_id,
        "customGitBuildPath": build_path,
        "customGitUrl": custom_git_url,
        "enableSubmodules": False,
    }
    return _post("/application.saveGitProdiver", json=payload)

def save_build_type(application_id: str,
                    build_type: str = "dockerfile",
                    dockerfile: str = "./DockerFile",
                    docker_context_path: str = "",
                    docker_build_stage: str = "",
                    is_static_spa: bool = False,
                    publish_directory: Optional[str] = None) -> Dict[str, Any]:
    """
    POST /application.saveBuildType
    Accepts either defaults for Dockerfile builds or custom payloads for SPA.
    """
    payload = {
        "applicationId": application_id,
        "buildType": build_type,
        "dockerfile": dockerfile,
        "dockerContextPath": docker_context_path,
        "dockerBuildStage": docker_build_stage,
        "isStaticSpa": is_static_spa,
    }
    if publish_directory:
        payload["publishDirectory"] = publish_directory
    return _post("/application.saveBuildType", json=payload)

def save_environment(application_id: str, env_str: str) -> Dict[str, Any]:
    """
    POST /application.saveEnvironment
    env_str = multi-line 'KEY=VALUE\nKEY2=VALUE2'
    """
    payload = {"applicationId": application_id, "env": env_str}
    return _post("/application.saveEnvironment", json=payload)

def create_postgres(project_id: str, name: str, app_name: str, database_name: str, database_user: str, database_password: str, docker_image: str = "postgres:15") -> Any:
    """
    POST /postgres.create/ -> usually returns True or response. Return value forwarded.
    """
    payload = {
        "name": name,
        "appName": app_name,
        "databaseName": database_name,
        "databaseUser": database_user,
        "databasePassword": database_password,
        "dockerImage": docker_image,
        "projectId": project_id,
        "description": f"Postgres DB {database_name} for project {project_id}"
    }
    return _post("/postgres.create/", json=payload)

def deploy_postgres(postgres_id: str) -> Any:
    """POST /postgres.deploy"""
    return _post("/postgres.deploy", json={"postgresId": postgres_id})

def deploy_application(application_id: str) -> Any:
    """POST /application.deploy"""
    return _post("/application.deploy", json={"applicationId": application_id})

def create_domain(application_id: str, host: str, port: int = 80, https: bool = True,
                  certificate_type: str = "letsencrypt", domain_type: str = "application") -> Any:
    """POST /domain.create"""
    payload = {
        "host": host,
        "port": port,
        "https": https,
        "applicationId": application_id,
        "certificateType": certificate_type,
        "domainType": domain_type,
    }
    return _post("/domain.create", json=payload)

def get_all_projects() -> List[Dict[str, Any]]:
    """GET /project.all"""
    return _get("/project.all")

def delete_domain(domain_id: str, timeout: int = 30):
    """
    Delete a domain by domainId.

    POST /domain.delete
    Payload: {"domainId": "<id>"}

    Returns whatever dokploy returns (dict/True/etc). Raises DokployError on failure.
    """
    if not domain_id:
        raise ValueError("domain_id is required")

    payload = {"domainId": domain_id}
    return _post("/domain.delete", json=payload, timeout=timeout)




