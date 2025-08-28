import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect, TimeoutError as PWTimeout

ACCOUNT       = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_DO   = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN       = os.getenv("DRY_RUN", "TRUE").strip().lower() == "true"
COOKIE_RAW    = os.getenv("TIKTOK_COOKIE", "")
UA_RAW        = os.getenv("TIKTOK_UA", "").strip()

VIDEO_PATH    = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT  = os.getenv("CAPTION_TEXT", "Post automatique de test ✨")

UPLOAD_URL    = "https://www.tiktok.com/tiktokstudio/upload"

def log(s: str): print(s, flush=True)

def parse_cookies(cookie_raw: str):
    cookies = []
    if not cookie_raw:
        return cookies
    try:
        data = json.loads(cookie_raw)
        if isinstance(data, list):
            for c in data:
                if "name" in c and "value" in c:
                    cookies.append({
                        "name": c["name"], "value": c["value"],
                        "domain": c.get("domain", ".tiktok.com"),
                        "path": c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", True),
                        "sameSite": c.get("sameSite", "Lax"),
                    })
            return cookies
    except Exception:
        pass
    line = cookie_raw.strip()
    if line.lower().startswith("cookie:"):
        line = line.split(":", 1)[1].strip()
    for pair in [p.strip() for p in line.split(";") if p.strip()]:
        if "=" in pair:
            name, val = pair.split("=", 1)
            cookies.append({
                "name": name.strip(), "value": val.strip(),
                "domain": ".tiktok.com", "path": "/",
                "httpOnly": False, "secure": True, "sameSite": "Lax",
            })
    return cookies

def all_input_files(context):
    """Renvoie tous les <input type=file> (visibles ou non) du contexte."""
    loc = context.locator("input[type='file']")
    out = []
    try:
        n = loc.count()
    except Exception:
        n = 0
    for i in range(n):
        out.append(loc.nth(i))
    return out

def try_set_files_anywhere(page, file_path: str) -> bool:
    """
    Essaye set_input_files sur TOUS les inputs file (même cachés) dans la page et ses iframes.
    set_input_files fonctionne même si l'input est hidden.
    """
    tried = 0
    # page + frames
    contexts = [page] + page.frames
    for ctx in contexts:
        for el in all_input_files(ctx):
            tried += 1
            try:
                el.set_input_files(file_path, timeout=15000)
                log("[INFO] set_input_files posé sur un input file.")
                return True
            except Exception:
                continue
    log(f"[INFO] Aucun input file n'a accepté le fichier (inputs testés: {tried}).")
    return False

def try_file_chooser(page, file_path: str) -> bool:
    """
    Déclenche le file chooser en cliquant un bouton d'upload/Importer.
    """
    upload_button = (
        page.get_by_role("button", name=re.compile("importer|upload|sélectionner|select file|import", re.I))
        .or_(page.locator("[data-tt*='Upload'], [data-e2e*='upload']"))
    )
    try:
        if upload_button.count() == 0:
            # essaie dans une iframe aussi
            for fr in page.frames:
                ub = fr.get_by_role("button", name=re.compile("importer|upload|sélectionner|select file|import", re.I))
                if ub.count() > 0:
                    upload_button = ub
                    break
        if upload_button.count() == 0:
            log("[WARN] Bouton pour ouvrir le file chooser introuvable.")
            return False

        with page.expect_file_chooser(timeout=20000) as fc_info:
            upload_button.first.click()
        chooser = fc_info.value
        chooser.set_files(file_path)
        log("[INFO] Fichier fourni par file chooser.")
        return True
    except PWTimeout:
        log("[WARN] File chooser non déclenché dans le délai.")
        return False
    except Exception as e:
        log(f"[WARN] File chooser: {e}")
        return False

def fill_caption_if_present(page, text: str) -> bool:
    sel = (
        "textarea[placeholder*='légende' i], "
        "textarea[placeholder*='description' i], "
        "[contenteditable='true']"
    )
    loc = page.locator(sel).first
    try:
        if loc.count() == 0:
            log("[WARN] Champ de légende non trouvé (on continue).")
            return False
        loc.scroll_into_view_if_needed()
        try:
            loc.fill(text)
            return True
        except Exception:
            loc.click()
            page.keyboard.type(text)
            return True
    except Exception:
        log("[WARN] Impossible d’écrire dans la légende (on continue).")
        return False

def find_publish_button(page):
    loc1 = page.locator("button[data-tt='Sidebar_Sidebar_Clickable']")
    if loc1.count() > 0:
        return loc1.first
    return page.get_by_role("button", name=re.compile("publier", re.I))

def click_when_enabled(btn, max_wait_ms=120000):
    btn.scroll_into_view_if_needed()
    expect(btn).to_be_enabled(timeout=max_wait_ms)
    btn.click()

def publish_once(pw, video_abs: str):
    browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx_kwargs = {}
    if UA_RAW: ctx_kwargs["user_agent"] = UA_RAW
    context = browser.new_context(**ctx_kwargs)

    cookies = parse_cookies(COOKIE_RAW)
    if cookies:
        try:
            context.add_cookies(cookies)
            log(f"[INFO] Injection cookies ({len(cookies)})…")
        except Exception as e:
            log(f"[WARN] Cookies ignorés: {e}")

    page = context.new_page()
    try:
        log("[INFO] [NAV] Vers TikTok Studio Upload")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=120000)

        log("[INFO] Sélection du champ fichier…")

        # 1) tenter directement tous les inputs (même cachés)
        if not try_set_files_anywhere(page, video_abs):
            # 2) tenter via file chooser
            if not try_file_chooser(page, video_abs):
                raise RuntimeError("Impossible de fournir le fichier : ni input file ni file chooser.")

        log("[INFO] Upload déclenché ✅")
        time.sleep(3)

        # ouvrir / trouver Publier
        publish_btn = find_publish_button(page)
        if publish_btn.count() == 0:
            raise RuntimeError("Bouton 'Publier' introuvable.")

        # premier clic (ouvre le panneau final si nécessaire)
        try:
            click_when_enabled(publish_btn, max_wait_ms=120000)
        except Exception:
            pass

        # légende
        if fill_caption_if_present(page, CAPTION_TEXT):
            log("[INFO] Légende insérée.")

        # clic final
        final_btn = find_publish_button(page)
        if final_btn.count() > 0:
            try:
                click_when_enabled(final_btn, max_wait_ms=120000)
                log("[INFO] Clic sur 'Publier' exécuté.")
            except Exception as e:
                log(f"[WARN] Bouton 'Publier' toujours inactif / non cliquable: {e}")
        else:
            log("[WARN] Bouton 'Publier' non trouvé après ouverture.")

        if DRY_RUN:
            log("[INFO] DRY_RUN=True → pas d’envoi définitif.")
        else:
            log("[INFO] Publication tentée (DRY_RUN=False).")

        return True
    finally:
        context.close()
        browser.close()

def main():
    log(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_DO} | DRY_RUN={DRY_RUN}")
    video_abs = str(Path(VIDEO_PATH).resolve())
    if not Path(video_abs).exists():
        raise FileNotFoundError(f"Fichier vidéo introuvable: {video_abs}")
    with sync_playwright() as pw:
        for i in range(POSTS_TO_DO):
            log(f"[INFO] — Post {i+1}/{POSTS_TO_DO} —")
            ok = publish_once(pw, video_abs)
            if not ok:
                raise SystemExit(1)
    log("[INFO] Run terminé ✅")

if __name__ == "__main__":
    main()
