# main.py
import os, json, time, re, sys
from pathlib import Path
from typing import List, Optional
from playwright.sync_api import sync_playwright, Browser, Page, expect

# --------------------------
# Config lecture ENV
# --------------------------
ACCOUNT = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "FALSE").strip().upper() == "TRUE"
TIKTOK_COOKIE = os.getenv("TIKTOK_COOKIE", "").strip()  # string JSON (clé=valeur) ou lignes "clé=valeur"
TIKTOK_UA = os.getenv("TIKTOK_UA", "").strip()

VIDEO_PATH = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "Post upload vidéo automatique")
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"

# --------------------------
# Utilitaires
# --------------------------
def log(msg: str):
    print(msg, flush=True)

def parse_cookie_blob(blob: str) -> List[dict]:
    """
    Accepte:
      - JSON: {"sessionid":"...","msToken":"..."}
      - Texte: lignes "nom=valeur"
    Retourne un tableau [{name, value, domain, path, httpOnly, secure, sameSite}]
    """
    cookies = []
    if not blob:
        return cookies

    def base_cookie(name, value):
        return {
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        }

    # JSON ?
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            for k, v in data.items():
                if v is None:
                    continue
                cookies.append(base_cookie(k, str(v)))
            return cookies
        elif isinstance(data, list):
            # déjà une liste de cookies Playwright ?
            for c in data:
                if "name" in c and "value" in c:
                    # complétion domaine / chemin si absents
                    c.setdefault("domain", ".tiktok.com")
                    c.setdefault("path", "/")
                    c.setdefault("secure", True)
                    c.setdefault("httpOnly", False)
                    c.setdefault("sameSite", "Lax")
                    cookies.append(c)
            return cookies
    except Exception:
        pass

    # sinon: lignes nom=valeur; séparateurs ; ou \n
    parts = re.split(r"[;\n]+", blob)
    for p in parts:
        p = p.strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        cookies.append(base_cookie(k, v))
    return cookies

def attach_video(page: Page, file_path: str) -> bool:
    """
    Tente plusieurs sélecteurs d'input file et set_input_files.
    """
    selectors = [
        'input[type="file"][accept*="video"]',
        'input[type="file"]',
        '[data-e2e="upload-video"] input[type="file"]',
        # fallback: bouton "Sélectionner une vidéo" -> son input
        # on clique d'abord le bouton si présent
    ]

    # Essai clic sur CTA "Sélectionner une vidéo"
    try:
        btn = page.locator("button:has-text('Sélectionner une vidéo')")
        if btn.count() > 0:
            btn.first.click(timeout=3000)
            time.sleep(0.5)
    except Exception:
        pass

    for sel in selectors:
        try:
            file_input = page.locator(sel).first
            file_input.wait_for(state="visible", timeout=5000)
            file_input.set_input_files(file_path)
            log("[INFO] Upload déclenché ✅")
            return True
        except Exception:
            continue
    return False

def fill_caption(page: Page, caption: str) -> bool:
    """
    Remplit la légende en essayant plusieurs cibles: textarea, contenteditable, data-e2e…
    """
    if not caption:
        return True

    targets = [
        "textarea",
        '[data-e2e="video-caption"]',
        '[data-e2e="caption-input"]',
        'div[contenteditable="true"]',
        'div[role="textbox"]',
    ]

    # parfois le champ est dans un iframe studio: on tente la page d'abord, puis iframes
    contexts = [page]
    contexts.extend(page.frames)

    for ctx in contexts:
        for sel in targets:
            try:
                loc = ctx.locator(sel).first
                loc.wait_for(state="visible", timeout=4000)
                # clear + type
                try:
                    loc.fill("")  # si c'est un textarea/input
                except Exception:
                    # contenteditable
                    ctx.evaluate(
                        """(el)=>{el.innerHTML=''; el.textContent='';}""",
                        loc.element_handle()
                    )
                # type doucement
                loc.type(caption, delay=15)
                log("[INFO] Légende insérée.")
                return True
            except Exception:
                continue
    return False

