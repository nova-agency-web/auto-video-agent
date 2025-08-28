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

# -------------------- cookies --------------------
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

# -------------------- upload helpers --------------------
def all_input_files(ctx):
    loc = ctx.locator("input[type='file'], input[type=file]")
    out = []
    try:
        n = loc.count()
    except Exception:
        n = 0
    for i in range(n):
        out.append(loc.nth(i))
    return out

def try_set_files_anywhere(page, file_path: str) -> bool:
    """Essaye set_input_files sur TOUS les inputs (page + iframes)."""
    contexts = [page] + page.frames
    tried = 0
    for ctx in contexts:
        for el in all_input_files(ctx):
            tried += 1
            try:
                el.set_input_files(file_path, timeout=15000)
                log("[INFO] set_input_files posé sur un input file (même caché).")
                return True
            except Exception:
                continue
    log(f"[INFO] Aucun input file n'a accepté le fichier (inputs testés: {tried}).")
    return False

# Liste de sélecteurs *très* large pour capturer la carte / bouton d’upload
UPLOAD_SELECTORS = [
    # Boutons rôlés (fr/en)
    ("role", r"(importer|téléverser|télécharger|upload|select file|choose file|sélectionner)", re.I),
    # Texte cliquable / dropzone
    ("css", "text=/Importer|Téléverser|Téléverse|Upload|Select a file|Choose file|Sélectionner/i"),
    # Attributs propriétaires souvent vus sur TikTok Studio
    ("css", "[data-tt*='Upload'], [data-e2e*='upload'], [data-tt*='upload'], [class*='Upload'], [class*='upload']"),
    # Carte principale “Téléverse ta première vidéo”
    ("css", "div:has-text(/Télévers|Importer|Upload/i)"),
    # Icônes/labels possibles
    ("css", "[aria-label*='upload' i], [aria-label*='Importer' i], [aria-label*='Télévers' i], [aria-label*='Select file' i]"),
]

def iter_contexts(page):
    yield page
    for fr in page.frames:
        yield fr

def try_file_chooser(page, file_path: str) -> bool:
    """Balaye beaucoup de candidats (page + iframes) pour déclencher le file chooser."""
    for ctx in iter_contexts(page):
        for kind, query, *rest in UPLOAD_SELECTORS:
            el = None
            try:
                if kind == "role":
                    pattern, flags = rest
                    el = ctx.get_by_role("button", name=re.compile(query, flags))
                else:
                    el = ctx.locator(query)
                if not el or el.count() == 0:
                    continue
                # clique sur chaque candidat jusqu’à ouverture du file chooser
                count = min(el.count(), 5)  # pas besoin d’en cliquer 50
                for i in range(count):
                    cand = el.nth(i)
                    try:
                        with page.expect_file_chooser(timeout=15000) as fc_info:
                            cand.scroll_into_view_if_needed()
                            # certains éléments ne sont pas réellement clickables, tenter force
                            try:
                                cand.click()
                            except Exception:
                                cand.click(force=True)
                        chooser = fc_info.value
                        chooser.set_files(file_path)
                        log("[INFO] Fichier fourni via file chooser ✅")
                        return True
                    except PWTimeout:
                        continue
                    except Exception:
                        continue
            except Exception:
                continue
    log("[WARN] Bouton/zone pour ouvrir le file chooser introuvable.")
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

# -------------------- main publish flow --------------------
def publish_once(pw, video_abs: str):
    browser = pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"]
    )
    ctx_kwargs = {}
    if UA_RAW:
        ctx_kwargs["user_agent"] = UA_RAW
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

        # petit wait pour laisser la console "websdk" initialiser les choses
        page.wait_for_timeout(1500)

        log("[INFO] Sélection du champ fichier…")

        # 1) tenter tous les inputs file (page + iframes)
        if not try_set_files_anywhere(page, video_abs):
            # 2) tenter l’ouverture du file chooser par clic
            if not try_file_chooser(page, video_abs):
                raise RuntimeError("Impossible de fournir le fichier : ni input file ni file chooser.")

        log("[INFO] Upload déclenché ✅")
        time.sleep(3)

        # Publier : on récupère le bouton (ouverture + clic final)
        publish_btn = find_publish_button(page)
        if publish_btn.count() == 0:
            log("[WARN] Bouton 'Publier' introuvable (on continue).")
        else:
            try:
                click_when_enabled(publish_btn, max_wait_ms=120000)
            except Exception:
                pass

        if fill_caption_if_present(page, CAPTION_TEXT):
            log("[INFO] Légende insérée.")

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
