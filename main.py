# main.py
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
# Réglages principaux (→ adapté pour assets/test.mp4)
# ──────────────────────────────────────────────────────────────────────────────
ACCOUNT = os.getenv("ACCOUNT", "default")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))

# >>> ICI le chemin de la vidéo dans le dépôt GitHub :
VIDEO_PATH = Path("assets/test.mp4")             # ⟵ CHANGÉ
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "")     # optionnel

# Secrets (GH Actions → Settings → Secrets and variables → Actions)
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "")      # attendu: JSON array de cookies [{...}, ...]
UA_RAW = os.getenv("TIKTOK_UA", "")              # user-agent (string)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)

def fail(msg: str) -> None:
    print(f"[ERREUR] {msg}", flush=True)
    sys.exit(1)

def warn(msg: str) -> None:
    print(f"[WARN] {msg}", flush=True)

def info(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


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

def parse_cookie_secret(raw: str) -> List[Dict[str, Any]]:
    """
    Accepte un JSON array de cookies au format:
    [
      {"name":"...","value":"...","domain":"tiktok.com",".path":"/","secure":true,"httpOnly":false,"sameSite":"Lax","expires": 1761472716},
      ...
    ]
    On filtre et on convertit pour Playwright BrowserContext.add_cookies.
    """
    if not raw.strip():
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"Impossible de parser TIKTOK_COOKIE: {e}")

    if not isinstance(parsed, list):
        fail("TIKTOK_COOKIE doit être un JSON array de cookies.")

    mapped = []
    for c in parsed:
        try:
            name = c.get("name") or c.get("Name")
            value = c.get("value") or c.get("Value")
            domain = c.get("domain") or c.get("Domain")
            path = c.get("path") or c.get("Path") or "/"

            if not name or not value or not domain:
                continue

            # Filtre: on garde seulement les cookies utiles / valides
            if name not in WantedCookieNames:
                continue

            same_site = (c.get("sameSite") or c.get("SameSite") or "").lower()
            if same_site in ("lax", "strict", "none"):
                ss = same_site.capitalize()
            else:
                ss = "Lax"  # défaut

            expires = c.get("expires") or c.get("Expires")
            # Playwright accepte un int (seconds since epoch) ou None
            if isinstance(expires, (int, float)):
                expires_val = int(expires)
            else:
                expires_val = None

            mapped.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "httpOnly": bool(c.get("httpOnly", c.get("HttpOnly", False))),
                "secure": bool(c.get("secure", c.get("Secure", True))),
                "sameSite": ss,      # "Lax" | "Strict" | "None"
                "expires": expires_val
            })
        except Exception:
            continue

    return mapped


# ──────────────────────────────────────────────────────────────────────────────
# Sélecteurs robustes + actions
# ──────────────────────────────────────────────────────────────────────────────
STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"

def find_file_chooser_button(page) -> Optional[Any]:
    """
    Essaye plusieurs variantes de boutons/inputs d'upload.
    """
    candidates = [
        # input direct
        "input[type='file'][accept*='video']",
        "input[type='file']",

        # boutons qui ouvrent l'input
        "[data-e2e='upload-button']",
        "[data-e2e='file-select']",
        "[data-testid='upload-btn']",
        "button:has-text('Importer')",
        "button:has-text('Upload')",
        "button:has-text('Select file')",
    ]
    for sel in candidates:
        try:
            el = page.locator(sel)
            if el.count() > 0 and el.first.is_visible():
                return el.first
        except Exception:
            pass
    return None

def attach_file_direct_if_possible(page, video_path: Path) -> bool:
    """
    Essaye de repérer un <input type=file> visible et d'y attacher la vidéo.
    """
    try:
        inp = page.locator("input[type='file']")
        if inp.count() == 0:
            return False
        # On cherche un input acceptant la vidéo
        for i in range(inp.count()):
            el = inp.nth(i)
            try:
                if el.is_visible():
                    el.set_input_files(str(video_path))
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False

