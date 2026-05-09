"""Hermes per-user workspace provisioning。

PR3:把 AI Token(Fernet 加密在 ai_token_configs)解出來推給 sidecar 的
`/admin/users/<ws>/provision`,並做小量 in-process cache 避免每個 request 都打。

設計重點:
- workspace_id 用 sha256(username) 前 32 碼做 hash:
  * 這個 codebase 的 User 主鍵就是 username(沒有 surrogate UUID);
  * Hash 確保 path 安全(避免 username 帶 ./@ 等特殊字元打到 sidecar 的
    _validate_workspace_id);
  * 改 username 會 reset 對話 — v1 接受(改 username 是大事,且本來就會把
    擁有者欄位都改寫一輪)。
- API key 解密只在 backend 容器內;傳給 sidecar 的是 plaintext(走 internal docker
  network + X-Sidecar-Auth)。Volume hermes_data 限 root 讀。
- pick_token_for_user 沿用 ai_chat.py 早期的 `_resolve_provider` 邏輯
  (preferred_id → org default → any enabled)。
- 第一次 ensure_user_workspace 觸發 provision;5 min in-process cache 避免每 request 重推
  (token 換新時 router/settings.py 主動 invalidate)。
"""
import hashlib
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_token_config import AiTokenConfig
from app.models.user import User
from app.services.hermes_client import HermesClient, HermesError
from app.services.mem0_llm_config import build_embedder_config, build_llm_config

LOG = logging.getLogger(__name__)

# 從 ai_chat.py:40-44 搬來的預設 system prompt(原本是繁中、教學風格)。
# 寫入 <workspace>/config.yaml,Hermes 子進程啟動時讀。
_SYSTEM_PROMPT_DEFAULT = (
    "你是 RL 自動化測試平台的內建 AI 助理。請用繁體中文回答,"
    "回應要簡潔有用。當使用者問測試案例設計、Robot Framework 語法、"
    "BDD/AC 撰寫、缺陷分析、API/SQL 自動化時,請直接給可執行的範例。"
)

# Cache TTL 對齊 settings 變更時的 invalidate 視窗 — 5 min 之內 token 改了沒推
# 的話下個 request 觸發 ensure_user_workspace 自然會重建。
_PROVISION_CACHE_TTL_SEC = 300


def workspace_id_for_user(user: User) -> str:
    """User → Hermes workspace id。

    Hash username sha256 前 32 碼:這個 codebase 的 User PK 就是 username(沒
    surrogate id),Hash 既避免 username 帶特殊字元打到 sidecar
    _validate_workspace_id,也讓 username 改名 → workspace reset(可接受)。
    """
    h = hashlib.sha256(user.username.encode("utf-8")).hexdigest()[:32]
    return f"ws_{h}"


def mem0_user_id(user: User) -> str:
    """User → mem0 user_id(命名規則:`{org_id or 'default'}:{username}`)。

    與 workspace_id_for_user 不同 — workspace 是 hermes sidecar 的儲存隔離單位
    (sha256 hash);mem0 user_id 是 mem0 lib `Memory.search/add` 的 partition key,
    要對 hermes MCP 工具透明傳輸(會出現在 X-Mem0-User-Id header),所以保留
    可讀的 `org:username` 格式。
    """
    return f"{user.organization_id or 'default'}:{user.username}"


async def pick_token_for_user(
    db: AsyncSession,
    user: User,
    preferred_id: Optional[str] = None,
) -> Optional[AiTokenConfig]:
    """挑要餵給 Hermes 的 AI Token。

    優先順序對齊原 ai_chat.py:49-69 `_resolve_provider`:
      1) 指定的 token_id(若使用者有權限存取)
      2) 同 org 內 is_default=True 的設定(若多筆 — settings 是 per-provider
         唯一 default,不是全 org 唯一 — 取最近 updated_at 的)
      3) 同 org 內任何 enabled=True 的設定(以建立時間排序)
    """
    stmt = select(AiTokenConfig).where(AiTokenConfig.enabled.is_(True))
    if user.organization_id:
        stmt = stmt.where(AiTokenConfig.organization_id == user.organization_id)

    if preferred_id:
        cfg = (
            await db.execute(stmt.where(AiTokenConfig.id == preferred_id))
        ).scalar_one_or_none()
        if cfg:
            return cfg

    # NOTE: settings router 設 default 是 per (org, provider) 唯一 — 同 org 內
    # 跨 provider 可同時有多個 is_default=True(例:OpenAI default + Anthropic
    # default 並存)。.first() ordered by updated_at 取「最近被切成 default」那筆。
    cfg = (
        await db.execute(
            stmt.where(AiTokenConfig.is_default.is_(True))
                .order_by(AiTokenConfig.updated_at.desc())
        )
    ).scalars().first()
    if cfg:
        return cfg

    return (
        await db.execute(stmt.order_by(AiTokenConfig.created_at))
    ).scalars().first()


