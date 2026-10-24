"""Validate the optional host sample without exposing arbitrary host-file contents."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

CONTAINERS = ("aim_edge_agent", "aim_influxdb_edge", "aim_mosquitto_edge")
VOLUMES = ("aim_agent_state", "aim_mosquitto_data", "aim_mosquitto_logs")
MAX_FILE_BYTES = 256 * 1024


def number(value, maximum=2**63 - 1, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > maximum or not math.isfinite(value):
        return None
    return int(value) if integer and int(value) == value else (None if integer else value)


def text(value, maximum=100):
    return value if isinstance(value, str) and len(value) <= maximum else None


def stamp(value, now, max_age):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None, False
        age = (now - parsed).total_seconds()
        return parsed.isoformat(), -30 <= age <= max_age
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, False


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def section(value, now, max_age):
    value = object_or_empty(value)
    collected_at, fresh = stamp(value.get("collected_at"), now, max_age)
    status = value.get("status")
    if status not in ("ok", "partial", "unavailable"):
        status = "unavailable"
    if collected_at and not fresh:
        status = "stale"
    elif not collected_at:
        status = "unavailable"
    return {"status": status, "collected_at": collected_at}, value if fresh else {}


def read_resources(path, now=None):
    now = now or datetime.now(timezone.utc)
    payload = {}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Host sample too large")
        candidate = json.loads(raw)
        if isinstance(candidate, dict) and candidate.get("schema_version") == 1:
            payload = candidate
    except (OSError, ValueError, TypeError):
        pass

    host, raw_host = section(payload.get("host"), now, 180)
    host["cpu_percent"] = number(raw_host.get("cpu_percent"), 100)
    host["uptime_seconds"] = number(raw_host.get("uptime_seconds"), integer=True)
    memory = object_or_empty(raw_host.get("memory"))
    host["memory"] = {key: number(memory.get(key), 100 if key == "used_percent" else 2**63 - 1,
                                  integer=key != "used_percent")
                      for key in ("total_bytes", "available_bytes", "used_bytes", "used_percent")}
    disks = raw_host.get("disks", [])
    host["disks"] = []
    for disk in disks[:32] if isinstance(disks, list) else []:
        disk = object_or_empty(disk)
        drive = text(disk.get("drive"), 16)
        if drive is not None:
            host["disks"].append({"drive": drive, **{
                key: number(disk.get(key), 100 if key == "used_percent" else 2**63 - 1,
                            integer=key != "used_percent")
                for key in ("total_bytes", "free_bytes", "used_bytes", "used_percent")}})
    if host["status"] == "ok" and (host["cpu_percent"] is None or host["uptime_seconds"] is None
                                     or any(value is None for value in host["memory"].values())
                                     or not host["disks"]
                                     or any(value is None for disk in host["disks"] for value in disk.values())):
        host["status"] = "partial"

    docker, raw_docker = section(payload.get("docker"), now, 180)
    docker["containers"] = []
    rows = raw_docker.get("containers", [])
    by_name = {row.get("name"): row for row in rows if isinstance(row, dict)
               and isinstance(row.get("name"), str)} if isinstance(rows, list) else {}
    for name in CONTAINERS:
        row = by_name.get(name, {})
        state = row.get("state")
        health = row.get("health")
        docker["containers"].append({
            "name": name,
            "state": state if state in ("created", "running", "paused", "restarting", "removing", "exited", "dead") else "unknown",
            "health": health if health in ("healthy", "unhealthy", "starting", "none") else "unknown",
            "restart_count": number(row.get("restart_count"), integer=True),
            "cpu_percent": number(row.get("cpu_percent"), 1_000_000),
            **{key: number(row.get(key), integer=True) for key in ("memory_used_bytes", "memory_limit_bytes")},
        })
    if docker["status"] == "ok" and any(
        row["state"] == "unknown" or row["restart_count"] is None
        or (row["state"] == "running" and any(row[key] is None for key in ("cpu_percent", "memory_used_bytes", "memory_limit_bytes")))
        for row in docker["containers"]
    ):
        docker["status"] = "partial"

    # Storage refreshes more slowly and keeps its own timestamp, even if Docker is down.
    storage, raw_storage = section(object_or_empty(payload.get("docker")).get("storage"), now, 900)
    storage["size_basis"] = "logical_bytes"
    storage["containers"] = []
    rows = raw_storage.get("containers", [])
    by_name = {row.get("name"): row for row in rows if isinstance(row, dict)
               and isinstance(row.get("name"), str)} if isinstance(rows, list) else {}
    for name in CONTAINERS:
        row = by_name.get(name, {})
        storage["containers"].append({"name": name, **{
            key: number(row.get(key), integer=True) for key in ("writable_bytes", "log_bytes")}})
    raw_volumes = object_or_empty(raw_storage.get("volumes"))
    storage["volumes"] = {name: number(raw_volumes.get(name), integer=True) for name in VOLUMES}
    components = [row[key] for row in storage["containers"] for key in ("writable_bytes", "log_bytes")]
    components.extend(storage["volumes"].values())
    storage["non_db_mutable_bytes"] = sum(components) if all(value is not None for value in components) else None
    summary = object_or_empty(raw_storage.get("engine"))
    storage["engine"] = {key: number(summary.get(key), integer=True)
                         for key in ("images_bytes_estimate", "build_cache_bytes_estimate", "volumes_bytes_estimate", "containers_bytes_estimate")}
    if storage["status"] == "ok" and (storage["non_db_mutable_bytes"] is None
                                       or any(value is None for value in storage["engine"].values())):
        storage["status"] = "partial"
    docker["storage"] = storage
    return {"host": host, "docker": docker}
