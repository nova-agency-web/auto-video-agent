# main.py
import os, time
from pathlib import Path
from playwright.sync_api import sync_playwright

VIDEO_PATH   = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION", "Post auto vidéo automatique")
COOKIE_RAW   = os.getenv("TIKTOK_COOKIE", "")
UA_RAW       = os.getenv("TIKTOK_UA", "").strip()
DRY_RUN      = os.getenv("DRY_RUN", "FALSE").upper() == "TRUE"

STUDIO_URL   = "https://www.tiktok.com/tiktokstudio/upload"

def info(m):  print(f"[INFO] {m}")
def warn(m):  print(f"[WARN] {m}")
def err(m):   print(f"[ERREUR] {m}")

# ---------- Cookies ----------
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

# ---------- Navigation ----------
def goto_studio(page):
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90000)
    info("Navigation vers TikTok Studio Upload")
    page.wait_for_timeout(1500)

# ---------- Upload ----------
def find_file_input_anywhere(page):
    sels = [
        'input[type="file"][accept*="video"]',
        'input[type="file"][accept*="mp4"]',
        'input[type="file"]',
    ]
    # page
    for s in sels:
        loc = page.locator(s)
        if loc.count():
            return page, loc.first
    # frames
    for fr in page.frames:
        for s in sels:
            loc = fr.locator(s)
            if loc.count():
                return fr, loc.first
    return None, None

def force_visible_and_upload(owner, input_loc, video_path):
    input_loc.wait_for(state="attached", timeout=60000)
    input_loc.evaluate("""
        (el) => {
          el.removeAttribute('hidden');
          el.style.display='block';
          el.style.visibility='visible';
          el.style.opacity='1';
          el.style.position='fixed';
          el.style.left='0'; el.style.top='0';
          el.style.width='1px'; el.style.height='1px';
          el.disabled=false;
        }
    """)
    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Fichier vidéo introuvable: {p}")
    input_loc.set_input_files(str(p))
    info("Upload déclenché ✅")

def fill_caption(page, caption):
    cands = [
        'textarea[placeholder*="description" i]',
        'textarea[aria-label*="description" i]',
        'textarea[placeholder*="titre" i]',
        'textarea',
    ]
    for s in cands:
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
                    cb.check(timeout=800)
        except Exception:
            pass

def ensure_public_visibility(page):
    # Assure "Tout le monde"
    try:
        vis = page.get_by_text("Tout le monde peut voir cette publication", exact=False)
        if vis.count():
            vis.first.click(timeout=800)
            # si un sélecteur s'ouvre, clique sur "Tout le monde"
            opt = page.get_by_text("Tout le monde", exact=False)
            if opt.count():
                opt.first.click(timeout=800)
    except Exception:
        pass

def nudge_form(page):
    # Ouvre/ferme Programmer la publication (toggle) pour envoyer des events
    try:
        sched = page.get_by_text("Programmer la publication", exact=False)
        if sched.count():
            sched.first.click(timeout=800)
            page.wait_for_timeout(300)
            sched.first.click(timeout=800)
    except Exception:
        pass
    # Ajoute/supprime un petit hashtag pour déclencher input/change
    try:
        ta = page.locator('textarea').first
        ta.press("End")
        ta.type(" #ok", delay=20)
        page.wait_for_timeout(200)
        ta.press("Backspace")
        ta.press("Backspace")
        ta.press("Backspace")
        ta.press("Backspace")
    except Exception:
        pass

def wake_and_scroll(page):
    page.mouse.wheel(0, 2000)
    page.keyboard.press("End")
    page.wait_for_timeout(300)
    try:
        page.mouse.click(12, 12)
    except Exception:
        pass

# ---------- Publish ----------
def _pub_candidates(scope):
    texts = ["Publier", "Publish", "Post", "Poster"]
    csss  = [
        '[data-e2e*="publish"]','[data-e2e*="post"]','[data-testid*="publish"]',
        'button:has-text("Publier")','button:has-text("Publish")','button:has-text("Post")',
        'div[role="button"]:has-text("Publier")','div[role="button"]:has-text("Publish")','div[role="button"]:has-text("Post")',
    ]
    for t in texts:  yield scope.get_by_role("button", name=t, exact=False)
    for c in csss:   yield scope.locator(c)

def find_publish_anywhere(page):
    for loc in _pub_candidates(page):
        try:
            if loc.count(): return page, loc.first
        except Exception: pass
    for fr in page.frames:
        for loc in _pub_candidates(fr):
            try:
                if loc.count(): return fr, loc.first
            except Exception: pass
    return None, None

def activate_and_click(owner, btn):
    # essaie propre
    btn.scroll_into_view_if_needed(timeout=2000)
    owner.wait_for_timeout(100)
    aria = btn.get_attribute("aria-disabled")
    dis  = btn.get_attribute("disabled")
    is_disabled = (aria == "true") or (dis is not None)
    if not is_disabled:
        btn.click(timeout=2000)
        return True
    # dernier recours : enlève le disabled et clique JS
    btn.evaluate("""(el)=>{
        el.removeAttribute('disabled');
        el.setAttribute('aria-disabled','false');
        el.style.pointerEvents='auto';
        el.classList.remove('is-disabled');
    }""")
    owner.wait_for_timeout(50)
    btn.evaluate("(el)=>el.click()")
    return True

def publishing_completed(page):
    # Heuristiques de fin (nav/ toast / disparition)
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass
    # disparaît? (bouton absent)
    own, b = find_publish_anywhere(page)
    return b is None

def try_publish(page):
    deadline = time.time() + 240  # 4 min
    first_seen = True
    while time.time() < deadline:
        wake_and_scroll(page)

        owner, btn = find_publish_anywhere(page)
        if not btn:
            if first_seen:
                warn("Bouton 'Publier' introuvable pour l’instant…")
                first_seen = False
            page.wait_for_timeout(1500)
            continue

        # “nudges” pour activer le bouton
        tick_compliance(page)
        ensure_public_visibility(page)
        nudge_form(page)

        try:
            ok = activate_and_click(owner, btn)
            if ok:
                info("Clic sur 'Publier' ✅")
                # attend un signe de complétion
                for _ in range(20):
                    if publishing_completed(page):
                        return True
                    page.wait_for_timeout(800)
        except Exception:
            pass

        page.wait_for_timeout(1500)

    warn("Bouton 'Publier' toujours inactif / non cliquable après délais.")
    return False

# ---------- Main ----------
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

            found = None
            deadline = time.time() + 120
            while time.time() < deadline and not found:
                owner, inp = find_file_input_anywhere(page)
                if inp:
                    found = (owner, inp); break
                page.wait_for_timeout(1000)
            if not found:
                raise RuntimeError("Impossible de localiser un champ fichier sur la page/frames.")

            owner, inp = found
            force_visible_and_upload(owner, inp, VIDEO_PATH)

            fill_caption(page, CAPTION_TEXT)

            if DRY_RUN:
                warn("DRY_RUN=TRUE → pas de publication.")
            else:
                ok = try_publish(page)
                if not ok:
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
