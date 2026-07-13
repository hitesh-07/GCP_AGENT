# Gemini Request Gateway

This project puts a durable queue between your services and Gemini. Instead of every service calling Gemini at the same time, they submit work to one gateway. The gateway controls how quickly requests reach Gemini and gives each caller a job ID for retrieving the result.

## Start here: what each GCP component does

| Component | Responsibility in this project |
|---|---|
| **Cloud Run Service** | Hosts the HTTP API and executes queued Gemini requests |
| **Cloud Tasks** | Holds job IDs and controls dispatch rate, concurrency, retries, and backoff |
| **Firestore** | Stores prompts, job statuses, attempts, errors, and Gemini results |
| **Vertex AI Gemini** | Generates the model response |

A **Cloud Run Job is not used**. Cloud Run Jobs are run-to-completion batch programs. This project needs an HTTP service that remains available for submissions, task callbacks, and status checks, so it uses a Cloud Run Service.

## End-to-end architecture

```text
                           First HTTP request
Client service ───────────────────────────────────────────────┐
                                                              │
                                             POST /v1/requests│
                                                              ▼
                                                ┌────────────────────────┐
                                                │   Cloud Run Service    │
                                                │    Gemini Gateway      │
                                                └──────┬──────────┬──────┘
                                                       │          │
                                      store prompt/job │          │ enqueue job ID
                                                       ▼          ▼
                                                ┌───────────┐  ┌─────────────┐
                                                │ Firestore │  │ Cloud Tasks │
                                                └─────▲─────┘  └──────┬──────┘
                                                      │               │
                                                      │               │ rate-limited dispatch
                                                      │               │
                                                      │               ▼
                                                      │  POST /internal/tasks/generate
                                                      │      ┌────────────────────────┐
                                                      │      │   Cloud Run Service    │
                                                      │      │  same deployed service │
                                                      │      └───────────┬────────────┘
                                                      │                  │
                                                      │                  ▼
                                                      │          ┌──────────────┐
                                                      │          │ Gemini model │
                                                      │          └──────┬───────┘
                                                      │                 │
                                                      └─────────────────┘
                                                         store result/status

Client service ── GET /v1/requests/{job_id} ──> Cloud Run ──> Firestore
```

The two Cloud Run boxes are the **same Cloud Run Service**. They represent two separate HTTP requests and may be handled by different container instances.

## A request, step by step

### 1. The client submits a prompt

The caller sends an authenticated request to:

```http
POST /v1/requests
```

Example body:

```json
{
  "prompt": "Summarize this order history.",
  "model": "gemini-2.5-flash",
  "generation_config": {
    "temperature": 0.2,
    "max_output_tokens": 500
  },
  "metadata": {
    "caller": "orders-service"
  }
}
```

### 2. Cloud Run creates a Firestore job

The gateway validates the request, generates a job ID, and writes a document similar to:

```json
{
  "id": "abc123",
  "status": "enqueuing",
  "prompt": "Summarize this order history.",
  "model": "gemini-2.5-flash",
  "attempts": 0
}
```

The full prompt is stored in Firestore. It is deliberately not returned by the status API.

### 3. Cloud Run creates a Cloud Task

The Cloud Task contains only the job ID:

```json
{
  "job_id": "abc123"
}
```

Cloud Tasks is a queue and dispatcher. It does not call Gemini directly and it does not store the final result.

### 4. The client immediately receives the job ID

After the task is accepted, Firestore changes to `queued` and the API returns `202 Accepted`:

```json
{
  "id": "abc123",
  "status": "queued",
  "model": "gemini-2.5-flash"
}
```

The original client connection can now close. Processing continues even if the client restarts or disconnects.

### 5. Cloud Tasks waits for capacity

The learning deployment currently allows:

```text
Maximum dispatch rate: 1 task per second
Maximum concurrency:   2 tasks
Maximum attempts:      5
```

If ten services submit work at once, their jobs remain queued and are released at this controlled rate.

### 6. Cloud Tasks calls the private worker endpoint

Cloud Tasks authenticates as the dispatcher service account and sends:

```http
POST /internal/tasks/generate

{
  "job_id": "abc123"
}
```

This endpoint is part of the same Cloud Run Service as the submission API.

### 7. The worker claims the job and calls Gemini

The worker uses a Firestore transaction and processing lease so duplicate task deliveries do not normally execute simultaneously. It then:

1. Changes the job status to `processing`.
2. Reads the prompt and model configuration from Firestore.
3. Calls Gemini through the Google Gen AI SDK.
4. Records token usage when Gemini supplies it.

### 8. The worker stores the outcome

On success, Firestore contains a result similar to:

```json
{
  "id": "abc123",
  "status": "completed",
  "result": {
    "model": "gemini-2.5-flash",
    "text": "Here is the order history summary...",
    "usage": {
      "prompt_token_count": 120,
      "candidates_token_count": 60,
      "total_token_count": 180
    }
  }
}
```

