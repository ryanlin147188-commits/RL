"""Network host 判定 helper(供 config.py BASE_URL validator 與 oidc.py
_resolve_redirect_uri 共用)。

設計理念:OIDC 授權碼純 HTTP 在公網上會被 MitM 截取,故強制 HTTPS;但
localhost 與 RFC1918 私有網段是純內網部署常見場景(例如客戶自管 VM
192.168.4.89),要求 https 反而會打壞 self-signed cert 場景與 Zoho
後台 redirect URI 註冊。折衷:對「localhost + RFC1918」允許 http,其
餘 hostname / 公網 IP 仍強制 https。
"""
from __future__ import annotations

import ipaddress


_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_private_or_localhost_host(host: str) -> bool:
    """True 表示 host 是 localhost 或 RFC1918 私有 IP,允許 http。

    判定:
      * "localhost" / "127.0.0.1" / "::1" / "*.localhost" → True
      * 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16 → True(RFC1918)
      * IPv6 loopback / link-local fe80::/10 → True
      * 其他(公網 IP、自訂域名) → False
    """
    h = (host or "").lower().strip()
    if not h:
        return False
    if h in _LOCALHOST_HOSTS or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # 不是 IP literal(可能是 domain),不算內網
        return False
    # ipaddress.is_private 已涵蓋 RFC1918 + loopback + link-local
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