def click_publish(page: Page, max_wait_sec: int = 180) -> bool:
    """
    Attend que le bouton Publier devienne cliquable, puis clique. Réessaye jusqu'à max_wait_sec.
    """
    publish_selectors = [
        "button:has-text('Publier')",
        '[data-e2e="publish-button"]',
        'button[type="submit"]',
        'button[aria-label*="Publier"]',
    ]

    start = time.time()
    # Attente initiale (traitement vidéo)
    time.sleep(3)

    while time.time() - start < max_wait_sec:
        for sel in publish_selectors:
            try:
                btn = page.locator(sel).first
                # on vérifie existence, visibilité et état enabled
                if btn.count() > 0:
                    # parfois visible mais disabled -> on évalue disabled
                    try:
                        if btn.is_enabled(timeout=1000):
                            btn.click(timeout=1000)
                            log("[INFO] Clic sur 'Publier' ✅")
                            return True
                    except Exception:
                        pass
                    # forcer le clic (quelques sites le permettent)
                    try:
                        page.evaluate("(b)=>b.click()", btn.element_handle())
                        log("[INFO] Clic forcé sur 'Publier' ✅")
                        return True
                    except Exception:
                        pass
            except Exception:
                pass
        time.sleep(2)
    return False

def go_to_upload(page: Page):
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
    # Si TikTok redirige vers login, l’UI reste accessible si session OK. On laisse charger:
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    log(f"[INFO] [NAV] Vers {UPLOAD_URL}")

def inject_cookies(context, cookie_blob: str):
    cookies = parse_cookie_blob(cookie_blob)
    if not cookies:
        log("[WARN] Aucun cookie fourni (ou non valide). On continue sans injection.")
        return
    try:
        context.add_cookies(cookies)
        log(f"[INFO] Injection cookies ({len(cookies)} entrées)…")
    except Exception as e:
        log(f"[WARN] Cookies ignorés (invalides pour DevTools): {e}")

def main_once(play: "Playwright") -> bool:
    browser_args = [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-web-security",
        "--disable-blink-features=AutomationControlled",
    ]

    chromium = play.chromium
    browser: Browser = chromium.launch(headless=True, args=browser_args)
    ctx_kwargs = {}
    if TIKTOK_UA:
        ctx_kwargs["user_agent"] = TIKTOK_UA

    context = browser.new_context(**ctx_kwargs)
    page = context.new_page()

    # cookies avant navigation si on en a
    if TIKTOK_COOKIE:
        inject_cookies(context, TIKTOK_COOKIE)

    go_to_upload(page)

    # (re)charger la page après cookies si besoin
    if TIKTOK_COOKIE:
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

    # Vérif fichier
    video_abs = str(Path(VIDEO_PATH).resolve())
    if not Path(video_abs).exists():
        log(f"[ERREUR] Fichier vidéo introuvable: {video_abs}")
        browser.close()
        return False

    # Sélection champ file + upload
    if not attach_video(page, video_abs):
        log("[ERREUR] Impossible de localiser le champ fichier (input[type='file']).")
        browser.close()
        return False

    # Attendre un peu que TikTok ingère le fichier
    time.sleep(5)

    # Remplir légende (best-effort)
    if not fill_caption(page, CAPTION_TEXT):
        log("[WARN] Avertissement: impossible de remplir la légende (textarea non trouvé).")

    # Si DRY_RUN, on s'arrête avant la publication
    if DRY_RUN:
        log("[INFO] DRY_RUN=TRUE -> arrêt avant publication.")
        browser.close()
        return True

    # Attendre traitement + bouton Publier
    if not click_publish(page, max_wait_sec=180):
        log("[WARN] Avertissement: bouton 'Publier' non cliquable/détecté.")
        browser.close()
        return False

    # Laisser le temps à TikTok de répondre
    time.sleep(4)
    browser.close()
    return True


def main():
    log(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    ok_all = True
    with sync_playwright() as p:
        for i in range(POSTS_TO_PUBLISH):
            log(f"[INFO] — Post {i+1}/{POSTS_TO_PUBLISH} —")
            try:
                ok = main_once(p)
                if not ok:
                    ok_all = False
            except Exception as e:
                log(f"[ERREUR] Exception pendant la publication: {e}")
                ok_all = False
    if not ok_all:
        sys.exit(1)

if __name__ == "__main__":
    main()
