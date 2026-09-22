"""
Questrade OAuth token manager.

Questrade auth uses a *manual refresh token* (generated in the account portal)
that you exchange for a short-lived access token + the api_server to call.

CRITICAL: the refresh token is ONE-TIME-USE and ROTATES on every exchange. The
response hands back a NEW refresh token; the old one is dead immediately. We must
persist the new refresh token BEFORE using the access token, or a crash mid-flight
locks the account out and forces manual regeneration in the portal.

Token state lives in a gitignored JSON file (practice phase). Seed it once with the
refresh token from the portal:

    python -m data.questrade_auth --seed <REFRESH_TOKEN_FROM_PORTAL>

Then callers just use:

    auth = QuestradeAuth()
    token = auth.get_access_token()   # refreshes automatically when stale
    base  = auth.api_server           # e.g. https://api06.iq.questrade.com/
"""

import argparse
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# Questrade token endpoint. Same host for practice and live; the *api_server* in the
# response differs per account. VERIFY against the live portal if auth misbehaves.
DEFAULT_TOKEN_URL = "https://login.questrade.com/oauth2/token"

# Refresh a bit early so we never call the API with an about-to-expire token.
_EXPIRY_BUFFER_SEC = 60


class QuestradeAuthError(RuntimeError):
    pass


class QuestradeAuth:
    def __init__(
        self,
        token_file: str = ".questrade_token.json",
        token_url: str = DEFAULT_TOKEN_URL,
    ):
        self.token_file = token_file
        self.token_url = token_url
        self._state = self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> dict:
        if not os.path.exists(self.token_file):
            return {}
        with open(self.token_file) as f:
            return json.load(f)

    def _save(self) -> None:
        # Write to a temp file then atomically replace, so a crash mid-write can't
        # leave us with a truncated (and therefore useless) refresh token.
        tmp = self.token_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, self.token_file)
        os.chmod(self.token_file, 0o600)

    # ── Seeding ──────────────────────────────────────────────────

    def seed(self, refresh_token: str) -> None:
        """Store the initial refresh token generated in the Questrade portal."""
        self._state = {"refresh_token": refresh_token}
        self._save()
        logger.info("Seeded refresh token into %s", self.token_file)

    # ── Token exchange ───────────────────────────────────────────

    def refresh(self) -> None:
        """Exchange the current refresh token for a new access token + api_server.

        Persists the rotated refresh token FIRST — before returning — so we never
        lose it.
        """
        refresh_token = self._state.get("refresh_token")
        if not refresh_token:
            raise QuestradeAuthError(
                f"No refresh token in {self.token_file}. Seed it first:\n"
                f"    python -m data.questrade_auth --seed <REFRESH_TOKEN_FROM_PORTAL>"
            )

        resp = requests.get(
            self.token_url,
            params={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
        if resp.status_code != 200:
            # Do NOT wipe the stored token on failure — it may still be valid (e.g.
            # transient network / 5xx). Only a real "Bad Request" means it's spent.
            raise QuestradeAuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text[:200]}"
            )

        data = resp.json()
        try:
            new_state = {
                "refresh_token": data["refresh_token"],
                "access_token": data["access_token"],
                "api_server": data["api_server"],
                "token_type": data.get("token_type", "Bearer"),
                "expires_at": time.time() + int(data["expires_in"]) - _EXPIRY_BUFFER_SEC,
            }
        except KeyError as e:
            raise QuestradeAuthError(f"Unexpected token response, missing {e}: {data}")

        # Persist the rotated refresh token immediately.
        self._state = new_state
        self._save()
        logger.info("Refreshed access token; api_server=%s", new_state["api_server"])

    # ── Accessors ────────────────────────────────────────────────

    def _is_access_valid(self) -> bool:
        return (
            self._state.get("access_token") is not None
            and time.time() < self._state.get("expires_at", 0)
        )

    def get_access_token(self) -> str:
        if not self._is_access_valid():
            self.refresh()
        return self._state["access_token"]

    @property
    def api_server(self) -> str:
        if not self._is_access_valid():
            self.refresh()
        return self._state["api_server"]

    def auth_header(self) -> dict:
        return {"Authorization": f"{self._state.get('token_type', 'Bearer')} {self.get_access_token()}"}


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(description="Questrade OAuth token manager")
    parser.add_argument("--seed", metavar="REFRESH_TOKEN", help="Store initial refresh token from the portal")
    parser.add_argument("--check", action="store_true", help="Refresh and fetch account balances (read-only smoke test)")
    parser.add_argument("--token-file", default=".questrade_token.json")
    args = parser.parse_args()

    auth = QuestradeAuth(token_file=args.token_file)

    if args.seed:
        auth.seed(args.seed)
        print(f"Seeded. Now run: python -m data.questrade_auth --check")
        return

    if args.check:
        base = auth.api_server
        headers = auth.auth_header()
        # Read-only: list accounts, then balances for the first one. No orders.
        accounts = requests.get(base + "v1/accounts", headers=headers, timeout=15).json()
        print("Accounts:", json.dumps(accounts, indent=2))
        acct_list = accounts.get("accounts", [])
        if acct_list:
            acct_id = acct_list[0]["number"]
            bal = requests.get(base + f"v1/accounts/{acct_id}/balances", headers=headers, timeout=15).json()
            print(f"\nBalances for {acct_id}:", json.dumps(bal, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
