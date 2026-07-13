from copy import deepcopy

import pytest

from gateway import Settings, create_app


class MemoryStore:
    def __init__(self):
        self.jobs = {}

    def create(self, job_id, data):
        if job_id in self.jobs:
            return False
        self.jobs[job_id] = deepcopy(data)
        return True

    def get(self, job_id):
        value = self.jobs.get(job_id)
        return deepcopy(value) if value else None

    def update(self, job_id, values):
        self.jobs[job_id].update(deepcopy(values))

    def claim(self, job_id, lease_seconds):
        job = self.jobs.get(job_id)
        if not job:
            return "missing"
        if job["status"] in {"completed", "failed"}:
            return "finished"
        if job["status"] == "processing":
            return "busy"
        job["status"] = "processing"
        job["attempts"] += 1
        return "claimed"


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, job_id):
        self.jobs.append(job_id)
        return f"queues/test/tasks/{job_id}"


class FakeModel:
    def __init__(self, response=None, error=None):
        self.response = response or {"text": "hello", "model": "test", "usage": None}
        self.error = error

    def generate(self, job):
        if self.error:
            raise self.error
        return self.response


class ApiError(Exception):
    def __init__(self, status_code):
        super().__init__(f"API returned {status_code}")
        self.status_code = status_code


@pytest.fixture
def settings():
    return Settings(
        project_id="test-project",
        tasks_location="us-central1",
        tasks_queue="test-queue",
        worker_url="https://worker/internal/tasks/generate",
        tasks_service_account="tasks@test-project.iam.gserviceaccount.com",
        gemini_model="test-model",
        task_max_attempts=3,
    )


def build_client(settings, model=None):
    store = MemoryStore()
    queue = FakeQueue()
    app = create_app(settings, store, queue, model or FakeModel())
    return app.test_client(), store, queue


def test_enqueue_and_complete_job(settings):
    client, store, queue = build_client(settings)
    response = client.post(
        "/v1/requests",
        json={"prompt": "Say hello", "metadata": {"service": "orders"}},
    )
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    assert queue.jobs == [job_id]

    processed = client.post(
        "/internal/tasks/generate",
        json={"job_id": job_id},
        headers={"X-CloudTasks-TaskName": "task-1"},
    )
    assert processed.status_code == 200

    result = client.get(f"/v1/requests/{job_id}").get_json()
    assert result["status"] == "completed"
    assert result["result"]["text"] == "hello"
    assert "prompt" not in result
    assert store.jobs[job_id]["attempts"] == 1


def test_idempotency_key_only_enqueues_once(settings):
    client, _, queue = build_client(settings)
    headers = {"Idempotency-Key": "order-123"}
    first = client.post("/v1/requests", json={"prompt": "hello"}, headers=headers)
    second = client.post("/v1/requests", json={"prompt": "hello"}, headers=headers)
    assert first.get_json()["id"] == second.get_json()["id"]
    assert len(queue.jobs) == 1


def test_worker_requires_cloud_tasks_header(settings):
    client, _, _ = build_client(settings)
    response = client.post("/internal/tasks/generate", json={"job_id": "anything"})
    assert response.status_code == 403


def test_transient_error_is_retried(settings):
    client, store, _ = build_client(settings, FakeModel(error=ApiError(429)))
    created = client.post("/v1/requests", json={"prompt": "hello"}).get_json()
    response = client.post(
        "/internal/tasks/generate",
        json={"job_id": created["id"]},
        headers={"X-CloudTasks-TaskName": "task-1", "X-CloudTasks-TaskRetryCount": "0"},
    )
    assert response.status_code == 503
    assert store.jobs[created["id"]]["status"] == "retrying"


def test_transient_error_becomes_terminal_on_last_attempt(settings):
    client, store, _ = build_client(settings, FakeModel(error=ApiError(503)))
    created = client.post("/v1/requests", json={"prompt": "hello"}).get_json()
    response = client.post(
        "/internal/tasks/generate",
        json={"job_id": created["id"]},
        headers={"X-CloudTasks-TaskName": "task-1", "X-CloudTasks-TaskRetryCount": "2"},
    )
    assert response.status_code == 200
    assert store.jobs[created["id"]]["status"] == "failed"


def test_permanent_error_is_not_retried(settings):
    client, store, _ = build_client(settings, FakeModel(error=ApiError(400)))
    created = client.post("/v1/requests", json={"prompt": "hello"}).get_json()
    response = client.post(
        "/internal/tasks/generate",
        json={"job_id": created["id"]},
        headers={"X-CloudTasks-TaskName": "task-1"},
    )
    assert response.status_code == 200
    assert store.jobs[created["id"]]["status"] == "failed"


def test_rejects_unsupported_generation_config(settings):
    client, _, _ = build_client(settings)
    response = client.post(
        "/v1/requests",
        json={"prompt": "hello", "generation_config": {"unknown": True}},
    )
    assert response.status_code == 400
    assert "Unsupported" in response.get_json()["error"]


def test_health_endpoint(settings):
    client, _, _ = build_client(settings)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
