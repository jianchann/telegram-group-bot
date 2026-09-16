# Collaborative Telegram AI Assistant

The bot runs locally with FastAPI, Docker Postgres, and Gemini. It stores visible messages in allowlisted groups, stays silent during ordinary conversation, and answers an explicit mention, a reply to this bot, or `/ask`. It maintains durable shared memory, structured group plans, saved plan links, task assignments, and native Telegram polls. Invoked research can use Google Search, Google Maps, URL Context, and Gemini-managed Code Execution. `/start`, `/help`, `/memory`, `/memory_me`, `/plans`, `/plan`, and `/tasks` work without calling Gemini.

Reminders, poll-result tracking, saved file artifacts, and cloud deployment remain outside the current scope. Read [the specification](collaborative_telegram_ai_spec.md) and [project instructions](AGENTS.md) before extending it.

## Local setup

Install uv, Docker with Compose, gcloud, and ngrok. On Windows/WSL, enable Docker Desktop's WSL integration for this distribution and verify `docker info` before proceeding.

```bash
uv python install 3.12
uv sync
docker compose --env-file /dev/null up -d --wait db
```

The development database uses a persistent Docker volume and is bound to localhost. Its example credentials are for local development only. Compose commands specify `--env-file /dev/null` so Compose does not automatically load an existing project `.env`. Configure process environment variables in your own shell or secret manager; the application and CLI do **not** automatically read `.env` files. `.env.example` documents settings with placeholders. Never commit real credentials.

```bash
export DATABASE_URL='postgresql+psycopg://telegram:telegram_dev_only@127.0.0.1:5432/telegram'
export GCP_PROJECT_ID='your-cloud-project'
export GCP_LOCATION='global'
export GEMINI_MODEL='gemini-3.5-flash-lite'
```

Enter secrets without putting their values in shell history:

```bash
read -rsp 'Telegram bot token: ' TELEGRAM_BOT_TOKEN
export TELEGRAM_BOT_TOKEN
echo
```

The webhook secret does not come from Telegram or BotFather. Generate it yourself,
export it, and print it once so you can copy it into a password manager. The generated
value does not appear in shell history:

```bash
export TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
printf 'Save this webhook secret: %s\n' "$TELEGRAM_WEBHOOK_SECRET"
```

Keep this same value available both when starting FastAPI and when running
`webhook-set`. The setup command sends it to Telegram as `secret_token`; Telegram then
includes it in the `X-Telegram-Bot-Api-Secret-Token` header on webhook requests, which
the application verifies. After saving it, clear the terminal output if appropriate.
Store it like a password and never commit it. To restore an existing value from your
password manager in a new shell without putting it in shell history, use:

```bash
read -rsp 'Existing webhook secret: ' TELEGRAM_WEBHOOK_SECRET
export TELEGRAM_WEBHOOK_SECRET
echo
```

