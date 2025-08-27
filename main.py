# main.py
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VIDEO_PATH = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION", "Post auto vidéo automatique")
COOKIE_RAW   = os.getenv("TIKTOK_COOKIE", "")
UA_RAW       = os.getenv("TIKTOK_UA", "").strip()
DRY_RUN      = os.getenv("DRY_RUN", "FALSE").upper() == "TRUE"

STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"

def info(m):  print(f"[INFO] {m}")
def warn(m):  print(f"[WARN] {m}")
def err(m):   print(f"[ERREUR] {m}")

def parse_cookie_string(cookie_str: str):
    cookie_str = (cookie_str or "").strip()
    if not cookie_str:
        return []
    pairs = [p for p in cookie_str.split(";") if p.strip()]
    cookies = []
    for p in pairs:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip(); v = v.strip()
        if not k or not v: 
            continue
        cookies.append({
            "name": k,
            "value": v,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
        })
    return cookies

def inject_cookies(context, cookie_raw: str):
    cookies = parse_cookie_string(cookie_raw)
    if not cookies:
        raise RuntimeError("Impossible de parser TIKTOK_COOKIE (aucun cookie valide).")
    # Un premier hit sur le domaine est requis avant add_cookies
    p = context.new_page()
    p.goto("https://www.tiktok.com", wait_until="domcontentloaded", timeout=60000)
    context.add_cookies(cookies)
    p.close()
    info(f"Injection cookies ({len(cookies)})…")

def goto_studio(page):
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90000)
    info("Navigation vers TikTok Studio Upload")
    # L’app est SPA -> on attend qu’un composant d’upload apparaisse
    # On attend un conteneur générique de l’uploader (div qui contient un input file)
    try:
        page.wait_for_selector('input[type="file"]', timeout=30000)
    except PWTimeout:
        # tente un reload une fois
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

def find_file_input_anywhere(page):
    """
    Cherche un <input type="file"> sur la page ET dans toutes les iframes.
    Renvoie (frame_or_page, locator) ou (None, None) si introuvable.
    """
    selectors = [
        'input[type="file"][accept*="video"]',
        'input[type="file"][accept*="mp4"]',
        'input[type="file"]'
    ]

    # 1) Essai sur la page principale
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            return page, loc.first

    # 2) Essai dans toutes les frames
    for frame in page.frames:
        try:
            for sel in selectors:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    return frame, loc.first
        except Exception:
            continue

    return None, None

def force_visible_and_upload(container, file_input, video_path):
    # s’assure que l’élément existe dans le DOM
    file_input.wait_for(state="attached", timeout=60000)

    # le rendre visible (certains inputs sont hidden)
    file_input.evaluate("""
        (el) => {
          el.removeAttribute('hidden');
          el.style.display   = 'block';
          el.style.visibility= 'visible';
          el.style.opacity   = '1';
          el.style.position  = 'fixed';
          el.style.left      = '0';
          el.style.top       = '0';
          el.style.width     = '1px';
          el.style.height    = '1px';
          el.disabled        = false;
        }
    """)

    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Fichier vidéo introuvable: {p}")

    file_input.set_input_files(str(p))
    info("Upload déclenché ✅")

def fill_caption_and_scroll(page, caption: str):
    # essaie quelques variantes usuelles du textarea
    selectors = [
        'textarea[placeholder*="description" i]',
        'textarea[aria-label*="description" i]',
        'textarea'
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            loc.wait_for(state="visible", timeout=3000)
            loc.first.fill(caption)
            info("Légende insérée.")
            break
        except PWTimeout:
            continue
        except Exception:
            break

    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")

def tick_compliance(page):
    labels = [
        "J'atteste", "J’accepte", "Conformité", "droits", "respecte"
    ]
    for txt in labels:
        try:
            cb = page.get_by_role("checkbox", name=txt, exact=False)
            if cb.count():
                f = cb.first
                if not f.is_checked():
                    f.check(timeout=1500)
        except Exception:
            pass

def try_publish(page):
    names = ["Publier", "Post", "Publish"]
    btn = None
    for n in names:
        loc = page.get_by_role("button", name=n, exact=False)
        if loc.count():
            btn = loc.first
            break
    if not btn:
        warn("Bouton 'Publier' introuvable.")
        return False

    deadline = time.time() + 180  # jusqu’à 3 min
    while time.time() < deadline:
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
            aria_dis = btn.get_attribute("aria-disabled")
            dis_attr = btn.get_attribute("disabled")
            if aria_dis in (None, "false") and dis_attr is None:
                if not DRY_RUN:
                    btn.click(timeout=3000)
                info("Clic sur 'Publier' ✅")
                return True
        except Exception:
            pass
        page.wait_for_timeout(2000)

    warn("Traitement trop long ou bouton toujours inactif.")
    return False

def main():
    info(f"Compte ciblé: {os.getenv('ACCOUNT','?')} | Posts: {os.getenv('POSTS_TO_PUBLISH','1')} | DRY_RUN={DRY_RUN}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx_kwargs = {"locale":"fr-FR", "timezone_id":"Europe/Paris"}
        if UA_RAW:
            ctx_kwargs["user_agent"] = UA_RAW
        context = p.chromium.launch().new_context() if False else browser.new_context(**ctx_kwargs)

        try:
            inject_cookies(context, COOKIE_RAW)
        except Exception as e:
            err(str(e))
            context.close(); browser.close()
            raise SystemExit(1)

        page = context.new_page()
        page.set_default_timeout(60000)

        try:
            goto_studio(page)

            # boucle qui cherche l’input sur page/frames, avec un reload si nécessaire
            deadline = time.time() + 120
            found = None
            while time.time() < deadline and not found:
                container, input_loc = find_file_input_anywhere(page)
                if input_loc:
                    found = (container, input_loc)
                    break
                # parfois l’app recompose le DOM -> petit wait puis reload léger
                page.wait_for_timeout(1000)
                page.reload(wait_until="domcontentloaded")

            if not found:
                raise RuntimeError("Impossible de localiser un champ fichier sur la page/frames.")

            container, file_input = found
            force_visible_and_upload(container, file_input, VIDEO_PATH)
            fill_caption_and_scroll(page, CAPTION_TEXT)
            tick_compliance(page)
            ok = try_publish(page)
            if not ok and not DRY_RUN:
                raise SystemExit(1)

        except Exception as e:
            err(str(e))
            raise SystemExit(1)
        finally:
            page.wait_for_timeout(1500)
            context.close()
            browser.close()
        info("Run terminé ✅")

if __name__ == "__main__":
    main()
