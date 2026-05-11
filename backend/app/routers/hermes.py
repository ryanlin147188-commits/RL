"""Hermes-powered AI 對話 REST endpoints。

PR3 主切換 PR — 取代 ai_chat.py。Endpoints 維持與舊 `/api/ai/conversations/*`
近似的回傳結構,只是路徑換成 `/api/hermes/sessions/*`,讓前端只需改 fetch URL。

行為差異(v1 接受的取捨)
- 訊息歷史不存 backend DB(由 Hermes sidecar 的 SQLite + FTS5 全權管理),
  GET 單筆 session 的 `messages` 永遠回 [];使用者重整頁進舊 session 時看不到
  歷史訊息,但仍能繼續對話(Hermes 在 sidecar 端記得 context)。
- `provider_config_id` 在新世代由 backend 自動挑(predict default token),
  前端傳值會被忽略。回傳維持欄位但永遠 None。
- `message_count` 永遠 0(沒存就沒得算)。前端 UI badge 會顯示 0,可接受。

錯誤對應
- Sidecar 連不上 / 5xx → 503 + retry_after
- LLM 端 401 / 429 / quota fail(Hermes 包成 ACP error)→ 502
- 沒 token 設定 → 400(跟舊行為一致,提示去設定 → AI Token)
- HERMES_ENABLED=False → 整 router 503(graceful degradation)
"""
# NOTE: 不能用 `from __future__ import annotations` — FastAPI 在 route 註冊時
# 跑 Pydantic TypeAdapter,對 send_message 的 `payload: HermesMessageRequest`
# 會無法 resolve forward ref(實測會 raise PydanticUndefinedAnnotation)。
import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.models.ai_token_config import AiTokenConfig
from app.models.hermes_gateway_credential import HermesGatewayCredential
from app.models.hermes_memory_consent import HermesMemoryConsent
from app.models.hermes_session import HermesSessionRef
from app.models.user import User
from sqlalchemy import update
from app.rate_limit import limiter
from app.services.hermes_client import (
    HermesAcpError,
    HermesAuthFailed,
    HermesBadRequest,
    HermesError,
    HermesNotFound,
    HermesUnavailable,
    get_hermes_client,
)
from app.services.hermes_provisioning import (
    ensure_user_workspace,
    invalidate_user_workspace,
    mem0_user_id,
    pick_token_for_user,
    resolve_embedder_config,
    resolve_mem0_configs,
    workspace_id_for_user,
)
from app.services.mem0_client import (
    Mem0Error,
    Mem0NotFound,
    get_mem0_client,
)
from app.services.mem0_llm_config import (
    build_embedder_config,
    build_llm_config,
)

router = APIRouter()
LOG = logging.getLogger(__name__)


# ── Schemas(放這裡而不是 schemas/hermes.py — 避免拆 5 個小檔)─────────
class HermesSessionCreate(BaseModel):
    title: Optional[str] = None
    # 為了與舊 ai_chat 前端相容,仍接受這個欄位但 backend 忽略
    provider_config_id: Optional[str] = None


class HermesSessionUpdate(BaseModel):
    title: Optional[str] = None
    provider_config_id: Optional[str] = None  # 同上,被忽略


class HermesMessageRequest(BaseModel):
    content: str


class HermesMessage(BaseModel):
    """AI 訊息(synthetic — backend 不存,只在 send response 時即時組出來)。

    欄位名對齊舊 AiMessageResponse 讓前端 `_aiChatBubble` 能直接 render。
    """
    id: str
    conversation_id: str
    role: str
    content: str
    tokens_used: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    created_at: datetime


class HermesSessionResponse(BaseModel):
    id: str
    owner: str
    organization_id: Optional[str] = None
    title: str
    # 永遠 None — 新版 backend 自動挑 token,不再讓前端綁
    provider_config_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # 永遠 0(訊息不存 backend DB,沒得算)
    message_count: int = 0
    last_message_preview: Optional[str] = None


class HermesSessionDetail(HermesSessionResponse):
    # v1:訊息歷史不從 backend 取,永遠空陣列 — 前端會顯示「(歷史不可恢復)」提示
    messages: list[HermesMessage] = Field(default_factory=list)


class HermesSendMessageResponse(BaseModel):
    user_message: HermesMessage
    assistant_message: HermesMessage
    # PR6:pre-hook 從 mem0 召回的記憶 list(若 feature flag off / 沒 token /
    # mem0 down / query 太短就空)。前端用 length 顯示「引用了 N 條過往記憶」chip。
    recalled_memories: list[str] = Field(default_factory=list)


class HermesSkillSummary(BaseModel):
    name: str
    namespace: str = ""
    description: str = ""
    platforms: list[str] = Field(default_factory=list)
    path: str  # 相對 workspace 的路徑(例 "official/code-review/")


class HermesSkillsResponse(BaseModel):
    skills: list[HermesSkillSummary]


class HermesMemoryHit(BaseModel):
    session_id: str
    session_title: str
    role: str
    content: Optional[str] = None
    timestamp: float
    rank: float  # FTS5 bm25,值越小越相關


class HermesMemorySearchResponse(BaseModel):
    results: list[HermesMemoryHit]
    query: str
    sanitized_query: Optional[str] = None
    limit: int


class HermesCronJob(BaseModel):
    id: str
    name: Optional[str] = None
    prompt: Optional[str] = None
    schedule: Optional[str] = None
    schedule_kind: Optional[str] = None  # 'cron' | 'interval' | 'one_shot'
    enabled: bool = True
    state: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None


class HermesCronListResponse(BaseModel):
    jobs: list[HermesCronJob]


class HermesCronCreate(BaseModel):
    schedule: str
    prompt: str
    name: Optional[str] = None


# ── PR4:semantic memory(mem0)schemas ─────────────────────────────
class MemorySemanticHit(BaseModel):
    """單筆 mem0 fact;對齊 mem0 v0.1.x 回傳 schema(memory + 可選 metadata)。"""
    id: str
    memory: str
    score: Optional[float] = None
    metadata: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MemorySemanticSearchResponse(BaseModel):
    results: list[MemorySemanticHit]
    query: str
    limit: int
    # mem0 不 work 的原因(沒 token / Anthropic 沒 embedder / sidecar down 等),前端 UI hint
    degraded_reason: Optional[str] = None


class MemorySemanticListResponse(BaseModel):
    results: list[MemorySemanticHit]
    limit: int
    degraded_reason: Optional[str] = None


