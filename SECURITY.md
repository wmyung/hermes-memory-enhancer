# Security Policy

Hermes Memory Enhancer can store and retrieve long-lived agent memory. Treat the Memory Enhancer server and database as sensitive infrastructure.

## Safe defaults

The plugin is designed to fail closed for high-risk operations:

- Non-loopback remote endpoints must use HTTPS unless `MEMORY_ENHANCER_ALLOW_INSECURE_REMOTE=true` is explicitly set.
- Non-loopback remote endpoints require `MEMORY_ENHANCER_API_KEY` unless `MEMORY_ENHANCER_ALLOW_UNAUTHENTICATED_REMOTE=true` is explicitly set.
- `memory_enhancer_add_resource` is disabled unless `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true` is explicitly set.
- Local file and directory ingestion requires `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`.
- Obvious credential patterns are redacted from sync payloads and tool outputs when `MEMORY_ENHANCER_REDACT_SECRETS=true` (default).
- Automatic prefetch and synced message payloads are length-bounded.

## Deployment recommendations

- Prefer a loopback endpoint for single-user local deployments:

```bash
MEMORY_ENHANCER_ENDPOINT=http://127.0.0.1:1933
```

- For remote deployment, use HTTPS and an API key:

```bash
MEMORY_ENHANCER_ENDPOINT=https://memory.example.com
MEMORY_ENHANCER_API_KEY=replace-with-a-strong-secret
```

- Do not expose the Memory Enhancer server directly to the public internet without authentication, TLS, rate limiting, and normal web-service hardening.
- Store `.env` files and SQLite databases outside Git.
- Back up the SQLite database if it contains valuable memory.
- Do not store passwords, API keys, private keys, raw PHI/PII, or regulated data unless your own policy, consent, access controls, and retention rules allow it.

## Resource ingestion warnings

`memory_enhancer_add_resource` can ingest URLs, local files, or directories into long-term memory. Enable it only when you understand the data boundary.

If enabled for local uploads, restrict roots narrowly:

```bash
MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true
MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS=/home/user/project-notes:/home/user/public-docs
```

Do not allow broad roots such as:

```bash
/
/home
/home/user
~
```

The plugin blocks common sensitive filenames such as `.env`, private keys, token files, and credential files, but this is a safety net, not a data-loss-prevention system.

## Secret redaction limitations

Secret redaction is best-effort. It reduces accidental leakage of obvious credentials but cannot guarantee removal of every private value, medical datum, legal datum, or proprietary string.

Users remain responsible for deciding what may be sent to the Memory Enhancer server and stored in its database.

## Reporting vulnerabilities

Please report vulnerabilities through GitHub Issues if no private channel is available. Do not include real secrets, private memory databases, or sensitive user data in public reports.
