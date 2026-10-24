"""Cached read-only health and bounded HTTP requests; no cursor or retention writes."""
import copy
import json as json_module
import math
import os
import threading
import time
from datetime import datetime, timezone
from resource_report import read_resources

# Influx limit(n) applies per table, so a query can produce a much larger total.
# Bound the actual HTTP body, including health metadata and JSON escaping.
MAX_REQUEST_BYTES = 256 * 1024
MAX_RECORDS_PER_REQUEST = 500


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def influx_bytes(metrics):
    total = 0.0
    found = False
    for line in metrics.splitlines():
        parts = line.split()
        if not parts or parts[0].split("{", 1)[0] not in ("storage_tsm_files_disk_bytes", "storage_wal_size"):
            continue
        value = float(parts[1])
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid storage metric")
        total += value
        found = True
    return int(total) if found else None


class HealthReporter:
    def __init__(self, influx_url, mqtt_connected, get=None, resource_path=None):
        if get is None:
            import requests
            get = requests.get
        self.get = get
        self.influx_url = influx_url.rstrip("/")
        self.mqtt_connected = mqtt_connected
        self.resource_path = resource_path or os.getenv("EDGE_HOST_HEALTH_FILE", "/host-health/host-health.json")
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.report = None
        self.forwarding = {"last_ack_at": None, "last_http_status": None, "consecutive_errors": 0}

    def collect(self):
        probe = "unknown"
        size = None
        try:
            response = self.get(self.influx_url + "/health", timeout=2)
            probe = "ok" if response.status_code == 200 and response.json().get("status") == "pass" else "error"
        except Exception:
            probe = "error"
        try:
            response = self.get(self.influx_url + "/metrics", timeout=2)
            if response.status_code == 200:
                size = influx_bytes(response.text)
        except Exception:
            size = None
        try:
            mqtt = "connected" if self.mqtt_connected() else "disconnected"
        except Exception:
            mqtt = "unknown"
        try:
            resources = read_resources(self.resource_path)
        except Exception:
            resources = read_resources("/nonexistent-host-health")
        with self.lock:
            self.report = {
                "schema_version": 1, "collected_at": utc_now(),
                "uptime_seconds": int(time.monotonic() - self.started),
                "resources": {"influx_data_bytes": size},
                "services": {"mqtt_connection": mqtt, "influx_probe": probe},
                "forwarding": dict(self.forwarding),
                **resources,
            }

    def run(self):
        while True:
            try:
                self.collect()
            except Exception:
                pass  # A broken sampler must not stop forwarding.
            time.sleep(30)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.report)

    def record_send(self, status):
        with self.lock:
            self.forwarding["last_http_status"] = status
            if status == 200:
                self.forwarding["last_ack_at"] = utc_now()
                self.forwarding["consecutive_errors"] = 0
            else:
                self.forwarding["consecutive_errors"] += 1


def iter_request_bodies(body, *, max_bytes=None, max_records=None):
    max_bytes = MAX_REQUEST_BYTES if max_bytes is None else max_bytes
    max_records = MAX_RECORDS_PER_REQUEST if max_records is None else max_records
    if max_bytes < 1 or max_records < 1:
        raise ValueError("HTTP batch limits must be positive")
    records = body.get("data")
    if not isinstance(records, list) or not records:
        request = dict(body)
        if len(json_module.dumps(request, allow_nan=False).encode("utf-8")) > max_bytes:
            request.pop("edge_health", None)
        if len(json_module.dumps(request, allow_nan=False).encode("utf-8")) > max_bytes:
            raise ValueError("HTTP request metadata exceeds the byte limit")
        yield request
        return
    envelope = {**body, "data": []}
    envelope_bytes = len(json_module.dumps(envelope, allow_nan=False).encode("utf-8"))
    if envelope_bytes > max_bytes and "edge_health" in envelope:
        # Optional monitoring must never prevent a telemetry request.
        envelope.pop("edge_health")
        envelope_bytes = len(json_module.dumps(envelope, allow_nan=False).encode("utf-8"))
    chunk = []
    size = envelope_bytes
    for record in records:
        record_bytes = len(json_module.dumps(record, allow_nan=False).encode("utf-8"))
        if envelope_bytes + record_bytes > max_bytes and "edge_health" in envelope:
            envelope.pop("edge_health")
            reduced_bytes = len(json_module.dumps(envelope, allow_nan=False).encode("utf-8"))
            size -= envelope_bytes - reduced_bytes
            envelope_bytes = reduced_bytes
        if envelope_bytes + record_bytes > max_bytes:
            raise ValueError("A telemetry record exceeds the HTTP request byte limit; cursor retained")
        separator_bytes = 2 if chunk else 0  # Default JSON list separator: ', '.
        if chunk and (len(chunk) >= max_records or size + separator_bytes + record_bytes > max_bytes):
            yield {**envelope, "data": chunk}
            chunk = []
            size = envelope_bytes
            separator_bytes = 0
        chunk.append(record)
        size += separator_bytes + record_bytes
    if chunk:
        yield {**envelope, "data": chunk}


def post_with_health(reporter, session, url, *, json, **kwargs):
    # Never let collection/serialization/accounting failures prevent a send.
    body = dict(json)
    try:
        snapshot = reporter.snapshot()
        if snapshot is not None:
            json_module.dumps(snapshot, allow_nan=False)
            body["edge_health"] = snapshot
    except Exception:
        pass
    try:
        for request_body in iter_request_bodies(body):
            response = session.post(url, json=request_body, **kwargs)
            try:
                reporter.record_send(response.status_code)
            except Exception:
                pass
            if response.status_code != 200:
                # The caller must not advance its cursor after a partial ACK.
                return response
    except Exception:
        try:
            reporter.record_send(None)
        except Exception:
            pass
        raise
    # Return success only after every part of the original batch was confirmed.
    return response