class MemoryConsentResponse(BaseModel):
    extraction_enabled: bool
    paused_session_count: int = 0
    # 不回完整 paused_session_ids dict — 那是內部 timestamp,前端用 count 即可
    updated_at: Optional[datetime] = None


class MemoryConsentUpdate(BaseModel):
    extraction_enabled: bool


class MemorySessionPauseResponse(BaseModel):
    session_id: str
    paused_until: float  # epoch seconds


class HermesGatewayPlatformConfig(BaseModel):
    """單一 platform 的設定 — token 永不回客端,只回 has_token 旗標。"""
    enabled: bool = True
    has_token: bool = False
    extra: dict = Field(default_factory=dict)


class HermesGatewayDaemonStatus(BaseModel):
    running: bool
    uptime_sec: Optional[float] = None
    last_exit_code: Optional[int] = None
    recent_stderr: list[str] = Field(default_factory=list)


class HermesGatewayStatusResponse(BaseModel):
    platforms: dict[str, HermesGatewayPlatformConfig] = Field(default_factory=dict)
    daemon: HermesGatewayDaemonStatus


class HermesGatewayEnableRequest(BaseModel):
    """Enable a platform。token 必填(後端解密既存 cred 或從 body 直接帶)。

    為了避免「前端要傳明文 token 才能 enable」這個 UX 痛點,backend 會優先從
    `hermes_gateway_credentials` 解密拿 token;只有 token field 有帶才覆寫。
    """
    token: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class HermesGatewayEnableResponse(BaseModel):
    platform: str
    enabled: bool
    daemon: HermesGatewayDaemonStatus


# ── 輔助 ──────────────────────────────────────────────────────────────
def _resolve_locale(request: Request) -> str:
    """從 request 的 Accept-Language header 解出 zh-TW / en;沒帶就回 ''。

    前端 fetch wrapper 會把 localStorage["autotest.locale"] 對應的 zh-TW / en
    放進 Accept-Language。Backend 用這個決定 AI 助理當輪要用什麼語言回。
    """
    raw = request.headers.get("accept-language", "")
    if not raw:
        return ""
    # Accept-Language 可能含 quality 與多語(en-US,en;q=0.9,zh-TW;q=0.8)。
    # 我們只取第一個 token,broad-match 到 zh / en 兩類。
    first = raw.split(",")[0].strip().split(";")[0].strip().lower()
    if first.startswith("zh"):
        return "zh-TW"
    if first.startswith("en"):
        return "en"
    return ""


def _language_directive(locale: str) -> str:
    """組 per-request 語言指示 prefix(用 XML tag 標示,不會被誤當對話內容)。"""
    if locale == "zh-TW":
        return (
            "<language_directive>\n"
            "請務必使用「繁體中文」回答此輪訊息(無論前文如何)。\n"
            "</language_directive>\n\n"
        )
    if locale == "en":
        return (
            "<language_directive>\n"
            "Reply to this turn in English (regardless of prior conversation language).\n"
            "</language_directive>\n\n"
        )
    return ""


async def _check_session_or_404(
    db: AsyncSession, sid: str, user: User,
) -> HermesSessionRef:
    ref = await db.get(HermesSessionRef, sid)
    if not ref or ref.owner != user.username:
        raise HTTPException(404, "Session not found")
    return ref


def _ref_to_response(ref: HermesSessionRef) -> dict:
    return {
        "id": ref.id,
        "owner": ref.owner,
        "organization_id": ref.organization_id,
        "title": ref.title,
        "provider_config_id": None,
        "created_at": ref.created_at,
        "updated_at": ref.updated_at,
        "message_count": 0,
        "last_message_preview": ref.last_message_preview,
    }


def _hermes_error_to_http(exc: HermesError) -> HTTPException:
    """統一把 hermes_client exception 翻成 HTTPException。"""
    if isinstance(exc, HermesNotFound):
        return HTTPException(404, detail={"error": "not_found", "message": str(exc)})
    if isinstance(exc, HermesUnavailable):
        return HTTPException(
            503,
            detail={"error": "hermes_unavailable", "retry_after": 30,
                    "message": "AI 助理服務暫時無法使用,請稍後再試"},
        )
    if isinstance(exc, HermesAuthFailed):
        # 這是 backend ↔ sidecar 認證失敗 — 系統 misconfig,使用者也只能等 ops 修
        LOG.error("HermesAuthFailed — SIDECAR_AUTH_TOKEN mismatch between backend and sidecar")
        return HTTPException(
            503,
            detail={"error": "hermes_misconfig",
                    "message": "AI 助理服務設定錯誤,請聯絡管理員"},
        )
    if isinstance(exc, HermesAcpError):
        # LLM provider 回錯(quota / 401 key / model not found 等);把 detail 帶給前端
        return HTTPException(
            502,
            detail={"error": "ai_provider_error", "code": exc.code,
                    "message": exc.detail},
        )
    if isinstance(exc, HermesBadRequest):
        return HTTPException(400, detail={"error": "bad_request",
                                          "message": str(exc)})
    # generic — 不該發生,回 502 並 log
    LOG.exception("unmapped HermesError: %s", exc)
    return HTTPException(502, detail={"error": "hermes_error", "message": str(exc)})


# ── mem0 consent helpers(PR3)─────────────────────────────────────
async def _get_or_default_consent(
    db: AsyncSession, user: User,
) -> HermesMemoryConsent:
    """取使用者 consent;沒 row 視為 default(extraction_enabled=True、無 paused)。

    PR3 不主動建 row(避免 send_message 路徑寫入新 user 的 consent row 雜訊);
    PR4 的 PUT /api/hermes/memory/consent 才 upsert。
    """
    consent = await db.get(HermesMemoryConsent, user.username)
    if consent is None:
        # 暫時 in-memory 物件,只供讀;不 add 到 session
        return HermesMemoryConsent(
            username=user.username,
            organization_id=user.organization_id,
            extraction_enabled=True,
            paused_session_ids=None,
        )
    return consent


_QUERY_WORD_RE = None  # lazy compile


def _is_query_searchable(query: str) -> bool:
    """是否值得跑 mem0.search:跳過太短 / 純標點 / 純空白的 query。

    PR6 plan §「Pre-hook recall」要求:避免短 query 雜訊召回(例「OK」「謝謝」)
    且 LLM embed call 有成本,別浪費 user quota 在不會匹配的查詢上。
    """
    import re
    global _QUERY_WORD_RE
    if _QUERY_WORD_RE is None:
        # alphanum + 中日韓:有「實質字元」才算可搜尋
        _QUERY_WORD_RE = re.compile(r"[\w一-鿿]")
    stripped = (query or "").strip()
    if len(stripped) < 4:
        return False
    return bool(_QUERY_WORD_RE.search(stripped))


