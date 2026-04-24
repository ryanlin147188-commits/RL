"""
系統狀態 API — 提供首頁儀表板使用。

資料來源：
1. **docker SDK**（透過掛載 /var/run/docker.sock）：
   - Docker daemon 資訊（版本、映像、容器數）
   - 所有 `autotest-*` 容器的 CPU / 記憶體 / 網路 stats（逐容器抓 → 加總 = 平台總用量）
   - Host 資訊（NCPU、MemTotal）
2. **os.statvfs**：backend 容器 / 檔案系統（mysql_data / pic_data 等 volume 都在同一 Docker data root）

回傳結構：
{
  "timestamp": ...,
  "cpu":     { "percent": 平台 CPU % (sum of autotest-* containers / host_cores) },
  "memory":  { "used_mb": 平台記憶體, "total_mb": host memory, "percent": ... },
  "disk":    { ... },
  "network": { 累計 RX/TX / 即時速率 },
  "docker":  { status / version / containers_running / images },
  "host":    { cores / os / kernel / arch / total_mem_mb }
}
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter

router = APIRouter()

# 追蹤名稱前綴，只計算 AutoTest 平台自己的容器
_PLATFORM_PREFIX = "autotest-"

# 上次取樣：做差值計算網路速率
_prev_net: dict[str, Any] | None = None


def _read_disk() -> dict[str, Any] | None:
    """讀取根檔案系統的磁碟使用狀況（掛 docker data root）。"""
    try:
        st = os.statvfs("/")
        block_size = st.f_frsize or st.f_bsize
        total = st.f_blocks * block_size
        avail = st.f_bavail * block_size
        used = total - avail
        return {
            "total_gb": round(total / (1024 ** 3), 2),
            "used_gb": round(used / (1024 ** 3), 2),
            "available_gb": round(avail / (1024 ** 3), 2),
            "percent": round(used * 100 / total, 1) if total else 0.0,
        }
    except Exception:
        return None


def _calc_cpu_percent(stats: dict[str, Any]) -> float:
    """把 docker stats 的原始計數換算成 CPU %（單一容器）。"""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_total = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_total = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        online = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1]) or 1
        if cpu_total > 0 and sys_total > 0:
            return round((cpu_total / sys_total) * online * 100.0, 1)
    except Exception:
        pass
    return 0.0


def _collect_platform_stats() -> dict[str, Any]:
    """把所有 autotest-* 容器的 stats 加總，代表『平台總用量』。"""
    result = {
        "cpu_percent": 0.0,
        "mem_used_mb": 0.0,
        "mem_total_mb": 0.0,   # host 總記憶體（docker.info().MemTotal）
        "mem_percent": 0.0,
        "net_rx_bytes": 0,
        "net_tx_bytes": 0,
        "containers": [],      # 每容器細項，除錯用
        "container_count": 0,
    }
    try:
        import docker as docker_sdk  # type: ignore
        client = docker_sdk.from_env(timeout=3)
        info = client.info()
        mem_total = int(info.get("MemTotal", 0))
        result["mem_total_mb"] = round(mem_total / (1024 ** 2), 1) if mem_total else 0.0

        containers = client.containers.list()
        autotest = [c for c in containers if (c.name or "").startswith(_PLATFORM_PREFIX)]
        result["container_count"] = len(autotest)

        total_cpu = 0.0
        total_mem = 0
        total_rx = 0
        total_tx = 0
        for c in autotest:
            try:
                s = c.stats(stream=False)
            except Exception:
                continue
            cpu = _calc_cpu_percent(s)
            mem_usage = int(s.get("memory_stats", {}).get("usage", 0))
            # 扣掉 cache 讓數字更貼近「實際工作集記憶體」
            mem_cache = int(s.get("memory_stats", {}).get("stats", {}).get("inactive_file", 0))
            working_mem = max(0, mem_usage - mem_cache)
            rx = 0
            tx = 0
            for _iface, counters in (s.get("networks") or {}).items():
                rx += int(counters.get("rx_bytes", 0))
                tx += int(counters.get("tx_bytes", 0))
            total_cpu += cpu
            total_mem += working_mem
            total_rx += rx
            total_tx += tx
            result["containers"].append({
                "name": c.name,
                "cpu_percent": cpu,
                "mem_mb": round(working_mem / (1024 ** 2), 1),
                "rx_mb": round(rx / (1024 ** 2), 2),
                "tx_mb": round(tx / (1024 ** 2), 2),
            })
        result["cpu_percent"] = round(total_cpu, 1)
        result["mem_used_mb"] = round(total_mem / (1024 ** 2), 1)
        if mem_total:
            result["mem_percent"] = round(total_mem * 100 / mem_total, 1)
        result["net_rx_bytes"] = total_rx
        result["net_tx_bytes"] = total_tx
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def _net_rates(rx_total_bytes: int, tx_total_bytes: int) -> dict[str, Any]:
    """兩次取樣差值計算網路速率。"""
    global _prev_net
    now = time.time()
    rx_total_mb = round(rx_total_bytes / (1024 ** 2), 2)
    tx_total_mb = round(tx_total_bytes / (1024 ** 2), 2)
    if _prev_net is None:
        _prev_net = {"ts": now, "rx": rx_total_bytes, "tx": tx_total_bytes}
        return {
            "rx_total_mb": rx_total_mb,
            "tx_total_mb": tx_total_mb,
            "rx_rate_kbps": 0.0,
            "tx_rate_kbps": 0.0,
        }
    dt = max(0.001, now - _prev_net["ts"])
    rx_rate = max(0.0, (rx_total_bytes - _prev_net["rx"]) / dt)
    tx_rate = max(0.0, (tx_total_bytes - _prev_net["tx"]) / dt)
    _prev_net = {"ts": now, "rx": rx_total_bytes, "tx": tx_total_bytes}
    return {
        "rx_total_mb": rx_total_mb,
        "tx_total_mb": tx_total_mb,
        "rx_rate_kbps": round(rx_rate / 1024, 1),
        "tx_rate_kbps": round(tx_rate / 1024, 1),
    }


def _read_docker() -> tuple[dict[str, Any], dict[str, Any]]:
    """回傳 (docker_info, host_info)。
    docker_info：Docker daemon 狀態
    host_info：Docker host 作業系統 / CPU 總核數 / 記憶體總量
    """
    try:
        import docker as docker_sdk  # type: ignore
        client = docker_sdk.from_env(timeout=2)
        info = client.info()
        version = client.version()
        docker_info = {
            "status": "running",
            "version": version.get("Version", "unknown"),
            "containers_total": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "server_version": info.get("ServerVersion", "unknown"),
        }
        host_info = {
            "os": info.get("OperatingSystem", "unknown"),
            "kernel": info.get("KernelVersion", "unknown"),
            "arch": info.get("Architecture", "unknown"),
            "cores": info.get("NCPU", 0),
            "total_mem_mb": round(int(info.get("MemTotal", 0)) / (1024 ** 2), 0) if info.get("MemTotal") else 0,
        }
        return docker_info, host_info
    except Exception as e:
        return (
            {"status": "unknown", "error": str(e)[:120], "containers_running": None, "images": None},
            {"os": "unknown", "cores": 0, "total_mem_mb": 0},
        )


@router.get("/system/status", tags=["System"])
def get_system_status() -> dict[str, Any]:
    """回傳 AutoTest **平台** 執行狀態（以 `autotest-*` 容器為範圍加總）+ Host 對照資訊。

    範圍說明：
    - `cpu.percent` = 所有 `autotest-*` 容器 CPU % 加總（100% = 單一 CPU 核心 100%；
      多核系統可能 > 100%）
    - `memory.used_mb` = 所有 `autotest-*` 容器實際使用記憶體（扣除 cache）
    - `memory.total_mb` = Host 總記憶體（作為分母）
    - `disk` = backend 容器根檔案系統（與 Docker data volume 在同 data root）
    - `network.*` = 所有 `autotest-*` 容器的網路累計 / 即時速率
    - `host` = Docker host 資訊（作業系統、核心數、記憶體總量）
    """
    plat = _collect_platform_stats()
    docker_info, host_info = _read_docker()
    network = _net_rates(plat.get("net_rx_bytes", 0), plat.get("net_tx_bytes", 0))
    return {
        "timestamp": int(time.time()),
        "scope": "autotest-platform",  # 標示：以 AutoTest 平台容器為範圍加總
        "cpu": {"percent": plat.get("cpu_percent", 0.0)},
        "memory": {
            "used_mb": plat.get("mem_used_mb", 0.0),
            "total_mb": plat.get("mem_total_mb", 0.0),
            "percent": plat.get("mem_percent", 0.0),
        },
        "disk": _read_disk(),
        "network": network,
        "docker": docker_info,
        "host": host_info,
        "platform": {
            "container_count": plat.get("container_count", 0),
            "containers": plat.get("containers", []),
        },
    }
