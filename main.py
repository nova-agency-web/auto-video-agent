import os
import sys
import time
import traceback
from typing import List, Dict
from playwright.sync_api import sync_playwright

# ======================
# Utilitaires log
# ======================
def log(msg: str):
    print(f"[INFO] {msg}", flush=True)

def warn(msg: str):
    print(f"[WARN] {msg}", flush=True)

def die(msg: str):
    print(f"[ERREUR] {msg}", flush=True)
    sys.exit(1)

# ======================
# Parser cookies
# ======================
def parse_cookies(cookie_raw: str) -> List[Dict]:
    """
    Convertit l'en-tête Cookie brut en liste de cookies Playwright.
    On garde un sous-ensemble sûr. On nettoie quelques caractères parasites.
    """
    if not cookie_raw:
        die("Cookie TikTok manquant. Renseigne TIKTOK_COOKIE dans les Secrets.")

    # Une seule ligne 'name=value; name2=value2; ...' ou plusieurs lignes
    if ";" in cookie_raw and "\n" not in cookie_raw:
        items = [c.strip() for c in cookie_raw.split(";") if c.strip()]
    else:
        items = [l.strip() for l in cookie_raw.splitlines() if l.strip()]

    allow = {
        "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
        "s_v_web_id", "msToken", "odin_tt", "multi_sids",
        "passport_auth_status", "passport_auth_status_ss",
        "uid_tt", "uid_tt_ss", "ttwid"
    }

    cookies: List[Dict] = []
    for part in items:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name or not value:
            continue
        if name.lower() in ("path", "domain", "expires", "max-age", "secure", "httponly", "samesite"):
            # Ce sont des attributs Set-Cookie, pas des cookies utilisateur
            continue
        if name not in allow:
            continue
        cookies.append({"name": name, "value": value, "url": "https://www.tiktok.com"})

    if not cookies:
        die("Impossible de parser TIKTOK_COOKIE (aucun cookie autorisé/valide trouvé).")
    return cookies

# ======================
# Navigation / Upload
# ======================
def nav(msg: str):
    log(f"[NAVI] {msg}")

def go_studio_and_inject_cookies(context, page, cookie_raw: str):
    cookies = parse_cookies(cookie_raw)

    ok_count = 0
    bad: List[str] = []
    for ck in cookies:
        try:
            context.add_cookies([ck])
            ok_count += 1
        except Exception as e:
            bad.append(f"{ck.get('name')} ({type(e).__name__}: {e})")

    log(f"Injection cookies: {ok_count} acceptés / {len(cookies)} au total.")
    if bad:
        warn("Cookies ignorés (invalides pour DevTools):")
        for b in bad:
            warn(f"  - {b}")

    if ok_count == 0:
        die("Aucun cookie valide injecté -> session impossible.")

    url = "https://www.tiktok.com/tiktokstudio/upload"
    nav("Vers TikTok Studio Upload…")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

def upload_video_via_studio(page, video_path: str, caption: str):
    try:
        nav("Navigation et sélection du champ vidéo…")
        file_input = page.locator("input[type='file']")
        file_input.set_input_files(video_path)
    except Exception as e:
        die(f"Impossible de localiser le champ fichier: {e}")

    log(f"Upload vidéo: {video_path}")
    page.wait_for_timeout(4000)

    if caption:
        try:
            textarea = page.locator("textarea")
            textarea.fill(caption)
            log("Légende insérée.")
        except Exception:
            warn("Impossible d'insérer la légende.")

    try:
        btn = page.get_by_text("Publier", exact=True)
        btn.click()
        log("Clic sur 'Publier'.")
    except Exception:
        die("Bouton 'Publier' introuvable/non cliquable.")

    page.wait_for_timeout(4000)
    log("Upload terminé (à vérifier manuellement dans TikTok Studio).")

# ======================
# Main
# ======================
def publish_once(pw, video_abs: str):
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=os.getenv("TIKTOK_UA", "Mozilla/5.0"))
    page = context.new_page()

    cookie_raw = os.getenv("TIKTOK_COOKIE", "")
    caption = "Vidéo test upload auto ✅"

    go_studio_and_inject_cookies(context, page, cookie_raw)
    upload_video_via_studio(page, video_abs, caption)

    context.close()
    browser.close()
    return True

def main():
    account = os.getenv("ACCOUNT", "default")
    posts = int(os.getenv("POSTS_TO_PUBLISH", "1"))
    dry = os.getenv("DRY_RUN", "True").lower() == "true"

    log(f"Compte ciblé: {account} | Posts: {posts} | DRY_RUN={dry}")

    video_path = "assets/test.mp4"
    if not os.path.exists(video_path):
        die(f"Vidéo introuvable: {video_path}")

    video_abs = os.path.abspath(video_path)

    with sync_playwright() as pw:
        for i in range(posts):
            log(f"— Post {i+1}/{posts} —")
            if dry:
                log("Mode simulation -> pas d'upload réel.")
                continue
            ok = publish_once(pw, video_abs)
            if not ok:
                die("Publication échouée.")
            log("Publication réussie.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        die("Erreur critique.")
