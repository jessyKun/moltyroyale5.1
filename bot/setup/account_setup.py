"""Account setup for ClawRoyale.

Railway-first behavior:
- Prefer existing API_KEY + wallet variables.
- Do not silently create new accounts unless ONBOARDING_TOKEN is present.
- Fail clearly instead of looping forever on AUTH_TOKEN_INVALID.
"""

import os
import sys

from bot.api_client import MoltyAPI, APIError
from bot.credentials import (
    is_first_run,
    save_credentials,
    save_owner_intake,
    save_agent_wallet,
    save_owner_wallet,
    load_credentials,
    update_env_file,
)
from bot.web3.wallet_manager import generate_agent_wallet, generate_owner_wallet
from bot.config import ADVANCED_MODE, AGENT_NAME, OWNER_EOA, ONBOARDING_TOKEN
from bot.utils.logger import get_logger

log = get_logger(__name__)


class FatalSetupError(RuntimeError):
    """Raised when setup cannot continue without manual Railway variables."""


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _ask_or_env(prompt: str, env_value: str, default: str = "") -> str:
    if env_value:
        return env_value
    if _is_interactive():
        val = input(prompt).strip()
        if val:
            return val
    return default


def _restore_from_env() -> dict | None:
    api_key = os.getenv("API_KEY", "").strip()
    agent_pk = os.getenv("AGENT_PRIVATE_KEY", "").strip()
    agent_addr = os.getenv("AGENT_WALLET_ADDRESS", "").strip()
    owner_pk = os.getenv("OWNER_PRIVATE_KEY", "").strip()
    owner_addr = os.getenv("OWNER_EOA", "").strip()
    agent_name = os.getenv("AGENT_NAME", "").strip()

    # Railway needs a complete existing identity. API key alone is not enough for Web3/game actions.
    if not api_key:
        return None
    missing = []
    if not agent_pk:
        missing.append("AGENT_PRIVATE_KEY")
    if not agent_addr:
        missing.append("AGENT_WALLET_ADDRESS")
    if missing:
        raise FatalSetupError(
            "API_KEY exists but wallet credentials are incomplete. Missing Railway Variables: "
            + ", ".join(missing)
        )

    log.info("Restoring credentials from Railway Variables...")
    save_agent_wallet(agent_addr, agent_pk)
    if owner_pk and owner_addr:
        save_owner_wallet(owner_addr, owner_pk)

    creds = {
        "api_key": api_key,
        "agent_name": agent_name or "ClawAgent",
        "agent_wallet_address": agent_addr,
        "owner_eoa": owner_addr,
    }
    save_credentials(creds)
    save_owner_intake(
        {
            "agent_name": creds["agent_name"],
            "advanced_mode": ADVANCED_MODE,
            "owner_eoa": owner_addr,
            "agent_wallet_generated": True,
            "owner_wallet_generated": bool(owner_pk),
        }
    )
    log.info("Credentials restored from env vars; skipping account creation")
    return creds


def _require_onboarding_or_existing_credentials():
    if ONBOARDING_TOKEN:
        return
    raise FatalSetupError(
        "Missing API_KEY and ONBOARDING_TOKEN. ClawRoyale now requires an existing API key "
        "or an official onboarding token for POST /accounts. In Railway Variables, either set "
        "API_KEY + AGENT_PRIVATE_KEY + AGENT_WALLET_ADDRESS, or set ONBOARDING_TOKEN if ClawRoyale gave you one."
    )


async def run_first_run_intake() -> dict:
    restored = _restore_from_env()
    if restored:
        return restored

    # Old bot behavior generated wallets and then POSTed /accounts with no auth.
    # New behavior: fail fast unless ClawRoyale has provided ONBOARDING_TOKEN.
    _require_onboarding_or_existing_credentials()

    log.info("First-run account creation with ONBOARDING_TOKEN")

    agent_name = _ask_or_env("Enter agent name (max 50 chars): ", AGENT_NAME, "ClawAgent")[:50]

    log.info("Generating Agent EOA...")
    agent_address, agent_pk = generate_agent_wallet()
    save_agent_wallet(agent_address, agent_pk)
    update_env_file("AGENT_WALLET_ADDRESS", agent_address)
    update_env_file("AGENT_PRIVATE_KEY", agent_pk)

    owner_address = ""
    owner_pk = ""
    if ADVANCED_MODE:
        log.info("Advanced mode: generating Owner EOA...")
        owner_address, owner_pk = generate_owner_wallet()
        save_owner_wallet(owner_address, owner_pk)
        update_env_file("OWNER_EOA", owner_address)
        update_env_file("OWNER_PRIVATE_KEY", owner_pk)
    else:
        owner_address = _ask_or_env("Enter your Owner EOA address (0x...): ", OWNER_EOA, "")
        if not owner_address.startswith("0x") or len(owner_address) != 42:
            raise FatalSetupError("Missing or invalid OWNER_EOA. Set OWNER_EOA or use ADVANCED_MODE=true.")
        update_env_file("OWNER_EOA", owner_address)

    api = MoltyAPI(use_onboarding_token=True)
    try:
        result = await api.create_account(agent_name, agent_address)
    except APIError as exc:
        if exc.code == "CONFLICT":
            existing = load_credentials()
            if existing:
                return existing
            raise FatalSetupError(
                "Wallet is already registered, but local credentials are missing. "
                "Set the existing API_KEY in Railway Variables."
            ) from exc
        if exc.code == "AUTH_TOKEN_INVALID":
            raise FatalSetupError(
                "Invalid or missing ONBOARDING_TOKEN. Get a valid token from ClawRoyale, "
                "or set existing API_KEY + AGENT_PRIVATE_KEY + AGENT_WALLET_ADDRESS."
            ) from exc
        raise
    finally:
        await api.close()

    api_key = result.get("apiKey", "") or result.get("api_key", "")
    account_id = result.get("accountId", "")
    public_id = result.get("publicId", "")
    if not api_key:
        raise FatalSetupError("POST /accounts succeeded but no apiKey was returned.")

    creds = {
        "api_key": api_key,
        "agent_name": agent_name,
        "account_id": account_id,
        "public_id": public_id,
        "agent_wallet_address": agent_address,
        "owner_eoa": owner_address,
    }
    save_credentials(creds)
    update_env_file("API_KEY", api_key)
    update_env_file("AGENT_NAME", agent_name)
    save_owner_intake(
        {
            "agent_name": agent_name,
            "advanced_mode": ADVANCED_MODE,
            "owner_eoa": owner_address,
            "agent_wallet_generated": True,
            "owner_wallet_generated": ADVANCED_MODE,
        }
    )

    from bot.utils.railway_sync import is_railway, sync_all_to_railway

    if is_railway():
        await sync_all_to_railway(creds, agent_pk, owner_pk)

    return creds


