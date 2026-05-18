#!/usr/bin/env python3
"""Local attendance stress harness for the one-worker Backend hang incident.

The harness intentionally uses only the public Backend HTTP/WebSocket surface.
It can be run against the local Service compose stack with a single uvicorn
worker and an optionally slow/failing PresenceService.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:
    import websockets
except Exception:  # pragma: no cover - optional runtime dependency
    websockets = None


STUDENT_ID = "20203175"
PROFESSOR_ID = "PRF000"
COURSE_CODE = "CSE102"


def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("success") is True and "data" in payload:
        return payload["data"]
    return payload


@dataclass
class Result:
    endpoint: str
    status_code: int | None
    elapsed_ms: float
    error: str | None = None


@dataclass
class Metrics:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)

    def summarize(self) -> dict[str, Any]:
        by_endpoint: dict[str, list[Result]] = defaultdict(list)
        for result in self.results:
            by_endpoint[result.endpoint].append(result)

        summary: dict[str, Any] = {"total_requests": len(self.results), "endpoints": {}}
        for endpoint, rows in sorted(by_endpoint.items()):
            latencies = sorted(r.elapsed_ms for r in rows)
            status_counts = Counter(str(r.status_code) if r.status_code is not None else "error" for r in rows)
            errors = Counter(r.error for r in rows if r.error)
            summary["endpoints"][endpoint] = {
                "count": len(rows),
                "status_counts": dict(status_counts),
                "error_counts": dict(errors),
                "p50_ms": percentile(latencies, 50),
                "p95_ms": percentile(latencies, 95),
                "max_ms": max(latencies) if latencies else None,
            }
        return summary


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct / 100
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


async def timed_request(
    client: httpx.AsyncClient,
    metrics: Metrics,
    endpoint: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kwargs)
        metrics.add(Result(endpoint, response.status_code, (time.perf_counter() - started) * 1000))
        return response
    except Exception as exc:  # noqa: BLE001 - stress harness records all failures
        metrics.add(Result(endpoint, None, (time.perf_counter() - started) * 1000, exc.__class__.__name__))
        return None


async def fetch_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return unwrap(response.json())


async def setup_active_session(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
        unauth = await client.get("/api/auth/bootstrap")
        auth = await client.get("/api/auth/bootstrap", headers=auth_header(STUDENT_ID))

        timeline = await fetch_json(
            client,
            "GET",
            f"/api/professors/{PROFESSOR_ID}/courses/{COURSE_CODE}/attendance/timeline",
            headers=auth_header(PROFESSOR_ID),
        )
        projection_keys: list[str] = []
        for week in timeline.get("weeks", []):
            for slot in week.get("slots", []):
                key = slot.get("projection_key")
                if key:
                    projection_keys.append(key)
                if len(projection_keys) >= 2:
                    break
            if len(projection_keys) >= 2:
                break
        if not projection_keys:
            raise RuntimeError("no projection keys available for stress setup")

        opened = await fetch_json(
            client,
            "POST",
            f"/api/professors/{PROFESSOR_ID}/courses/{COURSE_CODE}/attendance/sessions/batch",
            headers=auth_header(PROFESSOR_ID),
            json={"projection_keys": projection_keys, "mode": "smart"},
        )
        session_ids = opened.get("changed_session_ids") or [
            row.get("session_id") for row in opened.get("results", []) if row.get("session_id")
        ]
        if not session_ids:
            raise RuntimeError(f"failed to open/reuse smart session: {opened}")

        return {
            "student_id": STUDENT_ID,
            "professor_id": PROFESSOR_ID,
            "course_code": COURSE_CODE,
            "session_id": session_ids[0],
            "projection_keys": projection_keys,
            "unauth_bootstrap_status": unauth.status_code,
            "auth_bootstrap_status": auth.status_code,
        }


async def websocket_probe(base_url: str, metrics: Metrics, stop_at: float, index: int) -> None:
    if websockets is None:
        raise RuntimeError("websockets package is required when --websockets is greater than 0")
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/ws/attendance?token=dev-token:{STUDENT_ID}&courseCode={COURSE_CODE}&view=student"
    while time.perf_counter() < stop_at:
        started = time.perf_counter()
        try:
            async with websockets.connect(url, open_timeout=5, close_timeout=1) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
                metrics.add(Result("websocket-bootstrap", 101, (time.perf_counter() - started) * 1000))
                await asyncio.sleep(1)
        except Exception as exc:  # noqa: BLE001
            metrics.add(Result("websocket-bootstrap", None, (time.perf_counter() - started) * 1000, exc.__class__.__name__))
            await asyncio.sleep(0.5)


async def worker(base_url: str, session_id: int, metrics: Metrics, stop_at: float, worker_id: int) -> None:
    timeout = httpx.Timeout(8.0, connect=2.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        while time.perf_counter() < stop_at:
            choice = worker_id % 7
            if choice in {0, 1, 2}:
                await timed_request(
                    client,
                    metrics,
                    "student-check-in",
                    "POST",
                    f"/api/students/{STUDENT_ID}/attendance/sessions/{session_id}/check-in",
                    headers=auth_header(STUDENT_ID),
                )
            elif choice == 3:
                await timed_request(client, metrics, "health", "GET", "/health")
            elif choice == 4:
                await timed_request(client, metrics, "auth-bootstrap", "GET", "/api/auth/bootstrap", headers=auth_header(STUDENT_ID))
            elif choice == 5:
                await timed_request(
                    client,
                    metrics,
                    "active-sessions",
                    "GET",
                    f"/api/students/{STUDENT_ID}/courses/{COURSE_CODE}/attendance/active-sessions",
                    headers=auth_header(STUDENT_ID),
                )
            else:
                await timed_request(
                    client,
                    metrics,
                    "semester-matrix",
                    "GET",
                    f"/api/students/{STUDENT_ID}/courses/{COURSE_CODE}/attendance/semester-matrix",
                    headers=auth_header(STUDENT_ID),
                )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    setup = await setup_active_session(args.base_url)
    metrics = Metrics()
    stop_at = time.perf_counter() + args.duration
    tasks = [
        asyncio.create_task(worker(args.base_url, int(setup["session_id"]), metrics, stop_at, idx))
        for idx in range(args.concurrency)
    ]
    tasks.extend(
        asyncio.create_task(websocket_probe(args.base_url, metrics, stop_at, idx))
        for idx in range(args.websockets)
    )
    await asyncio.gather(*tasks)
    report = {
        "base_url": args.base_url,
        "duration_seconds": args.duration,
        "concurrency": args.concurrency,
        "websockets": args.websockets,
        "setup": setup,
        "metrics": metrics.summarize(),
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def acceptance_failures(report: dict[str, Any], *, allow_failures: bool) -> list[str]:
    if allow_failures:
        return []
    failures: list[str] = []
    endpoints = report["metrics"]["endpoints"]
    for endpoint, metrics in endpoints.items():
        status_counts = metrics.get("status_counts", {})
        error_counts = metrics.get("error_counts", {})
        if error_counts:
            failures.append(f"{endpoint}: unexpected client/runtime errors {error_counts}")
        if status_counts.get("error"):
            failures.append(f"{endpoint}: request errors recorded ({status_counts['error']})")
        server_errors = {
            status_code: count
            for status_code, count in status_counts.items()
            if status_code.isdigit() and int(status_code) >= 500
        }
        if server_errors:
            failures.append(f"{endpoint}: unexpected 5xx responses {server_errors}")

    health = endpoints.get("health")
    if health is None:
        failures.append("health: endpoint was not exercised")
    else:
        health_p95 = health.get("p95_ms")
        health_max = health.get("max_ms")
        if health_p95 is None or health_p95 > 500:
            failures.append(f"health: p95_ms {health_p95} exceeds 500ms")
        if health_max is None or health_max > 2000:
            failures.append(f"health: max_ms {health_max} exceeds 2000ms")

    bootstrap = endpoints.get("auth-bootstrap")
    if bootstrap is None:
        failures.append("auth-bootstrap: endpoint was not exercised")
    elif bootstrap.get("status_counts", {}).get("200", 0) == 0:
        failures.append("auth-bootstrap: no authenticated 200 responses recorded")

    if report["websockets"] > 0:
        websocket = endpoints.get("websocket-bootstrap")
        if websocket is None:
            failures.append("websocket-bootstrap: requested but not exercised")
        elif websocket.get("status_counts", {}).get("101", 0) == 0:
            failures.append("websocket-bootstrap: no accepted websocket bootstrap responses recorded")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--websockets", type=int, default=2)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="write the report but exit 0 even if stress acceptance fails; use only for baseline reproduction",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failures = acceptance_failures(report, allow_failures=args.allow_failures)
    if failures:
        print("stress acceptance failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
