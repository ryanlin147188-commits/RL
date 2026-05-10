"""Hermes sidecar supervisor.

PR2: 接 ACP 子進程池 + provision/sessions/messages 路由。

架構
----
- 一個長駐 supervisor 進程跑 aiohttp 在 :7800(僅 docker network 內,不對 host 開 port)
- 每個 workspace_id 對應一個 ACP 子進程(`python -m acp_adapter.entry`),
  以 HERMES_HOME=/opt/data/<workspace_id>/ 隔離。子進程透過 stdin/stdout
  講 JSON-RPC(ACP 協定 v1)。
- LRU 在 HERMES_MAX_WORKERS 上限觸發 evict;HERMES_IDLE_TTL_SEC 沒活動就 GC。
- 除 /healthz 外的所有路由都驗 X-Sidecar-Auth header。

ACP 協定觀察(取自 acp_adapter/server.py、entry.py、session.py)
- entry 點:python -m acp_adapter.entry,純 stdio JSON-RPC;
  stderr 是 logging,stdout 必須 100% 是 JSON-RPC。
- 必經順序:initialize → authenticate(method_id) → new_session(cwd) → prompt
- prompt 呼叫期間,server 會送 session_update notification 帶 agent_message_chunk
  累積成最終文字;prompt 回傳 {stop_reason, usage}。
- request_permission notification:tool 呼叫前會問;PR2 階段 auto allow_once
  (PR4 把 permission UI 接到 backend 才換成 forward 給使用者)。

Auth 餵法
---------
ACP 子進程的 LLM 金鑰來自 HERMES_HOME/.env(load_hermes_dotenv 讀)。
provision 端點寫入 .env(0600),evict 既有子進程 → 下次呼叫時冷啟讀新環境。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

LOG = logging.getLogger("hermes.supervisor")

# ── Config ───────────────────────────────────────────────────────────
LISTEN_HOST = os.environ.get("SUPERVISOR_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("SUPERVISOR_PORT", "7800"))
HERMES_DATA_ROOT = Path(os.environ.get("HERMES_DATA_ROOT", "/opt/data"))
SIDECAR_AUTH_TOKEN = os.environ.get("SIDECAR_AUTH_TOKEN", "")
HERMES_MAX_WORKERS = int(os.environ.get("HERMES_MAX_WORKERS", "30"))
HERMES_IDLE_TTL_SEC = int(os.environ.get("HERMES_IDLE_TTL_SEC", "900"))
HERMES_RPC_TIMEOUT = int(os.environ.get("HERMES_RPC_TIMEOUT", "60"))
HERMES_GC_INTERVAL_SEC = int(os.environ.get("HERMES_GC_INTERVAL_SEC", "60"))
HERMES_AGENT_VERSION_TAG = "0.13.0"

if not SIDECAR_AUTH_TOKEN:
    print("[hermes] FATAL: SIDECAR_AUTH_TOKEN env not set", file=sys.stderr)
    sys.exit(1)


# ── Provider → .env mapping ──────────────────────────────────────────
def _normalize_provider(provider: str) -> str:
    """Map 我們的 provider 字串 → Hermes config 的 provider key。

    Hermes (NousResearch fork) 的 PROVIDER_REGISTRY 故意**沒有** "openai"
    entry — 他們假設一般 OpenAI 用戶都走 OpenRouter。所以我們對 OpenAI 必須
    報 "custom" + 顯式 base_url=https://api.openai.com/v1 才會打到真 OpenAI。

    其他第三方 OpenAI-compatible(deepseek/groq/lmstudio/...)為了不假設 user
    填的 base_url 是否與 Hermes registry 對得上,一律走 "custom" 路徑。
    "anthropic" / "gemini" 在 Hermes registry 有對應 entry,直接沿用。
    """
    p = (provider or "").lower().strip()
    if p == "anthropic":
        return "anthropic"
    if p in ("google", "gemini"):
        return "gemini"
    # openai / deepseek / groq / openrouter / lmstudio / ollama / azure-openai /
    # 其他自訂 OpenAI-compatible:全部走 custom,由 base_url 決定真正端點。
    return "custom"


def provider_env_lines(provider: str, api_key: str, base_url: Optional[str]) -> list[str]:
    """把 (provider, api_key, base_url) 攤平成 .env 行。

    Hermes detect_provider() 看 env vars 挑 provider;這裡只負責寫對 env var 名。
    第三方 OpenAI-compatible(deepseek/groq/openrouter/...)統一走 OPENAI_API_KEY +
    OPENAI_BASE_URL。
    """
    p = provider.lower().strip()
    lines: list[str] = []
    if p == "anthropic":
        lines.append(f"ANTHROPIC_API_KEY={api_key}")
    elif p in ("google", "gemini"):
        lines.append(f"GOOGLE_API_KEY={api_key}")
    else:
        lines.append(f"OPENAI_API_KEY={api_key}")
        # Hermes Agent 的 detect_provider() 看到「OPENAI_API_KEY 但沒 OPENAI_BASE_URL」
        # 會誤判成 openrouter(NousResearch 預設第三方代理),把 key 送到
        # https://openrouter.ai/api/v1 → 401 Missing Authentication header。
        # provider="openai" 沒給 base_url 時必須顯式落真正 OpenAI 端點;其他
        # OpenAI-compatible 第三方(deepseek/groq/openrouter/lmstudio/...)
        # 預期 user 自帶 base_url,不在這層假設。
        effective_base_url = base_url
        if not effective_base_url and p == "openai":
            effective_base_url = "https://api.openai.com/v1"
        if effective_base_url:
            lines.append(f"OPENAI_BASE_URL={effective_base_url}")
    return lines


_RL_PROVIDER_PREFIX = "rl-"

# 平台只開放這幾類 tool — 對齊 system_prompt 的「平台內運作」邊界。
# memory / todo / session_search / clarify / safe = 內部用、不會跳出平台。
# MCP server 的 mcp-* toolsets 由 _expand_acp_enabled_toolsets 動態加,不在這層擋。
# 其他全部黑名單(web/browser/terminal/file/code_execution/...等)— 這些 tool
# 即使被 LLM 呼叫,Hermes 也不會把它們的 schema 餵給 LLM,從根本不出現在
# tool_calls 候選裡。
_PLATFORM_ALLOWED_TOOLSETS = {
    "memory", "todo", "session_search", "clarify", "safe",
}
# 完整黑名單列在這 — 對應 toolsets.py 裡的 toolset name。新版 Hermes 加了 toolset
# 但沒加進這份清單時,fallback 是「除了 _PLATFORM_ALLOWED_TOOLSETS 之外全 disable」
# (見 _platform_disabled_toolsets)。
_PLATFORM_DISABLED_TOOLSETS = (
    "web", "search",
    "terminal", "process",
    "browser",
    "file",
    "code_execution", "delegation",
    "vision", "video", "image_gen", "tts",
    "moa", "skills",
    "messaging", "homeassistant", "kanban",
    "discord", "discord_admin",
    "yuanbao", "feishu_doc", "feishu_drive", "spotify",
    "debugging", "cronjob", "rl",
)


def _platform_disabled_toolsets() -> list[str]:
    """回傳要塞進 config.yaml 的 agent.disabled_toolsets。

    用「明確黑名單」而不是「allowlist 反推」,因為 toolsets.py 的清單會隨 Hermes
    版本擴張,反推可能誤殺新加的內部 tool;明確黑名單更可預測,新版要再評估。
    """
    return list(_PLATFORM_DISABLED_TOOLSETS)


def _build_config_yaml(provider: str, base_url: Optional[str], model: Optional[str],
                       system_prompt: Optional[str]) -> dict:
    """組整份 config.yaml(model + providers + system_prompt)。

    需要解決兩個 Hermes 預設行為:
    1) 沒明確 model.provider 時 fall back 到 openrouter(打到 openrouter.ai → 401)。
    2) base_url=api.openai.com 自動 detect 成 api_mode=codex_responses(GPT-5+ 才
       支援 Responses API,gpt-4 / gpt-4o 用 codex_responses 會 400 "Encrypted
       content is not supported with this model")。

    解法:
    - anthropic / gemini 是 Hermes registry 內建,直接用 model.provider=<name>。
    - 其他(openai 含直連 / deepseek / groq / openrouter / 自架 / ...)走 named
      custom provider:在 `providers:` 加一條 entry,顯式寫 base_url + key_env +
      api_mode=chat_completions(與所有 OpenAI-compatible 服務相容,規避 codex_responses
      陷阱),model.provider 指到該 entry name。
    """
    p_orig = (provider or "").lower().strip()
    out: dict = {}

    if p_orig == "anthropic":
        out["model"] = {"provider": "anthropic"}
        if model:
            out["model"]["default"] = model
    elif p_orig in ("google", "gemini"):
        out["model"] = {"provider": "gemini"}
        if model:
            out["model"]["default"] = model
    else:
        # OpenAI-compatible(含直連 OpenAI、DeepSeek、Groq、自架 vLLM/Ollama 等)
        # 統一走 named custom provider 路徑,避開 Hermes 的 codex_responses 自動偵測。
        custom_name = _RL_PROVIDER_PREFIX + (p_orig or "openai")
        eff_base_url = base_url
        if not eff_base_url and p_orig == "openai":
            eff_base_url = "https://api.openai.com/v1"
        provider_entry = {
            "key_env": "OPENAI_API_KEY",
            # api_mode: chat_completions — 對所有 OpenAI-compatible 服務相容,
            # 規避 base_url=api.openai.com 被自動 detect 成 codex_responses。
            "api_mode": "chat_completions",
        }
        if eff_base_url:
            provider_entry["api"] = eff_base_url
        if model:
            provider_entry["default_model"] = model
        out["providers"] = {custom_name: provider_entry}
        out["model"] = {"provider": custom_name}
        if model:
            out["model"]["default"] = model

    if system_prompt:
        out["system_prompt"] = system_prompt

    # 把「跳出平台」的 toolset 全部 disable;Hermes 啟動時 tools_config.py
    # 會從 enabled set 扣掉 disabled,這些 tool 的 schema 就不會餵給 LLM,
    # LLM 自然連 tool_call 都做不出來。第二道防線(第一道是 system_prompt)。
    out["agent"] = {"disabled_toolsets": _platform_disabled_toolsets()}
    return out


def _normalize_mcp_server(s: dict) -> dict:
    """Normalize a partial MCP-server dict to ACP wire format.

    ACP `mcpServers` 是 tagged union(`HttpMcpServer | SseMcpServer | McpServerStdio`),
    discriminator 是 `type` field(const: "http" / "sse"),stdio 沒有 type field 但有
    `command`。Backend 通常只送 `{name, url, headers}` — 在這裡補 `type: "http"`
    並把缺的 list 欄位補成 `[]`,降低 caller 的負擔。
    """
    if not isinstance(s, dict):
        raise ValueError(f"mcp_server entry must be dict, got {type(s).__name__}")
    out = dict(s)
    if "command" in out:
        # stdio:必須有 args / env(ACP required)— 缺的補 []
        out.setdefault("args", [])
        out.setdefault("env", [])
        return out
    if "url" not in out:
        raise ValueError(f"mcp_server '{out.get('name')}' needs 'url' or 'command'")
    # HTTP / SSE — caller 沒帶 type 就預設 http(streamable HTTP transport)
    out.setdefault("type", "http")
    out.setdefault("headers", [])
    return out


# ── ACP client (per-workspace subprocess wrapper) ────────────────────
class ACPError(Exception):
    """ACP JSON-RPC error response。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"ACP error {code}: {message}")


