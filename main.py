# main.py
import os, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VIDEO_PATH   = os.getenv("VIDEO_PATH", "assets/test.mp4")
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
    out = []
    for p in cookie_str.split(";"):
        p = p.strip()
        if not p or "=" not in p: 
            continue
        k, v = p.split("=", 1)
        k = k.strip(); v = v.strip()
        if k and v:
            out.append({
                "name": k, "value": v,
                "domain": ".tiktok.com", "path": "/",
                "httpOnly": False, "secure": True
            })
    return out

def inject_cookies(context, raw):
    cookies = parse_cookie_string(raw)
    if not cookies:
        raise RuntimeError("Impossible de parser TIKTOK_COOKIE (aucun cookie valide).")
    p = context.new_page()
    p.goto("https://www.tiktok.com", wait_until="domcontentloaded", timeout=60000)
    context.add_cookies(cookies)
    p.close()
    info(f"Injection cookies ({len(cookies)})…")

def goto_studio(page):
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90000)
    info("Navigation vers TikTok Studio Upload")
    # L’app est une SPA : laisse le temps aux modules de charger
    page.wait_for_timeout(1500)

def find_file_input_anywhere(page):
    sels = [
        'input[type="file"][accept*="video"]',
        'input[type="file"][accept*="mp4"]',
        'input[type="file"]',
    ]
    # page principale
    for s in sels:
        loc = page.locator(s)
        if loc.count():
            return page, loc.first
    # frames éventuelles
    for fr in page.frames:
        try:
            for s in sels:
                loc = fr.locator(s)
                if loc.count():
                    return fr, loc.first
        except Exception:
            pass
    return None, None

def force_visible_and_upload(container, input_loc, video_path):
    input_loc.wait_for(state="attached", timeout=60000)
    input_loc.evaluate("""
        (el) => {
          el.removeAttribute('hidden');
          el.style.display    = 'block';
          el.style.visibility = 'visible';
          el.style.opacity    = '1';
          el.style.position   = 'fixed';
          el.style.left       = '0';
          el.style.top        = '0';
          el.style.width      = '1px';
          el.style.height     = '1px';
          el.disabled         = false;
        }
    """)
    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Fichier vidéo introuvable: {p}")
    input_loc.set_input_files(str(p))
    info("Upload déclenché ✅")

def fill_caption(page, caption):
    candidates = [
        'textarea[placeholder*="description" i]',
        'textarea[aria-label*="description" i]',
        'textarea[placeholder*="titre" i]',
        'textarea',
    ]
    for s in candidates:
        loc = page.locator(s)
        try:
            loc.wait_for(state="visible", timeout=3000)
            loc.first.fill(caption)
            info("Légende insérée.")
            return
        except Exception:
            continue

def tick_compliance(page):
    labels = ["J'atteste", "J’accepte", "Conformité", "droits", "respecte"]
    for t in labels:
        try:
            loc = page.get_by_role("checkbox", name=t, exact=False)
            if loc.count():
                cb = loc.first
                if not cb.is_checked():
                    cb.check(timeout=1000)
        except Exception:
            pass

def _candidate_publish_locators(scope):
    # Multiples stratégies: nom i18n, data attrs, texte, rôle
    texts = ["Publier", "Publish", "Post", "Poster"]
    csss  = [
        '[data-e2e*="publish"]',
        '[data-e2e*="post"]',
        '[data-testid*="publish"]',
        'button:has-text("Publier")',
        'button:has-text("Publish")',
        'button:has-text("Post")',
        'div[role="button"]:has-text("Publier")',
        'div[role="button"]:has-text("Publish")',
        'div[role="button"]:has-text("Post")',
    ]
    for t in texts:
        yield scope.get_by_role("button", name=t, exact=False)
    for sel in csss:
        yield scope.locator(sel)

def find_publish_anywhere(page):
    # Page principale d'abord
    for loc in _candidate_publish_locators(page):
        try:
            if loc.count():
                return page, loc.first
        except Exception:
            pass
    # Puis toutes les frames
    for fr in page.frames:
        for loc in _candidate_publish_locators(fr):
            try:
                if loc.count():
                    return fr, loc.first
            except Exception:
                pass
    return None, None

def wake_and_scroll(page):
    # Certaines UIs ne “réveillent” le footer qu’après scroll/interactions
    page.mouse.wheel(0, 2000)
    page.wait_for_timeout(500)
    page.keyboard.press("End")
    page.wait_for_timeout(500)
    # petit click inoffensif
    try:
        page.mouse.click(10, 10)
    except Exception:
        pass

def try_publish(page):
    deadline = time.time() + 240  # 4 minutes max pour encodage + activation
    last_seen = None

    while time.time() < deadline:
        wake_and_scroll(page)

        owner, btn = find_publish_anywhere(page)
        if not btn:
            # pas encore visible → laisse charger puis essaye un léger reload SPA
            warn("Bouton 'Publier' introuvable pour l’instant…")
            page.wait_for_timeout(2000)
            continue

        # mémorise pour debug
        try:
            last_seen = btn.text_content()
        except Exception:
            last_seen = "?"

        # essaie de l’activer
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
            aria_dis = btn.get_attribute("aria-disabled")
            dis_attr = btn.get_attribute("disabled")
            is_disabled = (aria_dis == "true") or (dis_attr is not None)

            if not is_disabled:
                if not DRY_RUN:
                    btn.click(timeout=3000)
                info("Clic sur 'Publier' ✅")
                return True
        except Exception:
            pass

        # Attends le traitement/validation côté TikTok
        page.wait_for_timeout(3000)

    warn(f"Bouton 'Publier' toujours inactif / non cliquable (dernier libellé: {last_seen}).")
    return False

def main():
    info(f"Compte ciblé: {os.getenv('ACCOUNT','?')} | Posts: {os.getenv('POSTS_TO_PUBLISH','1')} | DRY_RUN={DRY_RUN}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx_kwargs = {"locale":"fr-FR", "timezone_id":"Europe/Paris"}
        if UA_RAW:
            ctx_kwargs["user_agent"] = UA_RAW
        context = browser.new_context(**ctx_kwargs)

        try:
            inject_cookies(context, COOKIE_RAW)
        except Exception as e:
            err(str(e)); context.close(); browser.close(); raise SystemExit(1)

        page = context.new_page()
        page.set_default_timeout(60000)

        try:
            goto_studio(page)

            # Recherche de l’input sur page/frames, avec patience
            found = None
            deadline = time.time() + 120
            while time.time() < deadline and not found:
                owner, inp = find_file_input_anywhere(page)
                if inp:
                    found = (owner, inp)
                    break
                page.wait_for_timeout(1000)

            if not found:
                raise RuntimeError("Impossible de localiser un champ fichier sur la page/frames.")

            owner, inp = found
            force_visible_and_upload(owner, inp, VIDEO_PATH)

            fill_caption(page, CAPTION_TEXT)
            tick_compliance(page)

            ok = try_publish(page)
            if not ok and not DRY_RUN:
                raise SystemExit(1)

        except Exception as e:
            err(str(e)); raise SystemExit(1)
        finally:
            page.wait_for_timeout(1200)
            context.close()
            browser.close()

        info("Run terminé ✅")

if __name__ == "__main__":
    main()
