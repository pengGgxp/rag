# Production environment variable reference

This is the authoritative index of production environment variables. It does
not replace `app.env.example` and `deployment.env.example`: those are the
copyable templates that deployment validation checks. Keep all three files in
sync when a setting is added, renamed or removed.

## Where values live

There are three separate configuration layers:

| Layer | Location | Purpose | Contains secrets |
| --- | --- | --- | --- |
| Application runtime | `/opt/zhiku/config/app.env` | Database, Redis, auth, preview, parser, cache and legacy bootstrap settings used by API, Worker, preview Worker and preview gateway. | Yes |
| Compose deployment | Generated `/opt/zhiku/current/.env.release` | Image digests, database container password, internal RustFS keys and Compose-only cache/deployment values. | Yes; mode `600` |
| Question workbench bridge | `/opt/zhiku/config/.env.question-workbench` | Separate shared secret for the question-workbench integration. | Yes |

`deployment.env.example` documents Compose keys but is not a second copy of
application configuration. Normally `deploy_release.py` derives
`.env.release` from the verified release manifest, `app.env` and the database
URL. An operator must not copy application secrets into Compose.

The Web API address is a build-time input named `NEXT_PUBLIC_API_URL`. CI and
manual image builds must set it to `https://zk.str1ct.top/api`. It is not a
backend runtime setting and is deliberately not part of `app.env`.

GitHub is not required to run production. GitHub Actions may build images and
upload a release when enabled, but production configuration and releases are
server-owned. GitHub Settings are therefore not part of this table.

## Application runtime: `app.env`

### Core and release metadata

| Variable | Required | Function |
| --- | --- | --- |
| `DATABASE_URL` | Yes | PostgreSQL DSN used by API, Workers, preview gateway and migration jobs. Must include user, password and host. |
| `REDIS_URL` | Yes | Celery broker and shared transient state. |
| `AUTH_SECRET_KEY` | Yes | Session/token signing secret. At least 32 random characters. |
| `SITE_CONFIG_ENCRYPTION_KEY` | Yes | Fernet key encrypting site-managed credentials such as SMTP, storage providers and payment secrets. Back it up separately. |
| `MCP_CREDENTIAL_KEY` | Yes | Independent Fernet key for encrypted MCP authentication headers. Back it up separately. |
| `DEPLOY_CONTROL_TOKEN` | Yes | Server-only token for the loopback maintenance endpoint. Never expose it to browsers or GitHub. |
| `APP_VERSION` | Generated | Release version shown by the API; `deploy_release.py` replaces it from `release.json`. |
| `GIT_COMMIT` | Generated | Full release commit shown by the API; replaced from `release.json`. |
| `CONFIG_CONTRACT` | Generated | Compatibility contract number checked by the release tool. |
| `DEPLOYMENT_ROLLBACK` | Generated | Migration rollback policy: `automatic-app-rollback` or `restore-backup-required`. |

### Public URLs, authentication and previews

| Variable | Required | Function |
| --- | --- | --- |
| `FRONTEND_URL` | Yes | Public Web origin used for links, callbacks and CORS/origin checks. |
| `API_PUBLIC_URL` | Yes | Public API origin used in externally visible links. |
| `AUTH_COOKIE_SECURE` | Yes | Must be `true` in production so auth cookies are HTTPS-only. |
| `AUTH_SESSION_HOURS` | No | Session lifetime in hours; default `168`. |
| `PREVIEW_ENABLED` | Yes | Enables the HTTP cloud-computer preview gateway. |
| `PREVIEW_DOMAIN` | Yes | Parent preview domain, normally `preview.str1ct.top`. |
| `OIDC_NAME` | No | Display name of the optional enterprise login provider. |
| `OIDC_METADATA_URL` | No | OIDC discovery document URL; required only when OIDC is enabled. |
| `OIDC_CLIENT_ID` | No | OIDC client ID; required only when OIDC is enabled. |
| `OIDC_CLIENT_SECRET` | No | OIDC client secret; required only when OIDC is enabled. |

### RAG embedding and legacy LLM fallback

