# main.py
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ----------------------------
# Config lecture ENV
# ----------------------------
ACCOUNT = os.getenv("ACCOUNT", "unknown")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "TRUE").strip().upper() == "TRUE"
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "").strip()
UA_RAW = os.getenv("TIKTOK_UA", "").strip()

VIDEO_PATH = "assets/test.mp4"  # chemin par défaut attendu par le workflow
CAPTION_TEXT = "Post automatique de test — via agent vidéo 🤖"
STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"

def die(msg, code=1):
    print(f"[ERREUR]: {msg}")
    sys.exit(code)

def ensure_file_exists(path: str):
    p = Path(path)
    if not p.exists():
        die(f"Aucune vidéo trouvée. Placez un MP4 dans {VIDEO_PATH} ou renseignez la source.")
    if p.is_dir():
        die(f"Le chemin de vidéo pointe sur un dossier: {path}")
    return str(p.resolve())

def parse_cookies(cookie_raw: str):
    """
    Accepte :
      - une ligne 'name=value; name2=value2; ...'
      - ou plusieurs lignes (une par cookie), 'name=value'
    Retour : liste de dicts { 'name', 'value', 'domain', 'path', ... } minimale pour set_cookies.
    """
    if not cookie_raw:
        die("Cookie TikTok manquant. Renseigne TIKTOK_COOKIE dans les Secrets.")

    # devtools copie souvent au format 'name=value; name2=value2; ...'
    # on normalise en lignes 'name=value'
    if ";" in cookie_raw and "\n" not in cookie_raw:
        parts = [c.strip() for c in cookie_raw.split(";") if c.strip()]
    else:
        parts = [l.strip() for l in cookie_raw.splitlines() if l.strip()]

    cookies = []
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })
    if not cookies:
        die("Impossible de parser TIKTOK_COOKIE (aucun cookie valide trouvé).")
    return cookies

def go_studio_and_inject_cookies(context, page):
    print("[LOGIN] Injection cookies…")
    cookies = parse_cookies(COOKIE_RAW)
    context.add_cookies(cookies)
    # Aller sur Studio Upload
    print(f"[NAV] Vers {STUDIO_URL}")
    page.goto(STUDIO_URL, timeout=60000, wait_until="domcontentloaded")
    # Rechargement pour que les cookies s'appliquent bien
    page.reload(timeout=60000)
    # Attendre que la page Studio soit prête (zone centrale visible)
    page.wait_for_load_state("networkidle", timeout=60000)

def upload_video_via_studio(page, video_path: str):
    print("[UPLOAD] Navigation et sélection du champ vidéo…")
    # Parfois TikTok affiche directement la dropzone + bouton
    # Sélecteurs tolérants (FR/EN)
    # 1) Cliquer le bouton visible
    try:
        upload_btn = page.locator(
            "button:has-text('Sélectionner la vidéo'), "
            "button:has-text('Select video'), "
            "div:has(button:has-text('Sélectionner la vidéo')), "
            "div:has(button:has-text('Select video'))"
        ).first
        # le bouton peut ne pas exister (si l'input apparait directement)
        if upload_btn.count() > 0:
            upload_btn.click(force=True, timeout=5000)
    except Exception:
        pass  # pas bloquant si pas de bouton

    # 2) L'input file caché
    try:
        file_input = page.locator("input[type='file'][accept*='video']").first
        # attendre qu'il existe dans le DOM même s'il est caché
        file_input.wait_for(state="attached", timeout=15000)
        file_input.set_input_files(video_path)
        print(f"[UPLOAD] Upload vidéo: {video_path}")
    except PWTimeoutError:
        raise RuntimeError("Impossible de localiser le champ fichier (input[type='file'][accept*='video']).")
    except Exception as e:
        raise RuntimeError(f"Échec set_input_files: {e}")

def wait_upload_finished(page):
    """
    Surveille l'apparition/disparition d'indicateurs pour estimer la fin d'upload/processing.
    On reste tolérant (UI peut changer) : on attend que le bouton 'Publier' devienne cliquable.
    """
    print("[WAIT] Attente de la fin d'upload / préparation…")
    # attendre qu’un champ de légende soit présent
    try:
        caption_area = page.locator("textarea, [data-testid='caption'], div[role='textbox']").first
        caption_area.wait_for(state="visible", timeout=120000)
    except Exception:
        print("[WARN] Aucun indicateur clair 'upload terminé' détecté (on continue).")

def fill_caption(page, caption: str):
    print("[CAPTION] Remplissage légende…")
    # plusieurs variantes possibles
    candidates = [
        "textarea[placeholder*='Ajoute une légende']",
        "textarea[placeholder*='Add a caption']",
        "[data-testid='caption'] textarea",
        "[data-testid='caption'] div[contenteditable='true']",
        "div[role='textbox']"
    ]
    area = None
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count() > 0:
            area = loc
            break
    if not area:
        print("[WARN] Impossible de trouver la zone de légende (textarea non trouvé).")
        return
    try:
        area.click()
        # Certaines zones type contenteditable nécessitent fill() -> press pour coller
        try:
            area.fill(caption)
        except Exception:
            page.keyboard.type(caption, delay=10)
        print("[CAPTION] Légende insérée.")
    except Exception as e:
        print(f"[WARN] Échec d'insertion de légende: {e}")

def click_publish(page, dry_run: bool):
    # variantes FR/EN
    publish_candidates = [
        "button:has-text('Publier')",
        "button:has-text('Publish')",
        "[data-testid='publish-button']",
    ]
    pub = None
    for sel in publish_candidates:
        loc = page.locator(sel).first
        if loc.count() > 0:
            pub = loc
            break

    if not pub:
        raise RuntimeError("Bouton 'Publier' non cliquable/détecté.")

    if dry_run:
        print("[DRY RUN] Simulation — pas de clic sur 'Publier'.")
        return

    print("[PUBLISH] Clic sur 'Publier'…")
    try:
        pub.wait_for(state="visible", timeout=20000)
        # parfois disabled au début
        for _ in range(6):
            disabled = pub.get_attribute("disabled")
            if disabled is None:
                break
            time.sleep(2)
        pub.click(force=True)
        print("[OK] Publication demandée ✅")
    except Exception as e:
        raise RuntimeError(f"Échec clic 'Publier': {e}")

def publish_once(playwright, video_path: str):
    chromium = playwright.chromium
    launch_opts = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    context_opts = {
        "viewport": {"width": 1366, "height": 768},
    }
    if UA_RAW:
        context_opts["user_agent"] = UA_RAW

    browser = chromium.launch(**launch_opts)
    context = browser.new_context(**context_opts)
    page = context.new_page()

    try:
        go_studio_and_inject_cookies(context, page)
        upload_video_via_studio(page, video_path)
        wait_upload_finished(page)
        fill_caption(page, CAPTION_TEXT)
        click_publish(page, DRY_RUN)
        print("[DONE] Run terminé ✅")
        return True
    finally:
        context.close()
        browser.close()

def main():
    print(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={'True' if DRY_RUN else 'False'}")
    video_abs = ensure_file_exists(VIDEO_PATH)

    with sync_playwright() as pw:
        for i in range(POSTS_TO_PUBLISH):
            print(f"—— Post {i+1}/{POSTS_TO_PUBLISH} ——")
            ok = publish_once(pw, video_abs)
            if not ok:
                die("Publication échouée.")
            # Petite pause entre les posts (anti-bot), même en dry-run
            time.sleep(3)

if __name__ == "__main__":
    main()
