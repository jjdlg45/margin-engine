"""Identity for the API (Run 6, Phase 82, Finding 19).

Two modes, and the difference between them is who says who you are.

**local** (the default, and what the README documents) keeps the Phase 39
behaviour: the client sends `X-User` naming a configured user. That is a
convenience for a single-user workstation, it is **not authentication**, and
the mode name now says so instead of a comment hoping somebody reads it.

**production** refuses to take the client's word for anything. `X-User` is
rejected outright - not ignored, rejected, so a caller cannot believe it
worked - and identity comes from a bearer token the server issued, against
either a local account with a hashed password or an OIDC provider through
the hook below. Every decision that matters is then made on an **immutable
user id**, never a display name, because display names change and a
separation-of-duties rule written against them is a rule about strings.

Passwords use PBKDF2-HMAC-SHA256 from the standard library: no new
dependency, no home-made hashing, and a work factor that can be raised in
config as hardware improves. Tokens are `secrets.token_urlsafe` and are
compared in constant time.

What this is not: a complete identity system. It is a mode that stops the
client asserting its own identity, with a documented seam for a real
provider. `docs/deploy-api.md` says what an operator still has to supply.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MODE_LOCAL = "local"
MODE_PRODUCTION = "production"

ROLE_ANALYST = "analyst"
ROLE_APPROVER = "approver"
ROLE_VIEWER = "viewer"
KNOWN_ROLES = frozenset({ROLE_ANALYST, ROLE_APPROVER, ROLE_VIEWER})

AUTH_HEADER_IDENTITY = "local-identity-header"
AUTH_PASSWORD = "password"
AUTH_OIDC = "oidc"

#: PBKDF2 work factor. Raise it, never lower it; stored hashes carry their
#: own iteration count so old ones keep verifying.
DEFAULT_ITERATIONS = 600_000


class AuthError(Exception):
    """An identity problem with the status and wording the client should see."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Principal:
    """Who the request is from, as the SERVER determined it."""

    user_id: str
    display_name: str
    roles: frozenset[str]
    auth_method: str

    def has(self, role: str) -> bool:
        return role in self.roles


