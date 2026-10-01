"""Every assumption about YouTube's front end lives here.

The active Short is found geometrically (the visible <video> covering the viewport
centre), not by custom-element names, so it survives most markup changes. Element
selectors are only used as extra signals for ads and interstitials, and all text
patterns assume the page is loaded with hl=en. Re-check these on the pilot accounts
whenever YouTube changes its UI; the session log's active_video_rate shows breakage.
"""
import json

# Shared JS prelude: pick the active video and the reel card around it.
_PICK = r"""
const pickVideo = () => {
  const cx = innerWidth / 2, cy = innerHeight / 2;
  let best = null, bestArea = 0;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) continue;
    const covers = r.left <= cx && r.right >= cx && r.top <= cy && r.bottom >= cy;
    const area = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0)) *
                 Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    if (area > 0 && (covers ? area * 4 : area) > bestArea) { best = v; bestArea = covers ? area * 4 : area; }
  }
  return best;
};
const cardOf = (v) => {
  if (!v) return null;
  const sel = v.closest('ytd-reel-video-renderer, ytm-reel-video-renderer, [is-active]');
  if (sel) return sel;
  let el = v, card = v;
  while (el.parentElement && el.parentElement !== document.body) {
    el = el.parentElement;
    if (el.getBoundingClientRect().height > innerHeight * 1.15) break;
    card = el;
  }
  return card;
};
"""

_AD_SELECTORS = ("ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer, ad-badge-view-model, "
                 "[class*='ad-badge'], .ytp-ad-player-overlay, ytd-ad-inline-playback-meta-block, "
                 "ytd-reel-player-overlay-renderer [aria-label='Sponsored']")

# (type, lowercase substrings). Matched against the active card and any open dialog.
INTERSTITIALS = (
    ("self_harm_warning", ("suicide or self-harm", "self-harm topics", "suicide and self-harm")),
    ("crisis_resources", ("help is available", "speak with someone", "need support", "helpline", "crisis line")),
    ("discretion_warning", ("viewer discretion", "may be disturbing", "graphic content", "sensitive content")),
    ("age_gate", ("inappropriate for some users", "age-restricted", "confirm your age")),
)

STATE_JS = _PICK + r"""
const v = pickVideo();
const card = cardOf(v);
const out = {url: location.href, video: !!v, card_tag: card ? card.tagName.toLowerCase() : null,
             active_selector: !!document.querySelector('ytd-reel-video-renderer[is-active]'),
             is_ad: false, ad_signal: null, interstitial: [], snippet: null};
if (v) Object.assign(out, {paused: v.paused, muted: v.muted, volume: v.volume, currentTime: v.currentTime,
                           duration: isFinite(v.duration) ? v.duration : null, readyState: v.readyState});
if (card) {
  if (card.querySelector(AD_SELECTORS)) { out.is_ad = true; out.ad_signal = 'dom'; }
  const text = (card.innerText || '');
  if (!out.is_ad && /(^|\n)\s*Sponsored\s*(·|\n|$)/.test(text)) { out.is_ad = true; out.ad_signal = 'text'; }
  const dialogs = [...document.querySelectorAll('tp-yt-paper-dialog, ytd-popup-container [role=dialog], yt-playability-error-supported-renderers')]
    .filter(d => d.offsetParent !== null).map(d => d.innerText || '').join('\n');
  const hay = (text + '\n' + dialogs).toLowerCase();
  for (const [type, pats] of INTERSTITIALS) {
    const hit = pats.find(p => hay.includes(p));
    if (hit) {
      out.interstitial.push(type);
      if (!out.snippet) { const i = hay.indexOf(hit); out.snippet = hay.slice(Math.max(0, i - 60), i + 100).replace(/\s+/g, ' '); }
    }
  }
  if (card.querySelector('ytd-info-panel-content-renderer, ytd-clarification-renderer, yt-info-panel-container-view-model'))
    out.interstitial.push('info_panel');
}
return out;
"""

YTCFG_JS = r"""
const c = window.ytcfg;
if (!c || !c.get) return null;
return {logged_in: c.get('LOGGED_IN'), datasync_id: c.get('DATASYNC_ID') || null, hl: c.get('HL') || null, gl: c.get('GL') || null};
"""

TEXT_JS = r"""
return (document.body ? document.body.innerText : '').slice(0, 50000).toLowerCase();
"""

FRAME_JS = _PICK + r"""
const v = pickVideo();
if (!v || v.readyState < 2 || !v.videoWidth) return null;
const scale = Math.min(1, maxW / v.videoWidth);
const c = document.createElement('canvas');
c.width = Math.round(v.videoWidth * scale); c.height = Math.round(v.videoHeight * scale);
try { c.getContext('2d').drawImage(v, 0, 0, c.width, c.height); return c.toDataURL('image/jpeg', 0.7); }
catch (e) { return 'tainted'; }
"""

CHROME_JS = r"""
try {
  const d = await navigator.userAgentData.getHighEntropyValues(['fullVersionList']);
  const b = d.fullVersionList.find(x => x.brand === 'Google Chrome') || d.fullVersionList.find(x => x.brand === 'Chromium');
  return b ? b.version : navigator.userAgent;
} catch (e) { return navigator.userAgent; }
"""

HISTORY_OFF = ("watch history is off", "watch history is paused", "your watch history is off",
               "history is paused")
CHALLENGE = ("confirm you’re not a bot", "confirm you're not a bot", "unusual traffic from your computer")


def _fn(body: str, arg: str = "") -> str:
    return f"async ({arg}) => {{ {body} }}"


def _inline_constants(js: str) -> str:
    return (js.replace("AD_SELECTORS", json.dumps(_AD_SELECTORS))
              .replace("INTERSTITIALS", json.dumps([[t, list(p)] for t, p in INTERSTITIALS])))


STATE_FN = _fn(_inline_constants(STATE_JS))
YTCFG_FN = _fn(YTCFG_JS)
TEXT_FN = _fn(TEXT_JS)
FRAME_FN = _fn(FRAME_JS, "maxW")
CHROME_FN = _fn(CHROME_JS)


async def _eval(page, fn: str, arg=None, default=None):
    try:
        return await (page.evaluate(fn, arg) if arg is not None else page.evaluate(fn))
    except Exception:   # navigation in progress, page closed, etc.
        return default


async def state(page) -> dict:
    return await _eval(page, STATE_FN, default=None) or {"url": page.url, "video": False, "interstitial": []}


async def ytcfg(page) -> dict | None:
    return await _eval(page, YTCFG_FN)


async def page_text(page) -> str:
    return await _eval(page, TEXT_FN, default="") or ""


async def history_off(page) -> bool:
    text = await page_text(page)
    return any(p in text for p in HISTORY_OFF)


async def challenge(page) -> bool:
    text = await page_text(page)
    return any(p in text for p in CHALLENGE)


async def grab_frame(page, max_w: int = 480) -> str | None:
    return await _eval(page, FRAME_FN, max_w)


async def chrome_version(page) -> str | None:
    return await _eval(page, CHROME_FN)
