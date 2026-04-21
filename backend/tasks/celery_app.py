from celery import Celery

from app.config import settings

celery_app = Celery(
    "autotest_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["tasks.execution_tasks"],
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
