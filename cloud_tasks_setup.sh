#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ] || [ "$#" -gt 7 ]; then
  echo "Usage: $0 PROJECT_ID LOCATION QUEUE_ID SERVICE_ACCOUNT_NAME [DISPATCHES_PER_SECOND] [CONCURRENCY] [MAX_ATTEMPTS]"
  echo "Example: $0 my-project us-central1 gemini-requests cloud-tasks-dispatcher 2 4 5"
  exit 1
fi

PROJECT_ID="$1"
LOCATION="$2"
QUEUE_ID="$3"
SERVICE_ACCOUNT_NAME="$4"
DISPATCH_RATE="${5:-1}"
CONCURRENCY="${6:-2}"
MAX_ATTEMPTS="${7:-5}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable \
  cloudtasks.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  --project="$PROJECT_ID"

if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
    --project="$PROJECT_ID" \
    --display-name="Gemini gateway task dispatcher"
fi

if gcloud tasks queues describe "$QUEUE_ID" --location="$LOCATION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  QUEUE_COMMAND=update
else
  QUEUE_COMMAND=create
fi

gcloud tasks queues "$QUEUE_COMMAND" "$QUEUE_ID" \
  --location="$LOCATION" \
  --max-dispatches-per-second="$DISPATCH_RATE" \
  --max-concurrent-dispatches="$CONCURRENCY" \
  --max-attempts="$MAX_ATTEMPTS" \
  --max-retry-duration=3600s \
  --min-backoff=5s \
  --max-backoff=120s \
  --max-doublings=5 \
  --project="$PROJECT_ID"

echo "Queue: $QUEUE_ID ($DISPATCH_RATE dispatches/s, $CONCURRENCY concurrent, $MAX_ATTEMPTS attempts)"
echo "Dispatcher service account: $SERVICE_ACCOUNT_EMAIL"
echo "Next: grant this account roles/run.invoker on the private Cloud Run gateway."
