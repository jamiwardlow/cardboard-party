# Deploying & environments

Cardboard Party runs on Google App Engine with Firestore. This describes the
environments and how to deploy to each.

## Environments

| Env | Project | URL | Data | Config file |
|-----|---------|-----|------|-------------|
| **Production** | `cardboard-party` | https://cardboardparty.gg | live Firestore + `cardboard-party-avatars` bucket | `app.yaml` |
| **Staging** | `cardboard-party-staging` | `cardboard-party-staging.<region>.r.appspot.com` | its own Firestore + `cardboard-party-staging-avatars` bucket | `staging.yaml` |

The **same code** runs in both; only per-environment config differs (bucket,
OAuth client, secrets, and whether `CANONICAL_HOST` is set). Anything
environment-specific is read from the environment, never hardcoded:
- `AVATARS_BUCKET` — GCS bucket for avatars/brand images (`storage.py`).
- `CANONICAL_HOST` — set **only** in prod (`app.yaml`); unset in staging so
  staging never 301-redirects to the prod domain.
- `GOOGLE_CLIENT_ID` (app.yaml/staging.yaml) + `GOOGLE_CLIENT_SECRET` &
  `FLASK_SECRET_KEY` (Secret Manager) — per project.

## Deploy

```bash
# Production
gcloud app deploy --project=cardboard-party

# Staging
gcloud app deploy staging.yaml --project=cardboard-party-staging
```

### Safer prod deploys (test the build before it takes traffic)

```bash
gcloud app deploy --project=cardboard-party --no-promote   # deploys, gets NO traffic
# visit https://<VERSION>-dot-cardboard-party.wl.r.appspot.com to smoke-test
gcloud app services set-traffic default --splits=<VERSION>=1 --project=cardboard-party
```
Note: `--no-promote` still shares the **prod** database/bucket — good for catching
code/deploy breakage, not for destructive data testing. Use staging for that.

## Local development (fast, isolated)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Isolated fake data via the Firestore emulator:
gcloud emulators firestore start --host-port=localhost:8086   # in one shell
export FIRESTORE_EMULATOR_HOST=localhost:8086
export GOOGLE_CLOUD_PROJECT=cardboard-party-local
export AVATARS_BUCKET=""        # avoid touching real buckets
python main.py                  # http://localhost:8080
```
Google login needs OAuth creds/redirect URIs, so test logged-out flows locally or
point at staging's OAuth client.

## One-time staging setup (console + gcloud)

Done once by an owner of the GCP org:

1. **Create the project & app**
   ```bash
   gcloud projects create cardboard-party-staging
   gcloud app create --project=cardboard-party-staging --region=us-west2
   gcloud firestore databases create --project=cardboard-party-staging --location=us-west2
   ```
2. **Bucket** (public-read, like prod's avatars bucket)
   ```bash
   gcloud storage buckets create gs://cardboard-party-staging-avatars \
     --project=cardboard-party-staging --location=us-west2
   gcloud storage buckets add-iam-policy-binding gs://cardboard-party-staging-avatars \
     --member=allUsers --role=roles/storage.objectViewer
   ```
   Grant the staging App Engine service account `roles/storage.objectAdmin` on it.
3. **OAuth client** — in the staging project's Cloud Console → APIs & Services →
   Credentials, create an OAuth 2.0 Web client. Authorized redirect URI:
   `https://cardboard-party-staging.<region>.r.appspot.com/auth/callback`.
   Put the client ID in `staging.yaml` (`GOOGLE_CLIENT_ID`).
4. **Secrets** in the staging project's Secret Manager (grant the App Engine
   default service account `roles/secretmanager.secretAccessor`):
   ```bash
   printf '<staging oauth client secret>' | gcloud secrets create GOOGLE_CLIENT_SECRET \
     --data-file=- --project=cardboard-party-staging
   python -c "import secrets;print(secrets.token_urlsafe(48))" | gcloud secrets create FLASK_SECRET_KEY \
     --data-file=- --project=cardboard-party-staging
   ```
5. **First admin** (so you can manage staging):
   ```bash
   GOOGLE_CLOUD_PROJECT=cardboard-party-staging python bootstrap_admin.py you@gmail.com "Your Name"
   ```
6. **Deploy:** `gcloud app deploy staging.yaml --project=cardboard-party-staging`
7. Optionally seed sample data: `seed_staging.py` (see that file).
