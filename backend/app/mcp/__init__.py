"""MCP (Model Context Protocol) client 子系統 — Phase 2。

* ``client.py`` — 包 ``mcp`` SDK 的 ClientSession + streamable HTTP transport
* ``connection_pool.py`` — 進程級連線池(LRU + idle GC + per-server 並發上限)
* ``tool_adapter.py`` — 把 MCP tool 包成內部 ``Tool`` 子類 factory

Phase 2 範圍只實作 ``transport='http'``;stdio 留給 Phase 3(等 backend image
加 node/uvx 後)。
"""
