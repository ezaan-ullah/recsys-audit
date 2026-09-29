"""Sign an account in by hand (once), or check that all accounts are signed in.

    python login.py acct01      # opens Chrome with acct01's profile; sign in manually
    python login.py --check     # reports signed-in status for every account
"""
import argparse
import asyncio

from playwright.async_api import async_playwright

from audit.browser import is_signed_in, open_profile
from audit.config import load_accounts, load_config


async def login(account_id: str) -> None:
    cfg = load_config()
    async with async_playwright() as pw:
        ctx = await open_profile(pw, cfg["browser"], account_id, blur="none")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.youtube.com")
        print(f"\n[{account_id}] In the browser window:\n"
              "  1. Sign in to this account's Google login.\n"
              "  2. Confirm YouTube watch history is ON.\n"
              "  3. Don't watch, search, like, or subscribe to anything.\n")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when done... ")
        ok = await is_signed_in(ctx)
        await ctx.close()
    print(f"[{account_id}] signed in: {ok}")


async def check() -> None:
    cfg = load_config()
    async with async_playwright() as pw:
        for a in load_accounts(cfg):
            ctx = await open_profile(pw, cfg["browser"], a["id"])
            ok = await is_signed_in(ctx)
            await ctx.close()
            print(f"{a['id']:>8}  group={a.get('group')!s:<10}  signed_in={ok}")


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
