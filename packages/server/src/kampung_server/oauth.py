"""OAuth for the Claude app connector, on FastMCP's authorization server.

FastMCP serves discovery, /authorize, /token and /revoke and checks PKCE. This
module supplies the one fixed public client, the pending request that the
connect page completes, and opaque tokens held hashed in the store. There is
no client registration: the client lives in code.
"""

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, OAuthProvider
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.concurrency import run_in_threadpool

from . import audit, db, oauth_store
from .clock import parse_ts
from .keys import hash_key
from .throttle import CLIENT_ADDRESS
from .limits import (
    OAUTH_ACCESS_TTL,
    OAUTH_CODE_TTL,
    OAUTH_REFRESH_REUSE,
    OAUTH_REFRESH_TTL,
)

if TYPE_CHECKING:
    from .deps import ServerDeps

CLIENT_ID = "kampung-claude"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
SCOPES = ["kampung", "offline_access"]
ACCESS_PREFIX = "kpa_"
REFRESH_PREFIX = "kpr_"
CODE_PREFIX = "kpc_"
MAX_STATE_LENGTH = 1024
MIN_CHALLENGE_LENGTH = 43
MAX_CHALLENGE_LENGTH = 128
INDEX_MAX_AGE = 1.0


class TokenIndex:
    """Hashes of access tokens worth a database lookup, held in memory so a
    wrong token costs nothing. Same shape as KeyIndex."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._monotonic = monotonic
        self._hashes: frozenset[bytes] = frozenset()
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def is_unknown(self, token: str) -> bool:
        """True when the token is surely wrong; never touches the database."""
        return self._fresh() and hash_key(token) not in self._hashes

    def is_known(self, token: str) -> bool:
        return hash_key(token) in self._hashes

    def add(self, token: str) -> None:
        """A token minted now, so the index never 401s it as stale."""
        with self._lock:
            self._hashes = self._hashes | {hash_key(token)}

    def check(self, token: str) -> bool:
        """Whether the token is in the index after a reload if needed; blocking."""
        if hash_key(token) in self._hashes:
            return True
        conn = self._connect()
        try:
            self._reload(conn)
        finally:
            conn.close()
        return hash_key(token) in self._hashes

    def _fresh(self) -> bool:
        loaded = self._loaded_at
        return loaded is not None and self._monotonic() - loaded < INDEX_MAX_AGE

    def _reload(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if self._fresh():
                return
            self._hashes = oauth_store.live_access_hashes(conn, self._clock())
            self._loaded_at = self._monotonic()


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class KampungOAuth(OAuthProvider):
    def __init__(self, deps: "ServerDeps", public_url: str) -> None:
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=False, valid_scopes=SCOPES, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._deps = deps
        self.public_url = public_url.rstrip("/")
        self.resource = f"{self.public_url}/mcp"
        # Hash of a rotated refresh token -> (rotated at, the pair issued for
        # it). In memory only: a retry after a restart is refused. Refreshes
        # run on threadpool threads, so the dict is guarded.
        self._retries: dict[bytes, tuple[int, OAuthToken]] = {}
        self._retries_lock = threading.Lock()

    # Client

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id != CLIENT_ID:
            return None
        return OAuthClientInformationFull(
            client_id=CLIENT_ID,
            client_name="Claude",
            redirect_uris=[AnyUrl(CLAUDE_CALLBACK)],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            scope=" ".join(SCOPES),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise RegistrationError(
            "invalid_client_metadata",
            "This server does not register clients; use the client id " f"{CLIENT_ID}.",
        )

    # Authorize and connect

    def _is_this_resource(self, resource: str) -> bool:
        return resource.rstrip("/") in (self.public_url, self.resource)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if str(params.redirect_uri) != CLAUDE_CALLBACK:
            raise AuthorizeError("invalid_request", "redirect_uri is not allowed")
        if params.resource is not None and not self._is_this_resource(params.resource):
            # RFC 8707 says invalid_target; the SDK's error codes stop at RFC 6749.
            raise AuthorizeError("invalid_request", "unknown resource")
        if params.state is not None and len(params.state) > MAX_STATE_LENGTH:
            raise AuthorizeError("invalid_request", "state is too long")
        challenge = len(params.code_challenge)
        if not MIN_CHALLENGE_LENGTH <= challenge <= MAX_CHALLENGE_LENGTH:
            raise AuthorizeError("invalid_request", "code_challenge: wrong length")
        requested = SCOPES if params.scopes is None else params.scopes
        pending = params.model_copy(
            update={"scopes": list(dict.fromkeys(requested)), "resource": self.resource}
        )
        request_id = secrets.token_urlsafe(32)
        client_address = CLIENT_ADDRESS.get()

        def work() -> bool:
            conn = self._deps.connect()
            try:
                with db.transaction(conn):
                    return oauth_store.save_request(
                        conn,
                        request_id,
                        pending.model_dump_json(),
                        self._deps.clock(),
                        client_address,
                    )
            finally:
                conn.close()

        if not await run_in_threadpool(work):
            raise AuthorizeError(
                "temporarily_unavailable", "too many sign-ins waiting; try again soon"
            )
        return f"{self.public_url}/oauth/connect?request={request_id}"

    def complete_connect(
        self, conn: sqlite3.Connection, request_id: str, machine_id: int, now: datetime
    ) -> str:
        """Finish a request after a valid code: grant the machine, mint the
        authorization code, return the redirect back to Claude. Runs inside
        the caller's transaction, so a dead request rolls the code back too.
        Raises ValueError for an unknown, used or expired request."""
        stored = oauth_store.take_request(conn, request_id, now)
        if stored is None:
            raise ValueError("unknown or expired connect request")
        pending = AuthorizationParams.model_validate_json(stored)
        row = conn.execute(
            "SELECT name FROM machines WHERE id = ?", (machine_id,)
        ).fetchone()
        grant_id = oauth_store.replace_grant(conn, machine_id, now)
        code = CODE_PREFIX + secrets.token_urlsafe(32)
        expires = now + OAUTH_CODE_TTL
        record = AuthorizationCode(
            code=code,
            scopes=pending.scopes or [],
            expires_at=_epoch(expires),
            client_id=CLIENT_ID,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
        )
        oauth_store.save_token(
            conn,
            grant_id,
            "code",
            code,
            record.model_dump_json(exclude={"code"}),
            expires,
        )
        audit.record(conn, now, f"machine:{row[0]}", "connect", {"grant": grant_id})
        return construct_redirect_uri(
            str(pending.redirect_uri), code=code, state=pending.state
        )

    # Codes

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def work() -> AuthorizationCode | None:
            conn = self._deps.connect()
            try:
                loaded = oauth_store.load_token(
                    conn, "code", authorization_code, self._deps.clock()
                )
            finally:
                conn.close()
            if loaded is None or loaded[2] is not None:
                return None
            code = AuthorizationCode(code=authorization_code, **json.loads(loaded[1]))
            return code if code.client_id == client.client_id else None

        return await run_in_threadpool(work)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # TokenError is a frozen dataclass, so it cannot pass through the
        # transaction context manager: decide inside, raise outside.
        def work() -> OAuthToken | None:
            now = self._deps.clock()
            conn = self._deps.connect()
            try:
                with db.transaction(conn):
                    taken = oauth_store.take_token(
                        conn, "code", authorization_code.code, now
                    )
                    if taken is None:
                        return None
                    oauth_store.delete_token(conn, "code", authorization_code.code)
                    return self._issue(
                        conn,
                        taken[0],
                        authorization_code.scopes,
                        authorization_code.scopes,
                        authorization_code.resource,
                        now,
                    )
            finally:
                conn.close()

        issued = await run_in_threadpool(work)
        if issued is None:
            raise TokenError("invalid_grant", "authorization code is not valid")
        self._deps.tokens.add(issued.access_token)
        return issued

    # Refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """A rotated token loads too: reuse is judged in the exchange."""

        def work() -> RefreshToken | None:
            conn = self._deps.connect()
            try:
                loaded = oauth_store.load_token(
                    conn, "refresh", refresh_token, self._deps.clock()
                )
            finally:
                conn.close()
            if loaded is None:
                return None
            token = RefreshToken(token=refresh_token, **json.loads(loaded[1]))
            return token if token.client_id == client.client_id else None

        return await run_in_threadpool(work)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Tokens minted here are indexed only after COMMIT (see _issue).
        fresh: list[str] = []

        def work() -> OAuthToken | None:
            now = self._deps.clock()
            epoch = _epoch(now)
            secret = refresh_token.token
            key = hash_key(secret)
            reuse = OAUTH_REFRESH_REUSE.total_seconds()
            with self._retries_lock:
                for stale in [
                    k for k, v in self._retries.items() if epoch - v[0] > reuse
                ]:
                    del self._retries[stale]
            conn = self._deps.connect()
            try:
                with db.transaction(conn):
                    loaded = oauth_store.load_token(conn, "refresh", secret, now)
                    if loaded is None:
                        return None
                    grant_id, _, used_at = loaded
                    if used_at is None:
                        oauth_store.take_token(conn, "refresh", secret, now)
                        issued = self._issue(
                            conn,
                            grant_id,
                            scopes,
                            refresh_token.scopes,
                            refresh_token.resource,
                            now,
                        )
                        with self._retries_lock:
                            self._retries[key] = (epoch, issued)
                        fresh.append(issued.access_token)
                        return issued
                    rotated_at = _epoch(parse_ts(used_at))
                    if epoch - rotated_at > reuse:
                        oauth_store.delete_grant(conn, grant_id, now)
                        audit.record(
                            conn, now, "oauth", "token_reuse", {"grant": grant_id}
                        )
                        return None
                    # A retry inside the window gets the pair already issued,
                    # never a new one, so one token cannot become two sessions.
                    # Once that pair's own refresh token has been used, the
                    # retry is refused: handing it out again would make the
                    # client's next refresh look like a reuse.
                    with self._retries_lock:
                        entry = self._retries.get(key)
                    if entry is None or entry[1].refresh_token is None:
                        return None
                    pair_refresh = oauth_store.load_token(
                        conn, "refresh", entry[1].refresh_token, now
                    )
                    if pair_refresh is None or pair_refresh[2] is not None:
                        return None
                    issued_at, issued = entry
                    expires_in = (issued.expires_in or 0) - (epoch - issued_at)
                    return issued.model_copy(update={"expires_in": expires_in})
            finally:
                conn.close()

        issued = await run_in_threadpool(work)
        for token in fresh:
            self._deps.tokens.add(token)
        if issued is None:
            raise TokenError("invalid_grant", "refresh token is not valid")
        return issued

    # Access

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(ACCESS_PREFIX) or self._deps.tokens.is_unknown(token):
            return None

        def work() -> AccessToken | None:
            if not self._deps.tokens.check(token):
                return None
            now = self._deps.clock()
            conn = self._deps.connect()
            try:
                loaded = oauth_store.load_token(conn, "access", token, now)
                if loaded is None:
                    return None
                record = json.loads(loaded[1])
                revoked = conn.execute(
                    "SELECT revoked_at FROM machines WHERE id = ?",
                    (record["machine_id"],),
                ).fetchone()
            finally:
                conn.close()
            if revoked is None or revoked[0] is not None:
                return None
            return AccessToken(
                token=token,
                client_id=record["client_id"],
                scopes=["kampung"],
                expires_at=record["expires_at"],
                resource=record["resource"],
                claims={"machine_id": record["machine_id"], "connector": True},
            )

        return await run_in_threadpool(work)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """End the machine's grant, whichever token Claude sends."""

        def work() -> None:
            now = self._deps.clock()
            conn = self._deps.connect()
            try:
                with db.transaction(conn):
                    for kind in ("access", "refresh"):
                        loaded = oauth_store.load_token(conn, kind, token.token, now)
                        if loaded is not None:
                            oauth_store.delete_grant(conn, loaded[0], now)
                            audit.record(
                                conn, now, "oauth", "disconnect", {"grant": loaded[0]}
                            )
                            return
            finally:
                conn.close()

        await run_in_threadpool(work)

    def _issue(
        self,
        conn: sqlite3.Connection,
        grant_id: int,
        scopes: list[str],
        refresh_scopes: list[str],
        resource: str | None,
        now: datetime,
    ) -> OAuthToken:
        """Mint an access and refresh pair. Rotation keeps the refresh scopes."""
        bound = self.resource if resource is None else resource
        machine_id = conn.execute(
            "SELECT machine_id FROM oauth_grants WHERE id = ?", (grant_id,)
        ).fetchone()[0]
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        access_expires = now + OAUTH_ACCESS_TTL
        oauth_store.save_token(
            conn,
            grant_id,
            "access",
            access,
            json.dumps(
                {
                    "client_id": CLIENT_ID,
                    "scopes": scopes,
                    "expires_at": _epoch(access_expires),
                    "resource": bound,
                    "machine_id": machine_id,
                }
            ),
            access_expires,
        )
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        refresh_expires = now + OAUTH_REFRESH_TTL
        oauth_store.save_token(
            conn,
            grant_id,
            "refresh",
            refresh,
            RefreshToken(
                token=refresh,
                client_id=CLIENT_ID,
                scopes=refresh_scopes,
                expires_at=_epoch(refresh_expires),
                resource=bound,
            ).model_dump_json(exclude={"token"}),
            refresh_expires,
        )
        # Not indexed here: the caller adds it after COMMIT, or a reload in
        # between could read the committed rows without it and drop it.
        return OAuthToken(
            access_token=access,
            expires_in=int(OAUTH_ACCESS_TTL.total_seconds()),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
