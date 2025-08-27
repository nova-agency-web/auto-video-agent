# main.py
# --- Auto Video Agent: TikTok uploader (Playwright) ---
# - Lit la config depuis les variables d'environnement (GitHub Actions inputs + Secrets)
# - Ouvre TikTok Studio Upload, envoie la vidéo, ajoute la légende, publie (ou simule si DRY_RUN=TRUE)
#
# ENV attendues:
#   ACCOUNT           -> ex: "trucs→malins" (info de log uniquement)
#   POSTS_TO_PUBLISH  -> nombre de posts à tenter (int; par défaut "1")
#   DRY_RUN           -> "TRUE" (simulation) ou "FALSE" (publication réelle)
#   TIKTOK_COOKIE     -> cookie brut "k1=v1; k2=v2; ...", copié depuis l’onglet "Application > Cookies"
#   TIKTOK_UA         -> (optionnel) User-Agent à utiliser; sinon Chrome 128 par défaut
#   VIDEO_PATH        -> (optionnel) chemin d'une vidéo; sinon "assets/test.mp4"
#
# Dépendances: playwright==1.46.0, pandas (facultatif), python-dateutil (déjà dans requirements.txt)
# -------------------------------------------------------

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ----------------- Utilitaires -----------------

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def getenv_bool(key: str, default_false: bool = True) -> bool:
    raw = os.getenv(key, "")
    if not raw:
        return default_false is False and False or False if default_false else False
    return raw.strip().lower() in ("1", "true", "vrai", "yes", "y")

def parse_cookie_string(cookie_raw: str):
    """
    Transforme "k1=v1; k2=v2; ..." en liste de dicts cookies pour Playwright.
    - On ignore les paires vides ou invalides.
    - Domain fixé sur ".tiktok.com" (et on duplique pour "tiktok.com").
    """
    cookies = []
    if not cookie_raw:
        return cookies

    parts = [p.strip() for p in cookie_raw.split(";")]
    pairs = []
    for p in parts:
        if not p or "=" not in p:
            continue
        name, value = p.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or value is None:
            continue
        # filtres: Playwright n'accepte pas des noms/valeurs vides ou avec ';'
        if ";" in name or name.startswith("$"):
            continue
        pairs.append((name, value))

    # Domain de base
    base_domains = [".tiktok.com", "tiktok.com"]

    for name, value in pairs:
        for dom in base_domains:
            cookies.append({
                "name": name,
                "value": value,
                "domain": dom,
                "path": "/",
                # laisser Playwright injecter HttpOnly/Secure si nécessaire
            })

    return cookies

def require_file(path_str: str) -> Path:
    p = Path(path_str)
    if not p.exists():
        raise FileNotFoundError(
            f"Aucune vidéo trouvée. Placez un MP4 dans {path_str} "
            f"ou définissez VIDEO_PATH vers un fichier existant."
        )
    return p

def find_in_any_frame(page, selector: str, timeout_ms: int = 30000):
    """
    Cherche un sélecteur dans la page et toutes ses iframes.
    Retourne l'élément Playwright si trouvé, sinon lève PWTimeoutError.
    """
    # Essai direct sur la page
    try:
        el = page.wait_for_selector(selector, timeout=1000, state="visible")
        return el
    except PWTimeoutError:
        pass

    # Parcourt les frames
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                el = frame.wait_for_selector(selector, timeout=800, state="visible")
                return el
            except PWTimeoutError as e:
                last_err = e
                continue
        time.sleep(0.2)
    raise PWTimeoutError(str(last_err) if last_err else f"Selector not found: {selector}")

def set_files_in_any_frame(page, selector: str, file_path: Path, timeout_ms: int = 30000):
    """
    Fait set_input_files sur input[type=file] dans n'importe quel frame.
    """
    # Essaye quelques variantes courantes d'input d'upload TikTok
    candidates = [
        selector,
        "input[type='file']",
        "[data-e2e='pc-upload-btn'] input[type='file']",
        "[data-e2e='upload-btn'] input[type='file']",
        "div input[type='file']",
    ]
    deadline = time.time() + (timeout_ms / 1000.0)

    while time.time() < deadline:
        for frame in page.frames:
            for s in candidates:
                try:
                    el = frame.query_selector(s)
                    if el:
                        el.set_input_files(str(file_path))
                        return True
                except Exception:
                    continue
        time.sleep(0.25)
    raise PWTimeoutError("Timeout while trying to find file input to upload the video.")

# ----------------- Publication -----------------

