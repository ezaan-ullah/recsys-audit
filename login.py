"""Sign an account in by hand (once), or check every account.

    python login.py acct03      # opens Chrome on the Google sign-in page; sign in manually
    python login.py --check     # signed-in state, identity, and watch history for every account

Signing in records the account's YouTube DATASYNC_ID in accounts.yaml. Every session
checks it, so a profile that silently switched accounts is caught.
"""
import argparse
import asyncio

from playwright.async_api import async_playwright

from audit import dom
from audit.browser import apply_screen, is_signed_in, open_profile
from audit.config import load_accounts, load_config, save_accounts

SIGNIN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&hl=en"
              "&continue=https%3A%2F%2Fwww.youtube.com%2F%3Fhl%3Den")
HISTORY_URL = "https://www.youtube.com/feed/history?hl=en"


async def identity(page) -> tuple[dict | None, bool]:
    """(ytcfg identity, watch history off?) read from the history page."""
    await page.goto(HISTORY_URL, wait_until="domcontentloaded")
    cfg = None
    for _ in range(30):
        cfg = await dom.ytcfg(page)
        if cfg and cfg.get("logged_in") is not None:
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(2.0)
    return cfg, await dom.history_off(page)


async def login(account_id: str) -> None:
    cfg = load_config()
    accounts = load_accounts(cfg)
    acct = next((a for a in accounts if a["id"] == account_id), None)
    if acct is None:
        raise SystemExit(f"{account_id} is not in accounts.yaml; add it first.")
    async with async_playwright() as pw:
        ctx = await open_profile(pw, cfg["browser"], account_id, screen="none")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(SIGNIN_URL)
        print(f"\n[{account_id}] In the browser window:\n"
              "  1. Sign in to this account's Google login (only the sign-in pages are visible).\n"
              "  2. In YouTube settings, make sure watch history is ON.\n"
              "  3. Don't watch, search, like, or subscribe to anything.\n")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when done... ")
        await apply_screen(ctx, cfg["browser"].get("screen", "overlay"))   # hide the feed from here on
        signed = await is_signed_in(ctx)
        ycfg, hist_off = await identity(page)
        await ctx.close()
    did = (ycfg or {}).get("datasync_id")
    if signed and ycfg and ycfg.get("logged_in") and did:
        if acct.get("datasync_id") and acct["datasync_id"] != did:
            print(f"WARNING: {account_id} was previously recorded as {acct['datasync_id']}; now {did}.")
        acct["datasync_id"] = did
        save_accounts(cfg, accounts)
    print(f"[{account_id}] signed in: {bool(signed and ycfg and ycfg.get('logged_in'))}  "
          f"datasync_id: {did}  watch history off: {hist_off}")
    if hist_off:
        print("  -> turn watch history ON for this account before any session.")


async def check() -> None:
    cfg = load_config()
    async with async_playwright() as pw:
        for a in load_accounts(cfg):
            ctx = await open_profile(pw, cfg["browser"], a["id"])
            signed = await is_signed_in(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            ycfg, hist_off = await identity(page) if signed else (None, None)
            await ctx.close()
            did = (ycfg or {}).get("datasync_id")
            match = "n/a" if not a.get("datasync_id") else ("ok" if did == a["datasync_id"] else "MISMATCH")
            print(f"{a['id']:>8}  role={a.get('role')!s:<6} pair={a.get('pair')!s:<6} group={a.get('group')!s:<10} "
                  f"signed_in={bool(ycfg and ycfg.get('logged_in'))!s:<5} identity={match:<8} history_off={hist_off}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("account", nargs="?")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        asyncio.run(check())
    elif args.account:
        asyncio.run(login(args.account))
    else:
        ap.error("give an account id or --check")