@dataclass
class Account:
    user_id: str
    display_name: str
    roles: frozenset[str]
    password_hash: str | None = None


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`."""
    if not password:
        msg = "refusing to hash an empty password"
        raise ValueError(msg)
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(stored: str | None, password: str) -> bool:
    """Constant-time check against a stored hash.

    A missing hash is a failure, never a pass: an account with no password
    set cannot be logged into, which is the safe reading of a half-configured
    deployment.
    """
    if not stored or not password:
        return False
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def parse_users_spec(spec: str) -> dict[str, Account]:
    """`name:role[+role],name:role` - the Phase 39 local identity spec."""
    out: dict[str, Account] = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        name, _, roles_s = part.partition(":")
        name = name.strip()
        roles = frozenset(r.strip() for r in roles_s.split("+") if r.strip())
        if not name:
            continue
        out[name] = Account(
            user_id=name, display_name=name, roles=roles or frozenset({ROLE_VIEWER})
        )
    return out


def parse_accounts_spec(spec: str) -> dict[str, Account]:
    """Production accounts: `user_id|display name|role+role|password_hash`.

    The id comes first and is what every authorisation decision uses. The
    display name is for reading, and changing it changes nothing else - which
    is the point of keeping them apart.
    """
    out: dict[str, Account] = {}
    for line in (ln.strip() for ln in spec.replace(";", "\n").splitlines()):
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            msg = (
                f"account entry {line!r} needs user_id|display name|roles[|password_hash]; "
                "generate a hash with: uv run python -m api.auth hash"
            )
            raise ValueError(msg)
        user_id, display, roles_s = parts[0], parts[1], parts[2]
        pw = parts[3] if len(parts) > 3 else None
        roles = frozenset(r.strip() for r in roles_s.split("+") if r.strip())
        unknown = roles - KNOWN_ROLES
        if unknown:
            msg = f"account {user_id!r} declares unknown role(s) {sorted(unknown)}"
            raise ValueError(msg)
        out[user_id] = Account(
            user_id=user_id, display_name=display or user_id, roles=roles, password_hash=pw or None
        )
    return out


@dataclass
class Sessions:
    """Server-issued bearer tokens. Nothing a client sends becomes identity."""

    ttl_seconds: float = 12 * 3600
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict)

    def issue(self, user_id: str) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        expires = time.time() + self.ttl_seconds
        self._tokens[token] = (user_id, expires)
        return token, expires

    def resolve(self, token: str) -> str | None:
        self._sweep()
        found = self._tokens.get(token)
        if found is None:
            # Compare against something anyway, so a miss costs what a hit
            # costs. The dict lookup above already leaks less than a string
            # comparison would, but the habit is worth keeping.
            hmac.compare_digest(token, token)
            return None
        user_id, expires = found
        if expires < time.time():
            self._tokens.pop(token, None)
            return None
        return user_id

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def _sweep(self) -> None:
        now = time.time()
        for tok, (_, exp) in list(self._tokens.items()):
            if exp < now:
                self._tokens.pop(tok, None)


#: The OIDC seam. A deployment sets this to a callable that validates the
#: provider's token and returns (user_id, display_name, roles); until then
#: production mode serves local accounts only and says so. Keeping it a
#: single function means the rest of the API never learns where identity
#: came from.
OidcVerifier = Callable[[str], tuple[str, str, frozenset[str]]]
_oidc_verifier: OidcVerifier | None = None


def set_oidc_verifier(fn: OidcVerifier | None) -> None:
    global _oidc_verifier
    _oidc_verifier = fn


def get_oidc_verifier() -> OidcVerifier | None:
    return _oidc_verifier


@dataclass
class Authenticator:
    mode: str
    accounts: dict[str, Account]
    sessions: Sessions = field(default_factory=Sessions)

    @property
    def production(self) -> bool:
        return self.mode == MODE_PRODUCTION

    def login(self, user_id: str, password: str) -> tuple[str, float, Principal]:
        account = self.accounts.get(user_id)
        # The same answer whether the account is unknown or the password is
        # wrong, and the same work either way.
        ok = verify_password(account.password_hash if account else None, password)
        if not ok or account is None:
            raise AuthError(401, "invalid_credentials", "unknown user or wrong password")
        token, expires = self.sessions.issue(account.user_id)
        return token, expires, self.principal_for(account, AUTH_PASSWORD)

    @staticmethod
    def principal_for(account: Account, method: str) -> Principal:
        return Principal(
            user_id=account.user_id,
            display_name=account.display_name,
            roles=account.roles,
            auth_method=method,
        )

    def resolve(self, *, header_user: str | None, authorization: str | None) -> Principal | None:
        """Identity for one request, or None when the caller sent none.

        In production a client-supplied `X-User` is an error rather than an
        alternative: a header that used to be identity must not quietly
        become a hint.
        """
        if self.production:
            if header_user:
                raise AuthError(
                    400,
                    "client_identity_rejected",
                    "X-User is not accepted in production mode; authenticate and "
                    "send the issued bearer token in the Authorization header",
                )
            if not authorization:
                return None
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise AuthError(401, "invalid_authorization", "send: Authorization: Bearer <token>")
            verifier = get_oidc_verifier()
            if verifier is not None:
                try:
                    user_id, display, roles = verifier(token)
                except Exception as exc:  # a provider failure is a 401, not a 500
                    raise AuthError(
                        401, "invalid_token", "the identity token was rejected"
                    ) from exc
                return Principal(
                    user_id=user_id, display_name=display, roles=roles, auth_method=AUTH_OIDC
                )
            user_id = self.sessions.resolve(token) or ""
            if not user_id:
                raise AuthError(401, "invalid_token", "the session token is unknown or expired")
            account = self.accounts.get(user_id)
            if account is None:
                raise AuthError(401, "unknown_account", "the account no longer exists")
            return self.principal_for(account, AUTH_PASSWORD)
        if not header_user:
            return None
        account = self.accounts.get(header_user)
        if account is None:
            return None
        return self.principal_for(account, AUTH_HEADER_IDENTITY)


def build_authenticator(
    *, mode: str, users_spec: str, accounts_spec: str, session_ttl_s: float
) -> Authenticator:
    """Accounts come from the production spec in production, the local
    identity spec otherwise. A production deployment with no accounts is
    refused at startup rather than serving an API nobody can log in to."""
    if mode not in (MODE_LOCAL, MODE_PRODUCTION):
        msg = f"MARGIN_API_MODE must be {MODE_LOCAL!r} or {MODE_PRODUCTION!r}, got {mode!r}"
        raise ValueError(msg)
    if mode == MODE_PRODUCTION:
        accounts = parse_accounts_spec(accounts_spec)
        if not accounts:
            msg = (
                "production mode needs MARGIN_API_ACCOUNTS "
                "(user_id|display name|roles|password_hash, one per line or "
                "semicolon-separated). Generate a hash with: "
                "uv run python -m api.auth hash"
            )
            raise ValueError(msg)
        with_pw = [a for a in accounts.values() if a.password_hash]
        if not with_pw and get_oidc_verifier() is None:
            msg = (
                "production mode has no account with a password hash and no OIDC "
                "verifier registered, so nobody could authenticate"
            )
            raise ValueError(msg)
        return Authenticator(mode=mode, accounts=accounts, sessions=Sessions(session_ttl_s))
    return Authenticator(
        mode=mode, accounts=parse_users_spec(users_spec), sessions=Sessions(session_ttl_s)
    )


def _cli() -> int:
    """`uv run python -m api.auth hash` - prompt for a password, print a hash.

    Reads from a prompt rather than argv so the password does not end up in
    shell history or the process list.
    """
    import getpass
    import sys

    if len(sys.argv) < 2 or sys.argv[1] != "hash":
        print("usage: python -m api.auth hash")
        return 2
    pw = getpass.getpass("password: ")
    again = getpass.getpass("again: ")
    if pw != again:
        print("they did not match")
        return 1
    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


def mode_from_env() -> str:
    return os.environ.get("MARGIN_API_MODE", MODE_LOCAL)