def _session_paused(consent: HermesMemoryConsent, session_id: str) -> bool:
    """檢查該 session 是否在暫停名單內(且沒過期)。"""
    paused = consent.paused_session_ids or {}
    if not isinstance(paused, dict):
        return False
    until = paused.get(session_id)
    if until is None:
        return False
    try:
        return float(until) > time.time()
    except (TypeError, ValueError):
        return False


def _ensure_enabled() -> None:
    """Feature flag 守門員。整個 router 都需要這個檢查。"""
    if not settings.HERMES_ENABLED:
        raise HTTPException(
            503,
            detail={"error": "hermes_disabled",
                    "message": "AI 助理已停用,請聯絡管理員"},
        )


async def _build_mem0_mcp_servers(
    user: User,
    db: AsyncSession,
) -> list[dict]:
    """為 hermes create_session 組出 mcp_servers list。

    內含兩個 MCP 來源(任一可獨立 enable / disable):
    - memory:mem0 sidecar 的 search_memory(跨 session 語意記憶)
    - platform:backend 自家的 platform_mcp(create_project / list_projects 等
      平台動作 — 讓 LLM 真的能「幫使用者建專案」而不是只描述步驟)

    Headers 帶共用 X-Sidecar-Auth + 個別 user-id 識別 header;LLM key 永遠不出現在
    這層(各 sidecar 自己解析)。
    """
    servers: list[dict] = []

    # ── memory ──────────────────────────────────────────────
    mem0_ready = (
        settings.MEM0_HERMES_TOOL_ENABLED
        and settings.MEM0_ENABLED
        and (await resolve_mem0_configs(db, user)) is not None
    )
    if mem0_ready:
        servers.append({
            "name": "memory",
            "url": settings.MEM0_HERMES_TOOL_URL,
            "headers": [
                {"name": "X-Sidecar-Auth",  "value": settings.MEM0_SIDECAR_AUTH_TOKEN},
                {"name": "X-Mem0-User-Id",  "value": mem0_user_id(user)},
            ],
        })

    # ── platform(讓 LLM 能呼叫平台 API) ────────────────────
    if settings.PLATFORM_MCP_ENABLED:
        servers.append({
            "name": "platform",
            "url": settings.PLATFORM_MCP_URL,
            "headers": [
                # 與 hermes / mem0 共用同一個 sidecar secret(內網限定)
                {"name": "X-Sidecar-Auth",   "value": settings.SIDECAR_AUTH_TOKEN},
                # platform_mcp 用這個解出 ORM User → 在 user 的 org / 權限下執行
                {"name": "X-Platform-User",  "value": user.username},
            ],
        })

    # ── playwright(per-user autotest-mcp 容器,給 LLM 真正操作瀏覽器) ──
    # ensure_user_mcp_running 是 best-effort:image 還在 build / docker 連不上時
    # 回 None,我們就跳過(LLM 拿不到 browser_* tools,使用者下次再要時前端
    # 「啟動 MCP」按鈕會看到一致的 building progress)。
    if settings.PLAYWRIGHT_MCP_HERMES_ENABLED:
        try:
            from app.routers.ai import ensure_user_mcp_running
            mcp_state = await ensure_user_mcp_running(user.username)
        except Exception:  # noqa: BLE001
            LOG.exception("playwright mcp ensure failed user=%s", user.username)
            mcp_state = None
        if mcp_state and mcp_state.get("container_name"):
            servers.append({
                "name": "playwright",
                # 容器在同一個 docker network,用 service-name:port 連
                "url": f"http://{mcp_state['container_name']}:8931/mcp",
                # Playwright MCP 自身沒驗證(--allowed-hosts '*' 內網限定),
                # headers 只給 noop;但保留 list 以便未來加 capability token。
                "headers": [],
            })

    return servers


# ── Routes ────────────────────────────────────────────────────────────
@router.get("/hermes/health", tags=["V · AI"])
async def health(user: User = Depends(get_current_user)):
    """Probe sidecar — 前端可以用來判斷要不要顯示 AI 浮動按鈕。

    PR4 起加 `mem0_up` — 前端 Memory modal 的 Semantic 分頁靠這個決定要不要顯示。
    兩個 healthcheck 並行(避免單條失敗拖總時間 5s+)。
    """
    if not settings.HERMES_ENABLED:
        return {"enabled": False, "sidecar_up": False, "mem0_enabled": False, "mem0_up": False}
    hermes = get_hermes_client()
    mem0 = get_mem0_client()
    sidecar_up, mem0_up = await asyncio.gather(
        hermes.healthcheck(), mem0.healthcheck(),
        return_exceptions=False,
    )
    return {
        "enabled": True,
        "sidecar_up": bool(sidecar_up),
        "mem0_enabled": bool(settings.MEM0_ENABLED),
        "mem0_up": bool(mem0_up),
    }


