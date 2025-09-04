# provisioner/progress.py  (or top of tasks.py)
import traceback
from .models import ProvisionRequest

def mark_running(pr: ProvisionRequest, running: bool = True):
    pr.running = running
    pr.save()

def mark_step(pr: ProvisionRequest, step: str, status_text: str = None):
    """
    Set progress to step (must be one of Progress.* values) and optionally update status_text.
    """
    pr.progress = step
    if status_text:
        pr.status = status_text
    pr.last_error = None
    pr.failed_at = None
    pr.running = False
    pr.save()

def mark_failure(pr: ProvisionRequest, step_name: str, exc: Exception):
    pr.progress = ProvisionRequest.Progress.FAILED
    pr.failed_at = step_name
    pr.last_error = "".join(traceback.format_exception_only(type(exc), exc))[:4000]
    pr.status = "failed"
    pr.running = False
    pr.save()
