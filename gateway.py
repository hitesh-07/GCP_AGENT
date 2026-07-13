from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from flask import Flask, jsonify, request


UTC = timezone.utc
LOGGER = logging.getLogger("gemini_gateway")
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
TASK_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
ALLOWED_GENERATION_CONFIG = {
    "candidate_count",
    "frequency_penalty",
    "max_output_tokens",
    "presence_penalty",
    "response_mime_type",
    "seed",
    "stop_sequences",
    "temperature",
    "top_k",
    "top_p",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Settings:
    project_id: str
    tasks_location: str
    tasks_queue: str
    worker_url: str
    tasks_service_account: str
    firestore_collection: str = "gemini_gateway_jobs"
    gemini_backend: str = "vertex"
    gemini_location: str = "global"
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key: str | None = None
    gemini_timeout_seconds: int = 300
    task_max_attempts: int = 5
    max_prompt_chars: int = 200_000
    job_ttl_hours: int = 24
    require_cloud_tasks_header: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        project_id = first_env("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "PROJECT_ID")
        tasks_location = first_env("TASKS_LOCATION", "LOCATION_ID")
        tasks_queue = first_env("TASKS_QUEUE", "QUEUE_ID")
        worker_url = first_env("WORKER_URL", "TARGET_URL")
        service_account = first_env("TASKS_SERVICE_ACCOUNT", "SERVICE_ACCOUNT_EMAIL")
        missing = [
            name
            for name, value in {
                "GOOGLE_CLOUD_PROJECT": project_id,
                "TASKS_LOCATION": tasks_location,
                "TASKS_QUEUE": tasks_queue,
                "WORKER_URL": worker_url,
                "TASKS_SERVICE_ACCOUNT": service_account,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

        backend = os.getenv("GEMINI_BACKEND", "vertex").lower()
        if backend not in {"vertex", "developer"}:
            raise RuntimeError("GEMINI_BACKEND must be either 'vertex' or 'developer'")
        api_key = os.getenv("GEMINI_API_KEY")
        if backend == "developer" and not api_key:
            raise RuntimeError("GEMINI_API_KEY is required for GEMINI_BACKEND=developer")

        return cls(
            project_id=project_id,
            tasks_location=tasks_location,
            tasks_queue=tasks_queue,
            worker_url=worker_url,
            tasks_service_account=service_account,
            firestore_collection=os.getenv("FIRESTORE_COLLECTION", "gemini_gateway_jobs"),
            gemini_backend=backend,
            gemini_location=os.getenv("GOOGLE_CLOUD_LOCATION", os.getenv("GEMINI_LOCATION", "global")),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_api_key=api_key,
            gemini_timeout_seconds=env_int("GEMINI_TIMEOUT_SECONDS", 300, 1, 1800),
            task_max_attempts=env_int("TASK_MAX_ATTEMPTS", 5, 1, 100),
            max_prompt_chars=env_int("MAX_PROMPT_CHARS", 200_000, 1, 2_000_000),
            job_ttl_hours=env_int("JOB_TTL_HOURS", 24, 1, 24 * 365),
            require_cloud_tasks_header=env_bool("REQUIRE_CLOUD_TASKS_HEADER", True),
        )


def first_env(*names: str) -> str:
    return next((os.environ[name] for name in names if os.environ.get(name)), "")


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


class JobStore(Protocol):
    def create(self, job_id: str, data: dict[str, Any]) -> bool: ...

    def get(self, job_id: str) -> dict[str, Any] | None: ...

    def update(self, job_id: str, values: dict[str, Any]) -> None: ...

    def claim(self, job_id: str, lease_seconds: int) -> str: ...


class TaskQueue(Protocol):
    def enqueue(self, job_id: str) -> str: ...


class ModelClient(Protocol):
    def generate(self, job: dict[str, Any]) -> dict[str, Any]: ...


class FirestoreJobStore:
    def __init__(self, project_id: str, collection: str):
        from google.cloud import firestore

        self.client = firestore.Client(project=project_id)
        self.collection = self.client.collection(collection)

    def create(self, job_id: str, data: dict[str, Any]) -> bool:
        from google.api_core.exceptions import AlreadyExists

        try:
            self.collection.document(job_id).create(data)
            return True
        except AlreadyExists:
            return False

    def get(self, job_id: str) -> dict[str, Any] | None:
        snapshot = self.collection.document(job_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def update(self, job_id: str, values: dict[str, Any]) -> None:
        self.collection.document(job_id).update(values)

    def claim(self, job_id: str, lease_seconds: int) -> str:
        from google.cloud import firestore

        document = self.collection.document(job_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def claim_in_transaction(txn):
            snapshot = document.get(transaction=txn)
            if not snapshot.exists:
                return "missing"
            job = snapshot.to_dict()
            if job.get("status") in {"completed", "failed"}:
                return "finished"
            lease_until = job.get("lease_until")
            if job.get("status") == "processing" and lease_until and lease_until > utcnow():
                return "busy"
            txn.update(
                document,
                {
                    "status": "processing",
                    "started_at": utcnow(),
                    "updated_at": utcnow(),
                    "lease_until": utcnow() + timedelta(seconds=lease_seconds),
                    "attempts": firestore.Increment(1),
                },
            )
            return "claimed"

        return claim_in_transaction(transaction)


class CloudTasksQueue:
    def __init__(self, settings: Settings):
        from google.cloud import tasks_v2

        self.tasks_v2 = tasks_v2
        self.client = tasks_v2.CloudTasksClient()
        self.parent = self.client.queue_path(
            settings.project_id, settings.tasks_location, settings.tasks_queue
        )
        self.worker_url = settings.worker_url
        parsed_url = urlsplit(settings.worker_url)
        self.oidc_audience = f"{parsed_url.scheme}://{parsed_url.netloc}"
        self.service_account = settings.tasks_service_account
        self.timeout_seconds = settings.gemini_timeout_seconds

    def enqueue(self, job_id: str) -> str:
        from google.api_core.exceptions import AlreadyExists
        from google.protobuf import duration_pb2

        task_id = TASK_ID_RE.sub("-", job_id)[:500]
        task_name = f"{self.parent}/tasks/{task_id}"
        task = {
            "name": task_name,
            "http_request": {
                "http_method": self.tasks_v2.HttpMethod.POST,
                "url": self.worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"job_id": job_id}).encode("utf-8"),
                "oidc_token": {
                    "service_account_email": self.service_account,
                    "audience": self.oidc_audience,
                },
            },
            "dispatch_deadline": duration_pb2.Duration(seconds=self.timeout_seconds),
        }
        try:
            created = self.client.create_task(parent=self.parent, task=task)
            return created.name
        except AlreadyExists:
            return task_name


class GoogleGenAIClient:
    def __init__(self, settings: Settings):
        from google import genai

        if settings.gemini_backend == "vertex":
            self.client = genai.Client(
                vertexai=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    def generate(self, job: dict[str, Any]) -> dict[str, Any]:
        from google.genai import types

        config_values = dict(job.get("generation_config") or {})
        if job.get("system_instruction"):
            config_values["system_instruction"] = job["system_instruction"]
        response = self.client.models.generate_content(
            model=job["model"],
            contents=job["prompt"],
            config=types.GenerateContentConfig(**config_values),
        )
        usage = response.usage_metadata
        usage_data = usage.model_dump(mode="json", exclude_none=True) if usage else None
        return {"text": response.text, "usage": usage_data, "model": job["model"]}


def create_app(
    settings: Settings | None = None,
    store: JobStore | None = None,
    task_queue: TaskQueue | None = None,
    model_client: ModelClient | None = None,
) -> Flask:
    settings = settings or Settings.from_env()
    store = store or FirestoreJobStore(settings.project_id, settings.firestore_collection)
    task_queue = task_queue or CloudTasksQueue(settings)
    model_client = model_client or GoogleGenAIClient(settings)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    @app.post("/v1/requests")
    @app.post("/enqueue")
    def enqueue_request():
        payload = request.get_json(silent=True)
        validation_error = validate_request(payload, settings)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        idempotency_key = request.headers.get("Idempotency-Key")
        job_id = make_job_id(idempotency_key)
        now = utcnow()
        job = {
            "id": job_id,
            "status": "enqueuing",
            "prompt": payload["prompt"],
            "model": payload.get("model") or settings.gemini_model,
            "system_instruction": payload.get("system_instruction"),
            "generation_config": payload.get("generation_config") or {},
            "metadata": payload.get("metadata") or {},
            "created_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(hours=settings.job_ttl_hours),
            "attempts": 0,
        }
        created = store.create(job_id, job)
        existing = None if created else store.get(job_id)
        should_enqueue = created or (
            existing and existing.get("status") in {"enqueuing", "enqueue_failed"}
        )

        if should_enqueue:
            try:
                task_name = task_queue.enqueue(job_id)
                store.update(job_id, {"status": "queued", "task_name": task_name, "updated_at": utcnow()})
                log_event("job_queued", job_id=job_id, model=job["model"])
            except Exception as exc:
                store.update(
                    job_id,
                    {"status": "enqueue_failed", "error": safe_error(exc), "updated_at": utcnow()},
                )
                log_event("job_enqueue_failed", job_id=job_id, error_type=type(exc).__name__)
                return jsonify({"error": "Queue is temporarily unavailable", "job_id": job_id}), 503

        current = store.get(job_id) or job
        return jsonify(public_job(current)), 202

    @app.get("/v1/requests/<job_id>")
    def get_request(job_id: str):
        job = store.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(public_job(job))

    @app.post("/internal/tasks/generate")
    @app.post("/process")
    def process_request():
        if settings.require_cloud_tasks_header and not request.headers.get("X-CloudTasks-TaskName"):
            return jsonify({"error": "Cloud Tasks request required"}), 403
        payload = request.get_json(silent=True) or {}
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return jsonify({"error": "Task must contain job_id"}), 400

        claim = store.claim(job_id, settings.gemini_timeout_seconds + 30)
        if claim in {"missing", "finished"}:
            return jsonify({"status": claim}), 200
        if claim == "busy":
            return jsonify({"status": "already_processing"}), 409

        job = store.get(job_id)
        log_event("job_processing", job_id=job_id, model=job["model"])
        try:
            result = model_client.generate(job)
        except Exception as exc:
            error = safe_error(exc)
            if is_transient(exc):
                retry_count = header_int("X-CloudTasks-TaskRetryCount", 0)
                final_attempt = retry_count + 1 >= settings.task_max_attempts
                store.update(
                    job_id,
                    {
                        "status": "failed" if final_attempt else "retrying",
                        "error": error,
                        "updated_at": utcnow(),
                        "lease_until": utcnow(),
                    },
                )
                if not final_attempt:
                    log_event(
                        "job_retrying",
                        job_id=job_id,
                        retry_count=retry_count,
                        error_type=type(exc).__name__,
                    )
                    return jsonify({"error": "Transient Gemini failure"}), 503
            else:
                store.update(
                    job_id,
                    {"status": "failed", "error": error, "updated_at": utcnow(), "lease_until": utcnow()},
                )
            log_event("job_failed", job_id=job_id, error_type=type(exc).__name__)
            return jsonify({"status": "failed"}), 200

        store.update(
            job_id,
            {
                "status": "completed",
                "result": result,
                "completed_at": utcnow(),
                "updated_at": utcnow(),
                "lease_until": utcnow(),
            },
        )
        log_event("job_completed", job_id=job_id, model=job["model"])
        return jsonify({"status": "completed", "job_id": job_id})

    @app.get("/health")
    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


def validate_request(payload: Any, settings: Settings) -> str | None:
    if not isinstance(payload, dict):
        return "Request body must be a JSON object"
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if len(prompt) > settings.max_prompt_chars:
        return f"prompt exceeds the {settings.max_prompt_chars} character limit"
    if "model" in payload and (not isinstance(payload["model"], str) or not payload["model"]):
        return "model must be a non-empty string"
    if "system_instruction" in payload and not isinstance(payload["system_instruction"], str):
        return "system_instruction must be a string"
    if "metadata" in payload and not isinstance(payload["metadata"], dict):
        return "metadata must be an object"
    generation_config = payload.get("generation_config", {})
    if not isinstance(generation_config, dict):
        return "generation_config must be an object"
    unsupported = sorted(set(generation_config) - ALLOWED_GENERATION_CONFIG)
    if unsupported:
        return "Unsupported generation_config fields: " + ", ".join(unsupported)
    return None


def make_job_id(idempotency_key: str | None) -> str:
    if not idempotency_key:
        return uuid.uuid4().hex
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"idem-{digest}"


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    visible = {"id", "status", "model", "metadata", "created_at", "updated_at", "completed_at", "result", "error"}
    return {key: json_safe(value) for key, value in job.items() if key in visible and value is not None}


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def safe_error(exc: Exception) -> dict[str, Any]:
    status = error_status(exc)
    return {
        "type": type(exc).__name__,
        "message": str(exc)[:1000],
        **({"status_code": status} if status else {}),
    }


def error_status(exc: Exception) -> int | None:
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        value = value() if callable(value) else value
        if isinstance(value, int):
            return value
        numeric = getattr(value, "value", None)
        if isinstance(numeric, tuple) and numeric and isinstance(numeric[0], int):
            return numeric[0]
        if isinstance(numeric, int):
            return numeric
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_transient(exc: Exception) -> bool:
    status = error_status(exc)
    if status is not None:
        return status in {408, 409, 429, 500, 502, 503, 504}
    return isinstance(exc, (TimeoutError, ConnectionError))


def header_int(name: str, default: int) -> int:
    try:
        return int(request.headers.get(name, str(default)))
    except ValueError:
        return default


def log_event(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, separators=(",", ":")))
