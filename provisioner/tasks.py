# provisioner/tasks.py
import logging, uuid, re, yaml, time
import requests
import secrets
from django.conf import settings
from django.utils import timezone
from .utils import generate_project_name
from .dokploy_client import _post, _get, DokployError
from .models import ProvisionRequest
from .progress import mark_failure, mark_step, mark_running
from .dokploy_client import (
    create_application,
    save_git_provider,
    save_build_type,
    save_environment,
    create_domain,
    get_all_projects,
    deploy_postgres,
    deploy_application,
    
)

logger = logging.getLogger(__name__)


# Manual work
def extract_id_from_resp(resp):
    """
    Try to pull an id string from different Dokploy response shapes.
    Prioritize common keys returned by your Dokploy instance:
      - projectId (projects)
      - id / _id (apps/endpoints)
      - applicationId/appId etc.
    Accepts dict or raw string.
    """
    if resp is None:
        return None

    # If response is a plain string, assume it's the id
    if isinstance(resp, str):
        raw = resp.strip().strip('"').strip("'")
        return raw if raw else None

    # If it's a dict, try prioritized keys first
    if isinstance(resp, dict):
        # Priority map (most likely keys first)
        priority_keys = [
            "projectId",
            "applicationId",
            "appId",
            "id",
            "_id",
            "project_id",
            "application_id",
        ]

        for key in priority_keys:
            val = resp.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        # If 'data' contains nested dict with ids
        if isinstance(resp.get("data"), dict):
            for key in priority_keys:
                val = resp["data"].get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

        # Last-resort: take the first string-like value that looks like an id
        for v in resp.values():
            if isinstance(v, str) and v.strip() and " " not in v and len(v.strip()) >= 6:
                return v.strip()

    return None

def provision_tenant_task(prov_request_id, payload):
    """
    Orchestrator task: create project, deploy compose, create domains,
    patch compose envs (frontend -> backend URL), and call internal provision.
    """
    # Step 1: create project
    success = create_project_task(prov_request_id)

    # At this point, we know if project creation succeeded
    pr = ProvisionRequest.objects.get(id=prov_request_id)
    if not success:
        logger.error(f"Provisioning failed at project creation for {pr.client_name}")
        return
    
    time.sleep(2) # giving dokploy some time to refresh db
    ok = create_backend_service_task(prov_request_id)
    if not ok:
        print("process Failed while creating backend Service")
        return
    
    time.sleep(1) # giving dokploy some time to refresh db
    ok = create_postgres_task(prov_request_id)
    if not ok:
        return

    pg_resp, app_resp = deploy_db_then_app_quick(prov_request_id)
    if pg_resp is None:
        # DB deploy failed; pr.status already set to 'failed'
        return
    
    print("\n Waiting for 4 menutes so that backend deployment got finished")
    time.sleep(240) # giving backend some time to deploy
    ok = create_frontend_service_task(prov_request_id)
    if not ok:
        return
    
    ok = create_domains_task(prov_request_id)
    if not ok:
        return
    
    print("\njob Completed Successfully\n")
    return
    

# 1 create Project 
def create_project_task(prov_request_id):
    print(f'\n Creating Project \n')
    pr = ProvisionRequest.objects.get(id=prov_request_id)

    if not pr.client_name:
        pr.status = "failed"
        pr.detail = "Missing client_name"
        pr.save()
        return False

    mark_running(pr, True) # saving for progress
    
    project_name = generate_project_name(pr.client_name)
    pr.project_name = project_name
    pr.status = "provisioning"
    pr.save()

    try:
        payload = {"name": project_name, "description": f"Project for {pr.client_name}"}
        resp = _post("/project/create", json=payload)  # retry-enabled by default

        # extract project id robustly (will pick up "projectId")
        project_id = extract_id_from_resp(resp)
        if not project_id:
            pr.status = "failed"
            pr.detail = f"project.create returned unexpected response: {resp}"
            pr.save()
            logger.error("project.create unexpected response for prov_request=%s: %s", prov_request_id, resp)
            return False

        pr.project_id = project_id
        pr.status = "project_created"
        pr.detail = f"Project created (id={project_id})"
        pr.save()
        # mark progress
        mark_step(pr, ProvisionRequest.Progress.PROJECT_CREATED, status_text="project_created")
        
        logger.info("Project created for prov_request=%s project_id=%s", prov_request_id, project_id)
        print(f'\n Project Created Successfully\n')
        return True

    except DokployError as e:
        pr.status = "failed"
        pr.detail = f"Project creation failed: {e}"
        pr.save()
        logger.exception("Project creation DokployError for prov_request=%s: %s", prov_request_id, e)
        return False


