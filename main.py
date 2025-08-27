# main.py
import os, json, time, re, sys
from pathlib import Path
from typing import List
from playwright.sync_api import sync_playwright, Browser, Page

# --------------------------
# ENV
# --------------------------
ACCOUNT = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "FALSE").strip().upper() == "TRUE"
TIKTOK_COOKIE = os.getenv("TIKTOK_COOKIE", "").strip()
TIKTOK_UA = os.getenv("TIKTOK_UA", "").strip()
VIDEO_PATH = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "Post upload vidéo automatique")
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"  # mettre HEADLESS=false pour debug local

def log(msg: str): print(msg, flush=True)

# --------------------------
# Cookies
# --------------------------
def parse_cookie_blob(blob: str):
    cookies = []
    if not blob:
        return cookies

    def base(name, value):
        return {
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        }

    # JSON dict / list ?
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            for k, v in data.items():
                if v is not None:
                    cookies.append(base(k, str(v)))
            return cookies
        if isinstance(data, list):
            for c in data:
                if "name" in c and "value" in c:
                    c.setdefault("domain", ".tiktok.com")
                    c.setdefault("path", "/")
                    c.setdefault("secure", True)
                    c.setdefault("httpOnly", False)
                    c.setdefault("sameSite", "Lax")
                    cookies.append(c)
            return cookies
    except Exception:
        pass

    # lignes "k=v" séparées par ; ou \n
    for p in re.split(r"[;\n]+", blob):
        p = p.strip()
        if not p or "=" not in p: continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            cookies.append(base(k, v))
    return cookies

def inject_cookies(context, blob: str):
    cookies = parse_cookie_blob(blob)
    if not cookies:
        log("[WARN] Aucun cookie valide détecté. On continue sans injection.")
        return
    try:
        context.add_cookies(cookies)
        log(f"[INFO] Injection cookies ({len(cookies)})…")
    except Exception as e:
        log(f"[WARN] Cookies ignorés par DevTools: {e}")

# --------------------------
# Navigation
# --------------------------
def go_to_upload(page: Page):
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
    try: page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception: pass
    log(f"[INFO] [NAV] Vers {UPLOAD_URL}")

# --------------------------
# Upload via FileChooser
# --------------------------
def trigger_upload_ui(page: Page) -> bool:
    """
    Tente de cliquer différents CTA/zone pour ouvrir le sélecteur de fichiers.
    """
    triggers = [
        "button:has-text('Sélectionner une vidéo')",
        "button:has-text('Select video')",
        "[data-e2e='upload-video'] button",
        "[data-e2e='upload-video']",
        "button:has-text('Importer')",
        "div:has-text('Sélectionne une vidéo')",
        "div:has-text('Select a video')",
        # parfois une icône 'plus' ou 'upload' cliquable :
        "button svg",
    ]
    for sel in triggers:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0: continue
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=2000)
            return True
        except Exception:
            continue
    return False

def attach_video_via_filechooser(page: Page, file_path: str) -> bool:
    """
    Méthode robuste : on attend le file chooser, puis on clique différents triggers.
    """
    # 1) si un vrai input visible existe, on le prend directement
    try:
        inp = page.locator('input[type="file"]').first
        inp.wait_for(state="attached", timeout=2000)
        # si invisible, set_input_files marche parfois quand même :
        inp.set_input_files(file_path)
        log("[INFO] Upload (input direct) déclenché ✅")
        return True
    except Exception:
        pass

    # 2) filechooser (cas Studio)
    timeout_ms = 15_000
    try:
        with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
            # on déclenche via les différents boutons
            if not trigger_upload_ui(page):
                # un second essai après un petit scroll
                page.mouse.wheel(0, 600)
                time.sleep(0.3)
                if not trigger_upload_ui(page):
                    raise RuntimeError("Aucun déclencheur d'upload cliquable trouvé.")
        chooser = fc_info.value
        chooser.set_files(file_path)
        log("[INFO] Upload (file chooser) déclenché ✅")
        return True
    except Exception as e:
        log(f"[ERREUR] Ouverture du file chooser impossible: {e}")
        return False

# --------------------------
# Légende + Publication
# --------------------------
def fill_caption(page: Page, caption: str) -> bool:
    if not caption: return True
    selectors = [
        "textarea",
        "[data-e2e='video-caption']",
        "[data-e2e='caption-input']",
        'div[role="textbox"]',
        'div[contenteditable="true"]',
    ]
    contexts = [page, *page.frames]
    for ctx in contexts:
        for sel in selectors:
            try:
                loc = ctx.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                try:
                    loc.fill("")
                except Exception:
                    ctx.evaluate("(el)=>{el.textContent=''; el.innerHTML='';}", loc.element_handle())
                loc.type(caption, delay=15)
                log("[INFO] Légende insérée.")
                return True
            except Exception:
                continue
    log("[WARN] Champ légende introuvable.")
    return False

def click_publish(page: Page, max_wait_sec: int = 180) -> bool:
    publish_selectors = [
        "button:has-text('Publier')",
        "[data-e2e='publish-button']",
        'button[type="submit"]',
        'button[aria-label*="Publier"]',
        "button:has-text('Post')",
    ]
    start = time.time()
    time.sleep(2)
    while time.time() - start < max_wait_sec:
        for sel in publish_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() == 0: continue
                if btn.is_enabled(timeout=1000):
                    btn.click(timeout=1000)
                    log("[INFO] Clic 'Publier' ✅")
                    return True
                # fallback: clic forcé
                page.evaluate("(b)=>b.click()", btn.element_handle())
                log("[INFO] Clic forcé 'Publier' ✅")
                return True
            except Exception:
                pass
        time.sleep(2)
    log("[WARN] Bouton 'Publier' non cliquable dans les temps.")
    return False

# --------------------------
# Un post
# --------------------------
def run_once(play) -> bool:
    browser: Browser = play.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-web-security",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    kwargs = {}
    if TIKTOK_UA: kwargs["user_agent"] = TIKTOK_UA
    context = browser.new_context(**kwargs)
    page = context.new_page()

    if TIKTOK_COOKIE:
        inject_cookies(context, TIKTOK_COOKIE)

    go_to_upload(page)

    # si on vient d’injecter des cookies, petit reload
    if TIKTOK_COOKIE:
        page.reload(wait_until="domcontentloaded")
        try: page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception: pass

    video_abs = str(Path(VIDEO_PATH).resolve())
    if not Path(video_abs).exists():
        log(f"[ERREUR] Fichier vidéo introuvable: {video_abs}")
        browser.close()
        return False

    # Upload
    if not attach_video_via_filechooser(page, video_abs):
        log("[ERREUR] Impossible de localiser/déclencher l’upload (file chooser).")
        browser.close()
        return False

    # Laisser TikTok ingérer le fichier
    time.sleep(5)

    # Légende
    fill_caption(page, CAPTION_TEXT)

    if DRY_RUN:
        log("[INFO] DRY_RUN=TRUE -> arrêt avant la publication.")
        browser.close()
        return True

    # Publier
    ok = click_publish(page, max_wait_sec=180)
    time.sleep(3)
    browser.close()
    return ok

# --------------------------
# Main
# --------------------------
def main():
    log(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    ok_all = True
    with sync_playwright() as p:
        for i in range(POSTS_TO_PUBLISH):
            log(f"[INFO] — Post {i+1}/{POSTS_TO_PUBLISH} —")
            try:
                if not run_once(p):
                    ok_all = False
            except Exception as e:
                log(f"[ERREUR] Exception pendant la publication: {e}")
                ok_all = False
    if not ok_all:
        sys.exit(1)

if __name__ == "__main__":
    main()
