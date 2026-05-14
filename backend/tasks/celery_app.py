from celery import Celery

from app.config import settings

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

# v1.1.5:Casdoor 5-min reconcile beat 已隨 sidecar 下架移除。OIDC 改 in-process
# 後 source-of-truth 就是本地 users 表,user/role 變動透過 mutation hook
# (``schedule_user_resync``)即時觸發 Casbin grant 重建,不再需要 beat 兜底。
celery_app.conf.beat_schedule = {}