def wait_upload_ready(page, timeout_ms: int = 120_000) -> None:
    """
    Attend des marqueurs plausibles de fin d'upload/analyse (à adapter si besoin).
    """
    # Plusieurs signaux possibles, on en accepte un des deux
    # 1) Disparition d'un indicateur de progression
    # 2) Apparition d’un bouton/label "Publier" ou section des options
    candidates = [
        "button:has-text('Publier')",
        "button:has-text('Post')",
        "[data-e2e='publish-button']",
        "text=Confidentialité",     # section des paramètres visibles
        "text=Description",         # zone de légende visible
    ]
    with page.expect_console_message(timeout=timeout_ms) as maybe:
        # On ne compte pas vraiment sur les logs; on attend plutôt le DOM ci-dessous
        pass
    for sel in candidates:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return
        except PWTimeout:
            continue
    # Dernier essai: petit sleep en plus si rien de visible (tolérance)
    page.wait_for_timeout(2000)


def fill_caption_if_present(page, caption: str) -> None:
    if not caption.strip():
        return
    # Divers sélecteurs plausibles pour la zone description
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
    """
    Essaie de cliquer le bouton 'Publier' avec pas mal de tolérance.
    """
    labels = [
        "button:has-text('Publier')",
        "button:has-text('Post')",
        "[data-e2e='publish-button']",
    ]

    # Essai direct
    for sel in labels:
        try:
            ok = scroll_into_view_and_click(page, sel, retries=60)
            if ok:
                return True
        except Exception:
            pass

    # Fallback: fouille tout le DOM à la recherche d’un bouton activable
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
        context_args = {}
        if ua_raw.strip():
            context_args["user_agent"] = ua_raw.strip()
        context = browser.new_context(**context_args)

        # Ajout cookies
        info(f"Injection cookies ({len(cookies)} entrées)…")
        try:
            context.add_cookies(cookies)
        except Exception as e:
            fail(f"BrowserContext.add_cookies: {e}")

        page = context.new_page()
        info(f"[NAV] Vers TikTok Studio Upload")
        page.goto(STUDIO_URL, wait_until="domcontentloaded")

        # Tente input direct d'abord
        attached = attach_file_direct_if_possible(page, video_path)
        if not attached:
            # Sinon, clique un bouton qui ouvre le file chooser
            btn = find_file_chooser_button(page)
            if not btn:
                fail("Bouton pour ouvrir le file chooser introuvable.")
            with page.expect_file_chooser() as fc_info:
                btn.click()
            try:
                fc = fc_info.value
                fc.set_files(str(video_path))
                attached = True
            except Exception as e:
                fail(f"Impossible de fournir le fichier via 'file chooser': {e}")

        info("Upload déclenché ✅")

        # Attente fin d’upload
        wait_upload_ready(page, timeout_ms=180_000)

        # Légende (optionnel)
        if caption.strip():
            try:
                fill_caption_if_present(page, caption.strip())
                info("Légende insérée.")
            except Exception:
                warn("Avertissement: impossible de remplir la légende (textarea non trouvé).")

        if DRY_RUN:
            info("Publication tentée (DRY_RUN=True).")
            context.close()
            browser.close()
            return True

        # Publier maintenant
        ok = publish_now(page)
        context.close()
        browser.close()
        return ok


def main() -> None:
    # Vérifs de base
    if not VIDEO_PATH.exists():
        fail(f"Vidéo introuvable : {VIDEO_PATH.resolve()}")

    if not COOKIE_RAW.strip():
        fail("TIKTOK_COOKIE vide / non défini.")

    # On peut poster plusieurs vidéos si besoin via POSTS_TO_PUBLISH
    success = 0
    for i in range(POSTS_TO_PUBLISH):
        info(f"— Post {i+1} / {POSTS_TO_PUBLISH} —")
        try:
            ok = publish_once(COOKIE_RAW, CAPTION_TEXT, VIDEO_PATH, UA_RAW)
            if ok:
                success += 1
        except Exception as e:
            fail(str(e))

    info(f"Run terminé ✅ ({success}/{POSTS_TO_PUBLISH} réussis)")
    if success == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
