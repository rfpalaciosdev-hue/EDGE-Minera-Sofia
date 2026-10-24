"""Read-only, fixed-field connection summary; never print logs or configuration."""
import argparse
import json
import math
import os
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


def error_kind(error):
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "timeout" in name:
        return "timeout"
    if "ssl" in name or "certificate" in message:
        return "tls"
    if any(word in message for word in ("nameresolution", "getaddrinfo", "name resolution", "name or service not known")):
        return "dns"
    return "connection" if "connection" in name else "request_error"


def numeric(value, maximum=2**53 - 1):
    return value if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value) else None


def age_seconds(stamp):
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        return round((datetime.now(timezone.utc) - value).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return None


def collect(environ=None, get=None, resolve=None, host_utc=None, expected_site=None, expected_tenant=None):
    environ = os.environ if environ is None else environ
    if get is None:
        import requests
        get = requests.get
    resolve = resolve or socket.getaddrinfo
    report = {"device_token_present": bool(environ.get("DEVICE_TOKEN"))}
    if host_utc:
        report["container_clock_difference_seconds"] = age_seconds(host_utc)
    if expected_site:
        report["site_matches_expected"] = environ.get("SITE_ID") == expected_site
    if expected_tenant:
        report["tenant_matches_expected"] = environ.get("TENANT_SLUG") == expected_tenant
    try:
        response = get("http://127.0.0.1:8080/api/health", timeout=(2, 3), allow_redirects=False)
        local = {"http_status": response.status_code}
        if response.status_code == 200:
            sample = response.json()
            forwarding = sample.get("forwarding") or {}
            services = sample.get("services") or {}
            local.update({
                "sample_age_seconds": age_seconds(sample.get("collected_at")),
                "last_ack_age_seconds": age_seconds(forwarding.get("last_ack_at")),
                "consecutive_errors": numeric(forwarding.get("consecutive_errors")),
                "last_http_status": numeric(forwarding.get("last_http_status"), 599),
                "mqtt_connected": {"connected": True, "disconnected": False}.get(services.get("mqtt_connection")),
                "influx_ok": {"ok": True, "error": False}.get(services.get("influx_probe")),
            })
        report["local_agent"] = local
    except Exception as error:
        report["local_agent"] = {"error": error_kind(error)}
    try:
        target = urlsplit(environ.get("CLOUD_API_URL", ""))
        if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
            raise ValueError("Invalid cloud URL")
        port = target.port or (443 if target.scheme == "https" else 80)
        if not target.path.endswith("/sync"):
            report["cloud_ping"] = {"error": "unexpected_sync_path"}
            return report
        ping = urlunsplit((target.scheme, target.netloc, target.path[:-4] + "ping", "", ""))
    except (TypeError, ValueError):
        report["cloud_ping"] = {"error": "invalid_configuration"}
        return report
    try:
        resolve(target.hostname, port, type=socket.SOCK_STREAM)
        report["dns_ok"] = True
    except Exception:
        report["dns_ok"] = False
    started = time.monotonic()
    try:
        response = get(ping, timeout=(3, 7), allow_redirects=False)
        result = {"http_status": response.status_code, "seconds": round(time.monotonic() - started, 3)}
        if response.status_code == 200:
            try:
                result["pong"] = response.json().get("pong") is True
            except (ValueError, AttributeError):
                result["pong"] = False
        report["cloud_ping"] = result
    except Exception as error:
        report["cloud_ping"] = {"error": error_kind(error)}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-utc")
    parser.add_argument("--expected-site")
    parser.add_argument("--expected-tenant")
    args = parser.parse_args()
    try:
        result = collect(host_utc=args.host_utc, expected_site=args.expected_site, expected_tenant=args.expected_tenant)
    except Exception:
        result = {"diagnostic_error": True}
    print(json.dumps(result, allow_nan=False))
