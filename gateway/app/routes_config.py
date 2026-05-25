"""routes.yaml parser:把 yaml 載入記憶體,提供 match 函式。

啟動時 read once,沒做 hot reload(改完 yaml 必須 restart gateway container)。
未來要 hot reload 可加 watchdog 監聽檔案改動,但目前複雜度不值得。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_log = logging.getLogger("gateway.routes")


@dataclass
class RouteRule:
    path: str
    methods: list[str] = field(default_factory=list)
    rate_limit: Optional[str] = None
    circuit_group: str = "default"


@dataclass
class CircuitConfig:
    threshold: int = 5
    ttl_seconds: int = 30


@dataclass
class RoutesConfig:
    default_rate_limit: str = "600/minute"
    routes: list[RouteRule] = field(default_factory=list)
    circuit_breakers: dict[str, CircuitConfig] = field(default_factory=dict)

    def match(self, method: str, path: str) -> RouteRule:
        """找第一條符合的 rule;沒 match 回 fallback (default rate)。"""
        method_u = method.upper()
        for r in self.routes:
            if r.methods and method_u not in [m.upper() for m in r.methods]:
                continue
            # path 結尾 / → prefix match;否則 exact
            if r.path.endswith("/"):
                if path.startswith(r.path):
                    return r
            else:
                if path == r.path:
                    return r
        return RouteRule(
            path=path, methods=[], rate_limit=self.default_rate_limit,
            circuit_group="default",
        )

    def get_circuit(self, group: str) -> CircuitConfig:
        return self.circuit_breakers.get(group, CircuitConfig())


def load_routes(yaml_path: str) -> RoutesConfig:
    """讀 routes.yaml,失敗 fallback 到全空(default rate only)。"""
    p = Path(yaml_path)
    if not p.exists():
        _log.warning("routes.yaml not found at %s — using defaults", yaml_path)
        return RoutesConfig()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        _log.error("failed to parse routes.yaml: %s — using defaults", e)
        return RoutesConfig()

    routes = []
    for r in data.get("routes") or []:
        m = r.get("match") or {}
        routes.append(RouteRule(
            path=m.get("path", ""),
            methods=m.get("methods") or [],
            rate_limit=r.get("rate_limit"),
            circuit_group=r.get("circuit_group", "default"),
        ))

    cbs: dict[str, CircuitConfig] = {}
    for name, cfg in (data.get("circuit_breakers") or {}).items():
        cbs[name] = CircuitConfig(
            threshold=int(cfg.get("threshold", 5)),
            ttl_seconds=int(cfg.get("ttl_seconds", 30)),
        )

    cfg = RoutesConfig(
        default_rate_limit=data.get("default_rate_limit", "600/minute"),
        routes=routes,
        circuit_breakers=cbs,
    )
    _log.info(
        "routes loaded: %d rules, %d circuit groups, default=%s",
        len(cfg.routes), len(cfg.circuit_breakers), cfg.default_rate_limit,
    )
    return cfg
