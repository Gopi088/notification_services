# 17 — Eval Specification

## 17.1 Purpose

Evals are **not** unit tests. Evals measure complete system behavior against
expected outcomes, using deterministic graders. They exercise real flows:
API → queue → worker → provider (mocked) → DB, plus concurrency, duplicates,
scheduling, authorization, security, audit, logging, and regression.

## 17.2 Eval Structure

Each eval contains:

| Field | Description |
| ----- | ----------- |
| `eval_id` | unique id, e.g. `EVAL-DUPLICATE-001` |
| `version` | eval schema version |
| `category` | one of the categories below |
| `scenario` | human description |
| `input` | request/inputs to run |
| `preconditions` | required state (providers mocked, DB empty, etc.) |
| `expected_behavior` | observable expectations |
| `evidence` | what is collected (status, DB rows, queue, provider call count, audit, logs) |
| `graders` | deterministic checks mapping evidence to pass/fail |
| `critical` | true/false — critical evals must pass 100% |
| `pass_criteria` | threshold (all graders pass, etc.) |

## 17.3 Categories

```
functional          multi_user         concurrency
duplicate_prevention idempotency       retry
queue               worker             provider
scheduling          quiet_hours        authorization
security            audit              logging
rate_limit          failure_recovery   performance
docker              regression         end_to_end
```

## 17.4 Graders (deterministic)

- HTTP status grader
- Database state grader
- Queue event grader
- Provider-call-count grader
- Notification-state grader
- Audit grader
- Log-event grader
- Security grader (secret absence)
- Latency grader
- Concurrency grader (no lost/duplicate/ownership corruption)

**No LLM judge** for deterministic behavior (e.g. `status == submitted`,
`attempt_count == 2`, `provider_call_count == 1`, `audit_record_exists == true`).

## 17.5 Critical Evals

Critical eval categories (must pass 100%):

```
SMS regression
WhatsApp regression
Email regression
duplicate prevention
idempotency
notification persistence
queue reliability
authorization
secret leakage
audit integrity
```

Overall eval target: **>= 95%** pass rate across all evals.
Critical eval target: **100%**.

## 17.6 Example: EVAL-DUPLICATE-001

Scenario: user sends; provider is slow; user submits the same request again.

Expected:

- first request = accepted (202)
- second request = `already_processing` / replay (202 with `X-Idempotent-Replay: true`)
- provider call count = 1
- effective delivery count = 1
- audit records correct
- logs correct

## 17.7 Example: EVAL-CONCURRENCY-001

Scenario: 100 users submit notifications simultaneously.

Expected:

- all valid requests accepted
- no ownership corruption
- no unintended duplicates
- queue receives correct messages
- workers process correctly
- database remains consistent

## 17.8 Example: EVAL-QUIET-HOURS-001

Scenario: request at 23:30; allowed window 09:00–21:00.

Expected:

- notification NOT sent at 23:30
- scheduled for next allowed window
- user receives correct status (`scheduled`)
- audit record created
- logs explain deferral

## 17.9 Example: EVAL-PROVIDER-DOWN-001

Scenario: WhatsApp provider unavailable.

Expected:

- notification persisted
- retry scheduled
- exponential backoff
- no infinite retry
- eventually `failed`/`dead_lettered` when max retries exhausted
- audit record exists
- logs exist

## 17.10 Regression Discipline

Whenever an eval discovers a real bug:

1. Fix the bug.
2. Add a regression test.
3. Add/update the eval.
4. Keep it permanently in the regression suite.

Never remove a regression eval merely because it currently passes.

## 17.11 Eval Files

Evals live in `docs/evals/` as YAML:

```
functional.yaml      concurrency.yaml     reliability.yaml
idempotency.yaml     retry.yaml           scheduling.yaml
authorization.yaml   security.yaml        observability.yaml
regression.yaml      performance.yaml
```

Each file lists evals of that category with the full structure above.

## 17.12 Runner

A deterministic eval runner (implemented later) loads these YAML files,
executes the scenario against the running system (providers mocked),
collects evidence, applies graders, and reports pass/fail per eval plus
category and overall scores. The 100% critical / 95% overall gates are
enforced in CI.