If you generate a replacement, run `webhook-set` again so Telegram receives the new
value. Create your bot through BotFather, add it to a private test group, and disable
Privacy Mode through BotFather `/setprivacy`. If necessary, remove and re-add the bot
for that change to take effect. Telegram can only deliver messages visible to the bot;
historical messages are not imported. [Telegram bot privacy documentation](https://core.telegram.org/bots/features#privacy-mode)

Before registering a webhook, send a normal group message and discover its numeric ID:

```bash
uv run python -m app.cli discover-chats
export ALLOWED_TELEGRAM_CHAT_IDS='[-1001234567890]'
```

Replace the sample ID with your result; multiple group IDs use a JSON array. `discover-chats` explicitly reads pending `getUpdates` without an offset or acknowledgment, and prints only group IDs. It refuses to run while a webhook is configured. To switch an existing bot from webhook delivery, explicitly run `webhook-delete` first; pending updates are preserved. Alternatively, `uv run python -m app.cli identify-chat` accepts one Telegram update JSON on stdin and prints its group ID, without credentials, persistence, or Gemini access. [Telegram update delivery documentation](https://core.telegram.org/bots/api#getupdates)

Enable billing and the Vertex AI API in the chosen project, and grant your local identity the required Vertex AI permissions. Set up Application Default Credentials interactively:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GCP_PROJECT_ID"
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The server validates required database, Google project, bot, secret, and allowlist configuration at startup. ADC credentials remain outside the repository. [Google ADC setup](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc)

In another terminal, start the tunnel:

```bash
ngrok http 8000
```

In a shell with the token and secret exported, register the HTTPS URL ngrok gives you, including the route:

```bash
uv run python -m app.cli webhook-set --url https://YOUR-NGROK-HOST/telegram/webhook
uv run python -m app.cli webhook-info
```

Re-register when the tunnel URL changes. `webhook-info` prints sanitized status and pending counts, without URLs or provider error descriptions. `webhook-delete` explicitly removes delivery and preserves queued updates. No setup command drops pending updates automatically. [ngrok HTTP endpoints](https://ngrok.com/docs/gateway/endpoints/http), [Telegram webhook setup](https://core.telegram.org/bots/api#setwebhook)

## Behavior and limits

Every allowlisted group member may invoke the bot. Unlisted chats, private chats, and channels are ignored before persistence or AI. The webhook requires the exact Telegram secret header. No debug endpoints are exposed; `GET /health` provides a health response.

Invocations use up to `MAX_RECENT_MESSAGES=30` preceding messages from the current group, chronological speaker context, and the current request separately. `MAX_CONTEXT_CHARS=24000` caps conversation input, dropping older context first. `DEFAULT_CHAT_TIMEZONE=Asia/Manila` follows the specification independently of the workstation timezone. Default limits are `AI_USER_REQUESTS_PER_MINUTE=5` per user within a group and `AI_CHAT_REQUESTS_PER_MINUTE=20` per group; output defaults to `MAX_AI_OUTPUT_TOKENS=1024`. Network bounds use `GEMINI_TIMEOUT_SECONDS=20`, `TELEGRAM_TIMEOUT_SECONDS=10`, and an overall `WEBHOOK_TIMEOUT_SECONDS=55`.

On an AI invocation, older conversation is rolled into one chat summary after more than `SUMMARY_MESSAGE_THRESHOLD=100` unsummarized messages or `SUMMARY_CHAR_THRESHOLD=40000` characters. The newest 30 raw messages remain separate. Summary input is capped by `SUMMARY_MAX_INPUT_CHARS=20000`, output by `SUMMARY_MAX_OUTPUT_TOKENS=512`, and its provider call by `SUMMARY_TIMEOUT_SECONDS=10`. A provider failure leaves the prior summary in use; a storage failure stops processing. Summary calls are recorded separately in AI-run telemetry. Token and feature telemetry is stored, while dollar-cost estimates remain unset because model pricing is not encoded in the application.

An invoked message, or its directly replied-to message, may supply one JPEG, PNG, WebP, PDF, or UTF-8 plain-text attachment. `MAX_MEDIA_BYTES=10000000` limits the in-memory download. Passive media never downloads or calls Gemini. Unsupported, oversized, failed, or signature-mismatched files receive a deterministic response without a Gemini call. Albums, audio, video, office documents, and durable file artifacts remain deferred.

Bot answers use a safe Telegram HTML subset for headings, bold text, bullets, paragraphs, and clickable HTTP(S) URLs. Input is escaped, output is split by Telegram's UTF-16 limit without breaking tags, and an equivalent plain-text chunk is retried only when Telegram rejects formatting.

The gateway uses the SDK's asynchronous `generate_content` API with ADC and an explicit project/location. It retains `generateContent` rather than migrating the application to the preview Interactions API. Research is requested through an application-owned function, runs as a separate built-in-only call, and returns to the existing function/synthesis loop because the SDK's server-side invocation-circulation flag is not supported on the enterprise backend. Each research step enables one built-in kind; provider failures are returned to Gemini for an honest, limited final answer. Postgres owns history, memory, and plans, while provider conversation state remains invocation-local. `GeminiGateway` and `AIOrchestrator` isolate the provider details. Google's SDK documentation now calls the managed Google Cloud backend Gemini Enterprise Agent Platform; the installed SDK's enterprise configuration remains the ADC integration path. Model availability and IAM depend on the project; change `GEMINI_MODEL` if your project cannot access the configured default. [Official SDK documentation](https://googleapis.github.io/python-genai/), [tool combinations](https://ai.google.dev/gemini-api/docs/tool-combination), [Google Search](https://ai.google.dev/gemini-api/docs/google-search), [Google Maps](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/grounding/grounding-with-google-maps), [URL Context](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/url-context), [Code Execution](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/code-execution-api)

## Shared memory

Ask `@bot remember that we prefer morning flights`, `@bot what do you remember about us?`, or `@bot forget our morning flight preference`. Personal facts such as `@bot remember that Anna is vegetarian` must resolve to an observed member of this chat. The bot asks for clarification if a person is unknown or ambiguous. Usernames help resolve people; stable Telegram identities remain authoritative.

`/memory [page]` lists active group and personal memories with separate labels. `/memory_me [page]` lists facts about the caller in this group. These commands use Postgres directly, paginate 20 entries per page, and make no Gemini calls. Personal memories are shared within the group; they are not private or cross-group profiles. Any observed group member may save, correct, or forget these shared facts.

`AUTO_MEMORY_ENABLED=true` allows useful durable facts from recent human conversation to be saved when the bot is invoked. Passive messages never trigger extraction. Setting it to `false`, or setting the chat's `auto_memory_enabled` to `false`, disables automatic saves; explicit remember requests still work. `MAX_MEMORY_RESULTS=20` caps memories included in answer context, not the total number users may inspect through pagination. Memory and recent history use up to half the configured input-character limit, reserving room for tool results. Continued model requests are checked against the full limit; memory search results are capped at 4,000 characters per tool call.

Duplicate facts are not repeatedly inserted, conflicting keyed facts supersede older versions, and forgotten facts are soft-deleted. Matching deleted keys/content suppress automatic resaving from old context; an explicit new remember request can save them again. Forgetting durable memory does not delete raw chat history, so the conversation itself may still contain the statement.

Each AI invocation permits at most `MAX_AI_TURNS=4` model turns and `MAX_AI_TOOL_CALLS=8` validated tools under a shared AI timeout and output-token budget. The final turn is reserved for synthesis. Token usage is aggregated across turns, including reported thinking tokens. Saved/updated/forgotten acknowledgements come from successful database results. If the final AI answer fails after a memory change commits, the bot still confirms that change and explains the remaining answer failed. No state change is authorized by a quote, webpage, bot message, or stored memory instruction.

Apply new migrations with `uv run alembic upgrade head` using your configured `DATABASE_URL`, then restart FastAPI. Phases 2 and 3 require no dependencies beyond the existing project lockfile.

## Structured plans

Create a plan with a direct request such as `@bot start a Seoul trip plan for Nov 26–30`. Plans contain participants, constraints, decisions with their stated reasons and alternatives, tasks and assignees, activities, open questions, and notes. Missing dates or participants do not block creation. A yearless range uses its next non-past occurrence in the group timezone when unambiguous.

`/plans [page]` lists active plans, 20 per page. `/plan [name]` shows a named active plan; without a name it shows the sole active plan or asks which plan to use. Both commands read Postgres directly without Gemini or an AI rate-limit reservation. Plan details and confirmations do not expose internal identifiers.

Any observed member of an allowlisted group may edit its shared plans. Plan membership records participation and task assignment; it is not an edit permission boundary. The creator is a participant, while other people are added only when the conversation clearly identifies them as participants, travelers, attendees, owners, or assignees.

Plan selection follows explicit name, the replied-to plan, a unique recent plan mention, then the sole active plan. An ambiguous mutation is rejected so the bot can ask for clarification. Creation, assignment, completion, removal, cancellation, reversal, and lifecycle changes require direct current intent. On an invocation, the bot may conservatively attach a clear prior constraint or a confirmed decision to one unambiguous active plan; passive messages alone never call Gemini or change plan state.

Plan items are soft-deleted for audit history. Plans are ended through completed, cancelled, or archived status. Plan-specific constraints and decisions stay in structured plan items rather than being duplicated into durable memory. Ask the bot to save a URL under an unambiguous plan and optional category; saving the same normalized URL again updates its supplied title/category rather than duplicating it. `/plan` displays saved links. Links are saved only from direct user intent and are never created automatically from research. Telegram file artifacts remain deferred.

`/tasks [page]` lists outstanding tasks across active plans, ordered by due date. `/tasks mine [page]` shows only tasks assigned to the invoking member. These views paginate 20 tasks at a time, query Postgres directly, and do not call Gemini. Task creation, assignment, completion, and cancellation still require explicit current intent through an unambiguous plan.

Ask the bot to create a poll or vote on two to ten options to send a native regular Telegram poll. Polls default to non-anonymous and single-answer unless the request clearly asks otherwise. The returned poll message is persisted with normal bot history. Vote totals, voter identities, quizzes, media polls, and poll management are not tracked in this phase. [Telegram native poll documentation](https://core.telegram.org/bots/api#sendpoll)

## Grounded research

On an AI invocation, Gemini can answer from supplied context, read missing saved state,
or request the application-owned `research` function. Research is available regardless
of wording; no keyword router decides eligibility. Gemini is instructed to research
real-world recommendations and changing facts before making factual claims, while
answering saved-state, writing, and conceptual questions from supplied information.
This improves access to verification but does not guarantee factual accuracy.

Each research request selects one kind: Google Search for web facts, Google Maps for
places/routes, URL Context for linked content, or managed Code Execution for
calculations. The application runs a separate built-in-only provider call and returns
its results to the original reasoning conversation. Missing saved links can be read
from the current chat's plan before research. URLs must come from supplied human
context, server-checked saved artifacts, or verified grounding from this invocation;
`MAX_URL_CONTEXT_URLS=5` caps each URL Context request.

At most two actual research steps run per invocation. Identical requests reuse their
result, including failed results. Every research function request counts toward
`MAX_AI_TOOL_CALLS`; each provider request counts toward `MAX_AI_TURNS`. All steps
share the invocation timeout and output-token budget, and research must leave a
following turn for synthesis. Insufficient budget, invalid URLs, failed research,
and missing verification evidence return failed tool results so Gemini can explain
the limitation. Passive messages and direct database commands never trigger research.

Grounded responses append only provider-returned source links. Google Maps sources are labeled `Google Maps` and immediately follow the supported answer. URL retrieval failures, unsafe pages, and paywalls are reported without claiming the page was read. Retrieved pages and search results are untrusted data and cannot authorize memory or plan mutations. Code Execution runs on Google's managed service; the application never executes generated code locally.

The configured Gemini model and Google Cloud project must support each requested built-in tool. Provider availability still requires live acceptance with the configured project and model.

Incoming messages and processing claims are committed before network calls. Duplicate claimed or finished updates cannot repeat an AI invocation or reply. Initial persistence failure returns a retryable webhook failure without AI work. Gemini failure retains messages and sends a short retry response; the user should issue a fresh invocation.

Processing happens inside the webhook request with bounded timeouts. A process crash after claiming an update can leave its claim unfinished; use a fresh user invocation rather than automatically replaying that claim. An ambiguous Telegram send timeout is recorded as uncertain and is not automatically resent. Telegram sending and database commits cannot guarantee exactly-once delivery across crashes, so a delivered reply can exceptionally be missing from Postgres. Logs omit full message contents, prompts, and credential-bearing request URLs by default.

## Production deployment on Cloud Run

The following is an operator runbook. Run these commands yourself from a trusted
terminal. The application does not create cloud resources, change IAM, migrate the
production database, register the webhook, or move Cloud Run traffic automatically.

Use one Cloud Run service backed by Neon. This example uses Singapore and fixed names;
keep them consistent throughout the commands:

```bash
export GCP_PROJECT_ID='your-cloud-project'
export CLOUD_RUN_REGION='asia-southeast1'
export CLOUD_RUN_SERVICE='telegram-group-bot'
export RUNTIME_SA_NAME='telegram-group-bot-run'
export RUNTIME_SA="$RUNTIME_SA_NAME@$GCP_PROJECT_ID.iam.gserviceaccount.com"
gcloud config set project "$GCP_PROJECT_ID"
```

Enable the required APIs and create the runtime identity:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com
gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
  --display-name='Telegram group bot runtime'
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role='roles/aiplatform.user'
```

Create three Secret Manager secrets. This is required only once:

```bash
gcloud secrets create telegram-bot-token --replication-policy=automatic
gcloud secrets create telegram-webhook-secret --replication-policy=automatic
gcloud secrets create telegram-database-url --replication-policy=automatic
```

Enter the values without putting them in shell history, then create version 1 of each
secret. Use the pooled connection string from Neon's **Connect** dialog for the
Cloud Run database value; its host contains `-pooler`.

```bash
read -rsp 'Telegram bot token: ' TELEGRAM_BOT_TOKEN; echo
printf %s "$TELEGRAM_BOT_TOKEN" | \
  gcloud secrets versions add telegram-bot-token --data-file=-

read -rsp 'Existing webhook secret: ' TELEGRAM_WEBHOOK_SECRET; echo
printf %s "$TELEGRAM_WEBHOOK_SECRET" | \
  gcloud secrets versions add telegram-webhook-secret --data-file=-

read -rsp 'Neon pooled DATABASE_URL: ' PRODUCTION_DATABASE_URL; echo
printf %s "$PRODUCTION_DATABASE_URL" | \
  gcloud secrets versions add telegram-database-url --data-file=-
unset PRODUCTION_DATABASE_URL
```

Grant the runtime identity access to only those secrets:

```bash
for secret_name in \
  telegram-bot-token telegram-webhook-secret telegram-database-url
do
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --member="serviceAccount:$RUNTIME_SA" \
    --role='roles/secretmanager.secretAccessor'
done
```

Neon recommends a direct, non-pooled connection for schema migrations. Copy the direct
connection string from Neon, apply the migrations from this checkout, and then remove
it from the shell. Never run Alembic automatically at container startup.

```bash
read -rsp 'Neon direct migration URL: ' DATABASE_URL; echo
export DATABASE_URL
uv run alembic upgrade head
unset DATABASE_URL
```

Create a temporary file containing non-secret service configuration. Replace the chat
ID with the complete production allowlist. This file is outside the repository and can
be deleted after deployment.

```bash
cat > /tmp/telegram-group-bot-cloud-run-env.yaml <<EOF
ENV: production
GCP_PROJECT_ID: $GCP_PROJECT_ID
GCP_LOCATION: global
GEMINI_MODEL: gemini-3.5-flash-lite
ALLOWED_TELEGRAM_CHAT_IDS: '[-1001234567890]'
MAX_AI_TURNS: '6'
MAX_AI_TOOL_CALLS: '12'
GEMINI_TIMEOUT_SECONDS: '20'
SUMMARY_TIMEOUT_SECONDS: '10'
TELEGRAM_TIMEOUT_SECONDS: '10'
WEBHOOK_TIMEOUT_SECONDS: '55'
EOF
```

Deploy the source. The checked-in Dockerfile supplies the production start command.
Cloud Run must allow public requests because Telegram cannot attach Google IAM
credentials; the application still verifies Telegram's secret header before parsing a
webhook request.

```bash
gcloud run deploy "$CLOUD_RUN_SERVICE" \
  --source=. \
  --region="$CLOUD_RUN_REGION" \
  --service-account="$RUNTIME_SA" \
  --allow-unauthenticated \
  --env-vars-file=/tmp/telegram-group-bot-cloud-run-env.yaml \
  --set-secrets='DATABASE_URL=telegram-database-url:1,TELEGRAM_BOT_TOKEN=telegram-bot-token:1,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:1' \
  --cpu=1 \
  --memory=512Mi \
  --concurrency=20 \
  --min=0 \
  --max=3 \
  --timeout=70s
rm /tmp/telegram-group-bot-cloud-run-env.yaml
```

Retrieve the stable service URL and check startup. If `/health` fails, inspect the logs
before registering Telegram.

```bash
export SERVICE_URL="$(gcloud run services describe "$CLOUD_RUN_SERVICE" \
  --region="$CLOUD_RUN_REGION" --format='value(status.url)')"
curl --fail --show-error "$SERVICE_URL/health"
gcloud run services logs read "$CLOUD_RUN_SERVICE" \
  --region="$CLOUD_RUN_REGION" --limit=50
```

Register the production webhook once. These commands read version 1 into the current
shell without printing it. The Cloud Run URL remains stable across later revisions.

```bash
export TELEGRAM_BOT_TOKEN="$(gcloud secrets versions access 1 \
  --secret=telegram-bot-token)"
export TELEGRAM_WEBHOOK_SECRET="$(gcloud secrets versions access 1 \
  --secret=telegram-webhook-secret)"
uv run python -m app.cli webhook-set \
  --url "$SERVICE_URL/telegram/webhook"
uv run python -m app.cli webhook-info
unset TELEGRAM_BOT_TOKEN TELEGRAM_WEBHOOK_SECRET
```

Cloud Run configuration updates create revisions without changing `SERVICE_URL`. To
replace the allowed groups, pass the complete JSON list. The alternate `@` delimiter
allows commas inside the value:

```bash
gcloud run services update "$CLOUD_RUN_SERVICE" \
  --region="$CLOUD_RUN_REGION" \
  --update-env-vars='^@^MAX_AI_TURNS=6@MAX_AI_TOOL_CALLS=12@ALLOWED_TELEGRAM_CHAT_IDS=[-1001111111111,-1002222222222]'
```

No webhook update is required after an allowlist-only or ordinary code revision. If a
new group's ID is unknown while production already uses a webhook, use a short
maintenance window: export the bot token, run `webhook-delete`, send a group message,
run `discover-chats`, update the complete allowlist, wait for the new revision to become
healthy, and run `webhook-set` again with the unchanged production URL and secret.
These CLI commands preserve pending updates.

For subsequent code releases, run the checks below, apply any new migration with the
direct Neon URL, and repeat `gcloud run deploy --source=.` for the same service and
region. Existing service configuration and its public URL remain associated with the
service. Inspect revisions and roll back traffic when necessary:

```bash
gcloud run revisions list \
  --service="$CLOUD_RUN_SERVICE" --region="$CLOUD_RUN_REGION"
gcloud run services update-traffic "$CLOUD_RUN_SERVICE" \
  --region="$CLOUD_RUN_REGION" --to-revisions='PREVIOUS_REVISION=100'
```

### Investigating tool limits

Existing JSON logs now include `ai_turn_finished` for every provider attempt,
`ai_tool_finished` for executed or skipped custom calls, and `ai_tool_limit` when
requests exceed a tool budget. Filter by `request_id` to follow one invocation.
`turn_number` counts both reasoning and research provider turns; `attempt_number`
identifies each provider attempt. `tool_calls_executed` counts calls handed to an executor,
including research requests and calls that return an error. `ai_research_requested`
records the research kind, actual-step count, cache reuse, and rejection reason.
`ai_tool_finished.error_code` identifies rejected or unverified requests, including
`research_step_limit`, `research_turn_budget`, `url_not_allowed`, and
`research_unverified`. Requested tool names are logged without
arguments, results, messages, URLs, or prompts; unknown names appear as `unknown`.

`research_eligible` lists built-ins enabled for a separate research step;
`research_used` lists provider-reported
usage, not exact built-in execution counts. `tools_disabled_reason` explains why
custom functions were disabled: `research_stage`, `turn_budget`, or
`tool_call_budget`. The latter takes precedence if both budgets are exhausted.
`ai_tool_limit.limit_reason` distinguishes those two budgets. Check repeated tool
names and failed outcomes before increasing limits again. `ai_finished` also
includes the final error code.

Deploy these code changes yourself from the project directory (no database migration
is required). Use your existing service and region; this builds a new image and
retains existing environment variables and secret mappings:

```bash
gcloud run deploy "$CLOUD_RUN_SERVICE" \
  --project="$GCP_PROJECT_ID" \
  --source=. \
  --region="$CLOUD_RUN_REGION"
gcloud run revisions list \
  --service="$CLOUD_RUN_SERVICE" --region="$CLOUD_RUN_REGION"
```

Confirm the new revision is ready and receiving traffic in Cloud Run, then send a
fresh Telegram invocation and inspect its events in Logs Explorer. Keep your
current limit values, including in any deployment environment YAML you reuse.
The stable service URL and webhook secret are unchanged, so leave the webhook
registration in place. No Docker commands are required for this source deployment.

To rotate the webhook secret, add a new Secret Manager version, update the Cloud Run
secret mapping to that explicit version, verify the revision is healthy, and rerun
`webhook-set` locally with the same new value. Telegram retries non-successful webhook
deliveries during the brief secret transition. Re-register the webhook whenever its
secret, bot token, or public URL changes.

[Cloud Run source deployment](https://docs.cloud.google.com/run/docs/deploying-source-code),
[Cloud Run environment variables](https://cloud.google.com/run/docs/configuring/services/environment-variables),
[Cloud Run secrets](https://docs.cloud.google.com/run/docs/configuring/services/secrets),
[Neon connection pooling](https://neon.com/docs/connect/connection-pooling),
[Telegram webhook API](https://core.telegram.org/bots/api#setwebhook)

## Verification

```bash
docker build --tag telegram-group-bot:local .
docker compose --env-file /dev/null --profile test up -d --wait test-db
export TEST_DATABASE_URL='postgresql+psycopg://telegram:telegram_test_only@127.0.0.1:5433/telegram_test'
uv run pytest
uv run ruff check .
uv run mypy app
```

Start the test database and wait for its healthcheck before running pytest. The separate test database uses ephemeral storage on port 5433; never point integration tests at a database you need to keep. Tests use fake Telegram and Gemini clients, plus real-Postgres integration checks when `TEST_DATABASE_URL` is supplied. Without that URL, integration tests skip and do not establish database acceptance. The startup/configuration tests use synthetic credentials and fake clients; live server startup additionally requires your real exported allowlist, webhook secret, Cloud project, database, and ADC setup above.

For live acceptance in the private test group:

- Send ordinary conversation: it is persisted and the bot stays silent.
- Mention the bot and ask about that conversation: it answers with recent group context.
- Reply to the bot: it continues the exchange. Try `/ask`, `/help`, and `/start`.
- Add the bot to another, unlisted group: messages do not trigger Gemini or storage.
- Exceed the configured invocation limit: excess requests get a short explanation without Gemini calls.
- Replay a webhook update with its valid secret: it does not generate another reply.

For memory acceptance, remember a preference, move beyond the recent 30-message window, and verify the preference is still used. Correct it and verify only the latest fact is active. Forget it, then invoke the bot with old context still present and check `/memory` to verify it stays absent. Try the same actions in another group to confirm isolation.

Keep development Postgres running between sessions with its volume. `docker compose --env-file /dev/null stop` stops services without deleting the volume. Complete live Telegram, media, summary, and Gemini acceptance before production deployment.