| Variable | Required | Function |
| --- | --- | --- |
| `EMBEDDING_BASE_URL` | Capability | Embedding provider base URL. Required for ingestion and semantic retrieval. |
| `EMBEDDING_API_KEY` | Capability | Embedding provider key. Required with the base URL. |
| `EMBEDDING_MODEL` | Capability | Embedding model name. Required with the base URL. |
| `EMBEDDING_DIMENSION` | No | Embedding vector dimension; default `1024`. Must match the embedding model and index. |
| `EMBEDDING_TIMEOUT_SECONDS` | No | Embedding request timeout; default `30`. |
| `LLM_BASE_URL` | Legacy fallback | Single-model fallback URL. Normal chat providers and models are configured in site admin. |
| `LLM_API_KEY` | Legacy fallback | Single-model fallback key. |
| `LLM_MODEL` | Legacy fallback | Single-model fallback model name. |
| `LLM_TIMEOUT_SECONDS` | No | Legacy LLM timeout; default `60`. |

### Parsing, OCR and MCP

| Variable | Required | Function |
| --- | --- | --- |
| `PDF_PARSER` | No | `local` for local PDF parsing or `aliyun` for Alibaba DocMind. Default `local`. |
| `ALIYUN_ACCESS_KEY_ID` | Capability | Alibaba credential for DocMind when `PDF_PARSER=aliyun`. |
| `ALIYUN_ACCESS_KEY_SECRET` | Capability | Alibaba credential for DocMind when `PDF_PARSER=aliyun`. |
| `DOCMIND_TIMEOUT_SECONDS` | No | Maximum DocMind job wait; default `300`. |
| `DOCMIND_POLL_SECONDS` | No | DocMind polling interval; default `3`. |
| `MCP_ALLOWED_PRIVATE_HOSTS` | No | Comma-separated exact private hosts administrators explicitly trust for MCP. Keep empty by default. |
| `MCP_TIMEOUT_SECONDS` | No | MCP request timeout; default `60`. |

### SMTP and legacy payment bootstrap

These settings seed configuration only while no database-backed site setting
exists. After an administrator saves the corresponding settings page, the
versioned database configuration is authoritative.

| Variable | Required | Function |
| --- | --- | --- |
| `SMTP_HOST` | Capability | SMTP server host. |
| `SMTP_PORT` | No | SMTP port; default `587`. |
| `SMTP_USERNAME` | Capability | SMTP username when required by the provider. |
| `SMTP_PASSWORD` | Capability | SMTP password or app password. |
| `SMTP_FROM` | Capability | Sender address; SMTP is enabled when host and sender are present. |
| `SMTP_STARTTLS` | No | Enables STARTTLS; default `true`. |
| `ZHIFUX_ENABLED` | Legacy | Initial Chinese payment provider enablement draft. |
| `ZHIFUX_API_BASE_URL` | Legacy | Initial payment API endpoint. |
| `ZHIFUX_MERCHANT_NUM` | Legacy | Initial payment merchant number. |
| `ZHIFUX_SECRET` | Legacy | Initial payment signing secret. |
| `ZHIFUX_PAY_TYPE` | Legacy | Legacy single payment method code. |
| `ZHIFUX_PAYMENT_METHODS_JSON` | Legacy | Initial ordered payment-method list as JSON. |
| `ZHIFUX_CUSTOM_RECHARGE_ENABLED` | Legacy | Initial custom-amount enablement. |
| `ZHIFUX_CUSTOM_CREDITS_PER_YUAN` | Legacy | Initial credits-per-yuan rate for custom amounts. |
| `RECHARGE_PACKAGES_JSON` | Legacy | Initial recharge package array as JSON. |
| `EZFP_ENABLED` | Legacy | Initial Ezfp payment enablement draft. |
| `EZFP_API_BASE_URL` | Legacy | Ezfp payment API endpoint. |
| `EZFP_PID` | Legacy | Ezfp merchant ID. |
| `EZFP_PAY_TYPE` | Legacy | Legacy Ezfp method code. |
| `EZFP_PAYMENT_METHODS_JSON` | Legacy | Initial Ezfp method list as JSON. |
| `EZFP_PRIVATE_KEY` | Legacy | Ezfp signing private key. |
| `EZFP_PUBLIC_KEY` | Legacy | Ezfp verification public key. |
| `EZFP_RECHARGE_PACKAGES_JSON` | Legacy | Initial Ezfp package array. |
| `EZFP_CUSTOM_RECHARGE_ENABLED` | Legacy | Initial Ezfp custom-amount enablement. |
| `EZFP_CUSTOM_CREDITS_PER_YUAN` | Legacy | Initial Ezfp credits-per-yuan rate. |
| `USAGE_AUTHORIZATION_HOLD_CREDITS` | No | Minimum authorization hold for metered model use; default `1`. |

