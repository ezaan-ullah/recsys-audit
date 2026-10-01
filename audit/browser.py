"""Persistent per-account Chrome profiles.

What the researcher sees is controlled by `browser.screen`. The default `overlay` covers
the page with an opaque CSS pseudo-element (one <style> tag, no overlay element, no GPU
blur), and the video plays normally underneath, so the platform sees ordinary watch behavior.
Audio is muted at the browser level, which the page cannot observe: the player itself
stays unmuted (each row logs the player's `muted` and `volume` to verify this).
"""
import json

from .config import ROOT

_SCREEN_CSS = {
    "overlay": ("html::after { content: 'audit running: content hidden'; position: fixed; inset: 0; "
                "background: #1e1e1e; color: #777; font: 16px sans-serif; display: flex; "
                "align-items: center; justify-content: center; z-index: 2147483647; "
                "pointer-events: none; }"),
    "blur": "body { filter: blur(24px) grayscale(1) !important; }",
    "media": "video, img, canvas, yt-image, #thumbnail { filter: blur(48px) grayscale(1) !important; }",
}

_INJECT = """
(() => {
  const CSS = __CSS__;
  const add = () => {
    if (document.getElementById('__audit_screen')) return;
    const s = document.createElement('style');
    s.id = '__audit_screen';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  };
  add();
  document.addEventListener('DOMContentLoaded', add);
})();
"""

_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    # Keep playback running when windows are covered by other windows.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]

SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID"}


async def open_profile(pw, bcfg: dict, account_id: str, screen: str | None = None):
    profile_dir = ROOT / bcfg["profiles_dir"] / account_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = list(_ARGS)
    if bcfg.get("mute", True):
        args.append("--mute-audio")
    w, h = bcfg.get("viewport", [1280, 800])
    ctx = await pw.chromium.launch_persistent_context(
        str(profile_dir),
        channel=bcfg.get("channel") or None,
        headless=False,
        args=args,
        ignore_default_args=["--enable-automation"],
        viewport={"width": w, "height": h},
    )
    await apply_screen(ctx, screen if screen is not None else bcfg.get("screen", "overlay"))
    return ctx


async def apply_screen(ctx, mode: str) -> None:
    """Hide page content from the researcher for every page loaded from now on."""
    if mode not in _SCREEN_CSS and mode != "none":
        raise ValueError(f"browser.screen must be one of {sorted(_SCREEN_CSS)} or none")
    if mode in _SCREEN_CSS:
        await ctx.add_init_script(_INJECT.replace("__CSS__", json.dumps(_SCREEN_CSS[mode])))


async def is_signed_in(ctx) -> bool:
    """Cheap pre-check. Google SID cookies alone are not enough: a profile can hold them while
    YouTube itself is signed out (seen on the pilot). LOGIN_INFO is YouTube's own sign-in
    cookie. The authoritative check is ytcfg LOGGED_IN, read at the start of every session."""
    names = {c["name"] for c in await ctx.cookies("https://www.youtube.com")}
    return bool(names & SESSION_COOKIES) and "LOGIN_INFO" in names