class ACPClient:
    """包裝一個 ACP 子進程,序列化 JSON-RPC 呼叫。

    每個 workspace_id 對應一個獨立子進程,HERMES_HOME=/opt/data/<ws>/。
    子進程啟動時讀 <HERMES_HOME>/.env 取得 LLM 金鑰 — 沒 provision 過就無法 auth。
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.home = HERMES_DATA_ROOT / workspace_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.initialized = False
        self.authenticated_method: Optional[str] = None
        self.last_used = time.monotonic()
        self._send_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notify_queues: dict[str, asyncio.Queue] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closing = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        """冷啟子進程並跑完 initialize + authenticate。Idempotent。"""
        async with self._start_lock:
            if self.running:
                return
            if not self.home.is_dir():
                raise ACPError(-32000, f"workspace_not_provisioned:{self.workspace_id}")
            env = os.environ.copy()
            env["HERMES_HOME"] = str(self.home)
            env_file = self.home / ".env"
            if env_file.is_file():
                # Hermes 自己會讀,但顯式指定避免 fallback 路徑差異
                env["HERMES_DOTENV"] = str(env_file)
            LOG.info("spawn ACP workspace=%s home=%s", self.workspace_id, self.home)
            # 走 acp_lockdown wrapper:在 acp_adapter.entry 跑之前 monkey-patch
            # 把 config.yaml 的 agent.disabled_toolsets 套到 LLM 看到的 tool list,
            # ACP path 預設不接這條邏輯(只 gateway/CLI 接),wrapper 補上去。
            # PYTHONPATH 加 /opt/hermes 讓 python 找得到 acp_lockdown 模組。
            env["PYTHONPATH"] = "/opt/hermes" + (
                ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "acp_lockdown",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.home),
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                init_resp = await self._call("initialize", {
                    # ACP wire protocol = camelCase(由 acp lib 的 to_camel_case +
                    # Pydantic alias 強制)。所有 params 與讀 result keys 都用 camelCase。
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {
                        "name": "autotest-hermes-supervisor",
                        "version": "0.1.0",
                    },
                })
                self.initialized = True
                auth_methods = (init_resp or {}).get("authMethods") or []
                if auth_methods:
                    method = auth_methods[0]
                    method_id = method.get("id")
                    if method_id:
                        await self._call("authenticate", {"methodId": method_id})
                        self.authenticated_method = method_id
                        LOG.info("authenticated workspace=%s via %s",
                                 self.workspace_id, method_id)
                    else:
                        LOG.warning("auth method without id: %r", method)
                else:
                    LOG.warning(
                        "workspace=%s: no authMethods detected (detect_provider() returned None — env may be unrecognised)",
                        self.workspace_id,
                    )
            except Exception:
                LOG.exception("ACP init/authenticate failed workspace=%s", self.workspace_id)
                # 無法 init 的子進程沒用,殺掉
                await self._stop_locked()
                raise

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("non-JSON stdout workspace=%s: %r",
                                self.workspace_id, line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("reader crash workspace=%s", self.workspace_id)
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ACPError(-32099, "subprocess_exited"))
            self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                LOG.info("[acp:%s:stderr] %s", self.workspace_id,
                         line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("stderr reader crash workspace=%s", self.workspace_id)

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if not fut or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(ACPError(
                    err.get("code", -32000),
                    err.get("message", "unknown"),
                    err.get("data"),
                ))
            else:
                fut.set_result(msg.get("result"))
        elif "method" in msg:
            await self._handle_notification(msg)

    async def _handle_notification(self, msg: dict) -> None:
        # ACP wire convention(從 acp.meta.AGENT_METHODS / CLIENT_METHODS 確認):
        # - Method 名:path-style snake_case(session/update、session/request_permission)
        # - Params keys:camelCase(sessionId、optionId 等;Pydantic alias 序列化)
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            sid = params.get("sessionId")
            if sid and sid in self._notify_queues:
                await self._notify_queues[sid].put(params.get("update") or {})
        elif method == "session/request_permission":
            # PR2:任何 tool 呼叫一律 allow_once。PR4 會把 permission 轉發給 backend
            # 顯示給使用者,變成同步等待 user 回應。
            req_id = msg.get("id")
            if req_id is not None:
                await self._send_raw({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"outcome": {"optionId": "allow_once"}},
                })
        else:
            LOG.debug("unhandled notification %s workspace=%s",
                      method, self.workspace_id)

    async def _send_raw(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        async with self._send_lock:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def _call(self, method: str, params: dict) -> Any:
        if not self.proc or self.proc.returncode is not None:
            raise ACPError(-32099, "subprocess_not_running")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        async with self._send_lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = fut
            self.last_used = time.monotonic()
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }, separators=(",", ":")) + "\n"
            try:
                self.proc.stdin.write(payload.encode())
                await self.proc.stdin.drain()
            except Exception:
                self._pending.pop(req_id, None)
                raise
        try:
            return await asyncio.wait_for(fut, timeout=HERMES_RPC_TIMEOUT)
        finally:
            self._pending.pop(req_id, None)

    # ── high-level API ──
    # ACP wire convention(from acp.meta.AGENT_METHODS):
    # - Method 名:path-style(session/new、session/list、session/prompt;
    #   initialize / authenticate 沒 prefix)
    # - Params keys:camelCase(by_alias=True Pydantic 序列化)
    async def new_session(self, mcp_servers: Optional[list[dict]] = None) -> dict:
        """Create new ACP session, optionally with HTTP/SSE/stdio MCP servers.

        mcp_servers schema(對齊 ACP HttpMcpServer / SseMcpServer / McpServerStdio):
        - HTTP: {"name": str, "url": str, "headers": [{"name": str, "value": str}, ...], "type": "http"}
        - SSE:  {"name": str, "url": str, "headers": [...], "type": "sse"}
        - stdio: {"name": str, "command": str, "args": list[str], "env": [...]}

        Caller 可以省略 `type` — 我們用 url/command 自動 infer 並標記為 "http"
        (預設,因 stdio MCP 不適合在 hermes container 內 spawn 子進程)。

        Hermes 內部 _register_session_mcp_servers 會把這些註冊成 LLM 可見 tools
        (`tools.mcp_tool.register_mcp_servers`)。session-level config,既有 session
        不會自動更新 — 換 token 後要建新 session 才看得到 tool。
        """
        if not self.running:
            await self.start()
        normalized = [_normalize_mcp_server(s) for s in (mcp_servers or [])]
        return await self._call("session/new", {
            "cwd": str(self.home),
            "mcpServers": normalized,
        }) or {}

    async def list_sessions(self) -> dict:
        if not self.running:
            await self.start()
        return await self._call("session/list", {"cwd": str(self.home)}) or {}

    async def prompt(self, session_id: str, text: str) -> dict:
        """送 prompt → 累積 agent_message_chunk → 回傳 {content, stopReason, usage}。"""
        if not self.running:
            await self.start()
        queue: asyncio.Queue = asyncio.Queue()
        self._notify_queues[session_id] = queue
        chunks: list[str] = []
        try:
            prompt_task = asyncio.create_task(self._call("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }))

            async def collect():
                while True:
                    update = await queue.get()
                    # session_update notification 內 update field 也是 camelCase
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        c = update.get("content") or {}
                        if c.get("type") == "text":
                            chunks.append(c.get("text") or "")

            collect_task = asyncio.create_task(collect())
            try:
                result = await prompt_task
            finally:
                collect_task.cancel()
                try:
                    await collect_task
                except asyncio.CancelledError:
                    pass
            # Drain tail chunks
            while not queue.empty():
                update = queue.get_nowait()
                if update.get("sessionUpdate") == "agent_message_chunk":
                    c = update.get("content") or {}
                    if c.get("type") == "text":
                        chunks.append(c.get("text") or "")
            return {
                "content": "".join(chunks),
                "stop_reason": (result or {}).get("stopReason"),
                "usage": (result or {}).get("usage"),
            }
        finally:
            self._notify_queues.pop(session_id, None)

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._closing:
            return
        self._closing = True
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        for t in (self._reader_task, self._stderr_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ACPError(-32099, "subprocess_stopped"))
        self._pending.clear()


# ── Process pool ─────────────────────────────────────────────────────
class ProcessPool:
    """Per-workspace ACPClient 池。LRU + idle GC + 上限保護。"""

    def __init__(self):
        self._clients: dict[str, ACPClient] = {}
        self._lock = asyncio.Lock()
        self._gc_task: Optional[asyncio.Task] = None

    async def start_gc(self) -> None:
        self._gc_task = asyncio.create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HERMES_GC_INTERVAL_SEC)
                try:
                    await self._gc_once()
                except Exception:
                    LOG.exception("gc once error")
        except asyncio.CancelledError:
            raise

    async def _gc_once(self) -> None:
        now = time.monotonic()
        async with self._lock:
            stale = [
                ws for ws, c in self._clients.items()
                if now - c.last_used > HERMES_IDLE_TTL_SEC
            ]
        for ws in stale:
            LOG.info("idle GC workspace=%s", ws)
            await self.evict(ws)

    async def get(self, workspace_id: str) -> ACPClient:
        async with self._lock:
            client = self._clients.get(workspace_id)
            if client is None:
                if len(self._clients) >= HERMES_MAX_WORKERS:
                    lru_ws = min(
                        self._clients,
                        key=lambda w: self._clients[w].last_used,
                    )
                    if lru_ws != workspace_id:
                        LOG.info("LRU evict workspace=%s", lru_ws)
                        await self._evict_locked(lru_ws)
                client = ACPClient(workspace_id)
                self._clients[workspace_id] = client
        # start() 是 idempotent;放在鎖外避免 cold start 拖住整個 pool
        try:
            await client.start()
        except Exception:
            async with self._lock:
                if self._clients.get(workspace_id) is client:
                    self._clients.pop(workspace_id, None)
            raise
        return client

    async def evict(self, workspace_id: str) -> None:
        async with self._lock:
            await self._evict_locked(workspace_id)

    async def _evict_locked(self, workspace_id: str) -> None:
        client = self._clients.pop(workspace_id, None)
        if client:
            await client.stop()

    async def shutdown(self) -> None:
        if self._gc_task:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for ws in list(self._clients.keys()):
                await self._evict_locked(ws)

    def stats(self) -> dict:
        return {
            "active_workers": len(self._clients),
            "max_workers": HERMES_MAX_WORKERS,
        }


# ── Auth middleware ──────────────────────────────────────────────────
@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/healthz":
        return await handler(request)
    token = request.headers.get("X-Sidecar-Auth", "")
    if not token or token != SIDECAR_AUTH_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# ── Validation ───────────────────────────────────────────────────────
_INVALID_WS_CHARS = ("/", "\\", "..", "\x00", " ", "\t", "\n", ":")


def _validate_workspace_id(ws: str) -> None:
    """Defense-in-depth:supervisor 用 ws 拼路徑,要避免 path traversal。

    Backend 用 ws_<uuid> 命名(UUID hex 不會撞到任何特殊字元),這裡只是雙重保險。
    """
    if not ws or len(ws) > 128 or any(c in ws for c in _INVALID_WS_CHARS):
        raise web.HTTPBadRequest(reason="invalid_workspace_id")


def _validate_session_id(sid: str) -> None:
    if not sid or len(sid) > 256 or any(c in sid for c in ("\x00", "\n", " ")):
        raise web.HTTPBadRequest(reason="invalid_session_id")


# ── HTTP handlers ────────────────────────────────────────────────────
async def handle_healthz(request: web.Request) -> web.Response:
    if not HERMES_DATA_ROOT.is_dir() or not os.access(HERMES_DATA_ROOT, os.W_OK):
        return web.json_response(
            {"status": "unhealthy", "reason": "data_root_unavailable"},
            status=503,
        )
    pool: ProcessPool = request.app["pool"]
    return web.json_response({
        "status": "ok",
        "hermes_agent_version": HERMES_AGENT_VERSION_TAG,
        "data_root": str(HERMES_DATA_ROOT),
        "phase": "PR2-acp-pool",
        **pool.stats(),
    })


async def handle_provision(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(reason="invalid_json")
    provider = (body.get("provider") or "").strip()
    api_key = body.get("api_key") or ""
    base_url = body.get("base_url") or None
    system_prompt = body.get("system_prompt") or ""
    model = (body.get("model") or "").strip() or None
    if not provider or not api_key:
        raise web.HTTPBadRequest(reason="provider_and_api_key_required")
    home = HERMES_DATA_ROOT / ws
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    env_lines = provider_env_lines(provider, api_key, base_url)
    env_lines.append(f"HERMES_HOME={home}")
    env_path = home / ".env"
    env_path.write_text("\n".join(env_lines) + "\n")
    env_path.chmod(0o600)
    # config.yaml:寫 model + providers + system_prompt。
    # 用 yaml.safe_dump 確保所有 section(含巢狀 providers)正確 quote。
    config_path = home / "config.yaml"
    config_dict = _build_config_yaml(provider, base_url, model, system_prompt)
    if not config_dict:
        config_path.write_text("# Hermes per-workspace config\n")
    else:
        try:
            import yaml as _yaml  # 延遲 import:避免 yaml 缺失時 import 期就掛掉
            config_path.write_text(_yaml.safe_dump(config_dict, sort_keys=False, allow_unicode=True))
        except ImportError:
            # fallback:不安裝 PyYAML 時用手刻 JSON-as-YAML(扁平結構)
            lines: list[str] = []
            for top_k, top_v in config_dict.items():
                if isinstance(top_v, dict):
                    lines.append(f"{top_k}:")
                    for k, v in top_v.items():
                        if isinstance(v, dict):
                            lines.append(f"  {k}:")
                            for kk, vv in v.items():
                                lines.append(f"    {kk}: {json.dumps(vv)}")
                        else:
                            lines.append(f"  {k}: {json.dumps(v)}")
                else:
                    lines.append(f"{top_k}: {json.dumps(top_v)}")
            config_path.write_text("\n".join(lines) + "\n")
    config_path.chmod(0o600)
    pool: ProcessPool = request.app["pool"]
    await pool.evict(ws)  # 強制下次冷啟讀新 env
    LOG.info("provisioned workspace=%s provider=%s base_url=%s",
             ws, provider, base_url or "default")
    return web.json_response({"workspace_id": ws, "status": "provisioned"})


async def handle_create_session(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)

    # PR2:從 body 讀 mcp_servers(optional);backend 推 mem0 config 給 hermes
    # 子進程做 LLM tool registration。Body 為空時走原行為(無 MCP tools)。
    mcp_servers: Optional[list[dict]] = None
    if request.can_read_body:
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        if isinstance(body, dict):
            raw = body.get("mcp_servers") or body.get("mcpServers")
            if raw is not None:
                if not isinstance(raw, list):
                    raise web.HTTPBadRequest(reason="mcp_servers must be a list")
                # 每筆必須含 name + (url 或 command)— ACP schema 對齊;有 headers 必須是 list
                for entry in raw:
                    if not isinstance(entry, dict) or "name" not in entry:
                        raise web.HTTPBadRequest(
                            reason="mcp_servers entries need 'name' field",
                        )
                    has_url = "url" in entry
                    has_cmd = "command" in entry
                    if not (has_url or has_cmd):
                        raise web.HTTPBadRequest(
                            reason=f"mcp_server '{entry.get('name')}' needs 'url' or 'command'",
                        )
                mcp_servers = raw

    pool: ProcessPool = request.app["pool"]
    try:
        client = await pool.get(ws)
        result = await client.new_session(mcp_servers=mcp_servers)
    except ACPError as e:
        return web.json_response(
            {"error": "acp_error", "code": e.code, "detail": e.message},
            status=502,
        )
    # ACP 回 camelCase;對外 HTTP API 改回 snake_case 維持 Python 慣例。
    return web.json_response({
        "session_id": result.get("sessionId"),
        "models": result.get("models"),
    })


async def handle_list_sessions(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    pool: ProcessPool = request.app["pool"]
    try:
        client = await pool.get(ws)
        result = await client.list_sessions()
    except ACPError as e:
        return web.json_response(
            {"error": "acp_error", "code": e.code, "detail": e.message},
            status=502,
        )
    return web.json_response({
        "sessions": result.get("sessions") or [],
        "next_cursor": result.get("nextCursor"),
    })


# ── Skills(PR4)─────────────────────────────────────────────────────
# 走檔案系統,不開 ACP 子進程 — skills 是純檔不需 agent runtime。
# Hermes skill 規範:`<HERMES_HOME>/skills/<namespace>/<name>/SKILL.md`,frontmatter
# 是 YAML(`name`/`description`/`platforms`/`namespace` 等)+ markdown body。

# agent.skill_utils.parse_frontmatter 已在 hermes-agent[acp] extras 內,但 import
# 在 supervisor module-level 會 leak HERMES_HOME 環境到全 process(它 cache 了
# get_skills_dir);改成 lazy import + 自己用 PyYAML 解 frontmatter,風險最小。
_SKILL_INDEX_FILES = ("SKILL.md", "skill.md", "skill.yaml", "skill.yml")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _read_skill_metadata(index_path: Path, home: Path) -> Optional[dict]:
    try:
        text = index_path.read_text(encoding="utf-8")
    except OSError as e:
        LOG.warning("skill read failed at %s: %s", index_path, e)
        return None
    fm: dict = {}
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            import yaml  # PyYAML 是 hermes-agent 的 transitive dep,容器內有
            parsed = yaml.safe_load(m.group(1)) or {}
            if isinstance(parsed, dict):
                fm = parsed
        except Exception as e:
            LOG.warning("skill frontmatter parse failed at %s: %s", index_path, e)
    skill_dir = index_path.parent
    return {
        "name": str(fm.get("name") or skill_dir.name),
        "namespace": str(fm.get("namespace") or ""),
        "description": str(fm.get("description") or ""),
        "platforms": list(fm.get("platforms") or []),
        "path": str(skill_dir.relative_to(home)),
    }


async def handle_list_skills(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    home = HERMES_DATA_ROOT / ws
    skills_dir = home / "skills"
    if not home.is_dir() or not skills_dir.is_dir():
        # 還沒 provision 或還沒裝任何 skill — 回空陣列(前端 UI 才能正常 render)
        return web.json_response({"skills": []})

    found: list[dict] = []
    seen: set[Path] = set()
    for filename in _SKILL_INDEX_FILES:
        for index_path in skills_dir.rglob(filename):
            # 同 dir 多種副檔名只取第一個(SKILL.md > skill.md > skill.yaml ...)
            if index_path.parent in seen:
                continue
            seen.add(index_path.parent)
            meta = _read_skill_metadata(index_path, home)
            if meta:
                found.append(meta)
    found.sort(key=lambda s: (s["namespace"], s["name"]))
    return web.json_response({"skills": found})


# ── Memory search(PR4)──────────────────────────────────────────────
# 直接打開 state.db read-only(URI mode),不影響 ACP 子進程併發寫入。
# Schema 來自 hermes_state.SCHEMA_SQL — sessions / messages / messages_fts(FTS5)。

def _sanitize_fts_query(q: str) -> str:
    """把任意 user 輸入轉成安全的 FTS5 MATCH 表達式。

    丟掉 FTS5 特殊運算子,只留下 word tokens(\\w+ 含 Unicode word chars 對中文 OK)。
    Tokens 用空白接成 default-AND 查詢。
    """
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(tokens)


async def handle_memory_search(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    raw_q = request.query.get("q", "")
    try:
        limit = max(1, min(100, int(request.query.get("limit", "20"))))
    except ValueError:
        raise web.HTTPBadRequest(reason="limit_invalid")
    safe_q = _sanitize_fts_query(raw_q)
    if not safe_q:
        # 空 query 回空結果,不 raise — 讓前端能用「empty 結果頁」UI 而不 toast 錯誤
        return web.json_response({"results": [], "query": raw_q, "limit": limit})

    home = HERMES_DATA_ROOT / ws
    state_db = home / "state.db"
    if not state_db.is_file():
        return web.json_response({"results": [], "query": raw_q, "limit": limit})

    # Read-only URI 確保不會 lock Hermes 子進程的寫入
    uri = f"file:{state_db}?mode=ro"
    try:
        # 用 thread executor 跑 sqlite,避免 block aiohttp event loop
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None, _do_fts_search, uri, safe_q, limit,
        )
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "no such table" in msg:
            # state.db schema 還沒 init(沒跑過任何訊息)— 回空,別擋
            return web.json_response({"results": [], "query": raw_q, "limit": limit})
        raise web.HTTPBadRequest(reason=f"sqlite_error:{type(e).__name__}")
    return web.json_response({
        "results": rows,
        "query": raw_q,
        "sanitized_query": safe_q,
        "limit": limit,
    })


def _do_fts_search(uri: str, match_expr: str, limit: int) -> list[dict]:
    """阻塞式 sqlite query — 由 supervisor 用 executor 跑。"""
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT
                m.session_id,
                m.role,
                m.content,
                m.timestamp,
                COALESCE(s.title, s.id) AS session_title,
                bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.id
            LEFT JOIN sessions s ON m.session_id = s.id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (match_expr, limit))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Cron(PR5)──────────────────────────────────────────────────────
# Hermes cron 規範:`<HERMES_HOME>/cron/jobs.json` 是 JSON list,每個 entry 由
# cron.jobs.create_job() 產;欄位 schedule/prompt/name/repeat/skills/enabled/...
# Hermes ACP 子進程啟動後會自己讀取 + 派工(我們不需要另外啟 daemon)。
#
# Supervisor 直接讀寫 jobs.json,只 import create_job() 用來 normalize 新 entry
# (它純 dict factory,不讀 HERMES_HOME);讀寫由 supervisor 自己處理避免 env 競爭。

_JOBS_RELPATH = "cron/jobs.json"


def _jobs_path(ws: str) -> Path:
    return HERMES_DATA_ROOT / ws / _JOBS_RELPATH


def _load_jobs(ws: str) -> list[dict]:
    """讀 jobs.json,回 list of job dict。

    Hermes 寫的格式是 `{"jobs": [...], "updated_at": "..."}`(見
    cron.jobs.save_jobs),也接 raw list `[...]` 當 backward-compat fallback。
    """
    p = _jobs_path(ws)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("jobs.json read failed ws=%s: %s", ws, e)
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
    elif isinstance(data, list):
        jobs = data
    else:
        return []
    return [j for j in jobs if isinstance(j, dict)]


def _save_jobs(ws: str, jobs: list[dict]) -> None:
    """寫 jobs.json,格式對齊 Hermes cron.jobs.save_jobs 才能讓 ACP 子進程讀懂。"""
    from datetime import datetime, timezone

    p = _jobs_path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "jobs": jobs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _public_job(job: dict) -> dict:
    """把內部 dict 攤平成前端友善欄位(節錄,避免 leak Hermes internals)。"""
    schedule = job.get("schedule") or {}
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "prompt": job.get("prompt"),
        "schedule": (schedule.get("display") or schedule.get("expr")
                     if isinstance(schedule, dict) else str(schedule)),
        "schedule_kind": schedule.get("kind") if isinstance(schedule, dict) else None,
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "created_at": job.get("created_at"),
    }


async def handle_list_cron(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    jobs = _load_jobs(ws)
    return web.json_response({
        "jobs": [_public_job(j) for j in jobs],
    })


async def handle_add_cron(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(reason="invalid_json")
    schedule = (body.get("schedule") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    name = (body.get("name") or "").strip() or None
    if not schedule or not prompt:
        raise web.HTTPBadRequest(reason="schedule_and_prompt_required")

    # 借用 Hermes create_job 做 normalize(它驗 schedule 合法、產 id、算 next_run)
    try:
        from cron.jobs import create_job, parse_schedule
        # parse_schedule 會 raise 當 schedule 不合法 — 提早攔到讓使用者改
        parse_schedule(schedule)
        new_job = create_job(prompt=prompt, schedule=schedule, name=name)
    except Exception as e:
        # ValueError 的 message 多行(列各種 schedule 格式)— reason 只能單行,
        # 把詳細訊息塞 body 讓前端能完整顯示
        first_line = str(e).split("\n", 1)[0][:200]
        return web.json_response(
            {"error": "invalid_schedule",
             "type": type(e).__name__,
             "detail": str(e),
             "summary": first_line},
            status=400,
        )

    jobs = _load_jobs(ws)
    jobs.append(new_job)
    _save_jobs(ws, jobs)
    LOG.info("cron job added ws=%s id=%s schedule=%s",
             ws, new_job.get("id"), schedule)
    return web.json_response(_public_job(new_job), status=201)


async def handle_delete_cron(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    job_id = request.match_info["job_id"]
    _validate_workspace_id(ws)
    if not job_id or len(job_id) > 64 or "/" in job_id or ".." in job_id:
        raise web.HTTPBadRequest(reason="invalid_job_id")
    jobs = _load_jobs(ws)
    before = len(jobs)
    jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) == before:
        return web.Response(status=404, reason="job_not_found")
    _save_jobs(ws, jobs)
    LOG.info("cron job deleted ws=%s id=%s", ws, job_id)
    return web.Response(status=204)


# ── Gateway daemon(messaging integration)──────────────────────────
# 每個 workspace 一個 `python -m gateway.run` 子進程。Hermes gateway 進程內同時跑
# 所有啟用的 platform(telegram + discord + slack 等共用一個 daemon),所以
# 一個 workspace 一隻就夠;啟用第 N 個 platform 時只是改 gateway.json 然後 restart。
#
# Gateway daemon 與 ACPClient 的差別:
# - ACP 子進程是 RPC 模式(stdio JSON-RPC,supervisor 主動推 prompt)
# - Gateway daemon 是 long-poll / webhook 模式(自己對外連 Telegram API),
#   supervisor 不跟它互動,只 spawn/kill,讀 stderr 出 log。
# - Lifecycle:enable 時 spawn,disable 時 kill;不像 ACP 有 idle GC。

class GatewayDaemon:
    """Wraps one gateway.run subprocess for a workspace."""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.home = HERMES_DATA_ROOT / workspace_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.last_started_at: Optional[float] = None
        self.last_exit_code: Optional[int] = None
        self.recent_stderr: list[str] = []  # 最後 N 行(讓 status 可顯示啟動失敗原因)
        # platform daemons 在錯誤訊息 / log 內可能把 token 印出來(例:Telegram
        # InvalidToken 錯誤)。在 _read_stderr 寫入時對 sensitive tokens 做 redaction
        # 避免 status endpoint 把 plaintext 傳給前端。
        self._sensitive_tokens: set[str] = set()
        self._stderr_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def add_sensitive_token(self, token: str) -> None:
        """在 enable 寫入 gateway.json 時呼叫,讓後續 stderr 自動 redact。"""
        if token and len(token) >= 8:
            # 太短的 token(<8)redact 全文太誤判,跳過
            self._sensitive_tokens.add(token)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        """Spawn `python -m gateway.run`;若已在跑則 noop。"""
        async with self._lock:
            if self.running:
                return
            if not self.home.is_dir():
                raise web.HTTPBadRequest(reason="workspace_not_provisioned")
            env = os.environ.copy()
            env["HERMES_HOME"] = str(self.home)
            env_file = self.home / ".env"
            if env_file.is_file():
                env["HERMES_DOTENV"] = str(env_file)
            LOG.info("spawn gateway daemon workspace=%s", self.workspace_id)
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "gateway.run",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.home),
            )
            self.last_started_at = time.monotonic()
            self.last_exit_code = None
            self.recent_stderr = []
            self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        """讀 stderr 串流到 supervisor log,並保留最後 50 行給 /status 用。

        在儲存 / log 前對所有 sensitive_tokens 做 string replace,避免 platform 的
        錯誤訊息(例:Telegram `InvalidToken: token 'XYZ' was rejected`)把 plaintext
        token 透過 status endpoint 回給前端。
        """
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip()
                # 先 redact 才 log / 存
                for tok in self._sensitive_tokens:
                    if tok in decoded:
                        decoded = decoded.replace(tok, "<redacted>")
                LOG.info("[gateway:%s] %s", self.workspace_id, decoded)
                self.recent_stderr.append(decoded)
                if len(self.recent_stderr) > 50:
                    self.recent_stderr.pop(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("gateway stderr reader crash workspace=%s", self.workspace_id)
        finally:
            if self.proc:
                self.last_exit_code = self.proc.returncode

    async def stop(self) -> None:
        async with self._lock:
            proc = self.proc
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=8)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                except ProcessLookupError:
                    pass
            if self._stderr_task and not self._stderr_task.done():
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
            self.last_exit_code = proc.returncode if proc else None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def status(self) -> dict:
        # 計算 uptime 與最近的 stderr 摘要
        uptime = None
        if self.last_started_at and self.running:
            uptime = round(time.monotonic() - self.last_started_at, 1)
        return {
            "running": self.running,
            "uptime_sec": uptime,
            "last_exit_code": self.last_exit_code,
            "recent_stderr": list(self.recent_stderr[-20:]),
        }


# Process pool for gateway daemons(per workspace 一個 daemon)
_gateway_daemons: dict[str, GatewayDaemon] = {}


async def _get_or_create_daemon(ws: str) -> GatewayDaemon:
    daemon = _gateway_daemons.get(ws)
    if daemon is None:
        daemon = GatewayDaemon(ws)
        _gateway_daemons[ws] = daemon
    return daemon


# ── Gateway config(gateway.json)讀寫 ───────────────────────────────
# Hermes gateway.config.load_gateway_config 會優先讀 config.yaml,fallback 到
# gateway.json。我們選 gateway.json — 因為它純機器寫,不會踩到使用者寫的
# config.yaml(provision 也寫 config.yaml,但只放 system_prompt)。

def _gateway_json_path(ws: str) -> Path:
    return HERMES_DATA_ROOT / ws / "gateway.json"


def _load_gateway_json(ws: str) -> dict:
    p = _gateway_json_path(ws)
    if not p.is_file():
        return {"platforms": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"platforms": {}}
        data.setdefault("platforms", {})
        return data
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("gateway.json read failed ws=%s: %s", ws, e)
        return {"platforms": {}}


def _save_gateway_json(ws: str, data: dict) -> None:
    p = _gateway_json_path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    # 0600 — 含 plaintext bot token
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


_KNOWN_PLATFORMS = {"telegram", "discord", "slack", "matrix", "signal", "whatsapp"}


def _redact_platforms(platforms: dict) -> dict:
    """token 永遠不直接回給 client — 只回 has_token 旗標。"""
    redacted = {}
    for plat, cfg in platforms.items():
        if not isinstance(cfg, dict):
            continue
        cfg_out = {k: v for k, v in cfg.items() if k != "token" and k != "api_key"}
        cfg_out["has_token"] = bool(cfg.get("token") or cfg.get("api_key"))
        redacted[plat] = cfg_out
    return redacted


# ── Gateway HTTP handlers ───────────────────────────────────────────
async def handle_gateway_status(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    _validate_workspace_id(ws)
    daemon = _gateway_daemons.get(ws)
    cfg = _load_gateway_json(ws)
    return web.json_response({
        "platforms": _redact_platforms(cfg.get("platforms") or {}),
        "daemon": daemon.status() if daemon else {
            "running": False, "uptime_sec": None,
            "last_exit_code": None, "recent_stderr": [],
        },
    })


async def handle_gateway_enable(request: web.Request) -> web.Response:
    """Enable a platform(把 token 寫入 gateway.json + 起/重啟 daemon)。"""
    ws = request.match_info["ws"]
    platform = request.match_info["platform"]
    _validate_workspace_id(ws)
    if platform not in _KNOWN_PLATFORMS:
        raise web.HTTPBadRequest(reason=f"unknown_platform:{platform}")
    home = HERMES_DATA_ROOT / ws
    if not home.is_dir():
        raise web.HTTPBadRequest(reason="workspace_not_provisioned")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(reason="invalid_json")
    token = body.get("token") or ""
    extra = body.get("extra") or {}
    if not token:
        raise web.HTTPBadRequest(reason="token_required")
    if not isinstance(extra, dict):
        raise web.HTTPBadRequest(reason="extra_must_be_object")

    cfg = _load_gateway_json(ws)
    platforms = cfg.setdefault("platforms", {})
    platforms[platform] = {
        "enabled": True,
        "token": token,
        "extra": extra,
    }
    _save_gateway_json(ws, cfg)
    LOG.info("gateway enable workspace=%s platform=%s", ws, platform)

    daemon = await _get_or_create_daemon(ws)
    # 把目前 gateway.json 內所有 platform 的 token 都餵給 daemon 做 stderr redaction —
    # 任一 platform 啟動失敗(例:Telegram InvalidToken)都可能把 token 印出來。
    for plat_cfg in platforms.values():
        if isinstance(plat_cfg, dict):
            tk = plat_cfg.get("token") or plat_cfg.get("api_key") or ""
            if tk:
                daemon.add_sensitive_token(tk)
    try:
        await daemon.restart()
    except Exception as e:
        LOG.exception("daemon start failed workspace=%s: %s", ws, e)
        return web.json_response(
            {"error": "daemon_start_failed", "detail": str(e)},
            status=502,
        )
    return web.json_response({
        "platform": platform,
        "enabled": True,
        "daemon": daemon.status(),
    })


async def handle_gateway_disable(request: web.Request) -> web.Response:
    """Disable a platform(移除 gateway.json 內該 platform;若全部關掉就停 daemon)。"""
    ws = request.match_info["ws"]
    platform = request.match_info["platform"]
    _validate_workspace_id(ws)
    if platform not in _KNOWN_PLATFORMS:
        raise web.HTTPBadRequest(reason=f"unknown_platform:{platform}")

    cfg = _load_gateway_json(ws)
    platforms = cfg.get("platforms") or {}
    if platform not in platforms:
        return web.Response(status=204)  # idempotent — 沒設過就直接 OK
    platforms.pop(platform)
    cfg["platforms"] = platforms
    _save_gateway_json(ws, cfg)
    LOG.info("gateway disable workspace=%s platform=%s", ws, platform)

    daemon = _gateway_daemons.get(ws)
    if daemon and daemon.running:
        # 若還有其他 platform 啟用中 → restart 讓它讀新 config;
        # 全空了就 stop
        any_enabled = any(
            isinstance(c, dict) and c.get("enabled")
            for c in platforms.values()
        )
        if any_enabled:
            await daemon.restart()
        else:
            await daemon.stop()
    return web.Response(status=204)


async def handle_send_message(request: web.Request) -> web.Response:
    ws = request.match_info["ws"]
    sid = request.match_info["sid"]
    _validate_workspace_id(ws)
    _validate_session_id(sid)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(reason="invalid_json")
    content = body.get("content") or ""
    if not content.strip():
        raise web.HTTPBadRequest(reason="content_empty")
    pool: ProcessPool = request.app["pool"]
    try:
        client = await pool.get(ws)
        result = await client.prompt(sid, content)
    except ACPError as e:
        return web.json_response(
            {"error": "acp_error", "code": e.code, "detail": e.message},
            status=502,
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "rpc_timeout"}, status=504)
    return web.json_response({
        "session_id": sid,
        "content": result["content"],
        "stop_reason": result.get("stop_reason"),
        "usage": result.get("usage"),
    })


# ── App factory ──────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    app["pool"] = ProcessPool()
    await app["pool"].start_gc()


async def on_cleanup(app: web.Application) -> None:
    await app["pool"].shutdown()
    # 收乾所有 gateway daemons(避免 supervisor 結束後 messaging bot 繼續 polling)
    for ws, daemon in list(_gateway_daemons.items()):
        try:
            await daemon.stop()
        except Exception:
            LOG.exception("gateway shutdown failed ws=%s", ws)
    _gateway_daemons.clear()


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/admin/users/{ws}/provision", handle_provision)
    app.router.add_post("/admin/users/{ws}/rotate", handle_provision)  # alias
    app.router.add_post("/v1/workspaces/{ws}/sessions", handle_create_session)
    app.router.add_get("/v1/workspaces/{ws}/sessions", handle_list_sessions)
    app.router.add_post(
        "/v1/workspaces/{ws}/sessions/{sid}/messages",
        handle_send_message,
    )
    # PR4:skills 與 memory(read-only,不需 ACP 子進程)
    app.router.add_get("/v1/workspaces/{ws}/skills", handle_list_skills)
    app.router.add_get("/v1/workspaces/{ws}/memory/search", handle_memory_search)
    # PR5:cron(直接讀寫 jobs.json,Hermes ACP 子進程啟動後會自己讀)
    app.router.add_get("/v1/workspaces/{ws}/cron", handle_list_cron)
    app.router.add_post("/v1/workspaces/{ws}/cron", handle_add_cron)
    app.router.add_delete("/v1/workspaces/{ws}/cron/{job_id}", handle_delete_cron)
    # Gateway PR:messaging daemon(per-workspace `python -m gateway.run`)
    app.router.add_get("/v1/workspaces/{ws}/gateway", handle_gateway_status)
    app.router.add_post(
        "/v1/workspaces/{ws}/gateway/{platform}/enable", handle_gateway_enable,
    )
    app.router.add_post(
        "/v1/workspaces/{ws}/gateway/{platform}/disable", handle_gateway_disable,
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    LOG.info(
        "hermes supervisor starting host=%s port=%d data_root=%s "
        "max_workers=%d idle_ttl=%ds rpc_timeout=%ds",
        LISTEN_HOST, LISTEN_PORT, HERMES_DATA_ROOT,
        HERMES_MAX_WORKERS, HERMES_IDLE_TTL_SEC, HERMES_RPC_TIMEOUT,
    )
    web.run_app(build_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=None)


if __name__ == "__main__":
    main()