async def ensure_account_ready() -> dict:
    restored = _restore_from_env()
    if restored:
        return restored

    if is_first_run():
        return await run_first_run_intake()

    creds = load_credentials()
    if not creds or not creds.get("api_key"):
        log.warning("Credentials file exists but has no api_key. Re-running intake.")
        return await run_first_run_intake()

    log.info("Returning existing account: %s", creds.get("agent_name", "unknown"))
    return creds
    """
    # Step 0: Check if this is a Railway restart with existing env credentials
    restored = _restore_from_env()
    if restored:
        return restored

    log.info("═══ FIRST-RUN INTAKE ═══")
    if not _is_interactive():
        log.info("Non-interactive mode (Railway/Docker detected)")

    # Step 1: Agent name
    agent_name = _ask_or_env(
        "Enter agent name (max 50 chars): ",
        AGENT_NAME,
        "MoltyAgent",
    )
    if len(agent_name) > 50:
        agent_name = agent_name[:50]

    # Step 2: Generate Agent EOA (never ask the owner — setup.md)
    log.info("Generating Agent EOA...")
    agent_address, agent_pk = generate_agent_wallet()
    save_agent_wallet(agent_address, agent_pk)
    update_env_file("AGENT_WALLET_ADDRESS", agent_address)
    update_env_file("AGENT_PRIVATE_KEY", agent_pk)

    # Step 3: Owner EOA
    owner_address = ""
    owner_pk = ""
    if ADVANCED_MODE:
        log.info("Advanced mode: Generating Owner EOA...")
        owner_address, owner_pk = generate_owner_wallet()
        save_owner_wallet(owner_address, owner_pk)
        update_env_file("OWNER_EOA", owner_address)
        update_env_file("OWNER_PRIVATE_KEY", owner_pk)
        log.info(
            "Owner EOA generated: %s\n"
            "  → Private key stored at: dev-agent/owner-wallet.json\n"
            "  → You can view/download this file anytime\n"
            "  → To import into MetaMask: Settings → Import Account → paste private key",
            owner_address,
        )
    else:
        owner_address = _ask_or_env(
            "Enter your Owner EOA address (0x...): ",
            OWNER_EOA,
            "",
        )
        if not owner_address or not owner_address.startswith("0x") or len(owner_address) != 42:
            log.error(
                "Owner EOA address required but not provided or invalid. "
                "Set OWNER_EOA env var (0x + 40 hex chars) or use ADVANCED_MODE=true."
            )
            raise ValueError("Missing or invalid Owner EOA address")
        update_env_file("OWNER_EOA", owner_address)

    # Step 4: Create account via API
    log.info("Creating account via POST /accounts...")
    api = MoltyAPI()
    try:
        result = await api.create_account(agent_name, agent_address)
    except APIError as e:
        if e.code == "CONFLICT":
            log.warning("Wallet already registered. Loading existing credentials.")
            return load_credentials() or {}
        raise
    finally:
        await api.close()

    api_key = result.get("apiKey", "")
    account_id = result.get("accountId", "")
    public_id = result.get("publicId", "")

    if not api_key:
        raise RuntimeError("No apiKey returned from POST /accounts!")

    log.info("✅ Account created! apiKey=%s... accountId=%s", api_key[:15], account_id[:8])

    # Step 5: Persist
    creds = {
        "api_key": api_key,
        "agent_name": agent_name,
        "account_id": account_id,
        "public_id": public_id,
        "agent_wallet_address": agent_address,
        "owner_eoa": owner_address,
    }
    save_credentials(creds)
    update_env_file("API_KEY", api_key)
    update_env_file("AGENT_NAME", agent_name)

    intake = {
        "agent_name": agent_name,
        "advanced_mode": ADVANCED_MODE,
        "owner_eoa": owner_address,
        "agent_wallet_generated": True,
        "owner_wallet_generated": ADVANCED_MODE,
    }
    save_owner_intake(intake)

    # Step 6: Auto-sync to Railway Variables (if on Railway)
    from bot.utils.railway_sync import is_railway, sync_all_to_railway
    if is_railway():
        log.info("Detected Railway — syncing all variables in one batch...")
        await sync_all_to_railway(creds, agent_pk, owner_pk)

    return creds


async def ensure_account_ready() -> dict:
    """
    Ensure account exists. Run first-run intake if needed.
    Returns credentials dict with api_key.
    """
    if is_first_run():
        return await run_first_run_intake()

    creds = load_credentials()
    if not creds or not creds.get("api_key"):
        log.warning("Credentials file exists but no api_key. Re-running intake.")
        return await run_first_run_intake()

    log.info("Returning run: account=%s", creds.get("agent_name", "unknown"))
    return creds
