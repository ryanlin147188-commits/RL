"""ACP entry wrapper — 把 config.yaml 的 agent.disabled_toolsets 套用到 ACP 子進程。

Hermes Agent (NousResearch fork) 在 gateway / CLI 路徑會讀 `config.yaml` 的
`agent.disabled_toolsets` 過濾 tool;但在 ACP 路徑(我們用的)沒接這條邏輯,
session._make_agent 把 enabled_toolsets 硬寫成 ``["hermes-acp"]``,所有 tool 都會
餵給 LLM。

這支 wrapper 在啟 acp_adapter.entry 之前 monkey-patch 兩處:
    1. ``model_tools.get_tool_definitions`` — 第一次取 tool list 時自動拿掉 disabled
    2. ``acp_adapter.session.SessionManager._make_agent`` — agent 建好之後再把
       ``agent.tools`` / ``agent.valid_tool_names`` / ``agent.enabled_toolsets``
       的 disabled set 扣掉,避免 cached tool list 漏網。

讀的是 ``HERMES_HOME/config.yaml`` 的 ``agent.disabled_toolsets`` list — 與 gateway
路徑同欄位、同語意,使用者體驗一致。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Set

logger = logging.getLogger("acp_lockdown")


def _load_disabled_toolsets() -> Set[str]:
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return set()
    cfg_path = Path(home) / "config.yaml"
    if not cfg_path.is_file():
        return set()
    try:
        import yaml
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return set()
    agent_cfg = cfg.get("agent") or {}
    raw = agent_cfg.get("disabled_toolsets") or []
    if not isinstance(raw, (list, tuple)):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def _apply_lockdown() -> None:
    disabled = _load_disabled_toolsets()
    if not disabled:
        logger.info("acp_lockdown: no disabled_toolsets configured — pass-through")
        return

    logger.info("acp_lockdown: disabling toolsets=%s", sorted(disabled))

    # ── Patch 1:get_tool_definitions ─────────────────────────────────
    # 在 tool list 生成時就把 disabled toolset 的 tool 拿掉。
    try:
        import model_tools as _mt
        _orig_gtd = _mt.get_tool_definitions

        def _patched_gtd(enabled_toolsets: List[str] = None,
                         disabled_toolsets: List[str] = None,
                         quiet_mode: bool = False):
            merged_disabled = list(disabled_toolsets or []) + list(disabled)
            return _orig_gtd(
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=merged_disabled,
                quiet_mode=quiet_mode,
            )

        _mt.get_tool_definitions = _patched_gtd
        logger.info("acp_lockdown: patched model_tools.get_tool_definitions")
    except Exception as e:
        logger.warning("acp_lockdown: failed to patch get_tool_definitions: %s", e)

    # ── Patch 2:SessionManager._make_agent 後置 filter ───────────────
    # 萬一 patch 1 沒生效(版本變動 / cache),這層在 agent 建好後再清一次
    # tool list。雙保險。
    try:
        import acp_adapter.session as _sess
        _orig_make = _sess.SessionManager._make_agent

        def _patched_make(self, **kw):
            agent = _orig_make(self, **kw)
            try:
                from model_tools import get_toolset_for_tool
                if getattr(agent, "tools", None):
                    kept = []
                    for t in agent.tools:
                        name = (t.get("function") or {}).get("name") or ""
                        ts = get_toolset_for_tool(name) if name else None
                        if ts in disabled:
                            continue
                        kept.append(t)
                    if len(kept) != len(agent.tools):
                        agent.tools = kept
                        agent.valid_tool_names = {
                            (t.get("function") or {}).get("name", "")
                            for t in kept
                        }
                        logger.info(
                            "acp_lockdown: filtered agent.tools %d → %d (disabled=%s)",
                            len(agent.tools) + (len(kept) - len(kept)), len(kept),
                            sorted(disabled),
                        )
            except Exception as e:
                logger.warning("acp_lockdown: post-filter failed: %s", e)
            return agent

        _sess.SessionManager._make_agent = _patched_make
        logger.info("acp_lockdown: patched SessionManager._make_agent")
    except Exception as e:
        logger.warning("acp_lockdown: failed to patch SessionManager: %s", e)


def main() -> None:
    # 在 acp_adapter.entry import 之前先把 patch 套上 — 確保 agent 建出來
    # 看到的 tool list 已經是過濾後的。
    _apply_lockdown()

    # 接著走原本的 entry。entry 用 sys.argv 跟 stdio,所以 import 後直接 run。
    from acp_adapter import entry as _entry  # noqa: F401
    # acp_adapter.entry 在 import 時不啟動 server;真正啟動點看模組底部
    if hasattr(_entry, "main"):
        _entry.main()
    elif hasattr(_entry, "run"):
        _entry.run()
    else:
        # entry 用 if __name__ == "__main__":  asyncio.run(main()) 包起來,
        # 我們這層用 -m 走的話 entry 不會自己跑;手動觸發。
        import asyncio
        if hasattr(_entry, "_main"):
            asyncio.run(_entry._main())
        else:
            # fallback:重 import 用 -m 方式跑
            import runpy
            runpy.run_module("acp_adapter.entry", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
