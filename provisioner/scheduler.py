# provisioner/scheduler.py
from datetime import datetime, timedelta
import logging

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
