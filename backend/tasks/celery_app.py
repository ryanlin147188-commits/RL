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
    include=["tasks.execution_tasks", "tasks.email_tasks", "tasks.casdoor_reconcile"],
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

# Phase 6.3 beat schedule。要實際 fire 需要 worker 起 ``-B`` 或另起 ``celery beat``。
# task 本身會看 ``CASDOOR_RECONCILE_ENABLED``,沒啟用 → 立即 return,沒 IO 成本。
celery_app.conf.beat_schedule = {
    "casdoor-reconcile-5m": {
        "task": "tasks.casdoor_reconcile.run",
        "schedule": 300.0,  # 秒;5 分鐘
        "options": {"expires": 240},  # 沒在 4 分鐘內被 worker pick 走就丟掉,免堆積
    },
}
