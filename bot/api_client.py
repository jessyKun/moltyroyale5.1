"""Async REST API client for ClawRoyale."""

import json
from typing import Optional

import httpx

from bot.config import API_BASE, SKILL_VERSION, ONBOARDING_TOKEN
from bot.utils.logger import get_logger
from bot.utils.rate_limiter import rest_limiter

log = get_logger(__name__)


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message}")


class MoltyAPI:
    """Async HTTP client for ClawRoyale REST endpoints."""

    def __init__(self, api_key: str = "", use_onboarding_token: bool = False):
        self.api_key = api_key.strip()
        self.use_onboarding_token = use_onboarding_token
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers=self._headers(),
            )

    def _headers(self) -> dict:
        headers = {"X-Version": SKILL_VERSION}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        elif self.use_onboarding_token and ONBOARDING_TOKEN:
            headers["Authorization"] = f"Bearer {ONBOARDING_TOKEN}"
        return headers

    def _safe_parse_json(self, text: str) -> dict:
        text = text.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            try:
                obj, _ = decoder.raw_decode(text)
                log.debug("Parsed partial JSON response")
                return obj
            except json.JSONDecodeError as exc:
                log.warning("Unparseable API response: %s; err=%s", text[:120], exc)
                return {}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await rest_limiter.acquire()
        await self._ensure_client()
        resp = await self._client.request(method, path, **kwargs)

        if resp.status_code == 426:
            raise APIError(
                "VERSION_MISMATCH",
                "Skill version outdated. Check GET /api/version and update SKILL_VERSION in Railway Variables.",
                426,
            )
        if resp.status_code == 429:
            raise APIError("RATE_LIMITED", "Too many requests", 429)

        data = self._safe_parse_json(resp.text)

        if isinstance(data, dict) and not data.get("success", True) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                raise APIError(err.get("code", "UNKNOWN"), err.get("message", "Unknown error"), resp.status_code)
            raise APIError("UNKNOWN", str(err), resp.status_code)

        if resp.status_code >= 400:
            raise APIError("HTTP_ERROR", f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)

        if isinstance(data, dict):
            result = data.get("data", data)
            return result if isinstance(result, dict) else {"value": result, "_raw": data}
        return {"_raw": data}

    async def create_account(self, name: str, wallet_address: str) -> dict:
        log.info("Creating account: name=%s wallet=%s", name, wallet_address[:10] + "...")
        return await self._request(
            "POST",
            "/accounts",
            json={"name": name, "wallet_address": wallet_address},
        )

    async def get_accounts_me(self) -> dict:
        return await self._request("GET", "/accounts/me")

    async def put_wallet(self, wallet_address: str) -> dict:
        return await self._request("PUT", "/accounts/wallet", json={"wallet_address": wallet_address})

    async def create_wallet(self, owner_eoa: str) -> dict:
        log.info("Creating ClawRoyale Wallet for owner=%s", owner_eoa[:10] + "...")
        return await self._request("POST", "/create/wallet", json={"ownerEoa": owner_eoa})

    async def whitelist_request(self, owner_eoa: str) -> dict:
        log.info("Requesting whitelist for owner=%s", owner_eoa[:10] + "...")
        return await self._request("POST", "/whitelist/request", json={"ownerEoa": owner_eoa})

    async def post_identity(self, agent_id: int) -> dict:
        log.info("Registering identity: agentId=%d", agent_id)
        return await self._request("POST", "/identity", json={"agentId": agent_id})

    async def get_identity(self) -> dict:
        return await self._request("GET", "/identity")

    async def delete_identity(self) -> dict:
        log.info("Unregistering current identity")
        return await self._request("DELETE", "/identity")

    async def post_join(self, entry_type: str = "free") -> dict:
        await self._ensure_client()
        await rest_limiter.acquire()
        resp = await self._client.post(
            "/join",
            json={"entryType": entry_type},
            timeout=httpx.Timeout(20.0),
        )
        if resp.status_code == 426:
            raise APIError("VERSION_MISMATCH", "Skill version outdated", 426)
        if resp.status_code == 429:
            raise APIError("RATE_LIMITED", "Too many requests", 429)
        data = self._safe_parse_json(resp.text)
        if isinstance(data, dict) and not data.get("success", True) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                raise APIError(err.get("code", "UNKNOWN"), err.get("message", "Unknown error"), resp.status_code)
            raise APIError("UNKNOWN", str(err), resp.status_code)
        if isinstance(data, dict) and "data" in data:
            result = data["data"]
            return result if isinstance(result, dict) else {"value": result, "_raw": data}
        return data if isinstance(data, dict) else {"_raw": data}

    async def get_join_status(self) -> dict:
        return await self._request("GET", "/join/status")

    async def get_games(self, status: str = "waiting") -> dict:
        return await self._request("GET", "/games", params={"status": status})

    async def get_join_paid_message(self, game_id: str) -> dict:
        return await self._request("GET", f"/games/{game_id}/join-paid/message")

    async def post_join_paid(self, game_id: str, deadline: str, signature: str, mode: str = "offchain") -> dict:
        body = {"deadline": deadline, "signature": signature}
        if mode == "onchain":
            body["mode"] = "onchain"
        return await self._request("POST", f"/games/{game_id}/join-paid", json=body)

    async def get_version(self) -> dict:
        return await self._request("GET", "/version")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        return await self._request("POST", "/accounts", json={
            "name": name,
            "wallet_address": wallet_address,
        })

    async def get_accounts_me(self) -> dict:
        """GET /accounts/me — readiness check, state detection, balance."""
        return await self._request("GET", "/accounts/me")

    async def put_wallet(self, wallet_address: str) -> dict:
        """PUT /accounts/wallet — attach wallet to existing account."""
        return await self._request("PUT", "/accounts/wallet", json={
            "wallet_address": wallet_address,
        })

    # ── Wallet & whitelist ────────────────────────────────────────────

    async def create_wallet(self, owner_eoa: str) -> dict:
        """POST /create/wallet — create MoltyRoyale Wallet."""
        log.info("Creating MoltyRoyale Wallet for owner=%s", owner_eoa[:10] + "...")
        return await self._request("POST", "/create/wallet", json={
            "ownerEoa": owner_eoa,
        })

    async def whitelist_request(self, owner_eoa: str) -> dict:
        """POST /whitelist/request — request whitelist approval."""
        log.info("Requesting whitelist for owner=%s", owner_eoa[:10] + "...")
        return await self._request("POST", "/whitelist/request", json={
            "ownerEoa": owner_eoa,
        })

    # ── Identity ──────────────────────────────────────────────────────

    async def post_identity(self, agent_id: int) -> dict:
        """POST /api/identity — register ERC-8004 identity."""
        log.info("Registering identity: agentId=%d", agent_id)
        return await self._request("POST", "/identity", json={
            "agentId": agent_id,
        })

   # ── Version ───────────────────────────────────────────────────────

