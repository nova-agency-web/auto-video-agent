# main.py
# Agent vidéo automatique — TikTok Studio (upload & publication)
# Journaux en français pour suivre l'exécution dans GitHub Actions.

import os
import sys
import json
import time
from pathlib import Path
from typing import List, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# -----------------------------
# Utilitaires journalisation
# -----------------------------
def log(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def nav(msg: str) -> None:
    print(f"[NAV] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[AVERT] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[ERREUR] {msg}", flush=True)
    sys.exit(1)


# -----------------------------
# Entrées / configuration
# -----------------------------
ACCOUNT = os.getenv("ACCOUNT", "").strip()
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "FALSE").strip().upper() == "TRUE"

COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "").strip() or os.getenv("COOKIE", "").strip()
UA_RAW = os.getenv("TIKTOK_UA", "").strip()

# Vidéo locale du repo (par défaut).
VIDEO_PATH = "assets/test.mp4"
CAPTION_TEXT = "Test upload vidéo automatique"

# Timeout Playwright (ms)
SHORT = 5_000
NORMAL = 15_000
LONG = 30_000


# -----------------------------
# Cookies: parsing robuste
# -----------------------------
def parse_cookies(cookie_raw: str) -> List[Dict]:
    """
    Accepte une chaîne 'Cookie:' comme dans les en-têtes HTTP (une ligne),
    ou plusieurs lignes 'name=value'.
    - Filtre et ne garde que les cookies utiles/valides pour TikTok.
    - Utilise le champ 'url' (plus tolérant) au lieu de 'domain/path'.
    Évite ainsi l'erreur:
      BrowserContext.add_cookies: Protocol error (Storage.setCookies): Invalid cookie fields
    """
    if not cookie_raw:
        die("Cookie TikTok manquant. Renseigne TIKTOK_COOKIE dans les Secrets.")

    # Normalisation en éléments "name=value"
    if ";" in cookie_raw and "\n" not in cookie_raw:
        items = [c.strip() for c in cookie_raw.split(";") if c.strip()]
    else:
        items = [l.strip() for l in cookie_raw.splitlines() if l.strip()]

    # Sous-ensemble sûr & suffisant pour une session TikTok
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
        value = value.strip()
        if not name or not value:
            continue

        if name not in allow:
            continue

        cookies.append({
            "name": name,
            "value": value,
            "url": "https://www.tiktok.com",  # plus tolérant que domain/path
        })

    if not cookies:
        die("Impossible de parser TIKTOK_COOKIE (aucun cookie autorisé/valide trouvé).")
    return cookies


# -----------------------------
# Playwright helpers
# -----------------------------
def open_context(p, headless: bool = True):
    launch_args = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    browser = p.chromium.launch(**launch_args)

    context_args = {
        "viewport": {"width": 1440, "height": 900},
        "java_script_enabled": True,
        "user_agent": UA_RAW if UA_RAW else None,
        "locale": "fr-FR",
    }
    # Nettoie None
    context_args = {k: v for k, v in context_args.items() if v is not None}
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.set_default_timeout(NORMAL)
    return browser, context, page


def go_studio_and_inject_cookies(context, page, cookie_raw: str):
    cookies = parse_cookies(cookie_raw)
    log(f"Injection cookies ({len(cookies)} entrées)…")
    context.add_cookies(cookies)

    # Test de session : aller sur Studio de suite
    url = "https://www.tiktok.com/tiktokstudio/upload"
    nav(f"Vers TikTok Studio Upload…")
    page.goto(url, wait_until="domcontentloaded")
    # Petite attente de stabilisation
    page.wait_for_timeout(1500)
    # Si un mur de connexion apparaît, TikTok injecte parfois un redir — on force un refresh
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1200)


def set_files_resilient(page, selectors: List[str], filepath: str) -> bool:
    """
    Tente de trouver un input[type=file] et d'y attacher le fichier.
    Essaie plusieurs sélecteurs connus de TikTok Studio.
    """
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.set_input_files(filepath)
                return True
        except PWTimeout:
            continue
        except Exception:
            continue

    # Dernière cartouche: écouter un FileChooser si un input est déclenché par bouton
    try:
        with page.expect_file_chooser(timeout=SHORT) as fc:
            # tente de cliquer sur un bouton d'upload
            # plusieurs variantes possibles
            for btn_sel in [
                "button:has-text('Sélectionner une vidéo')",
                "button:has-text('Importer')",
                "button:has-text('Upload')",
                "[data-e2e='upload-card']",
            ]:
                btn = page.query_selector(btn_sel)
                if btn:
                    btn.click()
                    break
        chooser = fc.value
        chooser.set_files(filepath)
        return True
    except Exception:
        return False