# 2 create Backend Service
def create_backend_service_task(prov_request_id) -> bool:
    """
    Resume-aware creation/configuration of the backend application.

    Steps (each persisted to model as a boolean flag):
      1. create application -> sets backend_id, backend_created
      2. attach git provider -> sets backend_git_attached
      3. set build type -> sets backend_build_configured

    If a step was already completed previously (boolean flag True), it will be skipped.
    On any terminal failure the ProvisionRequest.status becomes 'failed' and failed flag is set.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_backend_service_task: ProvisionRequest %s does not exist", prov_request_id)
        return False

    # require project
    if not pr.project_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | cannot create backend: missing project_id"
        pr.failed = True
        pr.save()
        logger.error("create_backend_service_task: missing project_id for prov_request=%s", prov_request_id)
        return False

    backend_name = f"{pr.project_name}-backend" if pr.project_name else "lms-backend"

    # -------------------
    # Step A: Create application (if not already created)
    # -------------------
    if not pr.backend_created or not pr.backend_id:
        try:
            print('\n Creating BackendService \n ')
            logger.info("Creating backend application for prov_request=%s name=%s", prov_request_id, backend_name)
            resp = create_application(project_id=pr.project_id,
                                      name=backend_name,
                                      description=f"Backend for {pr.client_name or backend_name}")
            app_id = None
            # robust extraction
            if isinstance(resp, dict):
                app_id = resp.get("applicationId") or resp.get("applicationId") or resp.get("id") or resp.get("_id")
            if not app_id:
                app_id = extract_id_from_resp(resp)
            if not app_id:
                raise DokployError(f"application.create returned no application id: {resp}")

            pr.backend_id = app_id
            pr.backend_created = True
            pr.status = "backend_created"
            pr.detail = (pr.detail or "") + f" | backend_created:{app_id}"
            pr.save()
            logger.info("Backend application created: prov_request=%s app_id=%s", prov_request_id, app_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | application.create error: {e}"
            pr.save()
            logger.exception("create_backend_service_task: application.create failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_backend_service_task: backend already created for prov_request=%s, id=%s", prov_request_id, pr.backend_id)

    # Short sanity check: ensure backend_id is present
    if not pr.backend_id:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + " | backend_id missing after create step"
        pr.save()
        logger.error("create_backend_service_task: backend_id missing for prov_request=%s after creation", prov_request_id)
        return False

    app_id = pr.backend_id

    # -------------------
    # Step B: Attach Git provider (if not already attached)
    # -------------------
    if not pr.backend_git_attached:
        try:
            git_url = getattr(settings, "BACKEND_REPO", None)
            # allow per-request override if stored (optional)
            if hasattr(pr, "backend_repo") and pr.backend_repo:
                git_url = pr.backend_repo

            if not git_url:
                raise DokployError("BACKEND_REPO not configured")

            logger.info("Attaching git provider for backend app %s prov_request=%s", app_id, prov_request_id)
            git_resp = save_git_provider(application_id=app_id, custom_git_url=git_url, branch="main", build_path="/")
            pr.backend_git_attached = True
            pr.status = "backend_git_attached"
            pr.detail = (pr.detail or "") + f" | backend_git_attached:{git_resp}"
            pr.save()
            logger.info("Git provider attached for backend app %s prov_request=%s", app_id, prov_request_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | save_git_provider error: {e}"
            pr.save()
            logger.exception("create_backend_service_task: save_git_provider failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_backend_service_task: backend git already attached for prov_request=%s", prov_request_id)

    # -------------------
    # Step C: Set build type (if not already configured)
    # -------------------
    if not pr.backend_build_configured:
        try:
            logger.info("Setting build type for backend app %s prov_request=%s", app_id, prov_request_id)
            # choose defaults; you can change dockerfile path etc. or provide overrides in pr
            build_resp = save_build_type(
                application_id=app_id,
                build_type="dockerfile",
                dockerfile="./DockerFile",
                docker_context_path="",
                docker_build_stage="",
                is_static_spa=False
            )
            pr.backend_build_configured = True
            pr.status = "backend_ready"
            pr.detail = (pr.detail or "") + f" | backend_build_configured:{build_resp}"
            pr.save()
            logger.info("Build type configured for backend app %s prov_request=%s", app_id, prov_request_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | save_build_type error: {e}"
            pr.save()
            logger.exception("create_backend_service_task: save_build_type failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_backend_service_task: backend build already configured for prov_request=%s", prov_request_id)

    # All backend steps done successfully
    pr.status = pr.status or "backend_done"
    pr.save()
    return True


# 3 Create DB and save credentials
def find_project_in_all(projects_list, project_id):
    """
    Given the result of /project.all (list of project dicts),
    return the project dict matching projectId == project_id, or None.
    """
    for proj in projects_list:
        if proj.get("projectId") == project_id or proj.get("projectId") == str(project_id):
            return proj
    return None


def choose_postgres_entry(postgres_list):
    """
    Pick the best postgres entry from the list.
    Strategy: prefer latest createdAt (if present); otherwise first.
    """
    if not postgres_list:
        return None
    try:
        sorted_list = sorted(
            postgres_list,
            key=lambda x: x.get("createdAt") or "",
            reverse=True
        )
        return sorted_list[0]
    except Exception:
        return postgres_list[0]


def create_postgres_task(prov_request_id) -> bool:
    """
    Step 3: create postgres, then read project.all to extract DB info,
    and write env into backend application (application.saveEnvironment).
    Returns True on success, False on permanent failure.
    """
    print("\n Creating Data Base \n")
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("ProvisionRequest %s does not exist", prov_request_id)
        return False

    if not pr.project_id:
        pr.status = "failed"
        pr.detail = "Cannot create DB: missing project_id"
        pr.save()
        return False

    if not pr.backend_id:
        pr.status = "failed"
        pr.detail = "Cannot set environment: missing backend application id"
        pr.save()
        return False

    # Build sensible names / creds
    db_name = (pr.project_name or "lms").lower().replace("-", "_") + "_db"
    db_user = "lms_user"
    # generate a reasonably strong password (url-safe, but avoid '=')
    db_password = secrets.token_urlsafe(24).replace("=", "")[:32]

    postgres_payload = {
        "name": "lms-db",
        "appName": "lms",                # dokploy will generate a real appName value like "lms-b4tbmo"
        "databaseName": db_name,
        "databaseUser": db_user,
        "databasePassword": db_password,
        "dockerImage": "postgres:15",
        "projectId": pr.project_id,
        "description": f"Postgres DB for {pr.client_name or pr.project_name}"
    }

    # 1) Create postgres
    try:
        logger.info("Creating postgres for prov_request=%s project=%s", prov_request_id, pr.project_id)
        # dokploy returns True on success per your note. But still capture response.
        resp = _post("/postgres.create/", json=postgres_payload)

        print("\n DB Created Successfully \n")
        # If Dokploy returns True, continue to fetch project data. If it returns something else, log it.
        logger.debug("postgres.create response: %s", resp)

    except DokployError as e:
        pr.status = "failed"
        pr.detail = f"postgres.create failed: {e}"
        pr.save()
        logger.exception("postgres.create failed for prov_request=%s: %s", prov_request_id, e)
        return False

    # 2) Read project.all, find our project, then inspect postgres list
    print("\n Reading Projects with sleep \n")
    time.sleep(1)
    try:
        all_projects = _get("/project.all")  # retry-enabled by default
        project = find_project_in_all(all_projects, pr.project_id)
        if not project:
            pr.status = "failed"
            pr.detail = "project.all did not contain our project after postgres.create"
            pr.save()
            logger.error("project.all did not include project_id=%s", pr.project_id)
            return False

        postgres_entries = project.get("postgres", []) or []
        postgres_entry = choose_postgres_entry(postgres_entries)
        if not postgres_entry:
            pr.status = "failed"
            pr.detail = "No postgres entries found in project after creation"
            pr.save()
            logger.error("No postgres entries found for project %s", pr.project_id)
            return False

        print("\n DB Extracting Values \n")
        # Extract useful values with safety checks
        postgres_id = (
            postgres_entry.get("postgresId")
            or postgres_entry.get("id")
            or postgres_entry.get("_id")
            or postgres_entry.get("postgres_id")
        )
        app_name = postgres_entry.get("appName") or postgres_entry.get("name") or ""
        database_name = postgres_entry.get("databaseName") or postgres_entry.get("database_name") or ""
        database_user = postgres_entry.get("databaseUser") or postgres_entry.get("database_user") or ""
        database_password = postgres_entry.get("databasePassword") or postgres_entry.get("database_password") or ""
        external_port = postgres_entry.get("externalPort") or postgres_entry.get("port") or None

        # fallback port
        db_port = str(external_port) if external_port else "5432"

        # persist DB details to ProvisionRequest (if you added those fields)
        pr.db_id = postgres_id
        pr.db_app_name = app_name
        pr.db_name = database_name
        pr.db_user = database_user
        pr.db_password = database_password
        pr.db_port = db_port
        pr.status = "db_created"
        pr.detail = (pr.detail or "") + f" | postgres_created:{postgres_id}"
        pr.save()

        logger.info("Postgres entry extracted for prov_request=%s: app=%s db=%s user=%s",
                    prov_request_id, app_name, database_name, database_user)

    except DokployError as e:
        pr.status = "failed"
        pr.detail = f"project.all failed: {e}"
        pr.save()
        logger.exception("project.all failed for prov_request=%s: %s", prov_request_id, e)
        return False

    # 3) Build environment string and call application.saveEnvironment for backend app
    print("\n Setting up Environments Variables \n")
    try:
        # Build env content expected by your backend
        # Use the appName (service host) as DB host. This is what you showed in example:
        # DB_HOST = 'lms-b4tbmo'  # from appName
        env_lines = [
            f"POSTGRES_HOST={app_name}",
            f"POSTGRES_PORT={db_port}",
            f"POSTGRES_DB={database_name}",
            f"POSTGRES_USER={database_user}",
            f"POSTGRES_PASSWORD={database_password}",
            # add a generated DJANGO_SECRET_KEY if you want the backend to have one:
            f"DJANGO_SECRET_KEY={secrets.token_urlsafe(48)}",
            "ALLOWED_HOSTS=*",
        ]
        env_payload = {
            "applicationId": pr.backend_id,
            "env": "\n".join(env_lines)
        }
        logger.info("Setting backend environment for app %s prov_request=%s", pr.backend_id, prov_request_id)
        set_env_resp = _post("/application.saveEnvironment", json=env_payload)

        pr.status = "backend_env_configured"
        pr.detail = (pr.detail or "") + f" | backend_env_set:{set_env_resp}"
        pr.save()

        logger.info("Backend environment configured for prov_request=%s", prov_request_id)
        print("\n Envs are there to use \n")
        return True

    except DokployError as e:
        pr.status = "failed"
        pr.detail = f"application.saveEnvironment failed: {e}"
        pr.save()
        logger.exception("Failed to set backend environment for prov_request=%s: %s", prov_request_id, e)
        return False


def deploy_db_then_app_quick(prov_request_id) -> tuple:
    """
    Quick deploy flow: trigger postgres.deploy -> wait 1s -> trigger application.deploy.
    Returns (postgres_response, application_response) on success, (None, None) on failure.
    Assumes pr.db_id and pr.backend_id are already set.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("deploy_db_then_app_quick: ProvisionRequest %s not found", prov_request_id)
        return None, None

    if not pr.db_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing db_id for deploy"
        pr.save()
        logger.error("deploy_db_then_app_quick: missing db_id for prov_request=%s", prov_request_id)
        return None, None

    if not pr.backend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing backend_id for deploy"
        pr.save()
        logger.error("deploy_db_then_app_quick: missing backend_id for prov_request=%s", prov_request_id)
        return None, None

    # 1) Trigger DB deploy
    print(f'\n Deploying The Database \n')
    try:
        logger.info("Triggering postgres.deploy for prov_request=%s postgresId=%s", prov_request_id, pr.db_id)
        pg_resp = _post("/postgres.deploy", json={"postgresId": pr.db_id})
        # Save immediate response for debugging
        pr.detail = (pr.detail or "") + f" | postgres_deploy_resp:{pg_resp}"
        pr.status = "postgres_deploy_triggered"
        pr.save()
        
        logger.info("postgres.deploy response for prov_request=%s: %s", prov_request_id, pg_resp)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | postgres.deploy_error:{e}"
        pr.save()
        logger.exception("postgres.deploy failed for prov_request=%s: %s", prov_request_id, e)
        return None, None

    # 2) Wait 1 second
    logger.debug("Sleeping 1 second before deploying application for prov_request=%s", prov_request_id)
    print(f'\n Deploying The Backend with 1 second delay \n')
    time.sleep(1)

    # 3) Trigger application deploy
    try:
        logger.info("Triggering application.deploy for prov_request=%s applicationId=%s", prov_request_id, pr.backend_id)
        app_resp = _post("/application.deploy", json={"applicationId": pr.backend_id})
        pr.detail = (pr.detail or "") + f" | application_deploy_resp:{app_resp}"
        pr.status = "deploys_triggered"
        pr.save()
        logger.info("application.deploy response for prov_request=%s: %s", prov_request_id, app_resp)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.deploy_error:{e}"
        pr.save()
        logger.exception("application.deploy failed for prov_request=%s: %s", prov_request_id, e)
        return pg_resp, None

    # Return both responses so caller can inspect and react
    return pg_resp, app_resp