async def get_version(self) -> dict:
    """GET /version — check current server version."""
    # === SOLUSI ALTERNATIF: Langsung return versi tanpa request ===
    return {"data": {"version": "2.0.0"}, "success": True}

    async def delete_identity(self) -> dict:
        """DELETE /api/identity — unregister current identity.
        Per identity.md §3: Use to switch to a different ERC-8004 NFT.
        Unregister first, then register new agentId.
        """
        log.info("Unregistering current identity")
        return await self._request("DELETE", "/identity")

    # ── Free matchmaking ──────────────────────────────────────────────

    async def post_join(self, entry_type: str = "free") -> dict:
        """POST /join — enter free matchmaking queue (Long Poll ~15s)."""
        log.debug("Joining queue: entryType=%s", entry_type)
        # Long poll can take up to 15s
        await self._ensure_client()
        await rest_limiter.acquire()
        resp = await self._client.post(
            "/join",
            json={"entryType": entry_type},
            timeout=httpx.Timeout(20.0),
        )

        # Handle version mismatch
        if resp.status_code == 426:
            raise APIError("VERSION_MISMATCH", "Skill version outdated", 426)

        # Handle rate limiting
        if resp.status_code == 429:
            raise APIError("RATE_LIMITED", "Too many requests", 429)

        data = self._safe_parse_json(resp.text)

        # Check for error response shape (per errors.md)
        if isinstance(data, dict) and not data.get("success", True) and "error" in data:
            err = data["error"]
            raise APIError(
                err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN",
                err.get("message", "Unknown error") if isinstance(err, dict) else str(err),
                resp.status_code,
            )

        # Extract data per api-summary.md response shape
        if isinstance(data, dict) and "data" in data:
            result = data["data"]
            return result if isinstance(result, dict) else {"value": result, "_raw": data}
        return data if isinstance(data, dict) else {"_raw": data}

    async def get_join_status(self) -> dict:
        """GET /join/status — check queue status without new request."""
        return await self._request("GET", "/join/status")

    # ── Paid join ─────────────────────────────────────────────────────

    async def get_games(self, status: str = "waiting") -> dict:
        """GET /games?status=waiting — list waiting games."""
        return await self._request("GET", "/games", params={"status": status})

    async def get_join_paid_message(self, game_id: str) -> dict:
        """GET /games/{gameId}/join-paid/message — EIP-712 typed data."""
        return await self._request("GET", f"/games/{game_id}/join-paid/message")

    async def post_join_paid(self, game_id: str, deadline: str,
                             signature: str, mode: str = "offchain") -> dict:
        """POST /games/{gameId}/join-paid — submit signed paid join."""
        body = {"deadline": deadline, "signature": signature}
        if mode == "onchain":
            body["mode"] = "onchain"
        return await self._request("POST", f"/games/{game_id}/join-paid", json=body)

    # ── Version ───────────────────────────────────────────────────────

    async def get_version(self) -> dict:
        """GET /version — check current server version."""
        return await self._request("GET", "/version")

    # ── Cleanup ───────────────────────────────────────────────────────

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
