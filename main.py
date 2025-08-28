# main.py
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
# Réglages principaux
# ──────────────────────────────────────────────────────────────────────────────
ACCOUNT = os.getenv("ACCOUNT", "default")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))

VIDEO_PATH = Path("assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "")

COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "")
UA_RAW = os.getenv("TIKTOK_UA", "")

def log(msg: str) -> None: print(msg, flush=True)
def warn(msg: str) -> None: print(f"[WARN] {msg}", flush=True)
def info(msg: str) -> None: print(f"[INFO] {msg}", flush=True)
def fail(msg: str) -> None:
    print(f"[ERREUR] {msg}", flush=True)
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Cookies
# ──────────────────────────────────────────────────────────────────────────────
WantedCookieNames = {
    "passport_csrf_token",
    "passport_csrf_token_default",
    "s_v_web_id",
    "msToken",
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "odin_tt",
}

def _normalize_same_site(v: Any) -> str:
    s = (str(v) if v is not None else "").strip().lower()
    if s in ("lax", "strict", "none"):
        return s.capitalize()
    return "Lax"

def _coerce_expires(v: Any) -> Optional[int]:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None

def parse_cookie_secret(raw: str) -> List[Dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"Impossible de parser TIKTOK_COOKIE: {e}")
    if not isinstance(parsed, list):
        fail("TIKTOK_COOKIE doit être un JSON array.")
    mapped: List[Dict[str, Any]] = []
    for c in parsed:
        try:
            name   = c.get("name")   or c.get("Name")
            value  = c.get("value")  or c.get("Value")
            domain = c.get("domain") or c.get("Domain")
            path   = c.get("path")   or c.get("Path") or "/"
            if not name or not value or not domain:
                continue
            if name not in WantedCookieNames:
                continue
            cookie: Dict[str, Any] = {
                "name": name, "value": value, "domain": domain, "path": path,
                "httpOnly": bool(c.get("httpOnly", c.get("HttpOnly", False))),
                "secure":   bool(c.get("secure",   c.get("Secure",   True))),
                "sameSite": _normalize_same_site(c.get("sameSite") or c.get("SameSite")),
            }
            exp_raw = c.get("expires") or c.get("Expires")
            exp_num = _coerce_expires(exp_raw)
            if exp_num is not None:
                cookie["expires"] = exp_num
            mapped.append(cookie)
        except Exception:
            continue
    return mapped

# ──────────────────────────────────────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────────────────────────────────────
STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"

def wait_upload_shell(page, timeout_ms: int = 60_000) -> None:
    # Attendre un minimum d’UI de la page d’upload
    hints = [
        "input[type='file']",
        "[data-e2e='upload']",
        "button:has-text('Importer')",
        "button:has-text('Upload')",
    ]
    for sel in hints:
        try:
            page.locator(sel).first.wait_for(state="attached", timeout=timeout_ms)
            return
        except PWTimeout:
            continue
    # fallback léger
    page.wait_for_timeout(1500)

def _make_visible(el_handle) -> None:
    try:
        el_handle.evaluate("""(el) => {
            try {
              el.hidden = false;
              el.removeAttribute('hidden');
              el.style.display = 'block';
              el.style.opacity = '1';
              el.style.visibility = 'visible';
              el.style.pointerEvents = 'auto';
              const p = el.parentElement;
              if (p) {
                p.hidden = false;
                p.removeAttribute('hidden');
                p.style.display = 'block';
                p.style.opacity = '1';
                p.style.visibility = 'visible';
              }
            } catch (e) {}
        }""")
    except Exception:
        pass

def attach_file_direct_anywhere(page, video_path: Path) -> bool:
    """
    Essaie de poser le fichier sur TOUT input[type=file] trouvé :
    - sur la page
    - dans tous les iframes
    Même si caché : on force la visibilité via JS.
    """
    frames = [page] + page.frames
    for fr in frames:
        try:
            loc = fr.locator("input[type='file']")
            n = loc.count()
            for i in range(n):
                h = loc.nth(i).element_handle()
                if not h:
                    continue
                _make_visible(h)
                try:
                    loc.nth(i).set_input_files(str(video_path))
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False