### Resource and preview caches

| Variable | Required | Function |
| --- | --- | --- |
| `RESOURCE_CACHE_DIR` | No | Shared API/Worker cache for authorized temporary resource reads. Not cloud-computer OSS storage. |
| `RESOURCE_CACHE_MAX_BYTES` | No | Maximum resource cache size; default 5 GiB. |
| `RESOURCE_CACHE_IDLE_SECONDS` | No | Idle eviction threshold for the resource cache; default 24 hours. |
| `PREVIEW_CACHE_MAX_BYTES` | No | Preview Worker cache limit; the release generator derives the Compose value from this setting; default 5 GiB. |
| `PREVIEW_CACHE_IDLE_SECONDS` | No | Preview cache idle retention; the release generator derives the Compose value from this setting; default 7 days. |

### Legacy local executor and workbench bridge

| Variable | Required | Function |
| --- | --- | --- |
| `WORKSPACE_RUNNER_IMAGE` | Legacy | Local Docker executor image fallback retained for compatibility; production file work uses cloud computers. |
| `WORKSPACE_FILE_RETENTION_DAYS` | Legacy | Local workspace file retention fallback, default `3`. |
| `WORKSPACE_FILE_QUOTA_MB` | Legacy | Local workspace file quota fallback, default `500`. |
| `QUESTION_WORKBENCH_URL` | Capability | Public question-workbench URL for integration buttons and redirects. |
| `QUESTION_WORKBENCH_API_URL` | Capability | Internal question-workbench API base URL. |
| `QUESTION_WORKBENCH_SECRET` | Separate file | Shared bridge secret. It belongs in `.env.question-workbench`, not `app.env`. |

### Legacy S3/RustFS bootstrap

Production object storage is selected in `/admin/storage`; normal resource,
knowledge-base, chat and image files use that configured provider.

| Variable | Required | Function |
| --- | --- | --- |
| `S3_ENDPOINT` | Legacy bootstrap | Internal S3-compatible endpoint used by legacy storage migration/bootstrap paths. |
| `S3_ACCESS_KEY` | Legacy bootstrap | Internal S3/RustFS access key; may also seed the local object-store container. |
| `S3_SECRET_KEY` | Legacy bootstrap | Internal S3/RustFS secret key. |
| `S3_BUCKET` | Legacy bootstrap | Legacy local object-store bucket name. |
| `S3_REGION` | Legacy bootstrap | Legacy local object-store region. |

## Compose deployment: generated `.env.release`

| Variable | Source | Function |
| --- | --- | --- |
| `API_IMAGE` | Release manifest | Digest-pinned API image, also used for preview gateway and migration jobs. |
| `WORKER_IMAGE` | Release manifest | Digest-pinned Worker image, used by normal and preview Workers. |
| `WEB_IMAGE` | Release manifest | Digest-pinned Web image. |
| `APP_VERSION` | Release manifest | Version metadata passed to containers. |
| `GIT_COMMIT` | Release manifest | Commit metadata passed to containers. |
| `CONFIG_CONTRACT` | Release manifest | Configuration compatibility number. |
| `DEPLOYMENT_ROLLBACK` | Release manifest | Migration recovery policy used during deployment. |
| `POSTGRES_PASSWORD` | `DATABASE_URL` | Password for the production PostgreSQL container. |
| `RUSTFS_ACCESS_KEY` | `S3_ACCESS_KEY` | Internal RustFS access key for the Compose object-store container. |
| `RUSTFS_SECRET_KEY` | `S3_SECRET_KEY` | Internal RustFS secret key. |
| `ZHIKU_APP_ENV_FILE` | Release generator | Filename that Compose mounts as the application env file; normally `app.env`. |
| `PREVIEW_CACHE_MAX_BYTES` | `app.env` | Passed to the preview Worker container. |
| `PREVIEW_CACHE_IDLE_SECONDS` | `app.env` | Passed to the preview Worker container. |
| `MIGRATION_DATABASE_URL` | Release generator | Temporary isolated database URL used only by the migration rehearsal container. |