async def _pick_embedder_fallback_token(
    db: AsyncSession,
    user: User,
    *,
    exclude_id: str,
) -> Optional[AiTokenConfig]:
    """Primary token 沒 embedder 時(Anthropic 等)在同 org 內找替代。

    優先序:OpenAI(便宜)→ Gemini → 本地(Ollama/LMStudio)。同優先層內 default
    token 排前。回 None 表示 org 內找不到能做 embedding 的 token。
    """
    stmt = select(AiTokenConfig).where(
        AiTokenConfig.enabled.is_(True),
        AiTokenConfig.id != exclude_id,
    )
    if user.organization_id:
        stmt = stmt.where(AiTokenConfig.organization_id == user.organization_id)
    rows = (await db.execute(stmt)).scalars().all()

    def _priority(t: AiTokenConfig) -> int:
        p = (t.provider or "").lower()
        if "openai" in p:
            return 0
        if "gemini" in p or "google" in p:
            return 1
        if "ollama" in p or "lmstudio" in p:
            return 2
        return 9

    rows.sort(key=lambda t: (_priority(t), not t.is_default))
    for t in rows:
        if not t.api_key:
            continue
        if build_embedder_config(t) is not None:
            return t
    return None


async def resolve_embedder_config(
    db: AsyncSession,
    user: User,
    primary: AiTokenConfig,
) -> Optional[dict]:
    """挑 embedder config — primary 自己有就用,沒有就在 org 找 OpenAI/Gemini 替代。

    Anthropic / 純 LLM 沒 embedder API → 自動 fallback 到同 org 內任何能做
    embedding 的 token(_pick_embedder_fallback_token 排序:OpenAI > Gemini > 本地)。
    回 None 表示:org 內完全沒能做 embedding 的 token。
    """
    direct = build_embedder_config(primary)
    if direct is not None:
        return direct
    fallback = await _pick_embedder_fallback_token(db, user, exclude_id=primary.id)
    if fallback is None:
        return None
    return build_embedder_config(fallback)


async def resolve_mem0_configs(
    db: AsyncSession,
    user: User,
) -> Optional[tuple[dict, dict, str]]:
    """一次拿 (llm_config, embedder_config, primary_provider) — 高階 wrapper。

    LLM 用 user 的 default token(`pick_token_for_user`,自然 Anthropic / OpenAI /
    Gemini 任一)。Embedder 走 `resolve_embedder_config`(primary 不行則 org 找替代)。

    回 None 的條件:沒 enabled token / token 沒 api_key / 整 org 找不到能做 embedding
    的 token。

    給「不需要區分 degraded_reason」的呼叫端用(ensure/sync/build_mcp_servers)。
    routers/hermes.py 內既有 5 個 mem0 endpoint 直接用 `resolve_embedder_config`
    + `pick_token_for_user`,以保留 "no_token_configured" / "no_embedder" 的差異。
    """
    primary = await pick_token_for_user(db, user)
    if not primary or not primary.api_key:
        return None
    embedder_cfg = await resolve_embedder_config(db, user, primary)
    if embedder_cfg is None:
        return None
    return (build_llm_config(primary), embedder_cfg, str(primary.provider))


# In-process cache(per backend worker)— 不用 Redis 是為了避免引入新的 cache layer;
# 多 backend 副本各自 cache 沒問題,worst case 多打一次 provision RPC,sidecar 是 idempotent。
_provisioned_cache: dict[str, float] = {}


