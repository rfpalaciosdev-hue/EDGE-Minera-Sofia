"""Small read-only health sampler. No cursor, retention or telemetry dependencies."""
import copy
import math
import threading
import time
from datetime import datetime, timezone


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
    def __init__(self, influx_url, mqtt_connected, get=None):
        if get is None:
            import requests
            get = requests.get
        self.get = get
        self.influx_url = influx_url.rstrip("/")
        self.mqtt_connected = mqtt_connected
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
        with self.lock:
            self.report = {
                "schema_version": 1, "collected_at": utc_now(),
                "uptime_seconds": int(time.monotonic() - self.started),
                "resources": {"influx_data_bytes": size},
                "services": {"mqtt_connection": mqtt, "influx_probe": probe},
                "forwarding": dict(self.forwarding),
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


def post_with_health(reporter, session, url, *, json, **kwargs):
    # Never let collection/serialization/accounting failures prevent a send.
    body = dict(json)
    try:
        snapshot = reporter.snapshot()
        if snapshot is not None:
            import json as json_module
            json_module.dumps(snapshot, allow_nan=False)
            body["edge_health"] = snapshot
    except Exception:
        pass
    try:
        response = session.post(url, json=body, **kwargs)
    except Exception:
        try:
            reporter.record_send(None)
        except Exception:
            pass
        raise
    try:
        reporter.record_send(response.status_code)
    except Exception:
        pass
    return response
