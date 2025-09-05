# provisioner/scheduler.py
from datetime import datetime, timedelta
import logging
# provisioner/scheduler.py
import requests
from django.utils import timezone
from django.conf import settings
from .models import ProvisionRequest
from apscheduler.schedulers.background import BackgroundScheduler
from django_apscheduler.jobstores import DjangoJobStore, register_events

logger = logging.getLogger(__name__)

# create scheduler and attach the DjangoJobStore for persistence
scheduler = BackgroundScheduler()
scheduler.add_jobstore(DjangoJobStore(), "default")
register_events(scheduler)

def start_scheduler():
    """Start scheduler if not already running. Call this in AppConfig.ready()."""
    if not scheduler.running:
        logger.info("Starting Django APScheduler...")
        print("starting scheduler")
        scheduler.start(paused=False)

def schedule_provision_job(prov_request_id, payload, run_in_seconds: int = 1):
    """
    Schedule a one-off provision job.
    - prov_request_id: DB pk of ProvisionRequest
    - payload: dict passed to the task
    - run_in_seconds: delay before running (default 1s)
    Returns the scheduled job object.
    """
    from .tasks import provision_tenant_task  # import here to avoid import cycles

    job_id = f"provision-{prov_request_id}"

    # If a job with same id exists, return it (idempotency)
    existing = scheduler.get_job(job_id)
    if existing:
        return existing

    run_date = datetime.now() + timedelta(seconds=run_in_seconds)
    job = scheduler.add_job(
        func=provision_tenant_task,
        args=[prov_request_id, payload],
        trigger="date",
        id=job_id,
        run_date=run_date,
        replace_existing=False,
        max_instances=1,
    )
    logger.info("Scheduled provision job %s to run at %s", job_id, run_date)
    return job

def cancel_provision_job(prov_request_id):
    job_id = f"provision-{prov_request_id}"
    job = scheduler.get_job(job_id)
    if job:
        job.remove()
        logger.info("Cancelled job %s", job_id)
        return True
    return False


def backend_health_and_provision_attempt(prov_request_id, payload):
    """
    Perform one attempt:
    - Check /healthz
    - If healthy, call /internal/provision and mark complete
    - If not, compute backoff and reschedule itself
    """
    try:
        pr = ProvisionRequest.objects.get(id=prov_request_id)
    except ProvisionRequest.DoesNotExist:
        logger.error("ProvisionRequest %s not found", prov_request_id)
        return

    backend_url = pr.backend_domain
    if not backend_url:
        pr.failed = True
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | backend_domain missing"
        pr.save()
        return

    health_url = f"https://{backend_url}/healthz/"
    logger.info("Checking backend health for prov_request=%s try=%s", prov_request_id, pr.backend_health_tries + 1)

    success = False
    try:
        resp = requests.get(health_url, timeout=10)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            logger.info("Backend healthy for prov_request=%s", prov_request_id)
            success = True
    except Exception as e:
        logger.warning("Backend health check failed for prov_request=%s: %s", prov_request_id, e)

    if success:
        # --- Call internal provision ---
        provision_url = f"https://{backend_url}/internal/provision/"
        headers = {"X-Provision-Token": settings.PROVISION_CALLBACK_TOKEN}
        try:
            provision_resp = requests.post(provision_url, json=payload, headers=headers, timeout=15)
            if provision_resp.status_code == 200 and provision_resp.json().get("ok"):
                pr.completed = True
                pr.status = "completed"
                pr.detail = (pr.detail or "") + " | internal_provision_success"
                pr.super_user_created = True
                pr.internal_provision_scheduled = False
                pr.backend_health_job_id = None
                pr.save()
                logger.info("Provisioning complete for prov_request=%s", prov_request_id)
                return
            else:
                pr.failed = True
                pr.status = "failed"
                pr.detail = (pr.detail or "") + f" | internal_provision_failed: {provision_resp.text}"
                pr.save()
                return
        except Exception as e:
            pr.failed = True
            pr.status = "failed"
            pr.detail = (pr.detail or "") + f" | internal_provision_exception: {e}"
            pr.save()
            logger.exception("Internal provision call failed for prov_request=%s", prov_request_id)
            return

    # --- Retry logic ---
    pr.backend_health_tries += 1
    if pr.backend_health_tries >= 10:
        pr.failed = True
        pr.status = "failed"
        pr.detail = (pr.detail or "") + " | backend never became healthy after 10 tries"
        # clear scheduling flag so operator can reschedule manually if desired
        pr.internal_provision_scheduled = False
        pr.backend_health_job_id = None
        pr.save()
        logger.error("Giving up after 10 tries for prov_request=%s", prov_request_id)
        return

    # Compute backoff
    if pr.backend_health_tries <= 6:
        delay_minutes = 2 ** (pr.backend_health_tries - 1)  # 1,2,4,8,16,32
    else:
        delay_minutes = 60  # 1 hour

    pr.backend_next_wait = delay_minutes * 60
    pr.detail = (pr.detail or "") + f" | retry={pr.backend_health_tries} next_wait={delay_minutes}m"
    pr.save()

    # # --- Schedule itself again ---
    # from provisioner.scheduler import scheduler  # import the global scheduler
    job_id = f"backend-health-{prov_request_id}"
    run_at = timezone.now() + timedelta(seconds=pr.backend_next_wait)

    scheduler.add_job(
        func=backend_health_and_provision_attempt,
        args=[prov_request_id, payload],
        trigger="date",
        run_date=run_at,
        id=job_id,
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Rescheduled backend_health_and_provision_attempt for prov_request=%s in %sm",
                prov_request_id, delay_minutes)

