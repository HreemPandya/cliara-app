# Cliara Cloud — Proxy Backend

OpenAI-compatible FastAPI proxy that powers `cliara login` zero-friction onboarding.

## How it works

```
cliara client  →  Cliara Cloud (this service)  →  OpenAI / Groq
     ↑                      ↑
  JWT token            rate limit check
  (from Supabase)      (Supabase Postgres)
```

## Setup

### 1. Create a Supabase project

1. Go to [supabase.com](https://supabase.com) and create a new project.
2. Enable **GitHub** as an OAuth provider:
   - Supabase Dashboard → Authentication → Providers → GitHub
   - Create a GitHub OAuth App at github.com/settings/developers
   - Callback URL: `https://<your-project>.supabase.co/auth/v1/callback`
   - Copy Client ID + Secret into Supabase
3. Run the migration SQL:
   - Dashboard → SQL Editor → New query → paste `supabase/migrations/001_initial.sql` → Run

### 2. Collect required values

From **Supabase Dashboard → Settings → API**:
- `SUPABASE_URL` — the Project URL (e.g. `https://abcdefgh.supabase.co`)
- `SUPABASE_SERVICE_KEY` — the `service_role` key (secret, never commit)
- `SUPABASE_JWT_SECRET` — the JWT Secret (under Settings → API → JWT Settings)
- `SUPABASE_ANON_KEY` — the `anon` / `public` key (safe to embed in client)

### 3. Deploy to Railway

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init        # link to a new Railway project
railway up          # deploys from Dockerfile

# Set environment variables
railway variables set SUPABASE_URL=https://xxx.supabase.co
railway variables set SUPABASE_SERVICE_KEY=eyJ...
railway variables set SUPABASE_JWT_SECRET=your-jwt-secret
railway variables set OPENAI_API_KEY=sk-...
railway variables set GROQ_API_KEY=gsk_...  # optional

# Get the public URL Railway assigns
railway domain
```

### 4. Wire up the Cliara client

After Railway gives you a URL (e.g. `https://cliara-cloud.up.railway.app`), configure the client gateway URL in `cliara/auth.py` (or via env vars):

**`cliara/auth.py`** — fill in the Supabase constants:
```python
_SUPABASE_URL = "https://xxx.supabase.co"
_SUPABASE_ANON_KEY = "eyJ..."   # anon/public key — safe to ship in client
_CLIARA_GATEWAY_URL = "https://cliara-cloud.up.railway.app/v1"
```

You can also override these at runtime with environment variables:

```bash
CLIARA_SUPABASE_URL=https://xxx.supabase.co
CLIARA_SUPABASE_ANON_KEY=eyJ...
CLIARA_GATEWAY_URL=https://cliara-cloud.up.railway.app/v1
```

### 5. Test end-to-end

```bash
pip install --editable .   # install cliara in dev mode
cliara login               # should open browser → GitHub login
cliara                     # start shell — LLM should work with no API key
? list files here          # confirm a query reaches OpenAI via the proxy
```

## Local development

```bash
pip install -r requirements.txt

# Create a .env file with your secrets
cp .env.example .env
# Edit .env with real values

uvicorn main:app --reload --port 8000
```

`.env.example`:
```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
SUPABASE_JWT_SECRET=your-jwt-secret
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
```

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Railway health check |
| `GET /v1/usage` | Public tier info |
| `POST /v1/chat/completions` | Proxied chat (requires Bearer JWT) |
| `POST /v1/embeddings` | Proxied embeddings (requires Bearer JWT) |

## Adding paid tiers (next step)

1. Add a `tier` column to `user_usage` (or a separate `users` table)
2. Create a Stripe webhook that sets `tier = 'dev'` / `'pro'` on successful payment
3. In `proxy.py`, read the tier and skip the model override for paid users
4. Add `railway variables set STRIPE_WEBHOOK_SECRET=...`
