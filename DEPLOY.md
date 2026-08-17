# Hosting deploy runbook — Cloud Run (Phase G)

Deploys the hosted AdLoop MCP server to **Google Cloud Run**, region **`us-west1`**
(colocated with the Supabase DB in `us-west-1`). The server image is built in the
cloud by Cloud Build from the repo `Dockerfile` — **no local Docker required**.

Do **staging first** (Motivent Staging Supabase), verify, then repeat for prod
(Client Brain Supabase). Values below are for staging.

---

## 0. One-time project setup

```bash
gcloud auth login
gcloud config set project gads-mcp-490901

# APIs used by the build + deploy + secrets
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

## 1. Create the secrets (Secret Manager)

Only the genuinely secret values go here; plain config is passed as env in step 2.
Create these three secrets:

| Secret name | Value |
|---|---|
| `adloop-database-url` | IPv4 shared-pooler string (see § env reference), real password substituted, special chars percent-encoded |
| `adloop-google-client-secret` | Web OAuth client secret (from client `adloop-hosted-web`) |
| `adloop-ads-developer-token` | Google Ads developer token |

**Recommended: create them in the Console** — GCP Console → Security → Secret
Manager → Create Secret → paste the value → Create. It keeps secrets out of shell
history and avoids the trailing-newline trap below.

⚠️ **No trailing newline/space.** Cloud Run injects the value verbatim, so a stray
`\n` corrupts the connection string / token. The Console textarea is safe if you
paste cleanly; `echo`/`printf` piping is not (PowerShell's `echo` appends a
newline — use `--data-file=-` from a temp file written without one if you must
use the CLI).

Grant the Cloud Run runtime service account `secretAccessor` on each secret. This
**must happen before the deploy** — otherwise the revision fails to start with a
"Permission denied on secret" error. The default runtime SA is
`PROJECT_NUMBER-compute@developer.gserviceaccount.com` (here PROJECT_NUMBER is
`955371824855`):

```bash
gcloud secrets add-iam-policy-binding adloop-database-url          --member="serviceAccount:955371824855-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding adloop-google-client-secret  --member="serviceAccount:955371824855-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding adloop-ads-developer-token   --member="serviceAccount:955371824855-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

## 2. First deploy (to obtain the service URL)

`--allow-unauthenticated` is correct here: the server does its **own** auth at the
app layer (Supabase OAuth 2.1 + client_id pinning via `SupabaseProvider`), so
Cloud Run IAM must not also gate it — Claude reaches it with a bearer token, not a
Google identity.

`ADLOOP_TOOLSETS=ads,ga4,gtm,gsc` is baked into the image (Dockerfile), so it's not passed
here. `ADLOOP_BASE_URL` / allow-lists / `ADLOOP_EXPECTED_CLIENT_ID` are set in
step 4 once we know the URL + connector id.

Pass all env vars in ONE quoted `--set-env-vars`, and all secrets in ONE quoted
`--set-secrets`. **Repeating a `--set-*` flag does NOT accumulate — gcloud keeps
only the last occurrence**, so the multi-flag form silently drops values. The
double quotes are also required in PowerShell, which otherwise splits the
comma-separated value into separate arguments.

```bash
gcloud run deploy adloop-hosted --source . --region us-west1 --allow-unauthenticated --set-env-vars "ADLOOP_SUPABASE_URL=https://lkqinhtagvvzxhaxxsgq.supabase.co,ADLOOP_JWT_ALGORITHM=ES256,ADLOOP_GOOGLE_CLIENT_ID=955371824855-h0dpakb837g2egl2ehq8haeedjjnhs5k.apps.googleusercontent.com,ADLOOP_ADS_LOGIN_CUSTOMER_ID=4762726066" --set-secrets "ADLOOP_GOOGLE_CLIENT_SECRET=adloop-google-client-secret:latest,ADLOOP_ADS_DEVELOPER_TOKEN=adloop-ads-developer-token:latest"
```

> Add `ADLOOP_DATABASE_URL=adloop-database-url:latest` to `--set-secrets` once the
> DB password is available. It's optional at boot — `install_datastore()` falls
> back to an in-memory store if unset — so the first deploy can omit it (as above)
> and a later `gcloud run services update` can add it. `ADLOOP_TOOLSETS=ads,ga4,gtm,gsc`
> is baked into the image, so it's never passed here (a comma-valued env would
> also need `--env-vars-file`).

Grab the service URL it prints (e.g. `https://adloop-hosted-XXXXXXXXXXXX.us-west1.run.app`).

## 3. Enable Supabase OAuth Server + register the connector

Blocked until the Supabase **Owner/Admin** access lands (Auth-settings write gate):
1. Motivent Staging → `Authentication → OAuth Server` → enable + **dynamic client
   registration**.
2. Add the connector in Claude pointing at the service URL from step 2; Supabase
   dynamic registration mints a **connector `client_id`** — capture it.

