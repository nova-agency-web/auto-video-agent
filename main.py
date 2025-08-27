# main.py
import os
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VIDEO_PATH = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION", "Post auto vidéo automatique")
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "")
UA_RAW = os.getenv("TIKTOK_UA", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "FALSE").upper() == "TRUE"

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"

def parse_cookie_string(cookie_str: str):
    """
    Accepts the raw 'Cookie:' header string copied from DevTools and returns a list of Playwright cookie dicts.
    Only the minimal cookies that matter are kept; invalid pairs are ignored safely.
    """
    cookie_str = cookie_str.strip()
    parts = [p for p in cookie_str.split(";") if p.strip()]
    accepted = {}
    for p in parts:
        if "=" not in p: 
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            continue
        # skip known analytics caches to reduce noise
        accepted[k] = v
    # Build cookies for .tiktok.com
    cookies = []
    for k, v in accepted.items():
        cookies.append({
            "name": k,
            "value": v,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True
        })
    return cookies

def info(msg): print(f"[INFO] {msg}")
def warn(msg): print(f"[WARN] {msg}")
def err(msg): print(f"[ERREUR] {msg}")

def inject_cookies(context, cookie_raw: str):
    cookies = parse_cookie_string(cookie_raw)
    if not cookies:
        raise RuntimeError("Impossible de parser TIKTOK_COOKIE (aucun cookie valide trouvé).")
    # Playwright requires at least one page visit to set cookies for a domain
    tmp = context.new_page()
    tmp.goto("https://www.tiktok.com", wait_until="domcontentloaded", timeout=60000)
    context.add_cookies(cookies)
    tmp.close()
    info(f"Injection cookies ({len(cookies)})…")

def open_studio_and_get_upload_iframe(page):
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=90000)
    info("Navigation vers TikTok Studio Upload")
    # Wait for any iframe that hosts the uploader
    # TikTok often renders one or more app iframes; we’ll search for a file input within them.
    # Use generous timeout because Studio can be slow to hydrate.
    deadline = time.time() + 120
    target_frame = None
    while time.time() < deadline and target_frame is None:
        for f in page.frames:
            try:
                if f.url.startswith("https://www.tiktok.com") or "tiktok" in f.url:
                    # look for file input existence (attached, not necessarily visible)
                    loc = f.locator('input[type="file"][accept*="video"]')
                    if loc.count() > 0:
                        target_frame = f
                        break
            except Exception:
                continue
        if not target_frame:
            page.wait_for_timeout(500)
            page.reload(wait_until="domcontentloaded")
    if not target_frame:
        raise RuntimeError("Impossible de localiser l'iframe d'upload.")
    return target_frame

def force_show_and_upload(frame, video_path: str):
    """
    Find the (hidden) file input, force it visible, then set_input_files.
    """
    inputs = frame.locator('input[type="file"][accept*="video"]')
    count = inputs.count()
    if count == 0:
        raise RuntimeError("Aucun input[type='file'] trouvé dans l'iframe d'upload.")

    # Pick the first candidate and force it to be visible
    file_input = inputs.first()

    # Ensure the element exists in DOM
    file_input.wait_for(state="attached", timeout=60000)

    # Force show it (Playwright requires visibility for set_input_files)
    file_input.evaluate("""
        (el) => {
          el.removeAttribute('hidden');
          el.style.display = 'block';
          el.style.visibility = 'visible';
          el.style.opacity = '1';
          el.style.width = '1px';
          el.style.height = '1px';
          el.style.position = 'fixed';
          el.style.left = '0';
          el.style.top = '0';
          el.disabled = false;
        }
    """)

    # Now upload
    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Fichier vidéo introuvable: {p}")

    file_input.set_input_files(str(p))
    info("Upload déclenché ✅")

def fill_caption_and_scroll(page, caption: str):
    # Caption textarea on Studio page; avoid language-specific text by using role/placeholder attrs
    # Try common selectors, fallback to role=textbox
    selectors = [
        'textarea[placeholder*="description" i]',
        'textarea[placeholder*="Description" i]',
        'textarea[aria-label*="description" i]',
        'textarea'
    ]
    textarea = None
    for sel in selectors:
        loc = page.locator(sel)
        try:
            loc.wait_for(state="visible", timeout=5000)
            textarea = loc.first
            break
        except PWTimeout:
            continue

    if textarea:
        textarea.fill(caption)
        info("Légende insérée.")
    else:
        warn("Impossible de remplir la légende (textarea non trouvé).")

    # Scroll near bottom to ensure toggles/buttons are in view
    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")

def tick_compliance_and_try_publish(page):
    # Some accounts show compliance/privacy toggles; tick harmless ones if present.
    # We'll look for labeled checkboxes by accessible role.
    possible_labels = [
        "J'atteste", "J’accepte", "Conformité", "Cette vidéo respecte", "J’ai les droits"
    ]
    for text in possible_labels:
        try:
            cb = page.get_by_role("checkbox", name=text, exact=False)
            if cb.count() > 0:
                first = cb.first
                if not first.is_checked():
                    first.check(timeout=2000)
        except Exception:
            continue

    # Locate Publish button (fr or en)
    pub_names = ["Publier", "Post", "Publish"]
    publish_btn = None
    for name in pub_names:
        b = page.get_by_role("button", name=name, exact=False)
        if b.count():
            publish_btn = b.first
            break

    if not publish_btn:
        warn("Bouton 'Publier' introuvable.")
        return False

    # Ensure enabled and try click
    # Some buttons remain disabled until processing completes; poll for a while.
    deadline = time.time() + 180  # up to 3 min
    while time.time() < deadline:
        try:
            # Bring to view & click if enabled
            publish_btn.scroll_into_view_if_needed(timeout=2000)
            aria_disabled = publish_btn.get_attribute("aria-disabled")
            disabled_attr = publish_btn.get_attribute("disabled")
            if aria_disabled in ("false", None) and disabled_attr is None:
                if not DRY_RUN:
                    publish_btn.click(timeout=3000)
                info("Clic sur 'Publier' ✅")
                return True
        except Exception:
            pass
        page.wait_for_timeout(2000)

    warn("Traitement trop long ou bouton toujours inactif.")
    return False

def main():
    info(f"Compte ciblé: {os.getenv('ACCOUNT','(non précisé)')} | Posts: {os.getenv('POSTS_TO_PUBLISH','1')} | DRY_RUN={DRY_RUN}")
    with sync_playwright() as p:
        launch_args = {
            "headless": True,
            "args": [
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ]
        }
        browser = p.chromium.launch(**launch_args)

        context_args = {
            "locale": "fr-FR",
            "timezone_id": "Europe/Paris",
        }
        if UA_RAW:
            context_args["user_agent"] = UA_RAW

        context = browser.new_context(**context_args)

        # Inject cookies
        inject_cookies(context, COOKIE_RAW)

        page = context.new_page()
        page.set_default_timeout(60000)

        ok = False
        try:
            frame = open_studio_and_get_upload_iframe(page)
            force_show_and_upload(frame, VIDEO_PATH)
            fill_caption_and_scroll(page, CAPTION_TEXT)
            ok = tick_compliance_and_try_publish(page)
        except Exception as e:
            err(str(e))
        finally:
            # small grace period for any async processing
            page.wait_for_timeout(2000)
            if DRY_RUN:
                warn("DRY_RUN=TRUE -> pas de publication réelle.")
            context.close()
            browser.close()

        if not ok and not DRY_RUN:
            raise SystemExit(1)
        info("Run terminé ✅")

if __name__ == "__main__":
    main()
