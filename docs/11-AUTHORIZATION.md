# 11 — Authorization

## 11.1 Authentication vs Authorization

- **Authentication** answers: *who are you?* → API key / token identity.
- **Authorization** answers: *what are you allowed to do?* → scopes + ownership.

## 11.2 Identity

- Every request resolves a caller identity (API key id / user id).
- Every notification stores `created_by` (owner identity) at creation time.
- Requests without auth (dev mode, `AUTH_ENABLED=false`) run as an anonymous
  identity; ownership checks still apply between distinct anonymous sends.

## 11.3 Roles

Minimal role set for this system:

| Role | Description |
| ---- | ----------- |
| `USER` | Default caller; owns its own notifications. |
| `ADMIN` | Full access (all users' notifications, audit, config). |
| `SERVICE` | Machine identity; may send but not read other users' data. |

Roles map to a key's `scopes` (e.g. `user`, `admin`, `service`).

## 11.4 Permission Matrix

| Action | USER | ADMIN | SERVICE |
| ------ | :--: | :---: | :-----: |
| Send notification | YES | YES | YES |
| View own notification | YES | YES | YES |
| View another user's notification | NO | YES | NO |
| Cancel own scheduled notification | YES | YES | NO |
| View own audit records | Own only | YES | NO |
| View all audit records | NO | YES | NO |
| Manage system config | NO | YES | NO |
| Manage keys | NO | YES | NO |

## 11.5 Enforcement Points

1. **API layer**: `require_api_key` authenticates; `require_scope(...)`
   authorizes the action.
2. **Orchestrator**: scopes a send to `send:*` per channel.
3. **Storage/read path**: status/detail lookups filter by `created_by` unless
   the caller is `ADMIN`:
   ```sql
   WHERE id = $1 AND (created_by = $caller OR role = 'admin')
   ```
4. **Audit read**: scoped to own records unless `ADMIN`.

## 11.6 Denial Behavior

- Missing/insufficient scope → `403 forbidden`.
- Accessing another user's notification → `403 forbidden` (not 404, to avoid
  existence leaks; configurable).
- Denials are logged and audited (`authorization_denied`).

## 11.7 Implementation Notes

- Scopes are stored per API key (see `26-AUTH-AUDIT-DESIGN.md`).
- Ownership is enforced in the data layer so no caller can bypass it via a
  different endpoint.
- Tests cover: own access, other-user access (denied), admin access (allowed),
  unauthorized modify/cancel (denied).

## 11.8 Tests & Evals

- [`evals/authorization.yaml`](evals/authorization.yaml)
- [`27-TEST-PLAN.md`](27-TEST-PLAN.md)
