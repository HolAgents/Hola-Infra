---
name: deploy-hola-fc
description: Deploy the Hola-Infra FC webhook service to Alibaba Cloud Function Compute. Covers OIDC auth setup, RAM role configuration, s deploy, and post-deploy verification.
---

# deploy-hola-fc

Deploy `fc/` to Alibaba Cloud FC (cn-hangzhou) via GitHub Actions.

## Prerequisites

One-time setup (already done for HolAgents org):

- Alibaba Cloud RAM OIDC IdP: `action` (issuer: `https://token.actions.githubusercontent.com`, audience: `sts.aliyuncs.com`)
- RAM role: `fc-github-action` (trusts OIDC IdP, policies: `AliyunFCFullAccess` + `AliyunNASFullAccess` + `AliyunVPCFullAccess` + `AliyunECSFullAccess`)
- GitHub Secrets: `ALIBABA_CLOUD_ROLE_ARN`, `ALIBABA_CLOUD_OIDC_PROVIDER_ARN`, `ALIBABA_CLOUD_ACCOUNT_ID`, `WEBHOOK_SECRET`, `API_KEY`

## Deploy

### Manual (GitHub Actions)

1. Go to https://github.com/HolAgents/Hola-Infra/actions/workflows/ci.yml
2. **Run workflow** → select `production` → Run
3. Wait ~2 min. The pipeline runs package-check → s deploy.

### What happens during deploy

```
CI job:
  1. Assume RAM Role via OIDC (temp STS credentials, expires 1h)
  2. pip install -r requirements.txt -t .  (bundle deps into package)
  3. s config add (register STS credentials)
  4. s deploy -y --use-local (push code + trigger)
```

## Verify

```bash
# Health check
curl -s https://webhook-ingest-hola-we-service-nujmqfyilp.cn-hangzhou.fcapp.run/healthz

# Full flow test (local)
cd fc
GITHUB_WEBHOOK_SECRET=test API_KEY=test DB_PATH=/tmp/test.db \
  python -m uvicorn main:app --host 0.0.0.0 --port 9876 &
curl http://localhost:9876/healthz
```

## OIDC Setup Reference

If moving to a new Alibaba Cloud account, redo:

1. **RAM → Identity Providers → Create OIDC IdP**
   - Name: `action`
   - URL: `https://token.actions.githubusercontent.com`
   - Client IDs: `sts.aliyuncs.com`
   - Note: "Client ID" in Alibaba Cloud UI = audience in OIDC terms

2. **RAM → Roles → Create Role → Identity Provider**
   - Select OIDC IdP `action`
   - Trust policy:
     ```json
     {
       "Version": "1",
       "Statement": [{
         "Effect": "Allow",
         "Principal": {"Federated": ["acs:ram::ACCOUNT_ID:oidc-provider/action"]},
         "Action": "sts:AssumeRoleWithOIDC",
         "Condition": {
           "StringEquals": {
             "oidc:iss": "https://token.actions.githubusercontent.com",
             "oidc:aud": "sts.aliyuncs.com"
           }
         }
       }]
     }
     ```
   - Attach: `AliyunFCFullAccess` + `AliyunNASFullAccess` + `AliyunVPCFullAccess` + `AliyunECSFullAccess`

3. **GitHub → Repo Settings → Secrets → Actions**
   - `ALIBABA_CLOUD_ROLE_ARN` = `acs:ram::ACCOUNT_ID:role/fc-github-action`
   - `ALIBABA_CLOUD_OIDC_PROVIDER_ARN` = `acs:ram::ACCOUNT_ID:oidc-provider/action`
   - `ALIBABA_CLOUD_ACCOUNT_ID` = `ACCOUNT_ID`

## Common failures

| Error | Fix |
|-------|-----|
| `No module named 'uvicorn'` | `pip install -r requirements.txt -t .` not run in CI |
| `Invalid audience` | Alibaba Cloud IdP client IDs must match CI `audience` param |
| `NoPermission` / `ImplicitDeny` | Role trust policy Action must use `sts:AssumeRoleWithOIDC` |
| `No module named 'index'` | `runtime: python3.10` needs `index.py` with `handler = app` |
| `CAExited` / Python 3.7 errors | Do not use `runtime: custom` (Python 3.7); use `runtime: python3.10` |
| `TriggerConfig required` | s.yaml triggers use `config:` not `triggerConfig:` |

## Architecture note

FC codeUri is `./` (contents of `fc/` become the root). All internal imports use `from app.xxx` (no `fc.` prefix). The entry is `index.py` exposing `handler = app` (FastAPI ASGI app).
