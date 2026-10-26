---
title: "API authentication and errors"
---

The operator API is served by the local control plane at `http://localhost:3400/api/v1`. The documentation website is static and makes no requests to it. Interactive Swagger and ReDoc pages are disabled by the backend.

## Administrator session

`POST /auth/login` takes `{ "password": "..." }`, sets the HttpOnly `factory_session` cookie, and returns `{ "csrf": "...", "role": "administrator" }`. Sessions expire after 28,800 seconds. Cookies use SameSite Strict; Secure follows the deployment setting. Eight failed login attempts from a host within a minute cause a 429 response.

Send the cookie on authenticated requests. Mutations require the returned token in `X-CSRF-Token`. Retrieve it from `GET /auth/session` in an existing session. `POST /auth/logout` requires cookie and CSRF, deletes the session and clears the cookie. A supplied Origin on mutations must match the configured public origin (ingestion is exempt from that origin check).

## Worker authentication

`GET /worker-bootstrap` and `POST /ingest` use a worker-scoped `Authorization: Bearer ...` token with `X-Worker-Id`. Do not substitute administrator credentials. Bootstrap returns the applicable resolved snapshot; ingestion accepts bounded, validated events. The public health route is the other unauthenticated exception.

## Errors

| Status | Meaning |
| --- | --- |
| 401 | Missing/expired session, invalid login or invalid worker credentials |
| 403 | Invalid CSRF token or disallowed supplied Origin |
| 404 | Unknown resource or conversation absent/expired |
| 409 | Stale edit, duplicate name, incompatible state or idempotency conflict |
| 413 | Oversized action/ingestion payload |
| 422 | Request/configuration validation failure or unsupported action |
| 429 | Login attempt throttle |
| 503 | Unavailable integration; inspect server/connectivity configuration |

Middleware 401/403 responses can have empty bodies. Validation failures use a generic `detail` message; ValueErrors are redacted. Some list routes return warnings with empty items when Docker/Hatchet is unavailable, so HTTP 200 does not imply healthy integrations.

[All endpoint contracts](/flumic-software-factory/reference/api/) · [Request recipes](/flumic-software-factory/api/recipes/)

## GitLab webhook authentication (qualification pending)

`POST /api/v1/hooks/gitlab/{binding_id}` authenticates using the binding's `X-Gitlab-Token`, not the administrator cookie or CSRF token. Create or rotate the token through the project-owned operator APIs; plaintext is returned once. The receiver bounds the body to 1 MiB and rechecks token/configuration versions before acceptance. Prefer GitLab's stable `webhook-id` (or legacy `Idempotency-Key`) for delivery deduplication; absent those headers, the receiver uses a content digest. Recursive event correlation IDs do not select a workflow. See [GitLab setup](../../handbook/gitlab/) for configuration, current limitations and release status.