# 4 Create Frontend Service and deploy
def create_frontend_service_task(prov_request_id) -> bool:
    """
    Create frontend application, attach git provider, set build type, and trigger deploy.
    - Looks for settings.FRONTEND_REPO or pr.frontend_repo override.
    - If pr.frontend_build_type (JSON) exists, uses that payload for /application.saveBuildType.
      Otherwise uses a default SPA-friendly payload (isStaticSpa=True, publishDirectory="build").
    Returns True on success, False on failure.
    """
    
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_frontend_service_task: ProvisionRequest %s not found", prov_request_id)
        return False

    if not pr.project_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | cannot create frontend: missing project_id"
        pr.save()
        logger.error("create_frontend_service_task: missing project_id for prov_request=%s", prov_request_id)
        return False

    # derive a frontend app name
    frontend_name = f"{pr.project_name}-frontend" if pr.project_name else "lms-frontend"

    # 1) Create application (frontend service)
    print('\n Creating frontend Service \n')
    try:
        logger.info("Creating frontend application for prov_request=%s name=%s", prov_request_id, frontend_name)
        create_payload = {
            "name": frontend_name,
            "description": f"Frontend for {pr.client_name or frontend_name}",
            "projectId": pr.project_id,
        }
        resp = _post("/application.create/", json=create_payload)  # retry-enabled

        # extract application id robustly (use your extractor if present)
        app_id = None
        if isinstance(resp, dict):
            # prefer applicationId or id keys
            app_id = resp.get("applicationId") or resp.get("id") or resp.get("_id")
        if not app_id and isinstance(resp, str):
            app_id = resp.strip().strip('"').strip("'")

        # fallback: try generic extractor if present in module
        try:
            if not app_id and "extract_id_from_resp" in globals():
                app_id = extract_id_from_resp(resp)
        except Exception:
            pass

        if not app_id:
            pr.status = "failed"
            pr.detail = (pr.detail or "") + f" | application.create returned unexpected response: {resp}"
            pr.save()
            logger.error("application.create (frontend) returned no id for prov_request=%s response=%s", prov_request_id, resp)
            return False

        pr.frontend_id = app_id
        pr.status = "frontend_created"
        pr.detail = (pr.detail or "") + f" | frontend_app_created:{app_id}"
        pr.save()
        print('\n Frontend Service Created Successfully\n')
        logger.info("Frontend app created prov_request=%s app_id=%s", prov_request_id, app_id)

    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.create(frontend) failed: {e}"
        pr.save()
        logger.exception("Failed to create frontend application for prov_request=%s: %s", prov_request_id, e)
        return False

    # 2) Attach Git provider
    print('\n Git Provider for Frontend\n')
    try:
        # allow optional per-request override saved earlier in pr.frontend_repo (if you choose to add)
        git_url = getattr(settings, "FRONTEND_REPO", None)
        if hasattr(pr, "frontend_repo") and pr.frontend_repo:
            git_url = pr.frontend_repo

        if not git_url:
            pr.status = "failed"
            pr.detail = (pr.detail or "") + " | missing FRONTEND_REPO in settings"
            pr.save()
            logger.error("Missing FRONTEND_REPO setting; cannot attach git provider for frontend prov_request=%s", prov_request_id)
            return False

        logger.info("Attaching git provider for frontend app %s (prov_request=%s)", app_id, prov_request_id)
        git_payload = {
            "customGitBranch": "main",
            "applicationId": app_id,
            "customGitBuildPath": "/",
            "customGitUrl": git_url,
            "enableSubmodules": False,
        }
        git_resp = _post("/application.saveGitProdiver", json=git_payload)  # retry-enabled
        pr.status = "frontend_git_attached"
        pr.detail = (pr.detail or "") + f" | frontend_git_attached:{git_resp}"
        pr.save()
        print('\n Frontend Git provider saved Successfully\n')
        logger.info("Git provider attached for frontend app %s prov_request=%s", app_id, prov_request_id)

    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.saveGitProdiver(frontend) failed: {e}"
        pr.save()
        logger.exception("Failed to attach git provider for frontend prov_request=%s: %s", prov_request_id, e)
        return False

    # 3) Configure build type
    print('\n Frontend Build type setup\n')
    try:
        # If you previously stored a JSON build config on pr.frontend_build_type (optional), use it.
        # Otherwise use sensible default for a static SPA.
        build_payload = None
        if hasattr(pr, "frontend_build_type") and pr.frontend_build_type:
            # expect this is a dict with correct keys for dokploy
            build_payload = pr.frontend_build_type.copy()
            build_payload["applicationId"] = app_id
        else:
            # default SPA build config — adjust publishDirectory if your frontend uses "dist" or "build"
            # build_payload = {
            #     "applicationId": app_id,
            #     "buildType": "dockerfile",        # keep dockerfile type but mark isStaticSpa True — Dokploy often accepts this
            #     "dockerfile": "",                # Not used for SPA
            #     "dockerContextPath": "",
            #     "dockerBuildStage": "",
            #     "publishDirectory": "build",     # change to "dist" if your build outputs to dist
            #     "isStaticSpa": True
            # }
            build_payload = {
            "applicationId": app_id,
            "buildType": "dockerfile",
            "dockerfile": "./DockerFile",
            "dockerContextPath": "",
            "dockerBuildStage": "",
            "isStaticSpa": False,
             }

        logger.info("Setting frontend build type for app %s (prov_request=%s) payload=%s", app_id, prov_request_id, build_payload)
        build_resp = _post("/application.saveBuildType", json=build_payload)  # retry-enabled
        pr.status = "frontend_build_configured"
        pr.detail = (pr.detail or "") + f" | frontend_build_configured:{build_resp}"
        pr.save()
        print('\n Frontend Build type setup Successfull\n')
        logger.info("Frontend build configured for app %s prov_request=%s", app_id, prov_request_id)

    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.saveBuildType(frontend) failed: {e}"
        pr.save()
        logger.exception("Failed to set build type for frontend prov_request=%s: %s", prov_request_id, e)
        return False

    # 4) Trigger frontend deploy (quick)
    try:
        logger.info("Triggering frontend application.deploy for prov_request=%s app=%s", prov_request_id, app_id)
        deploy_resp = _post("/application.deploy", json={"applicationId": app_id})
        pr.status = "frontend_deploy_triggered"
        pr.detail = (pr.detail or "") + f" | frontend_deploy_resp:{deploy_resp}"
        pr.save()
        logger.info("application.deploy (frontend) response for prov_request=%s: %s", prov_request_id, deploy_resp)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.deploy(frontend) failed: {e}"
        pr.save()
        logger.exception("Failed to deploy frontend app for prov_request=%s: %s", prov_request_id, e)
        return False

    # <-- wait 1 second here so Dokploy registers the build config -->
    time.sleep(1)

    # 4) Trigger frontend deploy (quick)
    print('\n Frontend Deploy Trigger\n')
    try:
        logger.info("Triggering frontend application.deploy for prov_request=%s app=%s", prov_request_id, app_id)
        deploy_resp = _post("/application.deploy", json={"applicationId": app_id})
        pr.status = "frontend_deploy_triggered"
        pr.detail = (pr.detail or "") + f" | frontend_deploy_resp:{deploy_resp}"
        pr.save()
        logger.info("application.deploy (frontend) response for prov_request=%s: %s", prov_request_id, deploy_resp)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | application.deploy(frontend) failed: {e}"
        pr.save()
        logger.exception("Failed to deploy frontend app for prov_request=%s: %s", prov_request_id, e)
        return False

    # success
    pr.status = "frontend_ready"
    pr.detail = (pr.detail or "") + " | frontend_created_and_deployed"
    pr.save()
    logger.info("Frontend service created, configured and deploy triggered for prov_request=%s", prov_request_id)
    return True