def publish_to_tiktok(cookie_raw: str, caption: str, video_path: Path, user_agent: str, dry_run: bool) -> bool:
    """
    Ouvre la page d’upload et publie. Retourne True si success.
    """
    upload_url = "https://www.tiktok.com/tiktokstudio/upload"
    cookies = parse_cookie_string(cookie_raw)

    if not cookies:
        log("ERREUR: Cookie TikTok vide/mal formé (TIKTOK_COOKIE).")
        return False

    log(f"Ouverture Playwright (Chromium)…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="fr-FR",
            user_agent=user_agent,
            viewport={"width": 1440, "height": 900},
        )

        # Ajout des cookies AVANT de naviguer
        log(f"Injection cookies ({len(cookies)} entrées)…")
        try:
            context.add_cookies(cookies)
        except Exception as e:
            log(f"ERREUR: add_cookies -> {e}")
            browser.close()
            return False

        page = context.new_page()

        log(f"Navigation: {upload_url}")
        page.goto(upload_url, wait_until="domcontentloaded", timeout=60000)

        # Attendre l'UI d'upload prête (zone upload visible)
        # On essaie plusieurs ancres connues
        anchors = [
            "text=Sélectionne une vidéo à importer",
            "text=Select a video to upload",
            "[data-e2e='upload-card']",
            "[data-e2e='upload-drop-area']",
        ]
        found_anchor = False
        for sel in anchors:
            try:
                page.wait_for_selector(sel, timeout=15000, state="visible")
                found_anchor = True
                break
            except PWTimeoutError:
                continue

        if not found_anchor:
            log("Avertissement: ancre d'upload non détectée, on continue quand même…")

        log(f"Upload vidéo: {video_path}")
        try:
            set_files_in_any_frame(page, "input[type='file']", video_path, timeout_ms=60000)
        except PWTimeoutError as e:
            log(f"ERREUR: set_input_files (upload) -> {e}")
            context.close()
            browser.close()
            return False

        # Attendre que l'upload soit reconnu (barre/progression/miniature)
        # On guette plusieurs indicateurs possibles:
        progress_markers = [
            "[data-e2e='video-done-icon']",
            "text=Traitement terminé",
            "text=Processing complete",
            "text=Succès",
            "[data-e2e='cover-editor']",
        ]
        uploaded = False
        t0 = time.time()
        while time.time() - t0 < 120:
            for frame in page.frames:
                for sel in progress_markers:
                    try:
                        el = frame.query_selector(sel)
                        if el and el.is_visible():
                            uploaded = True
                            break
                    except Exception:
                        continue
                if uploaded:
                    break
            if uploaded:
                break
            time.sleep(1)

        if not uploaded:
            log("Avertissement: aucun indicateur ‘upload terminé’ détecté (on continue).")

        # Légende (textarea). On teste plusieurs sélecteurs.
        caption_selectors = [
            "textarea[placeholder*='description']",
            "textarea[placeholder*='Description']",
            "textarea[data-e2e='caption']",
            "textarea",
        ]
        caption_set = False
        for sel in caption_selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=15000)
                el.fill(caption)
                caption_set = True
                log("Légende insérée.")
                break
            except PWTimeoutError:
                continue
            except Exception:
                continue

        if not caption_set:
            log("Avertissement: impossible de remplir la légende (textarea non trouvé).")

        # Bouton ‘Publier’ (ou ‘Poster’)
        publish_selectors = [
            "button:has-text('Publier')",
            "button:has-text('Post')",
            "[data-e2e='post-button']",
        ]

        # Si DRY_RUN, on s’arrête ici avant clic
        if dry_run:
            log("DRY_RUN=TRUE → simulation OK ✅ (aucun clic sur Publier).")
            context.close()
            browser.close()
            return True

        # Attendre que le bouton soit activable, puis cliquer
        clicked = False
        for sel in publish_selectors:
            try:
                btn = find_in_any_frame(page, sel, timeout_ms=60000)
                # si disabled, on attend un peu et on re-vérifie
                t1 = time.time()
                while time.time() - t1 < 30:
                    try:
                        disabled = btn.get_attribute("disabled")
                        if not disabled:
                            btn.click()
                            clicked = True
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                if clicked:
                    break
            except PWTimeoutError:
                continue

        if not clicked:
            log("ERREUR: Bouton 'Publier' non cliquable/détecté.")
            context.close()
            browser.close()
            return False

        log("Clic sur Publier…")

        # Attendre confirmation/retour visuel de succès
        success_markers = [
            "text=Publication programmée",
            "text=Publication réussie",
            "text=Posted",
            "text=Votre vidéo a été publiée",
        ]
        success = False
        t2 = time.time()
        while time.time() - t2 < 90:
            for frame in page.frames:
                for sel in success_markers:
                    try:
                        el = frame.query_selector(sel)
                        if el and el.is_visible():
                            success = True
                            break
                    except Exception:
                        continue
                if success:
                    break
            if success:
                break
            time.sleep(1)

        if success:
            log("Publication confirmée ✅")
        else:
            log("Avertissement: pas d’indicateur de succès détecté (peut quand même être posté).")

        context.close()
        browser.close()
        return True

# ----------------- Entrée principale -----------------

def main():
    account = os.getenv("ACCOUNT", "").strip() or "compte-inconnu"
    posts_to_publish = int(os.getenv("POSTS_TO_PUBLISH", "1").strip() or "1")
    dry_run = os.getenv("DRY_RUN", "TRUE").strip().upper() in ("1", "TRUE", "VRAI", "YES", "Y")
    cookie_raw = os.getenv("TIKTOK_COOKIE", "").strip()
    user_agent = (os.getenv("TIKTOK_UA", "").strip()
                  or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    video_path_env = os.getenv("VIDEO_PATH", "").strip()
    video_path = Path(video_path_env) if video_path_env else Path("assets/test.mp4")

    log(f"Compte ciblé: {account} | Posts: {posts_to_publish} | DRY_RUN={dry_run}")
    video_path = require_file(video_path)

    # Exemple simple: un seul post “test” (tu peux remplacer par lecture de CSV si besoin)
    for i in range(posts_to_publish):
        log(f"—— Post {i+1}/{posts_to_publish} ——")
        title = "Test upload vidéo automatique"
        caption = title  # tu peux enrichir avec tags/CTA
        try:
            ok = publish_to_tiktok(cookie_raw, caption, video_path, user_agent, dry_run)
            if not ok:
                log("ERREUR: publication échouée.")
                sys.exit(1)
        except FileNotFoundError as e:
            log(f"ERREUR: {e}")
            sys.exit(1)
        except Exception as e:
            log(f"ERREUR inattendue: {e}")
            sys.exit(1)

    log("Run terminé ✅")

if __name__ == "__main__":
    main()
