# Deploying the CFO agent for a client pilot

This guide takes the app from the local demo to a client-hosted pilot: PostgreSQL,
sign-in with roles, real ERP data through the CSV file drop, a separate job worker,
and AI calls that never see customer or vendor names.

## What runs

| Service | What it does | Scale |
|---|---|---|
| `db` | PostgreSQL 16 + pgvector (all data, memory vectors) | 1 (use the client's managed Postgres if available) |
| `api` | Web UI + REST API (`uvicorn`, 2 workers), `RUN_SCHEDULER=false` | 1+ replicas |
| `worker` | Weekly digest, red-line sweep (every 15 min), connector syncs, news, memory consolidation | 1 (a 2nd is a hot standby) |

Workers compete for a database lease; only the holder runs jobs, and a standby takes
over within ~3 minutes. Only one digest per week can run at a time (database-enforced),
so API replicas cannot double-run it.

## 1. Prerequisites

- A Linux VM (4 vCPU / 8 GB is ample for a pilot) in the agreed region (India for DPDP), Docker 24+
- A TLS-terminating reverse proxy or load balancer in front (the client's ingress, nginx, ALB…)
- An Anthropic API key (see *Data protection* below for what is sent)
- For SSO: an OpenID Connect app registration (Entra ID, Okta, Google Workspace, Ping…)

## 2. Configure

```bash
cp .env.docker.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → SECRET_KEY
```

Fill in `.env`: `ANTHROPIC_API_KEY`, `POSTGRES_PASSWORD`, `SECRET_KEY`, `PUBLIC_BASE_URL`.
Keep `DEMO_MODE=false`, `DATA_MODE=live`, `PII_MASKING=true`. The app refuses to start
with an unsafe combination (no `SECRET_KEY`, or sign-in off outside demo mode).

### Sign-in

**SSO (recommended)** — register a web app with redirect URI
`<PUBLIC_BASE_URL>/api/auth/oidc/callback`, then set `AUTH_MODE=oidc`, `OIDC_ISSUER`,
`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`. Map the IdP's groups to app roles:

```
OIDC_ROLES_CLAIM=groups
OIDC_ROLE_MAP={"<cfo-office-group>": "cfo", "<finance-delegates>": "delegate", "<it-admins>": "admin"}
OIDC_DEFAULT_ROLE=viewer
```

For Entra ID, add the `groups` claim to the ID token (Token configuration → Add groups claim).

**Local users** — `AUTH_MODE=local`, then create users (passwords are prompted, hashed with
PBKDF2, never stored in plain text):

```bash
python scripts/manage_users.py add cfo@client.com --name "A. Sharma" --role cfo
python scripts/manage_users.py add fpa.lead@client.com --role delegate
python scripts/manage_users.py add it.admin@client.com --role admin
```

and mount the file (`USERS_FILE=./users.yaml` in `.env`). Five failed sign-ins in 15 minutes
lock that email/IP pair out for the rest of the window.

### Roles

| Role | Can |
|---|---|
| viewer | Read briefings, digests, evidence, regulatory feed |
| cfo | + feedback, drill-down chat, acknowledge alerts |
| delegate | + weighting, KRA framework, run digests, data health, memory, news topics |
| admin | + connectors, audit log, maintenance |

Every API route is checked against one policy table (`backend/cfo_agent/auth.py`); routes
not listed there are admin-only by default.

## 3. Start

```bash
mkdir -p inbox && sudo chown 10001 inbox      # the containers run as uid 10001
docker compose up -d --build
docker compose ps                             # db healthy, api healthy, worker running
curl -s localhost:8000/api/ops/ready          # {"ready": true}
```

The schema is created and upgraded automatically (Alembic, serialised with a database lock).
Point the reverse proxy at `127.0.0.1:8000` (change `API_BIND`/`API_PORT` if needed).

## 4. Load ERP data (CSV file drop)

1. Sign in as admin → **Connections** → **File drop (CSV exports)** → set *Inbox folder name*
   (e.g. `acme`) → Save → enable it → set a schedule (e.g. hourly).
2. Have the ERP (or an SFTP job) write exports into `inbox/acme/`. Templates for every file
   are linked in the connector dialog (`/api/connectors/templates/<table>.csv`):
   `customers, covenants, sales_invoices, receipts, purchase_invoices, payments, expenses,
   cash_daily, revenue_daily, gst_events`. Amounts in rupees; dates `YYYY-MM-DD` or `DD-MM-YYYY`.
3. **Test connection** validates waiting files without loading them. A sync then:
   - loads each valid file all-or-nothing, upserting by `business_key` (re-sent corrections,
     including back-dated ones, update existing records);
   - skips a file it has already loaded (same SHA-256);
   - moves files to `processed/<time>/`, or to `rejected/<time>/` with a `.errors.csv`
     report listing every bad line.

The app only reads these files; it has no write path into any client system.

New templates: `entities`, `bank_accounts`, `fx_rates`, `credit_notes`, `gl_entries`, `plan_metrics`.

- **Entities and intercompany.** Every transaction can carry an `entity` code (default `MAIN`).
  Metrics are consolidated across entities marked `consolidate`, and customers / vendors flagged
  `is_intercompany` are left out of receivables, payables and DSO. The Briefing shows a per-entity table
  when there is more than one entity.
- **Currencies.** Add `currency` (and optionally `fx_rate`, INR per unit) to a row and the amount column is
  read in that currency and converted to INR. Without `fx_rate`, the latest rate in `fx_rates` within 7 days
  is used; if there is none, the row is rejected with a clear message.
- **Bank accounts.** `cash_daily` takes an `account_key`; available cash is the latest balance of every
  unrestricted account (escrow / DSRA accounts marked `is_restricted` are excluded).
- **General ledger.** `gl_entries` (signed: debit +, credit −; `account_type` revenue / cogs / opex / …)
  drives the close-cycle metric (last posting after period end) and ledger-based custom metrics such as
  gross margin. Credit notes reduce the open receivable on their invoice.
- **Plan.** `plan_metrics` rows (`week`, `metric_id`, `value` in the metric's unit) put every signal against
  plan; a gap of 5% or more in the bad direction raises its priority.

## 4b. Other sources (read-only)

| Connector | How it reads | Incremental by |
|---|---|---|
| **Tally Prime** | Tally's XML server (`http://host:9000`), *Export* requests only: debtor ledgers, Sales, Receipt, Credit Note, Purchase and Payment vouchers (bill-wise) | Tally `AlterID` |
| **Database (read-only SQL)** | Your own `SELECT` per table against a reporting replica, with a **read-only login**; sessions are set READ ONLY and every transaction is rolled back | max `_modified_at` column, bound as `:since` |

Both go through the same validator as the file drop (masters first, each table all-or-nothing), and
the watermark only advances when nothing was rejected. **Test connection** connects and validates what a
sync would load without loading it. Deleted records are not visible to incremental pulls: schedule a
weekly *full* sync. Tally mapping assumptions (bill reference = invoice number; credit period from the
bill allocation) are documented in `backend/cfo_agent/connectors/tally.py` — confirm them against the
client's company. For SQL Server or Oracle sources add the driver (`pyodbc` / `oracledb`) to the image.

## 5. Verify

- Sign in as the delegate → **Digest runs** → generate the latest week (1–2 minutes).
- Sign in as the CFO → **Briefing**.
- `GET /api/ops/status` (delegate+): scheduler lease holder and heartbeat, whether the weekly
  digest is on schedule, failed runs in 7 days, connector status, AI spend for 30 days.
- **Audit log** (admin): sign-ins, every change, denied requests and every view of a digest,
  conversation or evidence record.

## Data protection

- **What leaves the deployment:** prompts to the Anthropic API containing the finance figures
  and record IDs needed for each insight. With `PII_MASKING=true`, customer and vendor names
  are replaced by stable aliases (`CUSTOMER_03`) before every call and restored in the output.
  Embeddings are computed locally (model baked into the image; no runtime download).
- **At rest:** connector secrets are encrypted (Fernet, key derived from `SECRET_KEY`).
  Encrypt the Postgres volume/disk and backups with the platform's KMS.
- **In transit:** terminate TLS at the proxy; set `PUBLIC_BASE_URL=https://…` so session
  cookies are marked `Secure`. Cookies are `HttpOnly`, `SameSite=Lax`; mutating requests also
  require an `X-Requested-With` header.
- **Browser hardening:** CSP, `X-Frame-Options: DENY`, `nosniff`, no CORS unless
  `CORS_ORIGINS` lists explicit origins.
- **Where the model runs:** see *Model provider and data residency* below.

## Model provider and data residency

Claude can be called through three platforms. Choose with `LLM_PROVIDER`:

| `LLM_PROVIDER` | Where prompts are processed | Credentials |
|---|---|---|
| `anthropic` (default) | Anthropic API — **not** in India | `ANTHROPIC_API_KEY` |
| `bedrock` | AWS region `AWS_REGION` (routing set by the model ID, below) | the instance/task IAM role (or `AWS_PROFILE`); needs `bedrock:InvokeModel*` |
| `vertex` | Google Cloud region `VERTEX_REGION` | a service account (`GOOGLE_APPLICATION_CREDENTIALS`) with Vertex AI User |

For an India-only pilot set **`DATA_RESIDENCY=india`**. The app then refuses to start unless:

- **Bedrock:** `AWS_REGION` is `ap-south-1` (Mumbai) or `ap-south-2` (Hyderabad), and
  `ANTHROPIC_MODEL` is either the **India geo inference profile** (`in.anthropic.<model>`,
  which routes only between the two Indian regions) or an **in-region** model ID
  (`anthropic.<model>`). `global.`, `apac.`, `us.`… profiles are rejected because they can
  process outside India, and application-profile ARNs are rejected because their routing
  can't be checked.
- **Vertex:** `VERTEX_REGION` is `asia-south1` (Mumbai) or `asia-south2` (Delhi); `global` is rejected.
- **Anthropic API:** always rejected under this policy.

Which Claude models are offered in-region or through the India profile changes often and
differs per model. Before go-live, confirm the exact ID from the source region (Bedrock:
`aws bedrock get-inference-profile --inference-profile-identifier in.anthropic.<model>
--region ap-south-1`; Vertex: the partner-model locations table or the Model Garden page).

Example (`.env`):

```
LLM_PROVIDER=bedrock
AWS_REGION=ap-south-1
ANTHROPIC_MODEL=in.anthropic.claude-sonnet-5
NEWS_MODEL=in.anthropic.claude-sonnet-5
DATA_RESIDENCY=india
PII_MASKING=true
NEWS_PROVIDER=off
```

Notes:

- **Structured answers** (every pipeline step) use Anthropic's native structured outputs on
  the Anthropic API and a forced tool call on Bedrock/Vertex (`STRUCTURED_OUTPUT_MODE` overrides).
  In tool mode the model's extended thinking is not used for those steps.
- **Regulatory feed:** its web search is an Anthropic API server tool. On Bedrock/Vertex it is off
  unless you set `NEWS_PROVIDER=anthropic`, which sends only the public search topics (no client
  data) to the Anthropic API and needs `ANTHROPIC_API_KEY`. The startup log notes this choice.
- `GET /api/ops/status` → `ai` shows the provider, model, where calls are processed, the
  residency policy and whether PII masking is on.
- **Docker credentials:** on AWS run the containers with an IAM role (ECS task role, EKS IRSA or
  the EC2 instance profile); on Google Cloud mount the service-account key and set
  `GOOGLE_APPLICATION_CREDENTIALS`, or use workload identity.

## Multi-tenant hosting (one database per client)

Set `TENANTS_FILE` to a YAML file:

```yaml
tenants:
  - id: acme
    name: Acme Industries
    database_url: postgresql://cfo:…@db/acme
    hosts: [acme.cfo.example.com]
    users_file: /config/acme-users.yaml   # optional, AUTH_MODE=local
    email_to: [cfo@acme.com, fc@acme.com] # digest and red-line alert recipients
    email_from: cfo-agent@acme.com        # optional; defaults to EMAIL_FROM
```

Requests are routed by the Host header (unknown hosts get 404). Each tenant has its own database
(migrated on start), file-drop inbox (`CSV_INBOX_DIR/<tenant>/`), custom metrics, caches and scheduler
lease; session cookies are bound to the tenant that issued them. The worker runs every scheduled job once
per tenant. Without `TENANTS_FILE` the app is single-tenant, as before.

**Email is per tenant.** Digests and alerts go only to that tenant's `email_to`; the global `EMAIL_TO`
is ignored in multi-tenant mode. A tenant with no `email_to` gets no email: its digests are written to
`data/outbox/<tenant>/` instead. In single-tenant mode `EMAIL_TO` may list several addresses, comma-separated.

## Product features that need set-up

- **Industry templates** (KRA framework page): manufacturing (default), FMCG, NBFC. Loading one fills the
  editor; save to adopt it. KRAs whose data the client cannot yet supply are marked ungrounded.
- **Custom metrics** (Metrics page, delegate): a formula over ERP aggregates, previewed on 8 weeks of real
  data before saving. Add the metric to a KRA for it to produce weekly signals.
- **Learning from feedback**: after each digest the app may *propose* small KRA weight changes from the
  CFO's verdicts (±0.1 per step, ×0.8–×1.25, 3+ verdicts). A delegate approves or rejects them on the
  Weighting page; approval creates a normal weighting version.
- **Exports and share links**: PDF and PowerPoint downloads per digest; read-only share links (1–30 days,
  revocable, every view audited). Set `PUBLIC_BASE_URL` so links use the public address. PDFs use DejaVu
  Sans (installed in the image) for the ₹ sign.
- **Actions and meeting briefs**: anyone with the CFO role can track actions from insights and prepare a
  citation-checked brief for a board, audit committee, lender or investor meeting (about $0.05 each).
- **What moved it** (insight cards): each surfaced metric carries a breakdown computed from records — customers
  behind a receivables move, vendors behind payables, the week's largest cash flows, cost centres against their
  8-week average, segments, unpaid invoices that fell due, open GST items. The AI is given these rows and must
  cite them. For cash, the card states how much of the bank-balance change the receipts, payments and expenses
  feed does not explain. Custom metrics have no breakdown. Digests made before this change do not show it.
- **Email is built for a phone**: a single fluid column with big tap targets, a light and a dark version, a plain-text
  alternative for clients that need it, and, per item, the headline sentences, the biggest movers and the action
  (the full reasoning and evidence stay in the app). Every email links into the app: "Open the briefing", each item's
  evidence, and Ask. Links use `PUBLIC_BASE_URL` (in multi-tenant mode, each tenant's own host), so set it to the public
  address or the links point nowhere useful. The red-line alert shows the figures against the limit, not raw data.
- **Ask** (CFO menu, CFO role and above): the CFO questions the whole briefing, not one insight. The agent answers
  from what the briefing already says (every insight, its evidence and its "what moved it" rows), reads the ERP
  data and trends, and can run simple what-if scenarios; every figure links to its source and anything the citation
  gate could not match is flagged. Questions and answers are private to the signed-in user (stored in `ask_messages`
  by email; viewers cannot ask) and can be deleted with "Start over". About $0.05 a question. Migration `0004`.
- **New vs continuing**: each insight is marked "New this week" or "Continuing since W27", worked out from the
  stored digests (same identity as the Briefing's "what changed": the metric, else the red-line rule). The AI is
  told which it is, the finished text is checked against it, and a contradiction (for example "a fresh matter" about
  a notice already in last week's briefing) gets one rewrite; any that remains shows as "Check the wording" on the card.
- **Pilot scorecard** (set-up menu): weeks the briefing was opened, insights surfaced, share given a verdict,
  and the success test — at least one insight acted on that the CFO had not already spotted. "Opened" counts
  views by users in the CFO role (any user when sign-in is off).
- **Metrics with no data are blank, never estimated.** Segments and cost centres are read from the client's
  own revenue and expense rows. Close cycle needs `gl_entries`; unreconciled items and board-pack readiness
  stay blank until a reconciliation feed exists. `free_cash_flow_cr` is net operating cash flow (no capex).
- **Period lens** (Briefing, `GET /api/periods`): change since the start of the month, fiscal quarter and fiscal
  year, and against a year ago, built from the weekly readings. Set `FISCAL_YEAR_START_MONTH` (default 4 for
  an Indian April–March year; 1 for a calendar year). Cash flow is totalled and rates averaged over the period.
  A comparison needs the data to reach back far enough (a month after the first record, and a full period
  before the one being compared), otherwise it shows a dash; year-on-year needs a year of history.
  Vs plan shows only for metrics that have plan rows.
- **Memory safety**: notes the model infers from conversations are sanitised, labelled untrusted in every
  prompt, and can never overwrite the CFO office's own notes (`cfo_notes` block, Memory page).

## Operations

- **Logs:** JSON lines with a request ID (also returned as `X-Request-ID`); ship stdout to the
  client's log platform.
- **Backups:** `docker compose exec db pg_dump -U cfo cfo | gzip > cfo-$(date +%F).sql.gz` (daily),
  plus the `inbox/processed` folder if source files must be retained.
- **Upgrades:** `git pull && docker compose up -d --build` — migrations run on start.
  New migrations: `cd backend && DATABASE_URL=… uv run alembic revision --autogenerate -m "…"`.
- **Cost guardrails:** `MAX_LLM_COST_USD_PER_RUN` (default $5) caps a digest run; a typical run
  costs about $0.20 and a chat question about $0.05.
- **Stuck work:** runs with no progress for 30 minutes are marked failed by the worker so the
  week can be re-run.
