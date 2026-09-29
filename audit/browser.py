"""Persistent per-account Chrome profiles.

The page is blurred and muted on the researcher's screen by default. Blurring is
client-side CSS: the video still plays normally, so the platform sees ordinary
watch behavior.
"""
import json

from .config import ROOT

_BLUR_CSS = {
    "all": "body { filter: blur(24px) grayscale(1) !important; }",
    "media": "video, img, canvas, yt-image, #thumbnail { filter: blur(48px) grayscale(1) !important; }",
}

_INJECT = """
(() => {
  const CSS = __CSS__;
  const add = () => {
    if (document.getElementById('__audit_blur')) return;
    const s = document.createElement('style');
    s.id = '__audit_blur';
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


async def open_profile(pw, bcfg: dict, account_id: str, blur: str | None = None):
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
    mode = blur if blur is not None else bcfg.get("blur", "all")
    if mode in _BLUR_CSS:
        await ctx.add_init_script(_INJECT.replace("__CSS__", json.dumps(_BLUR_CSS[mode])))
    return ctx


async def is_signed_in(ctx) -> bool:
    names = {c["name"] for c in await ctx.cookies("https://www.youtube.com")}
    return bool(names & SESSION_COOKIES)
