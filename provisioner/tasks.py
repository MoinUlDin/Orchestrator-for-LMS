# provisioner/tasks.py
import logging, re, time
from datetime import datetime, timedelta, timezone
import secrets
from django.conf import settings
from .utils import generate_project_name
from .scheduler import scheduler, backend_health_and_provision_attempt
from .dokploy_client import _post, DokployError
from .models import ProvisionRequest
from .dokploy_client import (
    create_application,
    save_git_provider,
    save_build_type,
    save_environment,
    create_domain,
    create_postgres,
    get_all_projects,
    deploy_postgres,
    deploy_application,
    delete_domain,
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
    Orchestrator task (resume-aware). Steps:
      1. create project
      2. create backend service (create app, git, build)
      3. create postgres, set backend env
      4. deploy postgres then backend app
      5. wait (only if we just triggered backend deploy)
      6. create frontend service (create app, git, build, deploy)
      7. create domains for both services
      8. mark completed
    """
    global_deley = 2
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("provision_tenant_task: ProvisionRequest %s not found", prov_request_id)
        return False

    # # If previously marked failed, bail out (you can change this to allow forced retries)
    # if pr.failed:
    #     logger.error("provision_tenant_task: ProvisionRequest %s is marked failed; aborting", prov_request_id)
    #     return False

    # ---------- Step 1: Project ----------
    if not pr.project_created:
        logger.info("provision_tenant_task: creating project for prov_request=%s", prov_request_id)
        ok = create_project_task(prov_request_id)
        if not ok:
            logger.error("provision_tenant_task: create_project_task failed for prov_request=%s", prov_request_id)
            return False
    else:
        logger.info("provision_tenant_task: project already created for prov_request=%s", prov_request_id)

    # Refresh request
    pr.refresh_from_db()

    # ---------- Step 2: Backend service (resume-aware) ----------
    if not (pr.backend_created and pr.backend_git_attached and pr.backend_build_configured):
        logger.info("provision_tenant_task: running backend creation/config for prov_request=%s", prov_request_id)
        ok = create_backend_service_task(prov_request_id)
        if not ok:
            logger.error("provision_tenant_task: create_backend_service_task failed for prov_request=%s", prov_request_id)
            return False
    else:
        logger.info("provision_tenant_task: backend already created and configured for prov_request=%s", prov_request_id)

    # Refresh request
    pr.refresh_from_db()

    # ---------- Step 3: Create Postgres & configure backend environment ----------
    time.sleep(global_deley)
    if not (pr.db_created and pr.backend_env_configured):
        logger.info("provision_tenant_task: creating/configuring postgres for prov_request=%s", prov_request_id)
        ok = create_postgres_task(prov_request_id)
        if not ok:
            logger.error("provision_tenant_task: create_postgres_task failed for prov_request=%s", prov_request_id)
            return False
    else:
        logger.info("provision_tenant_task: DB already created and backend env configured for prov_request=%s", prov_request_id)

    # Refresh before deploy checks
    pr.refresh_from_db()
    time.sleep(global_deley)
    # ---------- Step 4: Deploy DB then backend app (resume-aware) ----------
    # Save previous deploy-triggered state so we know whether to wait afterward
    was_app_deploy_triggered = pr.backend_deploy_triggered
    if not (pr.postgres_deploy_triggered and pr.backend_deploy_triggered):
        logger.info("provision_tenant_task: triggering DB+app deploy for prov_request=%s", prov_request_id)
        pg_resp, app_resp = deploy_db_then_app_quick(prov_request_id)
        if pg_resp is None:
            logger.error("provision_tenant_task: deploy_db_then_app_quick failed to trigger postgres for prov_request=%s", prov_request_id)
            return False
        # refresh to pick up any flags set by deploy_db_then_app_quick
        pr.refresh_from_db()
    else:
        logger.info("provision_tenant_task: DB and backend deploy already triggered for prov_request=%s", prov_request_id)

    # If the backend app.deploy was just triggered by this run (was not triggered before),
    # wait a bit to let the backend start (original flow waited 240s).
    pr.refresh_from_db()
    just_triggered_app_deploy = (not was_app_deploy_triggered) and pr.backend_deploy_triggered
    if just_triggered_app_deploy:
        wait_seconds = getattr(settings, "BACKEND_DEPLOY_WAIT", 240)
        logger.info("provision_tenant_task: backend deploy was just triggered; waiting %s seconds (prov_request=%s)",
                    wait_seconds, prov_request_id)
        time.sleep(wait_seconds)

    # ---------- Step 5: Create Frontend (resume-aware) ----------
    pr.refresh_from_db()
    time.sleep(global_deley)
    if not (pr.frontend_created and pr.frontend_git_attached and pr.frontend_build_configured and pr.frontend_deploy_triggered):
        logger.info("provision_tenant_task: creating/configuring/deploying frontend for prov_request=%s", prov_request_id)
        ok = create_frontend_service_task(prov_request_id)
        if not ok:
            logger.error("provision_tenant_task: create_frontend_service_task failed for prov_request=%s", prov_request_id)
            return False
    else:
        logger.info("provision_tenant_task: frontend already created/configured/deployed for prov_request=%s", prov_request_id)

    # ---------- Step 6: Create domains (atomic) ----------
    time.sleep(global_deley)
    pr.refresh_from_db()
    if not pr.domains_configured:
        logger.info("provision_tenant_task: creating domains for prov_request=%s", prov_request_id)
        ok = create_domains_task(prov_request_id)
        if not ok:
            logger.error("provision_tenant_task: create_domains_task failed for prov_request=%s", prov_request_id)
            return False
    else:
        logger.info("provision_tenant_task: domains already configured for prov_request=%s", prov_request_id)

    
    
    # ---------- Step 7: Kick off backend health + internal provision ----------
    if not pr.super_user_created:
        print("\n bakend Health & Internal Provission call after delay atleast 5 second")
        pr.refresh_from_db()
        dd = global_deley+5
        time.sleep(dd)
        logger.info("provision_tenant_task: scheduling backend health + internal provision for prov_request=%s", prov_request_id)
        n_payload = {
            "admin_email": payload.get("email"),       
            "admin_password": payload.get("admin_password"),
            }
        scheduler.add_job(
            backend_health_and_provision_attempt,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=global_deley),  # ✅ timezone-aware
            args=[prov_request_id, n_payload],
            id=f"backend_health_provision_{prov_request_id}",
            replace_existing=True,
        )

    logger.info("provision_tenant_task: provisioning completed for prov_request=%s", prov_request_id)
    print("\n\n ---------- Job Complete Successfully ---------- \n")
    return True


    

# 1 create Project 
def create_project_task(prov_request_id):
    print(f'\n Creating Project \n')
    pr = ProvisionRequest.objects.get(id=prov_request_id)

    if not pr.client_name:
        pr.status = "failed"
        pr.detail = "Missing client_name"
        pr.failed = True
        pr.save()
        return False

    # mark running by setting fields directly
    pr.status = "provisioning"
    pr.save()

    project_name = generate_project_name(pr.client_name)
    pr.project_name = project_name
    pr.save()

    try:
        payload = {"name": project_name, "description": f"Project for {pr.client_name}"}
        resp = _post("/project/create", json=payload)

        project_id = extract_id_from_resp(resp)
        if not project_id:
            pr.status = "failed"
            pr.detail = f"project.create returned unexpected response: {resp}"
            pr.failed = True
            pr.save()
            logger.error("project.create unexpected response for prov_request=%s: %s", prov_request_id, resp)
            return False

        pr.project_id = project_id
        pr.project_created = True
        pr.status = "project_created"
        pr.detail = f"Project created (id={project_id})"
        pr.save()

        logger.info("Project created for prov_request=%s project_id=%s", prov_request_id, project_id)
        print(f'\n Project Created Successfully\n')
        return True

    except DokployError as e:
        pr.status = "failed"
        pr.failed = True
        pr.detail = f"Project creation failed: {e}"
        pr.save()
        logger.exception("Project creation DokployError for prov_request=%s: %s", prov_request_id, e)
        return False
    except Exception as e:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + f" | unexpected error: {e}"
        pr.save()
        logger.exception("Create Project: unexpected error for prov_request=%s", prov_request_id)
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
    try: # top level try block
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
    except Exception as e:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + f" | unexpected error: {e}"
        pr.save()
        logger.exception("create_backend_service_task: unexpected error for prov_request=%s", prov_request_id)
        return False
    
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

def _fetch_postgres_entry_for_project(projects_list, project_id):
    project = find_project_in_all(projects_list, project_id)
    if not project:
        return None
    postgres_entries = project.get("postgres", []) or []
    return choose_postgres_entry(postgres_entries)

def _populate_db_fields_from_postgres_entry(pr, postgres_entry):
    """Populate ProvisionRequest DB fields from a postgres entry dict and save."""
    postgres_id = (
        postgres_entry.get("postgresId")
        or postgres_entry.get("id")
        or postgres_entry.get("_id")
        or postgres_entry.get("postgres_id")
    )
    app_name = postgres_entry.get("appName") or postgres_entry.get("app_name") or postgres_entry.get("name") or ""
    database_name = postgres_entry.get("databaseName") or postgres_entry.get("database_name") or ""
    database_user = postgres_entry.get("databaseUser") or postgres_entry.get("database_user") or ""
    database_password = postgres_entry.get("databasePassword") or postgres_entry.get("database_password") or ""
    external_port = postgres_entry.get("externalPort") or postgres_entry.get("port") or None
    db_port = str(external_port) if external_port else "5432"

    pr.db_id = postgres_id
    pr.db_app_name = app_name
    pr.db_name = database_name
    pr.db_user = database_user
    pr.db_password = database_password
    pr.db_port = db_port
    pr.db_created = True
    pr.detail = (pr.detail or "") + f" | postgres_populated:{postgres_id}"
    pr.status = pr.status or "db_created"
    pr.save()


def create_postgres_task(prov_request_id) -> bool:
    """
    Resume-aware:
      - If pr.db_created True and db fields exist -> skip creation (but ensure fields present).
      - Else: create postgres via create_postgres(...) and then query project.all to extract the postgres entry.
      - After extraction, set backend environment variables (application.saveEnvironment) and mark backend_env_configured.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_postgres_task: ProvisionRequest %s does not exist", prov_request_id)
        return False

    # sanity checks
    if not pr.project_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | cannot create DB: missing project_id"
        pr.failed = True
        pr.save()
        logger.error("create_postgres_task: missing project_id for prov_request=%s", prov_request_id)
        return False

    if not pr.backend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | cannot set environment: missing backend application id"
        pr.failed = True
        pr.save()
        logger.error("create_postgres_task: missing backend_id for prov_request=%s", prov_request_id)
        return False

    # If db already created and db fields are present, skip creation step
    if pr.db_created and pr.db_id and pr.db_app_name and pr.db_name and pr.db_user and pr.db_password:
        logger.info("create_postgres_task: DB already created for prov_request=%s db_id=%s", prov_request_id, pr.db_id)
    else:
        # Build sensible names / creds
        db_name = (pr.project_name or "lms").lower().replace("-", "_") + "_db"
        db_user = "lms_user"
        db_password = secrets.token_urlsafe(24).replace("=", "")[:32]

        postgres_payload_name = "lms-db"
        try:
            logger.info("Creating postgres for prov_request=%s project=%s", prov_request_id, pr.project_id)
            resp = create_postgres(
                project_id=pr.project_id,
                name=postgres_payload_name,
                app_name="lms",
                database_name=db_name,
                database_user=db_user,
                database_password=db_password,
                docker_image="postgres:15",
            )
            # create_postgres may return True or some dict; just log it
            pr.detail = (pr.detail or "") + f" | postgres.create_resp:{resp}"
            pr.save()
            logger.debug("postgres.create resp: %s", resp)
        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | postgres.create error: {e}"
            pr.save()
            logger.exception("create_postgres_task: postgres.create failed for prov_request=%s: %s", prov_request_id, e)
            return False

        # Wait briefly and then query project.all to find the created postgres entry
        time.sleep(1)

        try:
            all_projects = get_all_projects()
            postgres_entry = _fetch_postgres_entry_for_project(all_projects, pr.project_id)
            if not postgres_entry:
                pr.status = "failed"
                pr.failed = True
                pr.detail = (pr.detail or "") + " | project.all did not show postgres entry after creation"
                pr.save()
                logger.error("create_postgres_task: project.all missing postgres entry for project %s", pr.project_id)
                return False

            # populate pr fields from the found postgres entry
            _populate_db_fields_from_postgres_entry(pr, postgres_entry)
            logger.info("create_postgres_task: populated DB fields for prov_request=%s", prov_request_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | project.all error: {e}"
            pr.save()
            logger.exception("create_postgres_task: project.all failed for prov_request=%s: %s", prov_request_id, e)
            return False

    # Now ensure backend environment is set (resume-aware)
    if pr.backend_env_configured:
        logger.info("create_postgres_task: backend environment already configured for prov_request=%s", prov_request_id)
        return True

    # Build env content expected by your backend and set it on the backend app
    try:
        env_lines = [
            f"POSTGRES_HOST={pr.db_app_name}",
            f"POSTGRES_PORT={pr.db_port or '5432'}",
            f"POSTGRES_DB={pr.db_name}",
            f"POSTGRES_USER={pr.db_user}",
            f"POSTGRES_PASSWORD={pr.db_password}",
            f"DJANGO_SECRET_KEY={secrets.token_urlsafe(48)}",
            "ALLOWED_HOSTS=*",
        ]
        env_payload = "\n".join(env_lines)

        logger.info("Setting backend environment for app %s prov_request=%s", pr.backend_id, prov_request_id)
        resp = save_environment(application_id=pr.backend_id, env_str=env_payload)
        pr.backend_env_configured = True
        pr.status = "backend_env_configured"
        pr.detail = (pr.detail or "") + f" | backend_env_set:{resp}"
        pr.save()
        logger.info("create_postgres_task: backend environment configured for prov_request=%s", prov_request_id)

        return True

    except DokployError as e:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + f" | application.saveEnvironment failed: {e}"
        pr.save()
        logger.exception("create_postgres_task: save_environment failed for prov_request=%s: %s", prov_request_id, e)
        return False

def deploy_db_then_app_quick(prov_request_id) -> tuple:
    """
    Deploy DB first then app. Resume-aware using flags:
      - postgres_deploy_triggered
      - backend_deploy_triggered
    Returns (pg_resp, app_resp) or (None, None) on failure.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("deploy_db_then_app_quick: ProvisionRequest %s not found", prov_request_id)
        return None, None

    if not pr.db_id:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + " | missing db_id for deploy"
        pr.save()
        logger.error("deploy_db_then_app_quick: missing db_id for prov_request=%s", prov_request_id)
        return None, None

    if not pr.backend_id:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + " | missing backend_id for deploy"
        pr.save()
        logger.error("deploy_db_then_app_quick: missing backend_id for prov_request=%s", prov_request_id)
        return None, None

    pg_resp = None
    app_resp = None

    # 1) Trigger DB deploy (if not already triggered)
    if not pr.postgres_deploy_triggered:
        try:
            logger.info("Triggering postgres.deploy for prov_request=%s postgresId=%s", prov_request_id, pr.db_id)
            pg_resp = deploy_postgres(postgres_id=pr.db_id)
            pr.postgres_deploy_triggered = True
            pr.status = "postgres_deploy_triggered"
            pr.detail = (pr.detail or "") + f" | postgres_deploy_resp:{pg_resp}"
            pr.save()
            logger.info("postgres.deploy response for prov_request=%s: %s", prov_request_id, pg_resp)
        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | postgres.deploy_error:{e}"
            pr.save()
            logger.exception("postgres.deploy failed for prov_request=%s: %s", prov_request_id, e)
            return None, None
    else:
        logger.info("deploy_db_then_app_quick: postgres.deploy already triggered for prov_request=%s", prov_request_id)

    # small delay before application deploy (1 sec per your workflow)
    time.sleep(1)

    # 2) Trigger application deploy (if not already)
    if not pr.backend_deploy_triggered:
        try:
            logger.info("Triggering application.deploy for prov_request=%s applicationId=%s", prov_request_id, pr.backend_id)
            app_resp = deploy_application(application_id=pr.backend_id)
            pr.backend_deploy_triggered = True
            pr.status = "deploys_triggered"
            pr.detail = (pr.detail or "") + f" | application_deploy_resp:{app_resp}"
            pr.save()
            logger.info("application.deploy response for prov_request=%s: %s", prov_request_id, app_resp)
        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | application.deploy_error:{e}"
            pr.save()
            logger.exception("application.deploy failed for prov_request=%s: %s", prov_request_id, e)
            return pg_resp, None
    else:
        logger.info("deploy_db_then_app_quick: application.deploy already triggered for prov_request=%s", prov_request_id)

    return pg_resp, app_resp

# 4 Create Frontend Service and deploy
def create_frontend_service_task(prov_request_id) -> bool:
    """
    Resume-aware creation/configuration/deploy of the frontend application.

    Steps (persisted as boolean flags on ProvisionRequest):
      1. create application -> sets frontend_id, frontend_created
      2. attach git provider -> sets frontend_git_attached
      3. set build type -> sets frontend_build_configured
      4. trigger deploy (after 1s) -> sets frontend_deploy_triggered

    Returns True on success, False on failure.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_frontend_service_task: ProvisionRequest %s not found", prov_request_id)
        return False

    # require project
    if not pr.project_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | cannot create frontend: missing project_id"
        pr.failed = True
        pr.save()
        logger.error("create_frontend_service_task: missing project_id for prov_request=%s", prov_request_id)
        return False

    frontend_name = f"{pr.project_name}-frontend" if pr.project_name else "lms-frontend"

    # -------------------
    # Step A: Create application (if not already created)
    # -------------------
    if not pr.frontend_created or not pr.frontend_id:
        try:
            logger.info("Creating frontend application for prov_request=%s name=%s", prov_request_id, frontend_name)
            resp = create_application(project_id=pr.project_id,
                                      name=frontend_name,
                                      description=f"Frontend for {pr.client_name or frontend_name}")
            app_id = None
            if isinstance(resp, dict):
                app_id = resp.get("applicationId") or resp.get("id") or resp.get("_id")
            if not app_id:
                app_id = extract_id_from_resp(resp)

            if not app_id:
                raise DokployError(f"application.create returned no application id: {resp}")

            pr.frontend_id = app_id
            pr.frontend_created = True
            pr.status = "frontend_created"
            pr.detail = (pr.detail or "") + f" | frontend_created:{app_id}"
            pr.save()
            logger.info("Frontend application created: prov_request=%s app_id=%s", prov_request_id, app_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | application.create(frontend) error: {e}"
            pr.save()
            logger.exception("create_frontend_service_task: application.create failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_frontend_service_task: frontend already created for prov_request=%s id=%s", prov_request_id, pr.frontend_id)

    # sanity check
    if not pr.frontend_id:
        pr.status = "failed"
        pr.failed = True
        pr.detail = (pr.detail or "") + " | frontend_id missing after create step"
        pr.save()
        logger.error("create_frontend_service_task: frontend_id missing for prov_request=%s after creation", prov_request_id)
        return False

    app_id = pr.frontend_id

    # -------------------
    # Step B: Attach Git provider (if not already attached)
    # -------------------
    if not pr.frontend_git_attached:
        try:
            git_url = getattr(settings, "FRONTEND_REPO", None)
            if hasattr(pr, "frontend_repo") and pr.frontend_repo:
                git_url = pr.frontend_repo

            if not git_url:
                raise DokployError("FRONTEND_REPO not configured")

            logger.info("Attaching git provider for frontend app %s prov_request=%s", app_id, prov_request_id)
            git_resp = save_git_provider(application_id=app_id, custom_git_url=git_url, branch="main", build_path="/")
            pr.frontend_git_attached = True
            pr.status = "frontend_git_attached"
            pr.detail = (pr.detail or "") + f" | frontend_git_attached:{git_resp}"
            pr.save()
            logger.info("Git provider attached for frontend app %s prov_request=%s", app_id, prov_request_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | save_git_provider(frontend) error: {e}"
            pr.save()
            logger.exception("create_frontend_service_task: save_git_provider failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_frontend_service_task: frontend git already attached for prov_request=%s", prov_request_id)

    # -------------------
    # Step C: Set build type (if not already configured)
    # -------------------
    if not pr.frontend_build_configured:
        try:
            # Prefer per-request build config if present
            if hasattr(pr, "frontend_build_type") and pr.frontend_build_type:
                # assume dict saved; inject application id
                build_kwargs = pr.frontend_build_type.copy()
                build_kwargs["application_id"] = app_id  # our helper expects application_id param
                # If your save_build_type signature differs, adapt accordingly
                build_resp = save_build_type(
                    application_id=app_id,
                    build_type=build_kwargs.get("buildType", "dockerfile"),
                    dockerfile=build_kwargs.get("dockerfile", "./DockerFile"),
                    docker_context_path=build_kwargs.get("dockerContextPath", ""),
                    docker_build_stage=build_kwargs.get("dockerBuildStage", ""),
                    is_static_spa=build_kwargs.get("isStaticSpa", False),
                    publish_directory=build_kwargs.get("publishDirectory")
                )
            else:
                # sensible default for SPA — static SPA with publish directory "build"
                build_resp = save_build_type(
                    application_id=app_id,
                    build_type="dockerfile",
                    dockerfile="./DockerFile",
                    docker_context_path="",
                    docker_build_stage="",
                    is_static_spa=True,
                    publish_directory="build"
                )

            pr.frontend_build_configured = True
            pr.status = "frontend_build_configured"
            pr.detail = (pr.detail or "") + f" | frontend_build_configured:{build_resp}"
            pr.save()
            logger.info("Frontend build type configured for app %s prov_request=%s", app_id, prov_request_id)

        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | save_build_type(frontend) error: {e}"
            pr.save()
            logger.exception("create_frontend_service_task: save_build_type failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_frontend_service_task: frontend build already configured for prov_request=%s", prov_request_id)

    # -------------------
    # Step D: Trigger deploy (if not already triggered)
    # -------------------
    if not pr.frontend_deploy_triggered:
        # small delay so dokploy registers build config (per flow)
        time.sleep(1)
        try:
            logger.info("Triggering frontend application.deploy for prov_request=%s applicationId=%s", prov_request_id, app_id)
            deploy_resp = deploy_application(application_id=app_id)
            pr.frontend_deploy_triggered = True
            pr.status = "frontend_deploy_triggered"
            pr.detail = (pr.detail or "") + f" | frontend_deploy_resp:{deploy_resp}"
            pr.save()
            logger.info("frontend application.deploy response for prov_request=%s: %s", prov_request_id, deploy_resp)
        except DokployError as e:
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | deploy_application(frontend) error: {e}"
            pr.save()
            logger.exception("create_frontend_service_task: deploy_application failed for prov_request=%s: %s", prov_request_id, e)
            return False
    else:
        logger.info("create_frontend_service_task: frontend deploy already triggered for prov_request=%s", prov_request_id)

    # Success - mark ready
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
    Create frontend and backend domains using pr.subdomain and settings.BASE_DOMAIN.
    - Resume-aware: will only set pr.domains_configured = True when BOTH domains are successfully created.
    - If one creation fails, the function marks the ProvisionRequest as failed and returns False.
    - When the function creates the frontend domain in this run but backend creation fails,
      it will attempt to delete the frontend domain (best-effort rollback).
    - Uses create_domain(...) and delete_domain(...) helpers to call dokploy endpoints.
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("create_domains_task: ProvisionRequest %s not found", prov_request_id)
        return False

    # If already configured and both hosts exist, short-circuit
    if pr.domains_configured and pr.frontend_domain and pr.backend_domain:
        logger.info("create_domains_task: domains already configured for prov_request=%s", prov_request_id)
        return True

    # sanitize and validate input
    sub_raw = (pr.subdomain or "").strip()
    if not sub_raw:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing subdomain (should not happen - view validates)"
        pr.failed = True
        pr.save()
        logger.error("create_domains_task: missing subdomain for prov_request=%s", prov_request_id)
        return False

    sub = _sanitize_subdomain(sub_raw)
    if not sub:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + f" | invalid subdomain after sanitization: {sub_raw}"
        pr.failed = True
        pr.save()
        logger.error("create_domains_task: sanitized subdomain is empty for prov_request=%s input=%s", prov_request_id, sub_raw)
        return False

    base_domain = getattr(settings, "BASE_DOMAIN", None)
    if not base_domain:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing BASE_DOMAIN in settings"
        pr.failed = True
        pr.save()
        logger.error("create_domains_task: BASE_DOMAIN not configured for prov_request=%s", prov_request_id)
        return False

    # ensure app ids exist
    if not pr.frontend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing frontend_id (create frontend first)"
        pr.failed = True
        pr.save()
        logger.error("create_domains_task: missing frontend_id for prov_request=%s", prov_request_id)
        return False

    if not pr.backend_id:
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | missing backend_id (create backend first)"
        pr.failed = True
        pr.save()
        logger.error("create_domains_task: missing backend_id for prov_request=%s", prov_request_id)
        return False

    frontend_host = f"{sub}.{base_domain}"
    backend_host = f"{sub}-backend.{base_domain}"

    # helper to call create_domain and return (True, resp) or (False, exception)
    def _attempt_create(app_id: str, host: str):
        try:
            resp = create_domain(application_id=app_id, host=host, port=80, https=True,
                                 certificate_type="letsencrypt", domain_type="application")
            return True, resp
        except DokployError as e:
            return False, e

    created_frontend = False       # overall truth that frontend is present (existing or newly created)
    created_backend = False
    created_frontend_now = False   # True if we created it in this run and should rollback on backend failure
    created_backend_now = False

    frontend_resp = None
    backend_resp = None
    frontend_domain_id = None
    backend_domain_id = None

    # Try frontend domain if not already present
    if pr.frontend_domain and pr.frontend_domain == frontend_host:
        logger.info("create_domains_task: frontend domain already present on record for prov_request=%s", prov_request_id)
        created_frontend = True
        # try to read domain id if stored
        if hasattr(pr, "frontend_domain_id") and pr.frontend_domain_id:
            frontend_domain_id = pr.frontend_domain_id
    else:
        logger.info("create_domains_task: creating frontend domain %s for app=%s (prov=%s)", frontend_host, pr.frontend_id, prov_request_id)
        ok, result = _attempt_create(pr.frontend_id, frontend_host)
        if ok:
            created_frontend = True
            created_frontend_now = True
            frontend_resp = result
            # extract domainId if present
            if isinstance(frontend_resp, dict):
                frontend_domain_id = frontend_resp.get("domainId") or extract_id_from_resp(frontend_resp)
            else:
                frontend_domain_id = extract_id_from_resp(frontend_resp)

            # save the host and (if model supports) the domain id
            pr.frontend_domain = frontend_host
            if hasattr(pr, "frontend_domain_id"):
                pr.frontend_domain_id = frontend_domain_id
            else:
                # persist domainId into detail as fallback
                pr.detail = (pr.detail or "") + f" | frontend_domain_id:{frontend_domain_id}"
            pr.detail = (pr.detail or "") + f" | frontend_domain_created:{frontend_host}"
            pr.save()
            logger.info("Frontend domain created for prov_request=%s host=%s resp=%s", prov_request_id, frontend_host, frontend_resp)
        else:
            # frontend creation failed — record and fail
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | frontend domain.create error: {result}"
            pr.save()
            logger.exception("create_domains_task: frontend domain.create failed for prov_request=%s: %s", prov_request_id, result)
            return False

    # small wait before creating backend domain
    time.sleep(1)

    # Try backend domain if not already present
    if pr.backend_domain and pr.backend_domain == backend_host:
        logger.info("create_domains_task: backend domain already present on record for prov_request=%s", prov_request_id)
        created_backend = True
        if hasattr(pr, "backend_domain_id") and pr.backend_domain_id:
            backend_domain_id = pr.backend_domain_id
    else:
        logger.info("create_domains_task: creating backend domain %s for app=%s (prov=%s)", backend_host, pr.backend_id, prov_request_id)
        ok, result = _attempt_create(pr.backend_id, backend_host)
        if ok:
            created_backend = True
            created_backend_now = True
            backend_resp = result
            # extract domainId if present
            if isinstance(backend_resp, dict):
                backend_domain_id = backend_resp.get("domainId") or extract_id_from_resp(backend_resp)
            else:
                backend_domain_id = extract_id_from_resp(backend_resp)

            # save host and domain id if model supports it
            pr.backend_domain = backend_host
            if hasattr(pr, "backend_domain_id"):
                pr.backend_domain_id = backend_domain_id
            else:
                pr.detail = (pr.detail or "") + f" | backend_domain_id:{backend_domain_id}"
            pr.detail = (pr.detail or "") + f" | backend_domain_created:{backend_host}"
            pr.save()
            logger.info("Backend domain created for prov_request=%s host=%s resp=%s", prov_request_id, backend_host, backend_resp)
        else:
            # backend creation failed — attempt rollback of frontend if we created it now
            pr.status = "failed"
            pr.failed = True
            pr.detail = (pr.detail or "") + f" | backend domain.create error: {result}"
            pr.save()
            logger.exception("create_domains_task: backend domain.create failed for prov_request=%s: %s", prov_request_id, result)

            # Rollback frontend domain only if we created it in this run (do not delete pre-existing domains)
            if created_frontend_now and frontend_domain_id:
                try:
                    logger.info("create_domains_task: attempting rollback - deleting frontend domain id=%s for prov_request=%s", frontend_domain_id, prov_request_id)
                    # delete_domain should be imported from dokploy_client
                    delete_domain(frontend_domain_id)
                    # clear persisted frontend domain info if model supports it
                    pr.frontend_domain = None
                    if hasattr(pr, "frontend_domain_id"):
                        pr.frontend_domain_id = None
                    pr.detail = (pr.detail or "") + f" | frontend_domain_rollback:{frontend_domain_id}"
                    pr.save()
                    logger.info("create_domains_task: rolled back frontend domain id=%s for prov_request=%s", frontend_domain_id, prov_request_id)
                except DokployError as e:
                    # Rollback failed — log and keep failure state (we already set failed above)
                    pr.detail = (pr.detail or "") + f" | frontend_domain_rollback_failed:{frontend_domain_id}:{e}"
                    pr.save()
                    logger.exception("create_domains_task: failed to rollback frontend domain id=%s for prov_request=%s: %s", frontend_domain_id, prov_request_id, e)

            return False

    # Both created successfully -> mark task as complete
    if created_frontend and created_backend:
        pr.domains_configured = True
        pr.status = "domains_configured"
        pr.detail = (pr.detail or "") + f" | domains_configured:{frontend_host},{backend_host}"
        pr.save()
        logger.info("create_domains_task: both domains created for prov_request=%s frontend=%s backend=%s", prov_request_id, frontend_host, backend_host)
        return True

    # Should not reach here, but for safety:
    pr.status = "failed"
    pr.failed = True
    pr.detail = (pr.detail or "") + " | unexpected state in create_domains_task"
    pr.save()
    logger.error("create_domains_task: unexpected end state for prov_request=%s", prov_request_id)
    return False

