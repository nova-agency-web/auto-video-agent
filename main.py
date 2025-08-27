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

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"  # HEADLESS=false pour debug

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

    # JSON dict/list ?
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

    # “k=v” séparés par ; ou \n
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
    triggers = [
        "button:has-text('Sélectionner une vidéo')",
        "button:has-text('Select video')",
        "[data-e2e='upload-video'] button",
        "[data-e2e='upload-video']",
        "button:has-text('Importer')",
        "div:has-text('Sélectionne une vidéo')",
        "div:has-text('Select a video')",
        "button svg",  # icône upload
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
    # 1) input direct si accessible
    try:
        inp = page.locator('input[type="file"]').first
        inp.wait_for(state="attached", timeout=2000)
        inp.set_input_files(file_path)
        log("[INFO] Upload (input direct) déclenché ✅")
        return True
    except Exception:
        pass

    # 2) file chooser
    try:
        with page.expect_file_chooser(timeout=15_000) as fc_info:
            if not trigger_upload_ui(page):
                page.mouse.wheel(0, 800)
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
# Traitement & Publish
# --------------------------
PROCESS_TEXT_MARKERS = [
    "processing", "traitement", "generating", "génération",
    "checking", "analyse", "analyzing", "%",
]

def is_processing_visible(page: Page) -> bool:
    """Retourne True si on voit des pourcentages / messages de traitement."""
    try:
        # pourcentages typiques
        texts = page.locator("text=%").all_text_contents()
        if any(re.search(r"\d+\s*%", t) for t in texts):
            return True
    except Exception:
        pass

    try:
        # libellés fréquents
        txts = " ".join(page.locator("body").all_text_contents()[:3]).lower()
        if any(m in txts for m in PROCESS_TEXT_MARKERS):
            return True
    except Exception:
        pass

    # barres progress possibles
    try:
        bars = page.locator("progress,[role='progressbar']").count()
        if bars and bars > 0:
            return True
    except Exception:
        pass
    return False

def dismiss_small_blockers(page: Page):
    """Ferme quelques popins fréquentes qui bloquent la zone bouton."""
    candidates = [
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Compris')",
        "button:has-text('Close')",
        "button[aria-label='Close']",
        "[role='dialog'] button:has-text('OK')",
    ]
    for sel in candidates:
        try:
            b = page.locator(sel).first
            if b.count():
                b.click(timeout=800)
        except Exception:
            continue

def publish_button_locator(page: Page):
    sels = [
        "[data-e2e='publish-button']",
        "button:has-text('Publier')",
        "button:has-text('Post')",
        'button[type="submit"]',
    ]
    for sel in sels:
        loc = page.locator(sel).first
        if loc.count():
            return loc
    return page.locator("button:has-text('Publier')").first  # fallback

def is_btn_enabled(btn) -> bool:
    try:
        if btn.count() == 0: return False
        # attributs courants
        if btn.get_attribute("disabled") is not None:
            return False
        if (btn.get_attribute("aria-disabled") or "").lower() == "true":
            return False
        cls = (btn.get_attribute("class") or "").lower()
        if "disabled" in cls or "is-disabled" in cls:
            return False
        # sinon on considère cliquable
        return True
    except Exception:
        return False

def wait_processing_then_enable_publish(page: Page, max_wait_sec: int = 360):
    """Attend la fin du traitement et l’activation du bouton."""
    t0 = time.time()
    btn = publish_button_locator(page)

    # Boucle : tant que traitement visible OU bouton pas OK, on attend
    while time.time() - t0 < max_wait_sec:
        dismiss_small_blockers(page)
        try:
            btn.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass

        if not is_processing_visible(page) and is_btn_enabled(btn):
            return True

        time.sleep(2)

    return False

def click_publish(page: Page, max_wait_sec: int = 180) -> bool:
    btn = publish_button_locator(page)
    try:
        btn.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass

    # attente finale que le bouton soit actif
    t0 = time.time()
    while time.time() - t0 < max_wait_sec:
        dismiss_small_blockers(page)
        if is_btn_enabled(btn):
            try:
                btn.click(timeout=1500)
                log("[INFO] Clic 'Publier' ✅")
                return True
            except Exception:
                # essai force
                try:
                    page.evaluate("(b)=>b.click()", btn.element_handle())
                    log("[INFO] Clic forcé 'Publier' ✅")
                    return True
                except Exception:
                    pass
        time.sleep(1.5)

    log("[WARN] Bouton 'Publier' non cliquable dans les temps.")
    return False

# --------------------------
# Légende
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
                loc.type(caption, delay=12)
                log("[INFO] Légende insérée.")
                return True
            except Exception:
                continue
    log("[WARN] Champ légende introuvable.")
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

    if TIKTOK_COOKIE:
        page.reload(wait_until="domcontentloaded")
        try: page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception: pass

    video_abs = str(Path(VIDEO_PATH).resolve())
    if not Path(video_abs).exists():
        log(f"[ERREUR] Fichier vidéo introuvable: {video_abs}")
        browser.close()
        return False

    if not attach_video_via_filechooser(page, video_abs):
        log("[ERREUR] Impossible de localiser/déclencher l’upload (file chooser).")
        browser.close()
        return False

    # Laisser l’ingest démarrer
    time.sleep(4)

    # Légende (peut rester grisée tant que traitement en cours: ok)
    fill_caption(page, CAPTION_TEXT)

    # Attendre la fin du traitement ET l’activation du bouton
    if not wait_processing_then_enable_publish(page, max_wait_sec=360):
        log("[WARN] Traitement trop long ou bouton toujours inactif.")
        browser.close()
        return False

    if DRY_RUN:
        log("[INFO] DRY_RUN=TRUE -> arrêt avant publication.")
        browser.close()
        return True

    ok = click_publish(page, max_wait_sec=120)
    time.sleep(2)
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