async def ensure_user_workspace(
    user: User,
    db: AsyncSession,
    hermes: HermesClient,
    *,
    force: bool = False,
) -> str:
    """確保 sidecar 有該使用者的 workspace。回 workspace_id。

    behavior:
    - 第一次 / force=True / cache miss → 解 token → POST sidecar /admin/.../provision
    - 沒 token 設定 → raise HermesError("no_token_configured")
    - sidecar 連不上 / 401 / 5xx → 上層 router 接 HermesUnavailable / HermesAuthFailed
      決定回 user 503 / 503

    Token 換新時 routers/settings.py 主動呼叫 invalidate_user_workspace,
    所以 cache 不會卡舊 key 太久。
    """
    import time

    ws = workspace_id_for_user(user)
    now = time.monotonic()
    cached_at = _provisioned_cache.get(user.username)
    if not force and cached_at and now - cached_at < _PROVISION_CACHE_TTL_SEC:
        return ws

    if not settings.HERMES_ENABLED:
        # Feature flag 關閉 → 別打 sidecar 也別 cache;router 會在更上層擋住
        raise HermesError("hermes_disabled")

    cfg = await pick_token_for_user(db, user)
    if not cfg:
        raise HermesError("no_token_configured")

    # EncryptedString descriptor 自動解密
    api_key_plain = cfg.api_key or ""
    if not api_key_plain:
        raise HermesError("token_missing_api_key")

    base_url = cfg.base_url
    # provider 名稱沿用 ai_token_configs.provider(自由字串,e.g. "OpenAI"/"Anthropic"
    # /"DeepSeek"/...)。Sidecar 的 provider_env_lines 對 anthropic / google 特判,其餘
    # 統一走 OPENAI_API_KEY + OPENAI_BASE_URL 路徑(對 OpenAI-compatible 一律 work)。
    await hermes.provision(
        workspace_id=ws,
        provider=str(cfg.provider),
        api_key=api_key_plain,
        base_url=base_url,
        system_prompt=_SYSTEM_PROMPT_DEFAULT,
    )
    _provisioned_cache[user.username] = now
    LOG.info("provisioned hermes workspace user=%s ws=%s provider=%s",
             user.username, ws, cfg.provider)

    # ── 同步推 llm_config 給 mem0 sidecar(PR3:Hermes ↔ mem0 MCP tool)──
    # mem0 sidecar 維護 5min TTL cache,讓 ACP 子進程的 LLM 透過 MCP tool
    # `search_memory` 進來時可以直接拿 user 的 llm_config 跑 mem0.search,
    # 不需 backend 把 LLM key 透過 MCP layer 傳出去。
    #
    # 失敗只 log warning(不擋 hermes provision 主流程):後續 LLM invoke
    # tool 時拿到 friendly "memory unavailable" error,自然繼續對話(plan §6
    # graceful degrade)。
    if settings.MEM0_ENABLED and settings.MEM0_HERMES_TOOL_ENABLED:
        configs = await resolve_mem0_configs(db, user)
        if configs is None:
            # primary 是 Anthropic 且 org 內沒 OpenAI/Gemini 替代 → 跳過 mem0
            LOG.info("mem0 push skipped user=%s — provider=%s 無可用 embedder "
                     "(同 org 也找不到 OpenAI/Gemini fallback)",
                     user.username, cfg.provider)
        else:
            llm_cfg, embedder_cfg, primary_provider = configs
            mem0_uid = mem0_user_id(user)
            # 用 lazy import 避免 hermes_provisioning ↔ mem0_client 形成
            # 模組級循環(mem0_client 不需要 provisioning,但提早 import 會在
            # backend lifespan 啟動順序上多綁一條依賴)
            from app.services.mem0_client import get_mem0_client
            mem0 = get_mem0_client()
            ok = await mem0.push_llm_config_safe(mem0_uid, llm_cfg, embedder_cfg)
            if ok:
                LOG.info("mem0 llm_config pushed user=%s mem0_uid=%s "
                         "llm=%s embedder=%s",
                         user.username, mem0_uid, primary_provider,
                         embedder_cfg.get("provider"))
            # ok=False 時 mem0_client 已 log warning,這裡不重複

    return ws


def invalidate_user_workspace(username: str) -> None:
    """Token 換新 / 設新 default 後呼叫。

    從 cache 抹掉,讓下次 ensure 時重 provision sidecar(寫入新 .env)。
    """
    _provisioned_cache.pop(username, None)


async def sync_mem0_llm_config(user: User, db: AsyncSession) -> None:
    """Token 變動後同步 mem0 sidecar 的 per-user llm_config cache。

    routers/settings.py:create/update/delete_ai_token 用 — 旁邊既有的
    `invalidate_user_workspace(...)` 對應 hermes sidecar 那條路徑;這個 helper
    對應 mem0 sidecar。

    決策:
    - 該 user 還有 enabled token + 有 embedder → push 最新 default token 的 config
    - 沒 token / token 沒 api_key / provider 沒 embedder → clear cache
    - feature flag 關 → 不動

    一律走 *_safe — 失敗只 log warning,不擋 settings update response(plan §6:
    push 失敗 → user 下次 invoke MCP tool 拿到 friendly error,自然繼續對話)。
    """
    if not (settings.MEM0_ENABLED and settings.MEM0_HERMES_TOOL_ENABLED):
        return
    # Lazy import 避開 module-level 循環(mem0_client → settings → ai_token_config →
    # ... 其實沒繞,但保持與 ensure_user_workspace 一致)
    from app.services.mem0_client import get_mem0_client
    mem0 = get_mem0_client()
    mem0_uid = mem0_user_id(user)

    configs = await resolve_mem0_configs(db, user)
    if configs is None:
        # 沒 token / 純 Anthropic 且找不到 fallback embedder → 清 cache 避免舊 config 殘留
        await mem0.clear_llm_config_safe(mem0_uid)
        return
    llm_cfg, embedder_cfg, _provider = configs
    await mem0.push_llm_config_safe(mem0_uid, llm_cfg, embedder_cfg)