# 5 Create domains for backend and frontend
def _sanitize_subdomain(sub: str) -> str:
    """
    Lowercase, allow only a-z0-9 and hyphen. Replace other chars with hyphen.
    Trim leading/trailing hyphens and cap to 63 chars.
    """
    if not sub:
        return ""
    s = sub.strip().lower()
    # replace invalid chars with hyphen
    s = re.sub(r"[^a-z0-9-]", "-", s)
    # collapse multiple hyphens
    s = re.sub(r"-{2,}", "-", s)
    # trim hyphens
    s = s.strip("-")
    return s[:63]

def create_domains_task(prov_request_id) -> bool:
    """
    Create frontend and backend domains using provided pr.subdomain and settings.BASE_DOMAIN.
    - Requires pr.frontend_id and pr.backend_id to be present.
    - Saves constructed hostnames to pr.frontend_domain and pr.backend_domain.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_domains_task: ProvisionRequest %s not found", prov_request_id)
        return False

    # subdomain is required by your DRF view; still sanitize defensively
    sub_raw = (pr.subdomain or "").strip()
    if not sub_raw:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing subdomain (should not happen - view validates)"
        pr.save()
        logger.error("create_domains_task: missing subdomain for prov_request=%s", prov_request_id)
        return False

    sub = _sanitize_subdomain(sub_raw)
    if not sub:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | invalid subdomain after sanitization: {sub_raw}"
        pr.save()
        logger.error("create_domains_task: sanitized subdomain is empty for prov_request=%s input=%s", prov_request_id, sub_raw)
        return False

    base_domain = getattr(settings, "BASE_DOMAIN", None)
    if not base_domain:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing BASE_DOMAIN in settings"
        pr.save()
        logger.error("create_domains_task: BASE_DOMAIN not configured for prov_request=%s", prov_request_id)
        return False

    if not pr.frontend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing frontend_id (create frontend first)"
        pr.save()
        logger.error("create_domains_task: missing frontend_id for prov_request=%s", prov_request_id)
        return False

    if not pr.backend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing backend_id (create backend first)"
        pr.save()
        logger.error("create_domains_task: missing backend_id for prov_request=%s", prov_request_id)
        return False

    frontend_host = f"{sub}.{base_domain}"
    backend_host = f"{sub}-backend.{base_domain}"

    def domain_payload(app_id: str, host: str) -> dict:
        return {
            "host": host,
            "port": 80,
            "https": True,
            "applicationId": app_id,
            "certificateType": "letsencrypt",
            "domainType": "application",
        }

    # Create frontend domain
    try:
        logger.info("Creating frontend domain %s for app=%s (prov=%s)", frontend_host, pr.frontend_id, prov_request_id)
        _post("/domain.create", json=domain_payload(pr.frontend_id, frontend_host))
        # we intentionally do NOT parse Dokploy response — we trust our host pattern
        pr.frontend_domain = frontend_host
        pr.detail = (pr.detail or "") + f" | frontend_domain_set:{frontend_host}"
        pr.save()
        logger.info("Frontend domain set to %s for prov_request=%s", frontend_host, prov_request_id)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | frontend domain.create error: {e}"
        pr.save()
        logger.exception("create_domains_task: frontend domain.create failed for prov_request=%s: %s", prov_request_id, e)
        return False

    # slight pause to let domain propagation/startup (1s is fine)
    time.sleep(1)

    # Create backend domain
    try:
        logger.info("Creating backend domain %s for app=%s (prov=%s)", backend_host, pr.backend_id, prov_request_id)
        _post("/domain.create", json=domain_payload(pr.backend_id, backend_host))
        pr.backend_domain = backend_host
        pr.detail = (pr.detail or "") + f" | backend_domain_set:{backend_host}"
        pr.status = "domains_configured"
        pr.save()
        logger.info("Backend domain set to %s for prov_request=%s", backend_host, prov_request_id)
    except DokployError as e:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | backend domain.create error: {e}"
        pr.save()
        logger.exception("create_domains_task: backend domain.create failed for prov_request=%s: %s", prov_request_id, e)
        return False

    return True