def upload_video_via_studio(page, video_path: str) -> None:
    """
    Essaie plusieurs sélecteurs pour l'input fichier, puis vérifie progression.
    """
    # Vérif du fichier local
    vp = Path(video_path)
    if not vp.exists():
        die(f"Fichier vidéo introuvable: {video_path}")

    nav("Navigation et sélection du champ vidéo…")

    selectors = [
        "input[type='file']",
        "input[accept*='video']",
        "input[data-e2e='upload-file-input']",
        "#fileElem",
    ]

    # On attend qu'au moins un truc de la zone soit présent
    # (évite de courir après un input qui n'est pas encore dans le DOM)
    try:
        page.wait_for_selector("input[type='file'], input[accept*='video'], [data-e2e='upload-file-input']", state="attached", timeout=LONG)
    except PWTimeout:
        warn("Aucun indicateur 'upload terminé' détecté (on continue).")

    ok = set_files_resilient(page, selectors, str(vp.resolve()))
    if not ok:
        raise RuntimeError("Impossible de localiser le champ fichier (input[type='file']).")

    log(f"Upload vidéo: {video_path}")

    # Attendre un élément de progression/aperçu connu (non bloquant si absent)
    for maybe in [
        "[data-e2e='video-cover']",         # aperçu
        "text=Traitement",                  # message de traitement
        "[data-e2e='upload-progress']",
    ]:
        try:
            page.wait_for_selector(maybe, timeout=SHORT)
            break
        except PWTimeout:
            pass


def fill_caption(page, caption: str) -> None:
    """
    Remplissage résilient de la légende.
    """
    candidates = [
        "textarea[placeholder*='légende']",
        "textarea[placeholder*='Légende']",
        "div[contenteditable='true'][data-e2e*='caption']",
        "div[contenteditable='true']",
        "textarea",
    ]
    filled = False
    for sel in candidates:
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                el.fill("")            # vide
                el.type(caption, delay=10)
                filled = True
                break
        except Exception:
            continue

    if not filled:
        warn("Impossible de remplir la légende (textarea non trouvé).")
    else:
        log("Légende insérée.")


def click_publish(page) -> bool:
    """
    Clique sur Publier — essaie plusieurs libellés/boutons.
    """
    labels = ["Publier", "Poster", "Publish", "Mettre en ligne"]
    for lab in labels:
        try:
            btn = page.get_by_role("button", name=lab)
            if btn:
                btn.click()
                return True
        except Exception:
            pass

    # Essaie via sélecteurs data-e2e
    for sel in [
        "[data-e2e='publish-button']",
        "button[type='submit']",
        "button:has-text('Publier')",
    ]:
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                return True
        except Exception:
            pass

    warn("Bouton 'Publier' non cliquable/détecté.")
    return False


def wait_publication_confirmation(page) -> bool:
    """
    Cherche un signal fiable de publication.
    """
    signals = [
        "text=Votre vidéo est publiée",
        "text=Publication réussie",
        "[data-e2e='publish-success']",
        "text=Programmé",  # si planifié, selon paramétrage
    ]
    for sel in signals:
        try:
            page.wait_for_selector(sel, timeout=LONG)
            return True
        except PWTimeout:
            continue
    return False


# -----------------------------
# Orchestration d'un post
# -----------------------------
def publish_once(pw_context, page, video_abs: str) -> bool:
    """
    Réalise un post unique: upload, légende, publier (sauf DRY_RUN).
    """
    if DRY_RUN:
        warn("Mode simulation (DRY_RUN=True): aucune publication réelle ne sera faite.")

    # aller/studio déjà chargé (cookies injectés)
    upload_video_via_studio(page, video_abs)

    # Remplir légende
    fill_caption(page, CAPTION_TEXT)

    if DRY_RUN:
        log("Simulation: on s'arrête avant 'Publier'.")
        return True

    # Cliquer publier
    log("Recherche du bouton 'Publier'…")
    if not click_publish(page):
        warn("Clic forcé sur 'Publier' (fallback).")
        # Fallback: tente un 'Enter' sur le body (souvent submit)
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    ok = wait_publication_confirmation(page)
    if ok:
        log("Publication confirmée ✅")
        return True

    warn("Pas de confirmation explicite détectée (vérifie dans l'app).")
    return False


# -----------------------------
# main
# -----------------------------
def main():
    log(f"Compte ciblé: {ACCOUNT or '(non précisé)'} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")

    # Fichier vidéo
    video_abs = str(Path(VIDEO_PATH).resolve())

    with sync_playwright() as p:
        browser, context, page = open_context(p, headless=True)

        try:
            go_studio_and_inject_cookies(context, page, COOKIE_RAW)

            success = True
            for i in range(POSTS_TO_PUBLISH):
                print(f"— Post {i+1}/{POSTS_TO_PUBLISH} —", flush=True)
                ok = publish_once(context, page, video_abs)
                if not ok:
                    success = False
                    break
                # courte pause entre posts
                page.wait_for_timeout(1500)

            if success:
                log("Run terminé ✅")
            else:
                die("Publication échouée.")

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except PWTimeout as e:
        die(f"Timeout Playwright: {e}")
    except Exception as e:
        die(f"{type(e).__name__}: {e}")
