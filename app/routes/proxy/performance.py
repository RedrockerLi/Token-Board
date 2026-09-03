"""Functional proxy API route group."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.routes.proxy.common import bp_proxy, current_app, jsonify, request, _proxy_db

log = logging.getLogger(__name__)

@bp_proxy.route("/logs")
def request_logs():
    return jsonify(
        _proxy_db().get_request_logs(
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 50, type=int),
        )
    )


@bp_proxy.route("/perf/summary")
def perf_summary():
    minutes = request.args.get("minutes", 15, type=int)
    return jsonify(_proxy_db().get_perf_summary(minutes))


@bp_proxy.route("/perf/upstream-success-rate")
def perf_upstream_success_rate():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_upstream_success_rate(minutes))


@bp_proxy.route("/perf/latency")
def perf_latency():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_latency(minutes))


@bp_proxy.route("/perf/speed")
def perf_speed():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_speed(minutes))


@bp_proxy.route("/perf/throughput")
def perf_throughput():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_throughput(minutes))


@bp_proxy.route("/perf/models")
def perf_models():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_models(minutes))


@bp_proxy.route("/perf/realtime")
def perf_realtime():
    payload = _proxy_db().get_perf_realtime()
    health = current_app.config.get("BACKGROUND_TASK_HEALTH", {})
    lock = current_app.config.get("BACKGROUND_TASK_HEALTH_LOCK")
    if lock:
        with lock:
            dashboard_tasks = {
                name: dict(value) for name, value in health.items()
            }
    else:
        dashboard_tasks = dict(health)
    maintenance_path = Path(_proxy_db().db_path).resolve().parent / \
        "token-maintenance-health.json"
    maintenance_tasks = {}
    try:
        document = json.loads(maintenance_path.read_text(encoding="utf-8"))
        heartbeat = document.get("heartbeat_at")
        stale = False
        if heartbeat:
            stamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - stamp).total_seconds() > 120
        maintenance_tasks = document.get("tasks") or {}
        if stale:
            for name, item in maintenance_tasks.items():
                item = dict(item)
                item["status"] = "degraded"
                item.setdefault("last_error", "maintenance heartbeat stale")
                maintenance_tasks[name] = item
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        log.debug("maintenance health file is unavailable: %s", maintenance_path)
    payload["background_tasks"] = {**maintenance_tasks, **dashboard_tasks}
    payload["background_health"] = (
        "degraded" if payload.get("sync_health") not in {None, "ok", "unconfigured"}
        or any(item.get("status") == "degraded"
               for item in payload["background_tasks"].values())
        else "ok"
    )
    return jsonify(payload)
