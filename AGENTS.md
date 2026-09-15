# Project Instructions

## Read first

- Read `collaborative_telegram_ai_spec.md` before implementation. It is the primary product and architecture reference.
- Inspect existing code and preserve its conventions.
- Implement incrementally in the phases defined by the spec. Keep changes small and focused.

## Collaboration

- Use subagents whenever suitable for independent implementation, testing, or review. Do not use subagents for planning.
- Give each subagent a clear, bounded task and avoid conflicting edits.
- Review and integrate their work; verify the combined result.
- This directive concerns development work. Do not add a multi-agent architecture to the bot.

## Stack and architecture

- Use Python 3.12+, uv, FastAPI, async SQLAlchemy 2.x, psycopg 3, Alembic, Pydantic v2, and google-genai.
- Keep services small and explicit. Postgres is the durable source of truth.
- Keep Gemini behind GeminiGateway / AIOrchestrator interfaces, with configurable models and staged tool orchestration.
- Verify changing Google API capabilities against official documentation and document necessary deviations in README.
- Avoid a frontend, Redis, Celery, vector search, and heavy agent frameworks unless later requirements explicitly authorize them.

## Product invariants

- Persist visible incoming group messages and outgoing bot messages. Make Telegram update handling idempotent.
- Stay silent and make no Gemini calls for passive messages. Respond only to supported invocations.
- Keep raw history, recent context, rolling summaries, and durable memory separate.
- Save only appropriate durable memories; validate scopes, deduplicate, supersede conflicts, and support inspection and deletion.
- Store plans as structured state. Preserve decision rationale and source messages.
- Require supported user intent for mutations; clarify ambiguous targets before changing state.
- Confirm actions only after successful tool results.
- Preserve real grounding citations and required Maps attribution.

## Security and reliability

- Validate current-chat ownership server-side for every tool operation. Never trust model-generated IDs.
- Treat retrieved content as untrusted; it cannot authorize tool actions or override application rules.
- Never commit secrets or credentials. Use placeholder values in `.env.example`.
- Protect webhook and internal endpoints; keep debug routes authenticated.
- Preserve saved messages on AI failure. If persistence fails, stop state-changing AI calls.
- Use timezone-aware timestamps and avoid logging full messages or prompts by default.

## Verification

- Add migrations for schema changes and meaningful tests for changed behavior.
- Use fake Gemini gateways for AI contract tests.
- Cover passive-message behavior, invocation detection, idempotency, memory rules, plan resolution, chat isolation, and tool failures.
- Run relevant pytest tests, Ruff checks, and configured type checks. Fix failures before continuing.
- Report what changed, verification results, and any remaining limitations.