def try_open_file_chooser(page) -> Optional[Any]:
    """
    Clique des boutons connus pour ouvrir le file chooser.
    """
    candidates = [
        "[data-e2e='upload-button']",
        "[data-e2e='file-select']",
        "[data-testid='upload-btn']",
        "button:has-text('Importer')",
        "button:has-text('Upload')",
        "button:has-text('Select file')",
        "button:has-text('Select files')",
        "button:has-text('Choose file')",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                with page.expect_file_chooser() as fc_info:
                    btn.click()
                return fc_info.value
        except Exception:
            continue
    # tenter sur tous les boutons visibles si pas trouvé
    try:
        buttons = page.locator("button")
        for i in range(min(buttons.count(), 200)):
            b = buttons.nth(i)
            try:
                if b.is_visible():
                    with page.expect_file_chooser() as fc_info:
                        b.click()
                    return fc_info.value
            except Exception:
                continue
    except Exception:
        pass
    return None

def wait_upload_ready(page, timeout_ms: int = 180_000) -> None:
    probes = [
        "button:has-text('Publier')",
        "button:has-text('Post')",
        "[data-e2e='publish-button']",
        "textarea",
        "text=Confidentialité",
        "text=Description",
    ]
    for sel in probes:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return
        except PWTimeout:
            continue
    page.wait_for_timeout(2000)

def fill_caption_if_present(page, caption: str) -> None:
    if not caption.strip():
        return
    selectors = [
        "textarea[placeholder*='description']",
        "textarea[placeholder*='Description']",
        "textarea[placeholder*='légende']",
        "textarea[placeholder*='Légende']",
        "textarea",
        "[contenteditable='true']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.fill(caption)
                return
        except Exception:
            pass

def scroll_into_view_and_click(page, locator_str: str, retries: int = 80) -> bool:
    loc = page.locator(locator_str).first
    for _ in range(retries):
        try:
            if loc.is_visible() and not loc.is_disabled():
                loc.scroll_into_view_if_needed()
                loc.click()
                return True
        except Exception:
            pass
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(250)
    return False

def publish_now(page) -> bool:
    labels = [
        "button:has-text('Publier')",
        "button:has-text('Post')",
        "[data-e2e='publish-button']",
    ]
    for sel in labels:
        try:
            if scroll_into_view_and_click(page, sel, retries=80):
                return True
        except Exception:
            pass
    try:
        buttons = page.locator("button")
        for i in range(min(buttons.count(), 200)):
            btn = buttons.nth(i)
            try:
                txt = (btn.inner_text() or "").strip().lower()
                if txt in ("publier", "post") and btn.is_visible() and not btn.is_disabled():
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    warn("Bouton 'Publier' toujours inactif / non cliquable après délais.")
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Flux principal
# ──────────────────────────────────────────────────────────────────────────────
def publish_once(cookie_raw: str, caption: str, video_path: Path, ua_raw: str) -> bool:
    if not video_path.exists():
        fail(f"Vidéo introuvable : {video_path}")

    cookies = parse_cookie_secret(cookie_raw)
    if not cookies:
        fail("Impossible d'injecter des cookies (aucun cookie autorisé/valide trouvé).")

    info(f"Compte ciblé: {ACCOUNT} | Posts: 1 | DRY_RUN={DRY_RUN}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx_args = {}
        if ua_raw.strip():
            ctx_args["user_agent"] = ua_raw.strip()
        context = browser.new_context(**ctx_args)

        info(f"Injection cookies ({len(cookies)} entrées)…")
        try:
            context.add_cookies(cookies)
        except Exception as e:
            fail(f"BrowserContext.add_cookies: {e}")

        page = context.new_page()
        info("[NAV] Vers TikTok Studio Upload")
        page.goto(STUDIO_URL, wait_until="domcontentloaded")
        wait_upload_shell(page)

        # 1) Tenter l’injection directe partout (page + iframes), même si input caché
        if attach_file_direct_anywhere(page, video_path):
            info("Upload (input direct) déclenché ✅")
        else:
            # 2) Tenter l’ouverture d’un file chooser
            fc = try_open_file_chooser(page)
            if not fc:
                fail("Bouton pour ouvrir le file chooser introuvable.")
            try:
                fc.set_files(str(video_path))
                info("Upload (file chooser) déclenché ✅")
            except Exception as e:
                fail(f"Impossible de fournir le fichier via 'file chooser': {e}")

        # Attendre que l’écran de post soit prêt
        wait_upload_ready(page, timeout_ms=180_000)

        # Légende
        try:
            fill_caption_if_present(page, caption)
            if caption.strip():
                info("Légende insérée.")
        except Exception:
            warn("Impossible de remplir la légende (textarea non trouvé).")

        if DRY_RUN:
            info("Publication tentée (DRY_RUN=True).")
            context.close(); browser.close()
            return True

        ok = publish_now(page)
        context.close(); browser.close()
        return ok

def main() -> None:
    if not VIDEO_PATH.exists():
        fail(f"Vidéo introuvable : {VIDEO_PATH.resolve()}")
    if not COOKIE_RAW.strip():
        fail("TIKTOK_COOKIE vide / non défini.")

    success = 0
    for i in range(POSTS_TO_PUBLISH):
        info(f"— Post {i+1} / {POSTS_TO_PUBLISH} —")
        ok = publish_once(COOKIE_RAW, CAPTION_TEXT, VIDEO_PATH, UA_RAW)
        if ok: success += 1
    info(f"Run terminé ✅ ({success}/{POSTS_TO_PUBLISH} réussis)")
    if success == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
