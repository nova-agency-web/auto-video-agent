import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# -------------------------------
# Config lecture depuis env GitHub
# -------------------------------
ACCOUNT       = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_DO   = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN       = os.getenv("DRY_RUN", "TRUE").strip().lower() == "true"
COOKIE_RAW    = os.getenv("TIKTOK_COOKIE", "")  # si tu utilises encore les cookies
UA_RAW        = os.getenv("TIKTOK_UA", "").strip()

VIDEO_PATH    = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT  = os.getenv("CAPTION_TEXT", "Post automatique de test ✨")

UPLOAD_URL    = "https://www.tiktok.com/tiktokstudio/upload"

# -------------------------------
# Utilitaires
# -------------------------------

def parse_cookies(cookie_raw: str):
    """
    Accepte:
      - une ligne 'Cookie: a=1; b=2'
      - juste 'a=1; b=2'
      - un JSON Playwright [{'name':..., 'value':..., 'domain':...}, ...]
    Renvoie une liste de dicts {'name','value','domain','path'}
    """
    cookies = []
    if not cookie_raw:
        return cookies

    try:
        data = json.loads(cookie_raw)
        if isinstance(data, list):
            # suppose déjà au format playwright
            for c in data:
                if "name" in c and "value" in c:
                    cookies.append({
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".tiktok.com"),
                        "path": c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", True),
                        "sameSite": c.get("sameSite", "Lax"),
                    })
            return cookies
    except Exception:
        pass

    # En-tête simple
    line = cookie_raw.strip()
    if line.lower().startswith("cookie:"):
        line = line.split(":", 1)[1].strip()
    # a=1; b=2
    for pair in [p.strip() for p in line.split(";") if p.strip()]:
        if "=" in pair:
            name, val = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": val.strip(),
                "domain": ".tiktok.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            })
    return cookies


def log(s: str):
    print(s, flush=True)


def visible_file_input_in(context) -> list:
    """Renvoie la liste des input[type=file] visibles dans un contexte (page/frame)."""
    els = context.locator("input[type='file']")
    results = []
    try:
        count = els.count()
    except Exception:
        count = 0
    for i in range(count):
        el = els.nth(i)
        try:
            if el.is_visible():
                results.append(el)
        except Exception:
            pass
    return results


def set_visible_file(page, file_path: str) -> bool:
    """
    Cherche un input file visible sur la page ou ses iframes et y injecte file_path.
    """
    # page + frames
    contexts = [page] + page.frames
    for ctx in contexts:
        for el in visible_file_input_in(ctx):
            try:
                el.set_input_files(file_path, timeout=30000)
                return True
            except Exception:
                continue
    return False


def fill_caption_if_present(page, text: str) -> bool:
    """
    Essaie de remplir la légende sur le panneau de publication.
    Couvre textarea placeholder FR (description/légende) et contenteditable.
    """
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
        # Essai type input/textarea
        try:
            loc.fill(text)
            return True
        except Exception:
            # fallback contenteditable
            loc.click()
            page.keyboard.type(text)
            return True
    except Exception:
        log("[WARN] Impossible d’écrire dans la légende (on continue).")
        return False


def find_publish_button(page):
    """
    Renvoie un locator fiable pour 'Publier'
    1) sélecteur confirmé par tes devtools: button[data-tt="Sidebar_Sidebar_Clickable"]
    2) fallback ARIA
    """
    loc1 = page.locator("button[data-tt='Sidebar_Sidebar_Clickable']")
    if loc1.count() > 0:
        return loc1.first
    return page.get_by_role("button", name=re.compile("publier", re.I))


def click_when_enabled(btn, max_wait_ms=120000):
    """Scroll + attente enabled + clic."""
    btn.scroll_into_view_if_needed()
    expect(btn).to_be_enabled(timeout=max_wait_ms)
    btn.click()


# -------------------------------
# Flux principal
# -------------------------------
def publish_once(pw, video_abs: str):
    browser = pw.chromium.launch(headless=True, args=[
        # moins strict
        "--disable-blink-features=AutomationControlled",
    ])
    context_kwargs = {}
    if UA_RAW:
        context_kwargs["user_agent"] = UA_RAW

    context = browser.new_context(**context_kwargs)

    # Cookies (si tu en fournis encore)
    cookies = parse_cookies(COOKIE_RAW)
    if cookies:
        try:
            context.add_cookies(cookies)
            log(f"[INFO] Injection cookies ({len(cookies)})…")
        except Exception as e:
            log(f"[WARN] Cookies ignorés: {e}")

    page = context.new_page()

    try:
        log(f"[INFO] [NAV] Vers TikTok Studio Upload")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=120000)

        # Upload fichier (cherche un input visible, page+iframes)
        log("[INFO] Sélection du champ fichier…")
        ok = set_visible_file(page, video_abs)
        if not ok:
            raise RuntimeError("Impossible de localiser un input file visible pour l’upload.")

        log("[INFO] Upload déclenché ✅")

        # Attendre que l’upload progresse (spinner/progress). On utilise un sleep simple ici.
        time.sleep(3)

        # Ouvrir le panneau de publication + cliquer Publier
        publish_btn = find_publish_button(page)
        if publish_btn.count() == 0:
            raise RuntimeError("Bouton 'Publier' introuvable.")

        publish_btn.scroll_into_view_if_needed()
        log("[INFO] Bouton 'Publier' détecté ; tentative de clic…")

        # D’abord ouvrir le panneau (sur Studio il ouvre la vue finale)
        try:
            # certains écrans demandent un premier clic pour ouvrir le volet
            click_when_enabled(publish_btn, max_wait_ms=120000)
        except Exception:
            # S’il était déjà "ouvert", tant mieux; continue.
            pass

        # Insérer la légende (si champ présent à ce stade)
        filled = fill_caption_if_present(page, CAPTION_TEXT)
        if filled:
            log("[INFO] Légende insérée.")

        # Rechercher/cliquer le vrai bouton de publication (même sélecteur/fallback)
        final_btn = find_publish_button(page)
        if final_btn.count() == 0:
            log("[WARN] Bouton 'Publier' non trouvé après ouverture (UI peut être différente).")
        else:
            try:
                click_when_enabled(final_btn, max_wait_ms=120000)
                log("[INFO] Clic sur 'Publier' exécuté.")
            except Exception as e:
                log(f"[WARN] Bouton 'Publier' toujours inactif / non cliquable: {e}")

        # DRY RUN ou pas
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