Transient failures such as Gemini `429` or `503` responses change the status to `retrying`. Cloud Tasks retries them using exponential backoff. Permanent request errors are recorded as `failed` and are not repeatedly sent to Gemini.

### 9. The client retrieves the result

The client polls:

```http
GET /v1/requests/abc123
```

It stops when the job becomes `completed`, `failed`, or `enqueue_failed`.

## Job status lifecycle

```text
enqueuing ──> queued ──> processing ──> completed
                 ▲             │
                 │             └──────> retrying ──> processing
                 │
                 └── enqueue_failed

processing/retrying ──> failed
```

| Status | Meaning | What the client should do |
|---|---|---|
| `enqueuing` | Firestore document exists; task creation is in progress | Poll again |
| `queued` | Cloud Tasks accepted the job | Poll again |
| `processing` | A worker is calling Gemini | Poll again |
| `retrying` | A transient error occurred and Cloud Tasks will retry | Poll again |
| `completed` | Gemini result is stored | Read `result` and stop |
| `failed` | Processing permanently failed | Read `error` and stop |
| `enqueue_failed` | The gateway could not add the task | Retry submission with the same idempotency key |

## Try your deployed environment

Fill in the values from your Cloud Run deployment:

```text
Project: YOUR_PROJECT_ID
Region:  YOUR_REGION
Service: YOUR_SERVICE_NAME
URL:     YOUR_CLOUD_RUN_URL
Queue:   YOUR_QUEUE_NAME
```

Your account or calling service account needs `roles/run.invoker` on the Cloud Run service.

Set the URL and submit a request:

```bash
export GATEWAY_URL="YOUR_CLOUD_RUN_URL"
export DISPATCHER_SA="gemini-gateway-dispatcher@YOUR_PROJECT_ID.iam.gserviceaccount.com"
export ID_TOKEN="$(gcloud auth print-identity-token \
  --impersonate-service-account="$DISPATCHER_SA" \
  --audiences="$GATEWAY_URL" \
  --include-email)"

curl -X POST "$GATEWAY_URL/v1/requests" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: orders-123-summary-v1" \
  -d '{
    "prompt": "Explain why request queues are useful.",
    "generation_config": {
      "temperature": 0.2,
      "max_output_tokens": 300
    },
    "metadata": {
      "caller": "manual-demo"
    }
  }'
```

Copy the returned `id`, then retrieve its status:

```bash
curl "$GATEWAY_URL/v1/requests/JOB_ID" \
  -H "Authorization: Bearer $ID_TOKEN"
```

Use a stable `Idempotency-Key` when a caller may retry the submission. Repeating the same key returns the existing job instead of creating another Gemini request. A key should therefore identify one logical request and should not be reused for different prompts.

## End-to-end demo notebook

For the easiest demonstration, open [`notebook/gemini_gateway_demo.ipynb`](notebook/gemini_gateway_demo.ipynb) in Jupyter or VS Code and run its cells from top to bottom.

The notebook:

1. Authenticates to the private Cloud Run service.
2. Checks the health endpoint.
3. Submits a prompt and receives a job ID.
4. Polls through the queue and processing states.
5. Displays the Gemini response and token usage.
6. Retrieves the stored result again.

Before running it locally:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

The optional five-request batch demonstration is disabled by default because it creates additional billable Gemini requests.

## API reference

| Method | Path | Called by | Purpose |
|---|---|---|---|
| `POST` | `/v1/requests` | Client services | Validate, persist, and enqueue a request |
| `GET` | `/v1/requests/{job_id}` | Client services | Retrieve current status, result, or error |
| `POST` | `/internal/tasks/generate` | Cloud Tasks only | Execute the queued Gemini request |
| `GET` | `/health` | Monitoring or operators | Confirm that the service is running |

Legacy aliases `/enqueue` and `/process` remain available for the original prototype's callers.

## Failure and retry behavior

| Situation | Gateway behavior |
|---|---|
| Invalid client JSON or generation configuration | Returns `400`; nothing is queued |
| Cloud Tasks unavailable during submission | Returns `503` and records `enqueue_failed` |
| Gemini `429`, timeout, or most `5xx` responses | Records `retrying`; returns `503` to Cloud Tasks so it retries |
| Final transient attempt is exhausted | Records `failed`; acknowledges the task |
| Permanent Gemini request error such as `400` | Records `failed`; acknowledges the task without retrying |
| Duplicate client submission with the same idempotency key | Returns the existing job |
| Duplicate Cloud Tasks delivery | Firestore transaction and lease prevent simultaneous claims |

Cloud Tasks provides at-least-once delivery. If a worker successfully calls Gemini but crashes before saving the response, a later retry can call Gemini again. Idempotency and leases greatly reduce duplicates, but they cannot provide exactly-once execution across an external model call and a database write.

## Security model