## 4. Second deploy (pin base URL + connector id)

Use `--update-env-vars` (merges) — NOT `--set-env-vars`, which would wipe the env
vars set in step 2. One quoted, comma-joined value:

```bash
gcloud run services update adloop-hosted --region us-west1 --update-env-vars "ADLOOP_BASE_URL=https://adloop-hosted-XXXXXXXXXXXX.us-west1.run.app,ADLOOP_ALLOWED_HOSTS=adloop-hosted-XXXXXXXXXXXX.us-west1.run.app,ADLOOP_EXPECTED_CLIENT_ID=PASTE_CONNECTOR_CLIENT_ID"
```

In practice this is two updates: set `ADLOOP_BASE_URL` + `ADLOOP_ALLOWED_HOSTS`
right after the first deploy (turns auth on), then add `ADLOOP_EXPECTED_CLIENT_ID`
once the connector is registered and its client_id is known.

Without `ADLOOP_BASE_URL` the server logs a warning and runs **unauthenticated**
(see `install_auth`); without `ADLOOP_EXPECTED_CLIENT_ID` client-id pinning is OFF
(a token minted for another Supabase OAuth connector would verify). Both must be
set before real use.

---

## Environment variable reference

| Var | Where | Value / notes |
|---|---|---|
| `ADLOOP_SUPABASE_URL` | env | `https://lkqinhtagvvzxhaxxsgq.supabase.co` |
| `ADLOOP_BASE_URL` | env (step 4) | the Cloud Run service URL |
| `ADLOOP_JWT_ALGORITHM` | env | `ES256` |
| `ADLOOP_EXPECTED_CLIENT_ID` | env (step 4) | connector client_id to pin |
| `ADLOOP_GOOGLE_CLIENT_ID` | env | Web client `955371824855-h0dp…` |
| `ADLOOP_ADS_LOGIN_CUSTOMER_ID` | env | MCC `4762726066` |
| `ADLOOP_TOOLSETS` | image (Dockerfile) | `ads,ga4,gtm,gsc` |
| `ADLOOP_ALLOWED_HOSTS` / `_ORIGINS` | env (step 4) | Cloud Run host / origin |
| `ADLOOP_DATABASE_URL` | 🔒 secret | IPv4 shared-pooler string, port 6543 |
| `ADLOOP_GOOGLE_CLIENT_SECRET` | 🔒 secret | Web client secret |
| `ADLOOP_ADS_DEVELOPER_TOKEN` | 🔒 secret | Ads dev token |
| `ADLOOP_DB_POOL_MAX` | env (optional) | max pooled conns/instance (default 4) |
| `ADLOOP_DEV_REFRESH_TOKEN` | — | **local-dev only; never set in prod.** Phase E's per-user lookup replaces it. Set it temporarily only for a single-user staging smoke test. |

## Shared Google reporting (GA4 / GTM / GSC)

GA4/GTM/GSC run off the shared `reporting@` token via ClientBrain's Vault. The
server needs each service's Web OAuth client — the **same three clients
ClientBrain captured the tokens with** — plus a DB connection:

| Var | Where | Value / notes |
|---|---|---|
| `ADLOOP_GA4_CLIENT_ID` / `ADLOOP_GTM_CLIENT_ID` / `ADLOOP_GSC_CLIENT_ID` | env | the 3 shared Web client ids |
| `ADLOOP_GA4_CLIENT_SECRET` / `ADLOOP_GTM_CLIENT_SECRET` / `ADLOOP_GSC_CLIENT_SECRET` | 🔒 secret | the 3 shared Web client secrets |
| `ADLOOP_DATABASE_URL` | 🔒 secret | **Required** for the reporting tools (shared token lookup + client resolution), unlike Ads where it's optional. |

`ADLOOP_TOOLSETS=ads,ga4,gtm,gsc` is baked into the image. Pass the three client
ids in the `--set-env-vars` list and the three secrets in `--set-secrets` (create
them in Secret Manager like the others and grant the runtime SA `secretAccessor`).
The RPCs the server calls (`public.get_shared_google_refresh_credential`,
`public.resolve_client_google_targets`) are already granted to `postgres` in the
ClientBrain migrations, so no extra DB grant is needed.

## Security

- **Rotate** the old Desktop client (`GAds MCP`, `955371824855-m4ph…`) secret /
  refresh token — its credentials were exposed in chat during testing. The hosted
  flow uses the new Web client (`adloop-hosted-web`), so the Desktop creds can be
  retired.
- All three secrets live in Secret Manager and are injected at runtime; none are
  baked into the image (`.dockerignore` excludes `.env*`).

## Cutover

Once staging is verified end-to-end, repeat steps 0–4 against the Client Brain
prod Supabase project (`zqmteiehwhbhcsubcqvr`) with prod values, then retire the
local-install setup guide.
