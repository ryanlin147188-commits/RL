from celery import Celery

from app.config import settings
from app.observability import install_sentry, install_tracing, instrument_celery

# RFC-8: observability for the worker. Mirrors the FastAPI side; each call
# no-ops when its env switch is unset so dev runs stay quiet.
install_sentry("celery")
install_tracing(service_name="autotest-celery")
instrument_celery()

celery_app = Celery(
    "autotest_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["tasks.execution_tasks", "tasks.email_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Taipei",
    enable_utc=True,
    task_track_started=True,
    # Windows 開發環境請加 --pool=solo 啟動 worker
    worker_prefetch_multiplier=1,
)