@router.post("/hermes/reprovision", tags=["V · AI"])
async def reprovision(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """強制把目前 default AI Token 重新推給 sidecar。

    使用場景:
    - 使用者剛在「設定 → AI Token」改了 default,但 5min cache 還沒過期 → 點這個立即生效
    - 使用者懷疑 sidecar 用舊 token(例:在 sidecar 容器外部把 .env 改了)→ 強制重推
    - 換 model 或 base_url 後想立刻試新設定 → 重推

    流程:invalidate cache → 立即 ensure_user_workspace(force=True)→ 回 OK 或 sidecar 錯誤。
    """
    _ensure_enabled()
    invalidate_user_workspace(user.username)
    hermes = get_hermes_client()
    try:
        ws = await ensure_user_workspace(user, db, hermes, force=True)
    except HermesError as e:
        if str(e) in ("no_token_configured", "token_missing_api_key"):
            raise HTTPException(
                400,
                detail={"error": "no_token_configured",
                        "message": "尚未設定任何 AI Token,請先至設定 → AI Token 加入"},
            )
        raise _hermes_error_to_http(e)
    return {"workspace_id": ws, "status": "reprovisioned"}


@router.post("/hermes/default-token/{token_id}", tags=["V · AI"])
async def set_hermes_default_token(
    token_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把指定的 ai_token_config 設為「AI 助理唯一使用」並立即推給 sidecar。

    跟 PUT /api/settings/ai-tokens/{id} {is_default:true} 的區別:
    - settings 那條走「per (org, provider) 唯一 default」 — 跨 provider 可同時有
      多個 is_default=True(例:OpenAI + Anthropic 各保留一個 default)。
    - 這條走「全 org 唯一 default」 — 把該 user 所在 org 的所有其他 enabled
      token is_default 設成 false,只留指定那筆。LLM 串接 modal 的「設為預設」
      用這個語意才不會在 reprovision 時挑錯 token。

    流程:驗 token 屬於該 user 的 org → 清掉同 org 所有其他 default → 設新 default
    → invalidate cache → reprovision sidecar。
    """
    _ensure_enabled()
    target = await db.get(AiTokenConfig, token_id)
    if not target or (
        not user.is_superuser
        and target.organization_id != user.organization_id
    ):
        raise HTTPException(404, detail={"error": "token_not_found"})
    if not target.enabled:
        raise HTTPException(
            400,
            detail={"error": "token_disabled",
                    "message": "該 token 為停用狀態,請先在設定中啟用"},
        )
    if not target.api_key:
        raise HTTPException(
            400,
            detail={"error": "token_missing_api_key",
                    "message": "該 token 沒有 API key,請先在設定中補上"},
        )

    # 全 org 內(若 superuser 帶整個系統)清掉其他 default,再設新 default
    where_clause = AiTokenConfig.id != target.id
    if target.organization_id is not None:
        where_clause = where_clause & (
            AiTokenConfig.organization_id == target.organization_id
        )
    await db.execute(
        update(AiTokenConfig)
        .where(where_clause, AiTokenConfig.is_default.is_(True))
        .values(is_default=False)
    )
    target.is_default = True
    await db.flush()
    await db.refresh(target)

    invalidate_user_workspace(user.username)
    hermes = get_hermes_client()
    try:
        ws = await ensure_user_workspace(user, db, hermes, force=True)
    except HermesError as e:
        # 設了 default 但推送失敗 — DB 改動仍生效,告訴前端 sidecar 端問題
        raise _hermes_error_to_http(e)
    return {
        "workspace_id": ws,
        "default_token_id": target.id,
        "default_token_name": target.name,
        "default_provider": target.provider,
        "status": "default_set_and_reprovisioned",
    }


@router.get(
    "/hermes/sessions",
    response_model=list[HermesSessionResponse],
    tags=["V · AI"],
)
async def list_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    rows = (
        await db.execute(
            select(HermesSessionRef)
            .where(HermesSessionRef.owner == user.username)
            .order_by(desc(HermesSessionRef.updated_at))
            .limit(50)
        )
    ).scalars().all()
    return [_ref_to_response(r) for r in rows]


@router.post(
    "/hermes/sessions",
    response_model=HermesSessionResponse,
    status_code=201,
    tags=["V · AI"],
)
async def create_session(
    payload: HermesSessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    hermes = get_hermes_client()
    try:
        ws = await ensure_user_workspace(user, db, hermes)
        # ensure_user_workspace 已把 llm_config 推給 mem0 sidecar;這裡再組
        # mcp_servers 把 endpoint + auth header 透給 hermes,讓 ACP 子進程的
        # LLM 可以主動 invoke `search_memory` tool(plan §3.4)。
        mcp_servers = await _build_mem0_mcp_servers(user, db)
        result = await hermes.create_session(ws, mcp_servers=mcp_servers)
    except HermesError as e:
        if str(e) in ("no_token_configured", "token_missing_api_key"):
            raise HTTPException(
                400,
                detail={"error": "no_token_configured",
                        "message": "尚未設定任何 AI provider;請至設定 → AI Token "
                                   "加入一個並設為預設"},
            )
        raise _hermes_error_to_http(e)

    sid = result.get("session_id")
    if not sid:
        raise HTTPException(502, detail={"error": "hermes_no_session_id",
                                         "message": "Hermes 未回 session_id"})
    ref = HermesSessionRef(
        id=sid,
        workspace_id=ws,
        organization_id=user.organization_id,
        owner=user.username,
        title=(payload.title or "新對話")[:200],
    )
    db.add(ref)
    await db.flush()
    await db.refresh(ref)
    return _ref_to_response(ref)


@router.get(
    "/hermes/sessions/{sid}",
    response_model=HermesSessionDetail,
    tags=["V · AI"],
)
async def get_session(
    sid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    ref = await _check_session_or_404(db, sid, user)
    base = _ref_to_response(ref)
    # v1:歷史訊息不從 backend 取(也沒得取 — 訊息只在 Hermes sidecar 內存)。
    # 之後若 Hermes 暴露 fetch-history endpoint 再回填。
    base["messages"] = []
    return base


@router.put(
    "/hermes/sessions/{sid}",
    response_model=HermesSessionResponse,
    tags=["V · AI"],
)
async def update_session(
    sid: str,
    payload: HermesSessionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    ref = await _check_session_or_404(db, sid, user)
    if payload.title is not None:
        ref.title = payload.title[:200]
    # provider_config_id 被忽略 — 不再讓前端綁死 provider
    await db.flush()
    await db.refresh(ref)
    return _ref_to_response(ref)


@router.delete(
    "/hermes/sessions/{sid}",
    status_code=204,
    tags=["V · AI"],
)
async def delete_session(
    sid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    ref = await _check_session_or_404(db, sid, user)
    # NOTE: Hermes sidecar PR2 沒實作 session/delete — 只刪 backend ref,
    # sidecar SQLite 內留個 orphan session(無危害,後續 PR 補)。
    await db.delete(ref)
    await db.flush()


# ── Skills(PR4)─────────────────────────────────────────────────────
# 純讀取(走 sidecar 檔案系統),不會啟動 ACP 子進程。Workspace 還沒 provision
# 時 sidecar 也回 [],這層只透傳。
@router.get(
    "/hermes/skills",
    response_model=HermesSkillsResponse,
    tags=["V · AI"],
)
async def list_skills(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    hermes = get_hermes_client()
    # ensure_user_workspace 也許還沒 provision(沒設 token)— skills 是 read-only
    # 不應該因為沒 token 就 400。直接拿 workspace_id 去問,sidecar 回空陣列即可。
    ws = workspace_id_for_user(user)
    try:
        result = await hermes.list_skills(ws)
    except HermesError as e:
        raise _hermes_error_to_http(e)
    return HermesSkillsResponse(skills=result.get("skills") or [])


# ── Memory search(PR4)──────────────────────────────────────────────
# 直接查 sidecar 的 state.db FTS5。只查當前使用者自己的 workspace,跨 user 隔離
# 由 workspace_id 自然提供(sidecar 強制 path 邊界,不接受 client 自帶 ws_id)。
@router.get(
    "/hermes/memory/search",
    response_model=HermesMemorySearchResponse,
    tags=["V · AI"],
)
async def search_memory(
    q: str = Query("", description="搜尋字串(空字串回空結果)"),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    try:
        result = await hermes.search_memory(ws, q, limit=limit)
    except HermesError as e:
        raise _hermes_error_to_http(e)
    return HermesMemorySearchResponse(
        results=result.get("results") or [],
        query=result.get("query", q),
        sanitized_query=result.get("sanitized_query"),
        limit=result.get("limit", limit),
    )


# ── Semantic memory(mem0,PR4-mem0)─────────────────────────────────
# 跨 session LLM-extracted 事實庫,跟 hermes/memory/search 的 lexical FTS5 互補。
# 隔離靠 mem0 sidecar 的 user_id metadata filter — backend 強制傳對的 user_id,
# 即便有人想偽造 backend 也只接受 JWT decode 出來的 user.username。
#
# 沒 default token / Anthropic-only(無 embedder)時 search/list 降級回空 +
# `degraded_reason`,讓前端 UI 顯示 hint 而非 raise(plan §5)。

@router.get(
    "/hermes/memory/semantic",
    response_model=MemorySemanticSearchResponse,
    tags=["V · AI"],
)
async def semantic_search(
    q: str = Query("", description="搜尋字串(空回空結果)"),
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """語意記憶搜尋(mem0 + vector + LLM extraction)。"""
    _ensure_enabled()
    if not settings.MEM0_ENABLED:
        return MemorySemanticSearchResponse(
            results=[], query=q, limit=limit, degraded_reason="mem0_disabled",
        )
    if not q.strip():
        return MemorySemanticSearchResponse(results=[], query=q, limit=limit)

    cfg = await pick_token_for_user(db, user)
    if not cfg or not cfg.api_key:
        return MemorySemanticSearchResponse(
            results=[], query=q, limit=limit,
            degraded_reason="no_token_configured",
        )
    # primary 沒 embedder(Anthropic)→ 同 org 找 OpenAI/Gemini fallback;都沒才 degrade
    embed_cfg = await resolve_embedder_config(db, user, cfg)
    if not embed_cfg:
        return MemorySemanticSearchResponse(
            results=[], query=q, limit=limit, degraded_reason="no_embedder",
        )
    llm_cfg = build_llm_config(cfg)
    mem0 = get_mem0_client()
    try:
        results = await mem0.search(
            user_id=mem0_user_id(user),
            query=q.strip(),
            llm_config=llm_cfg,
            embedder_config=embed_cfg,
            top_k=limit,
        )
    except Mem0Error as e:
        # plan §6:服務端錯誤回 503 + retry_after,前端能 graceful 處理
        LOG.warning("mem0 search failed user=%s: %s", user.username, e)
        raise HTTPException(
            503,
            detail={"error": "mem0_unavailable",
                    "retry_after": 30,
                    "message": "語意記憶服務暫時無法使用,請稍後再試"},
        )
    return MemorySemanticSearchResponse(
        results=results, query=q, limit=limit,
    )


@router.get(
    "/hermes/memory/semantic/list",
    response_model=MemorySemanticListResponse,
    tags=["V · AI"],
)
async def semantic_list(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    if not settings.MEM0_ENABLED:
        return MemorySemanticListResponse(
            results=[], limit=limit, degraded_reason="mem0_disabled",
        )
    cfg = await pick_token_for_user(db, user)
    if not cfg or not cfg.api_key:
        return MemorySemanticListResponse(
            results=[], limit=limit, degraded_reason="no_token_configured",
        )
    embed_cfg = await resolve_embedder_config(db, user, cfg)
    if not embed_cfg:
        return MemorySemanticListResponse(
            results=[], limit=limit, degraded_reason="no_embedder",
        )
    llm_cfg = build_llm_config(cfg)
    mem0 = get_mem0_client()
    try:
        results = await mem0.list_memories(
            user_id=mem0_user_id(user),
            llm_config=llm_cfg,
            embedder_config=embed_cfg,
            limit=limit,
        )
    except Mem0Error as e:
        LOG.warning("mem0 list failed user=%s: %s", user.username, e)
        raise HTTPException(
            503,
            detail={"error": "mem0_unavailable", "retry_after": 30,
                    "message": "語意記憶服務暫時無法使用,請稍後再試"},
        )
    return MemorySemanticListResponse(results=results, limit=limit)


@router.delete(
    "/hermes/memory/semantic/{memory_id}",
    status_code=204,
    tags=["V · AI"],
)
async def semantic_delete(
    memory_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    if not settings.MEM0_ENABLED:
        raise HTTPException(
            503,
            detail={"error": "mem0_disabled",
                    "message": "mem0 已停用"},
        )
    if not memory_id or "/" in memory_id or ".." in memory_id or len(memory_id) > 64:
        raise HTTPException(400, detail={"error": "invalid_memory_id"})
    cfg = await pick_token_for_user(db, user)
    if not cfg or not cfg.api_key:
        raise HTTPException(
            400,
            detail={"error": "no_token_configured",
                    "message": "刪除語意記憶需先設定 AI Token"},
        )
    embed_cfg = await resolve_embedder_config(db, user, cfg)
    if not embed_cfg:
        raise HTTPException(
            400,
            detail={"error": "no_embedder",
                    "message": "目前 default token 沒 embedder,無法刪除語意記憶"},
        )
    llm_cfg = build_llm_config(cfg)
    mem0 = get_mem0_client()
    try:
        await mem0.delete_memory(
            user_id=mem0_user_id(user),
            memory_id=memory_id,
            llm_config=llm_cfg,
            embedder_config=embed_cfg,
        )
    except Mem0NotFound:
        raise HTTPException(404, detail={"error": "memory_not_found"})
    except Mem0Error as e:
        LOG.warning("mem0 delete failed user=%s mid=%s: %s",
                    user.username, memory_id, e)
        raise HTTPException(
            502,
            detail={"error": "mem0_error", "message": str(e)[:200]},
        )


@router.delete(
    "/hermes/memory/semantic",
    status_code=204,
    tags=["V · AI"],
)
async def semantic_wipe(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清空該 user 全部 semantic 記憶。需 `X-Confirm-Wipe: true` header 防誤觸。"""
    _ensure_enabled()
    if not settings.MEM0_ENABLED:
        raise HTTPException(
            503,
            detail={"error": "mem0_disabled", "message": "mem0 已停用"},
        )
    if request.headers.get("X-Confirm-Wipe", "").lower() != "true":
        raise HTTPException(
            400,
            detail={"error": "confirm_required",
                    "message": "需要 X-Confirm-Wipe: true header"},
        )
    cfg = await pick_token_for_user(db, user)
    if not cfg or not cfg.api_key:
        raise HTTPException(400, detail={"error": "no_token_configured"})
    embed_cfg = await resolve_embedder_config(db, user, cfg)
    if not embed_cfg:
        raise HTTPException(400, detail={"error": "no_embedder"})
    llm_cfg = build_llm_config(cfg)
    mem0 = get_mem0_client()
    try:
        await mem0.delete_all(
            user_id=mem0_user_id(user),
            llm_config=llm_cfg,
            embedder_config=embed_cfg,
        )
    except Mem0Error as e:
        LOG.warning("mem0 delete_all failed user=%s: %s", user.username, e)
        raise HTTPException(
            502,
            detail={"error": "mem0_error", "message": str(e)[:200]},
        )


@router.get(
    "/hermes/memory/consent",
    response_model=MemoryConsentResponse,
    tags=["V · AI"],
)
async def get_memory_consent(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """讀使用者的 memory consent 狀態。沒 row 時回預設(enabled=True、暫停 0 個)。"""
    _ensure_enabled()
    consent = await db.get(HermesMemoryConsent, user.username)
    if consent is None:
        return MemoryConsentResponse(extraction_enabled=True, paused_session_count=0)
    # 算未過期暫停 session 數;過期項忽略
    paused = consent.paused_session_ids or {}
    now = time.time()
    active_paused = sum(
        1 for ts in (paused.values() if isinstance(paused, dict) else [])
        if isinstance(ts, (int, float)) and float(ts) > now
    )
    return MemoryConsentResponse(
        extraction_enabled=bool(consent.extraction_enabled),
        paused_session_count=active_paused,
        updated_at=consent.updated_at,
    )


@router.put(
    "/hermes/memory/consent",
    response_model=MemoryConsentResponse,
    tags=["V · AI"],
)
async def update_memory_consent(
    payload: MemoryConsentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """切換 fact extraction 開關。Upsert 行為:沒 row 就建。"""
    _ensure_enabled()
    consent = await db.get(HermesMemoryConsent, user.username)
    if consent is None:
        consent = HermesMemoryConsent(
            username=user.username,
            organization_id=user.organization_id,
            extraction_enabled=payload.extraction_enabled,
        )
        db.add(consent)
    else:
        consent.extraction_enabled = payload.extraction_enabled
    await db.flush()
    await db.refresh(consent)
    paused = consent.paused_session_ids or {}
    now = time.time()
    active_paused = sum(
        1 for ts in (paused.values() if isinstance(paused, dict) else [])
        if isinstance(ts, (int, float)) and float(ts) > now
    )
    return MemoryConsentResponse(
        extraction_enabled=consent.extraction_enabled,
        paused_session_count=active_paused,
        updated_at=consent.updated_at,
    )


@router.post(
    "/hermes/sessions/{sid}/memory/pause",
    response_model=MemorySessionPauseResponse,
    tags=["V · AI"],
)
async def pause_session_memory(
    sid: str,
    duration_minutes: int = Query(60, ge=1, le=1440, description="暫停時長(分);上限 24h"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """該 session 後續訊息不抽 fact(N 分鐘後自動到期,默認 60 分)。"""
    _ensure_enabled()
    # 驗 session 屬於該 user — 跟既有 _check_session_or_404 一致
    ref = await _check_session_or_404(db, sid, user)
    paused_until = time.time() + duration_minutes * 60

    consent = await db.get(HermesMemoryConsent, user.username)
    if consent is None:
        consent = HermesMemoryConsent(
            username=user.username,
            organization_id=user.organization_id,
            extraction_enabled=True,
            paused_session_ids={sid: paused_until},
        )
        db.add(consent)
    else:
        # 不修改既有其他 sid 的暫停狀態,只 upsert 這條
        existing = consent.paused_session_ids or {}
        if not isinstance(existing, dict):
            existing = {}
        # 順便清過期項(避免 dict 無限長)
        now = time.time()
        cleaned = {k: v for k, v in existing.items()
                   if isinstance(v, (int, float)) and float(v) > now}
        cleaned[sid] = paused_until
        consent.paused_session_ids = cleaned
    await db.flush()
    return MemorySessionPauseResponse(
        session_id=sid, paused_until=paused_until,
    )


# ── Cron(PR5)──────────────────────────────────────────────────────
# 直接讀寫 sidecar 的 jobs.json — Hermes ACP 子進程啟動後會自己讀。
# 不存 backend DB,以 sidecar 為單一 source of truth(避免雙寫漂移)。
# ensure_user_workspace 仍要跑 — 沒設 token 就無法跑 cron(LLM 不可用)。
@router.get(
    "/hermes/cron",
    response_model=HermesCronListResponse,
    tags=["V · AI"],
)
async def list_cron(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    try:
        result = await hermes.list_cron_jobs(ws)
    except HermesError as e:
        raise _hermes_error_to_http(e)
    return HermesCronListResponse(jobs=result.get("jobs") or [])


@router.post(
    "/hermes/cron",
    response_model=HermesCronJob,
    status_code=201,
    tags=["V · AI"],
)
async def add_cron(
    payload: HermesCronCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    if not payload.schedule.strip() or not payload.prompt.strip():
        raise HTTPException(400, detail={"error": "schedule_and_prompt_required",
                                         "message": "schedule 與 prompt 都必填"})
    hermes = get_hermes_client()
    try:
        # cron 要能跑 = workspace 要 provisioned(有 LLM key)
        ws = await ensure_user_workspace(user, db, hermes)
        result = await hermes.add_cron_job(
            ws,
            schedule=payload.schedule.strip(),
            prompt=payload.prompt.strip(),
            name=(payload.name or "").strip() or None,
        )
    except HermesError as e:
        if str(e) in ("no_token_configured", "token_missing_api_key"):
            raise HTTPException(
                400,
                detail={"error": "no_token_configured",
                        "message": "Cron 需要先設定 AI Token 才能執行;"
                                   "請至設定 → AI Token 加入並設為預設"},
            )
        raise _hermes_error_to_http(e)
    return HermesCronJob(**result)


@router.delete(
    "/hermes/cron/{job_id}",
    status_code=204,
    tags=["V · AI"],
)
async def delete_cron(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    if not job_id or "/" in job_id or ".." in job_id:
        raise HTTPException(400, detail={"error": "invalid_job_id"})
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    try:
        await hermes.delete_cron_job(ws, job_id)
    except HermesError as e:
        raise _hermes_error_to_http(e)


# ── Gateway(Telegram / Discord / Slack 等)──────────────────────────
# Per-user / per-platform bot token 用 Fernet 加密存 hermes_gateway_credentials。
# Enable 把 token 推給 sidecar 寫 gateway.json 並 spawn 子進程。
_SUPPORTED_PLATFORMS = {"telegram", "discord", "slack", "matrix", "signal", "whatsapp"}


def _validate_platform(platform: str) -> None:
    if platform not in _SUPPORTED_PLATFORMS:
        raise HTTPException(
            400,
            detail={"error": "unsupported_platform",
                    "message": f"Platform '{platform}' 尚未支援;"
                               f"目前支援:{sorted(_SUPPORTED_PLATFORMS)}"},
        )


@router.get(
    "/hermes/gateway",
    response_model=HermesGatewayStatusResponse,
    tags=["V · AI"],
)
async def gateway_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    try:
        result = await hermes.gateway_status(ws)
    except HermesError as e:
        raise _hermes_error_to_http(e)
    return HermesGatewayStatusResponse(**result)


@router.post(
    "/hermes/gateway/{platform}/enable",
    response_model=HermesGatewayEnableResponse,
    tags=["V · AI"],
)
async def gateway_enable(
    platform: str,
    payload: HermesGatewayEnableRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    _validate_platform(platform)
    hermes = get_hermes_client()
    # ensure_user_workspace 才能寫 gateway.json(sidecar 端也驗 workspace 存在)
    try:
        ws = await ensure_user_workspace(user, db, hermes)
    except HermesError as e:
        if str(e) in ("no_token_configured", "token_missing_api_key"):
            raise HTTPException(
                400,
                detail={"error": "no_token_configured",
                        "message": "Gateway 需要先設定 AI Token(讓 daemon 內的 agent "
                                   "能呼叫 LLM)。請至設定 → AI Token 加入並設為預設"},
            )
        raise _hermes_error_to_http(e)

    # 取/建 cred — 若 payload.token 帶了,覆寫;沒帶就要既有 row 解出來
    cred = (await db.execute(
        select(HermesGatewayCredential).where(
            HermesGatewayCredential.owner == user.username,
            HermesGatewayCredential.platform == platform,
        )
    )).scalar_one_or_none()

    incoming_token = (payload.token or "").strip()
    if not cred and not incoming_token:
        raise HTTPException(
            400,
            detail={"error": "token_required",
                    "message": "首次 enable 必須提供 bot token"},
        )

    if cred:
        if incoming_token:
            cred.bot_token = incoming_token
        if payload.extra is not None:
            cred.extra_config = payload.extra
        cred.enabled = True
    else:
        cred = HermesGatewayCredential(
            owner=user.username,
            organization_id=user.organization_id,
            platform=platform,
            bot_token=incoming_token,
            extra_config=payload.extra or None,
            enabled=True,
        )
        db.add(cred)
    await db.flush()
    await db.refresh(cred)

    # cred.bot_token 是 EncryptedString descriptor — 自動解密
    plaintext_token = cred.bot_token or ""
    if not plaintext_token:
        raise HTTPException(500, detail={"error": "decrypt_failed"})

    try:
        result = await hermes.gateway_enable(
            ws, platform,
            token=plaintext_token,
            extra=cred.extra_config or {},
        )
    except HermesError as e:
        raise _hermes_error_to_http(e)
    return HermesGatewayEnableResponse(**result)


@router.post(
    "/hermes/gateway/{platform}/disable",
    status_code=204,
    tags=["V · AI"],
)
async def gateway_disable(
    platform: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    _validate_platform(platform)
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    try:
        await hermes.gateway_disable(ws, platform)
    except HermesError as e:
        raise _hermes_error_to_http(e)
    # 同步把 DB 內的 cred.enabled 設成 False(不刪 row,讓使用者保留 token 可再 enable)
    cred = (await db.execute(
        select(HermesGatewayCredential).where(
            HermesGatewayCredential.owner == user.username,
            HermesGatewayCredential.platform == platform,
        )
    )).scalar_one_or_none()
    if cred:
        cred.enabled = False
        await db.flush()


@router.delete(
    "/hermes/gateway/{platform}",
    status_code=204,
    tags=["V · AI"],
)
async def gateway_delete_credential(
    platform: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """完整刪除 cred(token + 設定)。相當於 disable + 忘記 token。"""
    _ensure_enabled()
    _validate_platform(platform)
    hermes = get_hermes_client()
    ws = workspace_id_for_user(user)
    # 先 disable sidecar(idempotent — 不在跑也回 204)
    try:
        await hermes.gateway_disable(ws, platform)
    except HermesError:
        # sidecar 連不上時還是要把 DB 清掉,避免使用者卡住
        LOG.warning("gateway disable failed during delete user=%s platform=%s "
                    "(continuing with DB cleanup)", user.username, platform)
    cred = (await db.execute(
        select(HermesGatewayCredential).where(
            HermesGatewayCredential.owner == user.username,
            HermesGatewayCredential.platform == platform,
        )
    )).scalar_one_or_none()
    if cred:
        await db.delete(cred)
        await db.flush()


@router.post(
    "/hermes/sessions/{sid}/messages",
    response_model=HermesSendMessageResponse,
    tags=["V · AI"],
)
@limiter.limit("60/hour")
async def send_message(
    request: Request,
    sid: str,
    payload: HermesMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    ref = await _check_session_or_404(db, sid, user)
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(400, detail={"error": "content_empty",
                                         "message": "content 不能為空"})

    # ── mem0 config(pre-hook + post-hook 共用,只 pick_token 一次)─────
    # 任一階段失敗都不應阻擋主對話。建一次 cfg + llm_cfg + embed_cfg,
    # pre-hook(PR6)用 search、post-hook(PR3)用 add。
    cfg = None
    llm_cfg = None
    embed_cfg = None
    if settings.MEM0_ENABLED:
        try:
            cfg = await pick_token_for_user(db, user)
            if cfg and cfg.api_key:
                llm_cfg = build_llm_config(cfg)
                embed_cfg = await resolve_embedder_config(db, user, cfg)
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 token resolve failed user=%s", user.username)

    # ── Pre-hook(PR6:search → inject prompt prefix)─────────────────
    # search_safe 失敗回 [];graceful degrade 不擋主對話。
    recalled: list[str] = []
    augmented_content = content
    if (settings.MEM0_ENABLED and settings.MEM0_PREHOOK_ENABLED
            and llm_cfg and embed_cfg
            and _is_query_searchable(content)):
        try:
            mem0 = get_mem0_client()
            search_results = await mem0.search_safe(
                user_id=mem0_user_id(user),
                query=content,
                llm_config=llm_cfg,
                embedder_config=embed_cfg,
                top_k=settings.MEM0_PREHOOK_TOP_K,
                threshold=settings.MEM0_PREHOOK_THRESHOLD,
            )
            recalled = [
                r["memory"] for r in (search_results or [])
                if isinstance(r, dict) and r.get("memory")
            ]
            if recalled:
                # 用 XML tag 標示 — 對 LLM 提示「這是過往記憶,僅供參考」。
                # User 看到的 user_message.content 仍是原 content,augmented 只給 sidecar。
                augmented_content = (
                    "<recalled_memory>\n"
                    + "\n".join(f"- {m}" for m in recalled)
                    + "\n</recalled_memory>\n\n"
                    + content
                )
                LOG.info(
                    "mem0 pre-hook injected %d memories user=%s session=%s",
                    len(recalled), user.username, sid,
                )
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 pre-hook failed user=%s", user.username)
            # 記憶失敗 = 沒記憶但對話正常 — 不影響主流程
            recalled = []
            augmented_content = content

    # ── Language directive(per-request,override session-level system_prompt)──
    # 前端會透過 fetch wrapper 把使用者選的 locale 帶進 Accept-Language;這裡讀出來
    # 在 prompt 最前面塞一段 <language_directive>,讓 LLM 當輪用對應語言回覆。
    # 這樣使用者改語言不必 reprovision Hermes,即時生效。
    locale = _resolve_locale(request)
    if locale:
        augmented_content = _language_directive(locale) + augmented_content

    hermes = get_hermes_client()
    # ── Phase 3.5: runtime fork on user.preferred_agent ──────────────
    # 預設(NULL / 'hermes')走原本路徑;'openclaw' 試 OpenClaw,失敗 graceful
    # fallback 回 Hermes 並在回應前 prepend 一則警告(讓使用者知道發生 fallback)。
    fallback_notice: Optional[str] = None
    runtime = (getattr(user, "preferred_agent", None) or "hermes").lower()
    if runtime == "openclaw":
        from app.services.openclaw_client import (
            OpenClawError,
            ensure_openclaw_provisioned,
            get_openclaw_client,
        )
        oc = get_openclaw_client()
        try:
            oc_ws, _key_preview = await ensure_openclaw_provisioned(user, db)
            oc_resp = await oc.chat(workspace_id=oc_ws, prompt=augmented_content)
            result = {"content": oc_resp.get("content") or "", "usage": {}}
        except OpenClawError as oe:
            LOG.warning("openclaw chat failed user=%s reason=%s — falling back to hermes",
                        user.username, str(oe))
            fallback_notice = (
                "⚠️ OpenClaw runtime 不可用("
                + str(oe).split(":", 1)[0].strip()
                + "),已自動 fallback 回 Hermes。請至「設定 → AI」確認 OpenClaw token / sidecar 狀態,或切回 Hermes 為預設。\n\n"
            )
            runtime = "hermes"

    if runtime == "hermes":
        try:
            # ensure 在這裡也跑一次 — 處理「create_session 後 cache TTL 過期」的情況
            ws = await ensure_user_workspace(user, db, hermes)
            result = await hermes.send_message(ws, sid, augmented_content)
        except HermesError as e:
            if str(e) in ("no_token_configured", "token_missing_api_key"):
                raise HTTPException(
                    400,
                    detail={"error": "no_token_configured",
                            "message": "尚未設定任何 AI provider;請至設定 → AI Token "
                                       "加入一個並設為預設"},
                )
            raise _hermes_error_to_http(e)
    if fallback_notice and isinstance(result, dict):
        result["content"] = fallback_notice + (result.get("content") or "")

    assistant_text = result.get("content") or ""
    usage = result.get("usage") or {}
    tokens = (usage.get("total_tokens")
              or (usage.get("input_tokens", 0) or 0)
                 + (usage.get("output_tokens", 0) or 0))

    # 第一次對話自動命名:取首則 user 訊息開頭
    if ref.title == "新對話":
        ref.title = content[:60]
    ref.last_message_preview = (assistant_text or content)[:200]
    await db.flush()

    # ── mem0 post-hook(PR3:fire-and-forget,絕不影響主對話)──────
    # cfg / llm_cfg / embed_cfg 已在前面 pick 過了 — 不重複 query DB。
    # 條件:有 token + 有 embedder + consent enabled + session 未暫停 + 有回覆。
    if settings.MEM0_ENABLED and assistant_text and llm_cfg and embed_cfg:
        try:
            consent = await _get_or_default_consent(db, user)
            if consent.extraction_enabled and not _session_paused(consent, sid):
                mem0 = get_mem0_client()
                task = asyncio.create_task(mem0.add_safe(
                    user_id=mem0_user_id(user),
                    messages=[
                        # 注意:存 raw content,不存 augmented(避免 recalled_memory
                        # block 也被學進去,造成記憶遞迴爆炸)
                        {"role": "user", "content": content},
                        {"role": "assistant", "content": assistant_text},
                    ],
                    llm_config=llm_cfg,
                    embedder_config=embed_cfg,
                    metadata={
                        "session_id": sid,
                        "ts": datetime.utcnow().isoformat(),
                    },
                ))
                # 持有 task ref 避免 GC 把 fire-and-forget 砍掉(plan §7 risk #5)
                bg = getattr(request.app.state, "background_tasks", None)
                if bg is not None:
                    bg.add(task)
                    task.add_done_callback(bg.discard)
        except Exception:  # noqa: BLE001
            # post-hook 任何 exception 都不能 cascade 到主流程 — 只 log
            LOG.exception("mem0 post-hook setup failed user=%s session=%s",
                          user.username, sid)

    now = datetime.utcnow()
    user_msg = HermesMessage(
        id=str(uuid.uuid4()),
        conversation_id=sid,
        role="user",
        content=content,
        created_at=now,
    )
    asst_msg = HermesMessage(
        id=str(uuid.uuid4()),
        conversation_id=sid,
        role="assistant",
        content=assistant_text,
        tokens_used=tokens or None,
        # provider/model 由 sidecar 控制,前端只當 metadata 顯示;
        # 之後 supervisor 回傳這兩欄再對接,目前 None
        provider=None,
        model=None,
        created_at=now,
    )
    return HermesSendMessageResponse(
        user_message=user_msg,
        assistant_message=asst_msg,
        recalled_memories=recalled,
    )
