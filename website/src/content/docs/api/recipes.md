---
title: "API request recipes"
---

Use these commands from your own machine against your local control plane. Placeholders are intentional. Never put real credentials in published examples or shell history.

## Establish a session

Put a password object in a private local file you control, then post it. This keeps the password out of the command arguments. The returned response contains a CSRF token; the cookie jar is also private.

```sh
umask 077
curl --fail-with-body -c /tmp/factory-cookies.txt \
  -H 'Content-Type: application/json' \
  --data-binary @/path/to/private/login.json \
  http://localhost:3400/api/v1/auth/login
```

Do not check in the request file, cookie jar or response. Copy the returned CSRF value into a local variable for subsequent mutations; the examples below use the literal placeholder `YOUR_CSRF_TOKEN`.

## Read the profile list and workers

```sh
curl --fail-with-body -b /tmp/factory-cookies.txt \
  http://localhost:3400/api/v1/worker-profiles
curl --fail-with-body -b /tmp/factory-cookies.txt \
  http://localhost:3400/api/v1/workers
```

## Create a stopped worker

Select a real profile family ID from your own profile response. Creation can build an image, but Start is separate.

```sh
curl --fail-with-body -b /tmp/factory-cookies.txt \
  -H 'Content-Type: application/json' \
  -H 'X-CSRF-Token: YOUR_CSRF_TOKEN' \
  -H 'Idempotency-Key: create-example-worker-001' \
  --data '{"name":"example-worker","worker_profile_id":"YOUR_PROFILE_FAMILY_ID"}' \
  http://localhost:3400/api/v1/workers
```

## Inspect stage accounting

Use the run/task identifiers from the UI links or API response; the placeholders below are not real IDs.

```sh
curl --fail-with-body -b /tmp/factory-cookies.txt --get \
  --data-urlencode 'run_id=YOUR_RUN_ID' \
  --data-urlencode 'engine_task_id=YOUR_ENGINE_TASK_ID' \
  --data-urlencode 'stage=generate_and_open_draft_pr' \
  http://localhost:3400/api/v1/usage
```

Example response shape (illustrative, not a real measurement):

```json
{"calls":2,"input_tokens":12000,"output_tokens":1800,"estimated_usd":null,"reported_usd":null,"unpriced_calls":2,"missing_usage_calls":0,"limit":10000,"truncated":false,"note":"Known subtotal. Estimated and reported USD are separate; provider invoices may differ."}
```

Use an attempt filter only with an attempt belonging to that task/run. Remove private request files and cookie jars when finished. [Authentication and error behavior](/flumic-software-factory/api/authentication/)
