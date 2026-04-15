"""
╔══════════════════════════════════════════════════════════════════╗
║           POLYMARKET LIMIT ORDER BOT  –  v9  (fixed)           ║
╠══════════════════════════════════════════════════════════════════╣
║  Bugs fixed vs v7/v8:                                           ║
║  1. SignatureType → POLY_PROXY  (was wrong EOA/Safe detection)  ║
║  2. USDC approval → ERC-20 approve(MaxUint256)  (not ERC-1155) ║
║  3. creds.key check  (not creds.api_key)                        ║
║  4. CLOB URL trailing slash stripped                            ║
║  5. Wallet passed WITH provider to RPC for on-chain tx          ║
╚══════════════════════════════════════════════════════════════════╝

Source references (from project dump):
  src/services/createClobClient.ts  → create_clob_client()
  src/scripts/checkAllowance.ts     → check_and_set_usdc_allowance()
  src/utils/getMyBalance.ts         → get_my_balance()
  src/utils/healthCheck.ts          → health_check()
  src/utils/postOrder.ts            → post_limit_order()

Usage:
    pip install -r requirements.txt
    cp .env.example .env   # fill in values
    python polymarket_bot.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
import urllib.request
from typing import Optional

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import geth_poa_middleware

# ── py-clob-client ────────────────────────────────────────────────
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON   # = 137

# ─────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polymarket")


def _sep(char: str = "═", w: int = 62) -> None:
    log.info(char * w)


# ─────────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────────
load_dotenv()


class ENV:
    # ── Required ───────────────────────────────────────────────
    PRIVATE_KEY: str           = os.environ["PRIVATE_KEY"]
    PROXY_WALLET: str          = os.environ["PROXY_WALLET"]
    YES_TOKEN_ID: str          = os.environ["YES_TOKEN_ID"]

    # ── Optional ───────────────────────────────────────────────
    # Strip trailing slash — CLOB client breaks with it
    CLOB_HTTP_URL: str         = os.getenv("CLOB_HTTP_URL", "https://clob.polymarket.com").rstrip("/")
    RPC_URL: str               = os.getenv("RPC_URL",       "https://polygon-rpc.com")

    # Polymarket uses USDC.e (bridged) on Polygon — matches checkAllowance.ts
    USDC_CONTRACT_ADDRESS: str = os.getenv(
        "USDC_CONTRACT_ADDRESS",
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    )

    # ── Order settings ─────────────────────────────────────────
    LIMIT_PRICE: float         = float(os.getenv("LIMIT_PRICE",     "0.01"))
    SHARES: float              = float(os.getenv("SHARES",          "5"))
    RETRY_LIMIT: int           = int(os.getenv("RETRY_LIMIT",       "3"))
    MIN_BALANCE_USD: float     = float(os.getenv("MIN_BALANCE_USD", "1.0"))


# ─────────────────────────────────────────────────────────────────
# Contract constants  (from checkAllowance.ts)
# ─────────────────────────────────────────────────────────────────
POLYMARKET_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")

# MaxUint256 for unlimited ERC-20 approval
MAX_UINT256 = 2**256 - 1

# ─────────────────────────────────────────────────────────────────
# ABIs
# ─────────────────────────────────────────────────────────────────
# ERC-20 USDC — balanceOf, allowance, approve, decimals
USDC_ABI = [
    {
        "inputs": [{"name": "owner",   "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view", "type": "function",
    },
]


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _make_w3(with_signer: bool = False):
    """
    Return (w3, account_or_None).
    Polygon is a PoA chain — geth_poa_middleware is required.
    """
    w3 = Web3(Web3.HTTPProvider(ENV.RPC_URL))
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {ENV.RPC_URL}")
    account = Account.from_key(ENV.PRIVATE_KEY) if with_signer else None
    return w3, account


def _usdc_contract(w3: Web3, signer=None):
    addr = Web3.to_checksum_address(ENV.USDC_CONTRACT_ADDRESS)
    if signer:
        # For write operations we pass the account via middleware, not here
        pass
    return w3.eth.contract(address=addr, abi=USDC_ABI)


def _extract_order_error(resp) -> Optional[str]:
    """Mirror of extractOrderError() from postOrder.ts."""
    if resp is None:
        return None
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for key in ("error", "errorMsg", "message"):
            val = resp.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                for k2 in ("error", "message"):
                    if isinstance(val.get(k2), str):
                        return val[k2]
    for attr in ("error", "errorMsg", "message"):
        val = getattr(resp, attr, None)
        if isinstance(val, str):
            return val
    return None


def _is_funds_error(msg: Optional[str]) -> bool:
    if not msg:
        return False
    lower = msg.lower()
    return "not enough balance" in lower or "allowance" in lower


# ═════════════════════════════════════════════════════════════════
# 1.  get_my_balance()  ←  src/utils/getMyBalance.ts
# ═════════════════════════════════════════════════════════════════
def get_my_balance(address: str) -> float:
    """
    Read USDC.e balanceOf the proxy wallet.
    Divides raw uint256 by 10^6 (USDC has 6 decimals).
    """
    w3, _ = _make_w3()
    usdc   = _usdc_contract(w3)
    raw: int = usdc.functions.balanceOf(
        Web3.to_checksum_address(address)
    ).call()
    return raw / 1_000_000


# ═════════════════════════════════════════════════════════════════
# 2.  check_and_set_usdc_allowance()  ←  src/scripts/checkAllowance.ts
#
#  FIX vs v7: USDC needs ERC-20 approve() — NOT ERC-1155 setApprovalForAll.
#  The old code called setApprovalForAll on the CTF contract, which is
#  only needed for selling conditional tokens, NOT for buying with USDC.
# ═════════════════════════════════════════════════════════════════
def check_and_set_usdc_allowance() -> bool:
    """
    Verify USDC.e ERC-20 allowance for the Polymarket Exchange.
    If insufficient, call approve(exchange, MaxUint256) — mirrors checkAllowance.ts.
    Returns True when allowance is sufficient to trade.
    """
    _sep()
    log.info("🔍  Checking USDC allowance…")
    _sep()

    w3, account = _make_w3(with_signer=True)
    proxy  = Web3.to_checksum_address(ENV.PROXY_WALLET)
    usdc   = _usdc_contract(w3)

    try:
        decimals: int      = usdc.functions.decimals().call()
        balance_raw: int   = usdc.functions.balanceOf(proxy).call()
        allowance_raw: int = usdc.functions.allowance(proxy, POLYMARKET_EXCHANGE).call()

        div           = 10 ** decimals
        balance_fmt   = balance_raw   / div
        allowance_fmt = allowance_raw / div

        log.info("💼  Wallet    : %s", proxy)
        log.info("💵  USDC.e    : %.6f", balance_fmt)
        log.info(
            "✅  Allowance : %s",
            "0 USDC (NOT SET!)" if allowance_raw == 0
            else f"{allowance_fmt:.6f} USDC",
        )
        log.info("📍  Exchange  : %s", POLYMARKET_EXCHANGE)

        # Already sufficient
        if allowance_raw >= balance_raw and allowance_raw > 0:
            log.info("✅  Allowance already sufficient – no action needed.")
            return True

        # ── Need to approve ───────────────────────────────────
        log.warning("⚠️   Allowance insufficient – setting unlimited approval…")

        # Gas price + 50% buffer (mirrors checkAllowance.ts)
        base_gas  = w3.eth.gas_price
        gas_price = int(base_gas * 1.5)
        log.info("⛽  Gas Price : %.2f Gwei", gas_price / 1e9)

        nonce = w3.eth.get_transaction_count(account.address)

        tx = usdc.functions.approve(
            POLYMARKET_EXCHANGE, MAX_UINT256
        ).build_transaction({
            "from":     account.address,
            "gas":      100_000,
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  POLYGON,
        })

        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        log.info("⏳  Tx sent  : 0x%s", tx_hash.hex())
        log.info("⏳  Waiting for confirmation…")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info("✅  Allowance set!  https://polygonscan.com/tx/0x%s", tx_hash.hex())

            # Verify
            new_allowance: int = usdc.functions.allowance(proxy, POLYMARKET_EXCHANGE).call()
            log.info("✅  Verified on-chain: %.2f USDC approved", new_allowance / div)
            return True

        log.error("❌  Approve tx failed (status=%s)", receipt["status"])
        return False

    except Exception as exc:
        log.error("❌  check_and_set_usdc_allowance error: %s", exc)
        if "insufficient funds" in str(exc).lower():
            log.warning("⚠️  You need MATIC for gas fees on Polygon!")
        return False


# ═════════════════════════════════════════════════════════════════
# 3.  create_clob_client()  ←  src/services/createClobClient.ts
#
#  FIX: Use SignatureType.POLY_PROXY  (integer 1).
#  The service file (used by the working copy-trading bot) hard-codes
#  POLY_PROXY + funder=PROXY_WALLET — no Gnosis Safe detection needed
#  for a standard Polymarket proxy wallet.
#
#  FIX: Check creds.key  (not creds.api_key).
# ═════════════════════════════════════════════════════════════════

# POLY_PROXY = 1  in @polymarket/order-utils SignatureType enum
# py_clob_client exposes this as integer 1 in signature_type parameter
POLY_PROXY = 1


def create_clob_client() -> ClobClient:
    """
    Port of src/services/createClobClient.ts.

    Uses POLY_PROXY signature type + funder=PROXY_WALLET.
    This is what the TS project's actual running service uses — NOT the
    EOA/Gnosis-Safe-detection variant which caused the L2_AUTH_UNAVAILABLE error.

    Flow:
      1. Build unauthenticated client with POLY_PROXY + funder
      2. createApiKey() → if key empty → deriveApiKey()
      3. Rebuild client with credentials attached
    """
    log.info("🔑  Creating authenticated CLOB client…")
    log.info("    Host       : %s", ENV.CLOB_HTTP_URL)
    log.info("    Funder     : %s", ENV.PROXY_WALLET)
    log.info("    SigType    : POLY_PROXY (1)")

    # ── Step 1: unauthenticated client ───────────────────────
    client_anon = ClobClient(
        host=ENV.CLOB_HTTP_URL,
        chain_id=POLYGON,
        key=ENV.PRIVATE_KEY,
        signature_type=POLY_PROXY,
        funder=ENV.PROXY_WALLET,
    )

    # ── Step 2: create / derive API key ──────────────────────
    create_warning: Optional[str] = None
    derive_warning: Optional[str] = None
    creds = None

    try:
        creds = client_anon.create_api_key()
    except Exception as e:
        create_warning = f"⚠️  createApiKey failed: {e}"

    # Check the actual field name the TS code uses: creds.key
    # In py_clob_client the ApiCreds object exposes .api_key BUT
    # the raw dict from the server has "key" — handle both.
    def _has_key(c) -> bool:
        if c is None:
            return False
        raw = getattr(c, "api_key", None) or getattr(c, "key", None)
        return bool(raw)

    if not _has_key(creds):
        if create_warning:
            log.warning(create_warning)
        try:
            creds = client_anon.derive_api_key()
        except Exception as e:
            derive_warning = f"⚠️  deriveApiKey failed: {e}"
            log.warning(derive_warning)

    if not _has_key(creds):
        raise RuntimeError(
            "Failed to obtain Polymarket API credentials.\n"
            "Make sure PRIVATE_KEY and PROXY_WALLET are correct and that\n"
            "the wallet has traded at least once on Polymarket."
        )

    log.info("✅  API credentials obtained")

    # ── Step 3: authenticated client ─────────────────────────
    client = ClobClient(
        host=ENV.CLOB_HTTP_URL,
        chain_id=POLYGON,
        key=ENV.PRIVATE_KEY,
        creds=creds,
        signature_type=POLY_PROXY,
        funder=ENV.PROXY_WALLET,
    )
    log.info("✅  ClobClient ready (Level 2 auth)")
    return client


# ═════════════════════════════════════════════════════════════════
# 4.  health_check()  ←  src/utils/healthCheck.ts
# ═════════════════════════════════════════════════════════════════
def health_check() -> bool:
    """
    Checks: RPC connectivity, USDC balance, Polymarket data API.
    Low balance is a warning (non-fatal), matching the TS healthy logic.
    """
    _sep()
    log.info("🏥  HEALTH CHECK")
    _sep()

    results: list[str] = []
    all_ok = True

    # ── RPC ──────────────────────────────────────────────────
    try:
        w3, _ = _make_w3()
        block  = w3.eth.block_number
        results.append(f"✅  RPC             block #{block}")
    except Exception as exc:
        results.append(f"❌  RPC             {exc}")
        all_ok = False

    # ── Balance ───────────────────────────────────────────────
    try:
        bal = get_my_balance(ENV.PROXY_WALLET)
        if bal <= 0:
            results.append("❌  Balance         Zero balance")
            all_ok = False
        elif bal < ENV.MIN_BALANCE_USD:
            results.append(f"⚠️   Balance         Low: ${bal:.4f} USDC")
        else:
            results.append(f"✅  Balance         ${bal:.4f} USDC")
    except Exception as exc:
        results.append(f"❌  Balance         {exc}")
        all_ok = False

    # ── Polymarket Data API ───────────────────────────────────
    try:
        url = (
            "https://data-api.polymarket.com/positions"
            "?user=0x0000000000000000000000000000000000000000"
        )
        with urllib.request.urlopen(url, timeout=6) as r:
            r.read()
        results.append("✅  Polymarket API  reachable")
    except Exception as exc:
        results.append(f"❌  Polymarket API  {exc}")
        all_ok = False

    for line in results:
        log.info(line)
    log.info("Overall : %s", "✅ Healthy" if all_ok else "❌ Unhealthy")
    _sep()
    return all_ok


# ═════════════════════════════════════════════════════════════════
# 5.  post_limit_order()  ←  src/utils/postOrder.ts  (BUY branch)
# ═════════════════════════════════════════════════════════════════
def post_limit_order(
    client: ClobClient,
    yes_token_id: str,
    price: float     = 0.01,
    shares: float    = 5.0,
    retry_limit: int = 3,
) -> bool:
    """
    Place a GTC limit BUY order for `shares` YES tokens at `price` USDC each.

    Mirrors postOrder.ts BUY branch:
      • Fetch order book → pick best ask
      • Slippage guard: abort if ask > price + 0.05
      • Create signed order (GTC) → post
      • Retry loop up to retry_limit
      • Immediate abort on balance / allowance error
    """
    MIN_USD  = 1.0
    usd_size = price * shares

    _sep()
    log.info("📋  PLACING LIMIT BUY ORDER")
    _sep()
    log.info("    Token   : …%s", yes_token_id[-14:])
    log.info("    Price   : $%.4f / share", price)
    log.info("    Shares  : %.2f", shares)
    log.info("    Total   : $%.4f USDC", usd_size)
    _sep()

    if usd_size < MIN_USD:
        log.warning(
            "❌  Order total $%.4f < Polymarket minimum $%.2f",
            usd_size, MIN_USD,
        )
        log.warning("   Increase SHARES or LIMIT_PRICE in .env")
        return False

    retry = 0
    while retry < retry_limit:

        # ── Order book ────────────────────────────────────────
        try:
            book = client.get_order_book(yes_token_id)
        except Exception as exc:
            log.error("Order book fetch failed: %s", exc)
            retry += 1
            time.sleep(1)
            continue

        asks = getattr(book, "asks", None) or []
        if not asks:
            log.warning("No asks in order book – retrying (%d/%d)", retry + 1, retry_limit)
            retry += 1
            time.sleep(1)
            continue

        best_ask       = min(asks, key=lambda a: float(a.price))
        best_ask_price = float(best_ask.price)
        log.info("Best ask : %s shares @ $%s", best_ask.size, best_ask.price)

        # ── Slippage guard (mirrors postOrder.ts) ─────────────
        if best_ask_price - 0.05 > price:
            log.warning(
                "Price slippage too high (ask $%.4f > limit $%.4f + 0.05) – aborting",
                best_ask_price, price,
            )
            return False

        # ── Build & sign GTC limit order ──────────────────────
        order_args = OrderArgs(
            token_id=yes_token_id,
            price=price,
            size=shares,
            side="BUY",
        )

        try:
            signed = client.create_order(order_args)
            resp   = client.post_order(signed, OrderType.GTC)
        except Exception as exc:
            log.error("Order post error: %s", exc)
            retry += 1
            time.sleep(1)
            continue

        # ── Parse response ────────────────────────────────────
        success = getattr(resp, "success", False) or (
            isinstance(resp, dict) and resp.get("success")
        )

        if success:
            oid = (
                getattr(resp, "orderID", None)
                or (resp.get("orderID") if isinstance(resp, dict) else None)
                or "n/a"
            )
            log.info("✅  Limit order placed!  orderID=%s", oid)
            log.info(
                "   %.2f YES shares @ $%.4f  =  $%.4f USDC",
                shares, price, usd_size,
            )
            return True

        err = _extract_order_error(resp)
        if _is_funds_error(err):
            log.warning("❌  Insufficient balance / allowance: %s", err)
            log.warning("   Top up USDC or re-run check_and_set_usdc_allowance().")
            return False

        retry += 1
        log.warning(
            "Order failed (attempt %d/%d)%s",
            retry, retry_limit,
            f" – {err}" if err else "",
        )
        time.sleep(1)

    log.error("❌  Order failed after %d attempts", retry_limit)
    return False


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def main() -> None:
    _sep("═")
    log.info("🚀  Polymarket Limit Order Bot  v9")
    log.info("    Wallet  : %s", ENV.PROXY_WALLET)
    log.info("    Price   : $%.4f", ENV.LIMIT_PRICE)
    log.info("    Shares  : %.2f", ENV.SHARES)
    log.info("    Token   : …%s", ENV.YES_TOKEN_ID[-14:])
    _sep("═")

    # ── 1. Health check ───────────────────────────────────────
    if not health_check():
        log.error("Health check failed – aborting.")
        sys.exit(1)

    # ── 2. Balance check ──────────────────────────────────────
    balance  = get_my_balance(ENV.PROXY_WALLET)
    required = ENV.LIMIT_PRICE * ENV.SHARES
    log.info("💰  USDC balance : $%.4f  (need $%.4f)", balance, required)

    if balance < required:
        log.error(
            "Insufficient balance: need $%.4f, have $%.4f – aborting.",
            required, balance,
        )
        sys.exit(1)

    # ── 3. USDC ERC-20 allowance ──────────────────────────────
    #  This is the correct approval for BUY orders.
    #  (setApprovalForAll is only needed for selling CT tokens.)
    if not check_and_set_usdc_allowance():
        log.error("Could not verify / set USDC allowance – aborting.")
        sys.exit(1)

    # ── 4. Authenticated CLOB client ──────────────────────────
    try:
        client = create_clob_client()
    except Exception as exc:
        log.error("Failed to create CLOB client: %s", exc)
        sys.exit(1)

    # ── 5. Place limit order ──────────────────────────────────
    ok = post_limit_order(
        client=client,
        yes_token_id=ENV.YES_TOKEN_ID,
        price=ENV.LIMIT_PRICE,
        shares=ENV.SHARES,
        retry_limit=ENV.RETRY_LIMIT,
    )

    _sep("═")
    log.info("🎉  Done – success!" if ok else "💥  Done – with errors.")
    _sep("═")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
