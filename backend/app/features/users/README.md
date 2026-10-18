# Private workspace authentication

One deployment is one shared business workspace. Managers and operators share its
processes and documents; roles control changes. This is not a tenant isolation boundary.
Use separate deployments and databases for unrelated businesses.

Production sets `TRACE_REQUIRE_PASSWORD_AUTH=true` and a random
`TRACE_SESSION_SECRET` of at least 32 characters. Startup fails if the required secret is
missing. Keep the secret in the private runtime environment, outside Git. Changing it
revokes every browser session. `TRACE_SESSION_COOKIE_SECURE` defaults to `true`; disable
it only for local HTTP tests. `TRACE_SESSION_HOURS` defaults to 12.

Apply migration `0020` before serving this version. Existing accounts have no password
and cannot sign in until provisioned. Run inside the backend environment:

```sh
python -m app.features.users.password admin@example.com --name Administrator --role manager
```

The command prompts twice without echoing the password. For automated provisioning,
`--password-stdin` reads one line from a protected pipe or secret file; never pass the
password as a command argument. It creates an account or changes an existing account,
revoking its previous sessions. Omit name and role when only resetting a password.
Passwords must contain 12–1024 characters and use salted scrypt hashing. The dedicated
API manager cannot be provisioned as a browser account.

`GET /auth-config` reports `mode=password`. `POST /login` accepts email and password,
returns the public user profile, and sets an HttpOnly, Secure, SameSite=Strict cookie.
The cookie path follows the ASGI root path: set the server root path to the published
API mount, such as `/nexia/trace-it/api`. Every business route, including reads and file
downloads, requires a session or valid API Bearer token. `X-User-Id` cannot select or
override a password session identity. Cross-origin browser mutations are rejected.

`POST /logout` removes the cookie and revokes all browser sessions for that user.
Password changes also revoke sessions. User roles are read from the database on each
request. `/db` remains Bearer-only; mail ingestion keeps its distinct process-scoped
service token. Invalid Bearer credentials never fall back to a browser cookie.

Login applies a bounded per-account limit of ten attempts in five minutes per API
worker. Also rate-limit login at the trusted reverse proxy. Do not trust client-supplied
forwarding headers without configuring trusted proxies. Return `Cache-Control: no-store`
for API responses. With password mode enabled, the API does not enable wildcard CORS.

Leaving both the secret empty and the requirement disabled preserves the development
identity selector. Never publish that mode as an authenticated business deployment.