`deployment.env.example` only lists the Compose values an operator may need to
provide. Image digests, release metadata and the migration rehearsal URL are
generated by the release tool. The actual `.env.release` is mode `600` and must
not be edited by hand.

## Current production state before this change

At the time this table was introduced, the server
`/opt/zhiku/config/app.env` had 35 keys and the question-workbench file had its
bridge secret. Most settings absent from `app.env` fell back to code defaults;
that kept existing services running but was not an explicit, auditable
contract. The new templates make those defaults visible. Adding a fallback
value is not a commercial configuration decision and does not enable a paid
capability by itself.

The audit command lists missing and unknown keys without printing values:

```bash
sudo /usr/bin/python3 \
  /opt/zhiku/current/validate_config.py \
  --audit-app-env /opt/zhiku/config/app.env \
  --audit-deployment-env /opt/zhiku/current/.env.release
```

## Minimum operational checklist

For a backend deployment, these are the values that should be explicitly
present and verified. Do not paste their values into tickets or chat.

| Variable | Consequence when absent |
| --- | --- |
| `DATABASE_URL` | Release validation stops; API and Worker cannot use PostgreSQL. |
| `REDIS_URL` | API and Workers cannot use the task broker. |
| `AUTH_SECRET_KEY` | Authentication cannot be considered production-ready. |
| `SITE_CONFIG_ENCRYPTION_KEY` | Admin-managed secrets cannot be safely encrypted or decrypted. |
| `MCP_CREDENTIAL_KEY` | Encrypted MCP authentication headers cannot be read. |
| `FRONTEND_URL` | External links and origin checks lose their production address. |
| `API_PUBLIC_URL` | Public API links lose their production address. |
| `AUTH_COOKIE_SECURE` | Must be `true`; release validation rejects an insecure production cookie. |
| `PREVIEW_ENABLED` | Release validation needs an explicit preview decision (`true` or `false`). |
| `PREVIEW_DOMAIN` | HTTP preview routing has no domain. |
| `DEPLOY_CONTROL_TOKEN` | Maintenance control during release cannot work. |

`APP_VERSION`, `GIT_COMMIT`, `CONFIG_CONTRACT` and `DEPLOYMENT_ROLLBACK` are
normally generated from the release manifest.

These do not need to be present before the related feature is used:

- `EMBEDDING_*` until embeddings and retrieval are enabled.
- `ALIYUN_*`/`DOCMIND_*` when local PDF parsing is used.
- `OIDC_*` until enterprise login is enabled.
- `SMTP_*` until mail is configured in the admin site.
- Legacy `ZHIFUX_*`, `EZFP_*` and recharge bootstrap values once database
  payment settings are the source of truth.
- Cache tuning, local-executor compatibility and workbench URL overrides,
  which have code defaults.

## Managed in the database, not environment variables

Do not solve these by adding variables to `app.env`:

- Model providers, provider credentials, model protocols, model capabilities,
  default chat/image models, image presets and image prices.
- Primary object storage provider, COS/OSS credentials, bucket, region and
  connection-test state.
- Cloud-computer enablement, Alibaba credentials, region, template, resource
  mount role ARN, price, idle/max lifetime and concurrency limits.
- Web search provider configuration and pricing.
- Knowledge-base storage quotas, download quotas and overage cleanup policy.
- SMTP, WeChat/OIDC after the first save, payment providers after the first
  save, recharge packages and commercial prices.

These are versioned administrator settings in PostgreSQL and can change
without rewriting the server env file.