- The Cloud Run service is private; unauthenticated internet requests are rejected by Cloud Run.
- Client service accounts need `roles/run.invoker`.
- Cloud Tasks uses `gemini-gateway-dispatcher` and an OIDC token to invoke the worker endpoint.
- The gateway runtime service account can write Firestore documents, enqueue tasks, and call Vertex AI.
- Prompts are stored in Firestore but omitted from status API responses and lifecycle logs.
- Checking `X-CloudTasks-*` headers is defense-in-depth, not a replacement for Cloud Run IAM authentication.

## Source-code map

| File | Purpose |
|---|---|
| `main.py` | Gunicorn entry point that creates the Flask application |
| `gateway.py` | API routes, Firestore store, Cloud Tasks client, Gemini client, retries, and validation |
| `Dockerfile` | Cloud Run container definition |
| `cloud_tasks_setup.sh` | Creates or updates the dispatcher account and queue limits |
| `requirements.txt` | Runtime Python dependencies |
| `requirements-dev.txt` | Test dependencies |
| `tests/test_gateway.py` | In-memory API, idempotency, worker, and retry tests |
| `notebook/gemini_gateway_demo.ipynb` | Runnable end-to-end demonstration |

## Configuration

### Required environment variables

| Variable | Description |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | Project containing Cloud Tasks, Firestore, and Vertex AI access |
| `TASKS_LOCATION` | Cloud Tasks region, such as `us-east1` |
| `TASKS_QUEUE` | Queue name |
| `WORKER_URL` | Full handler URL ending in `/internal/tasks/generate` |
| `TASKS_SERVICE_ACCOUNT` | Service account used for the task's OIDC identity |

### Optional environment variables

| Variable | Default | Description |
|---|---:|---|
| `GEMINI_BACKEND` | `vertex` | `vertex` or `developer` |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI endpoint location |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Default model ID |
| `GEMINI_API_KEY` | — | Required only for `GEMINI_BACKEND=developer` |
| `FIRESTORE_COLLECTION` | `gemini_gateway_jobs` | Firestore collection containing jobs |
| `GEMINI_TIMEOUT_SECONDS` | `300` | Gemini and Cloud Tasks dispatch deadline |
| `TASK_MAX_ATTEMPTS` | `5` | Must match the queue's maximum attempts |
| `MAX_PROMPT_CHARS` | `200000` | Admission limit applied before enqueueing |
| `JOB_TTL_HOURS` | `24` | Value written into each job's `expires_at` field |
| `REQUIRE_CLOUD_TASKS_HEADER` | `true` | Reject worker calls that lack Cloud Tasks headers |

Old prototype names (`PROJECT_ID`, `LOCATION_ID`, `QUEUE_ID`, `TARGET_URL`, and `SERVICE_ACCOUNT_EMAIL`) are accepted as fallbacks.

## Deploying to another GCP project

1. Create a Firestore Native mode database in the chosen region.
2. Enable Cloud Run, Cloud Build, Artifact Registry, Cloud Tasks, Firestore, IAM, and Vertex AI APIs.
3. Create separate runtime and Cloud Tasks dispatcher service accounts.
4. Grant the runtime account:
   - `roles/cloudtasks.enqueuer`
   - `roles/datastore.user`
   - `roles/aiplatform.user` when using Vertex AI
   - `roles/iam.serviceAccountUser` on the dispatcher account
5. Deploy the container to Cloud Run using the runtime service account.
6. Grant the dispatcher account `roles/run.invoker` on the Cloud Run service.
7. Run `cloud_tasks_setup.sh` to create or update the rate-limited queue.
8. Grant each client service account `roles/run.invoker`.
9. Keep `TASK_MAX_ATTEMPTS` equal to the queue's `--max-attempts` value.

Set the Cloud Run request timeout above `GEMINI_TIMEOUT_SECONDS`.

## Choosing queue limits

Start conservatively and observe queue wait time, Gemini p95 latency, and `429` responses before increasing capacity:

```bash
./cloud_tasks_setup.sh PROJECT_ID REGION QUEUE_ID DISPATCHER_SA_NAME \
  2 4 5
```

The final arguments are dispatches per second, concurrent dispatches, and maximum attempts. For token-heavy production workloads, use separate queues and gateway deployments for interactive and batch traffic so large batch prompts cannot block latency-sensitive work.

## Firestore expiration

Every job receives an `expires_at` timestamp. Enable Firestore TTL so expired jobs are deleted automatically:

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=gemini_gateway_jobs \
  --enable-ttl
```

TTL deletion is asynchronous and is intended for cleanup, not immediate deletion at the exact expiration time.

## Local tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The tests use in-memory fakes and do not call GCP or Gemini.

The service also emits prompt-free JSON lifecycle events—`job_queued`, `job_processing`, `job_retrying`, `job_completed`, and `job_failed`—to standard output for Cloud Logging.
