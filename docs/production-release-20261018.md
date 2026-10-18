# Private business workspace release

This installation is a password-authenticated workspace shared by one business, not a
multi-tenant SaaS. Members share processes/documents. Deploy separate databases, storage
and secrets for unrelated businesses. Historical sample records are retained.

The root URL opens login. Deep links survive login/reload. Account settings provide logout
and manager-only account creation. The server administrator handles password recovery;
no email reset service is advertised. See `backend/app/features/users/README.md`.

Documentation is a tab in the existing process navigation, palette, theme and mobile UI.
It searches persisted document names/text, paginates and opens original evidence/downloads.
It does not generate answers, change rules or decide cases. Unreadable documents retain
original-file access. This follows Zenith's evidence-first UX using Trace-it's persisted
readings, without a second app, login, iframe or data store.

Zenith's semantic/hybrid search, reranking, generated answers, tenant RLS and label ACLs
are not implemented. Its repository is cloned at `/home/kripta/zenithEnterprise`; the
separate integration review records the remaining architecture. The VPS did not have
sufficient available memory for the full model stack.

## Authentication

Apply migration0020, provision a manager password, and set a random32+ character
`TRACE_SESSION_SECRET`. Production requires `TRACE_REQUIRE_PASSWORD_AUTH=true`; startup
fails without a secret. Compose enables this by default; isolated legacy CI opts out.
Cookies are HttpOnly/Secure/SameSite Strict and scoped to the API path. Logout/password
reset revoke all sessions. Service Bearer authentication remains separate, and `/db`
always requires Bearer. Business API responses use no-store.

Caddy must be the only external gateway, with frontend port bound to loopback. Its trusted
X-Forwarded-For drives per-client login throttling; the API also limits attempts by account.
Runtime secrets remain outside Git under `/opt/trace-it/secrets` and never enter bundles,
configuration responses, reports or process arguments.

## OpenAI

`tools/migrate_helmcode_openai.py` preflights all transformed configurations and replays
historical deterministic rules before normal version-service writes. It refuses pending
drafts. Run with an explicit database target during maintenance after a verified backup.
It commits in stages, not one transaction; rehearse on a restored database first.

Helmcode agent models become `openai-responses:gpt-6-luna`; unrelated providers remain.
Helmcode vision readers become `compatible:gpt-6-luna` at the official OpenAI endpoint.
Repeated Luna calls are not independent verification votes. Historical snapshots and
decisions remain immutable; new settings become new published versions.

Official model documentation: https://developers.openai.com/api/docs/models/gpt-6-luna
Actual synthetic tests cover Responses typed output, image reading, extraction citations
and candidate selection. Generic compatible-provider cost remains explicitly unknown.

## Release operations

Build immutable images labeled with the tested revision. Under the deployment lock, stop
the API; back up PostgreSQL, ingestion files, runtime secrets, compose and image references;
verify archive integrity. Apply0020, provision the private administrator, migrate providers,
and start candidate images. Verify readiness, anonymous rejection, password login, routes,
protected document access and logout before completion.

Retain previous images/backups. Migration0020 is additive; complete rollback also needs
the previous active process-version pointers and active agent configuration selections.
Never restore a database after reopening without accounting for new user writes, and never
silently disable authentication to recover availability.

Mail reception stays disabled until a real mailbox is configured and tested. The validation
report covers observed scenarios; it is not a guarantee for every business or input.
