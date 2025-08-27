# main.py
# Publie une vidéo TikTok via TikTok Studio en utilisant un en-tête Cookie brut.
# Variables d'env attendues (déjà posées par ton workflow):
# - ACCOUNT           (string) : nom logique du compte (log uniquement)
# - POSTS_TO_PUBLISH  (int)    : nombre de posts à faire (on fait 1 par run)
# - DRY_RUN           (TRUE/FALSE)
# - TIKTOK_COOKIE     (string) : **chaîne Cookie complète** copiée de DevTools
# - TIKTOK_UA         (string) : User-Agent de ton Chrome (facultatif)
#
# Vidéo : assets/test.mp4 (ou change VIDEO_PATH ci-dessous)

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- Config ----------
VIDEO_PATH = Path("assets/test.mp4")            # Chemin vidéo par défaut
STUDIO_URLS = [
    "https://www.tiktok.com/tiktokstudio/upload",  # Studio
    "https://www.tiktok.com/upload",               # Uploader “classique”
]
CAPTION_TEXT = "Post automatisé ✅ #auto"        # Légende par défaut
FILE_INPUT_SELECTOR = 'input[type="file"]'       # input caché (on forcera)
PUBLISH_BUTTON_TEXTS = ["Publier", "Post", "Publish"]  # Sélecteurs de secours
CAPTION_CANDIDATES = [
    'textarea[placeholder*="légende"]',
    'textarea[placeholder*="Légende"]',
    'textarea[placeholder*="caption"]',
    '[data-e2e="caption"] textarea',
    '[data-e2e="caption"]',
    'textarea',
]
# ---------- Helpers ----------

def env_bool(name: str, default=False):
    val = os.getenv(name, "")
    if isinstance(val, bool):
        return val
    val = (val or "").strip().lower()
    if val in ("1", "true", "vrai", "yes", "y", "on"):
        return True
    if val in ("0", "false", "faux", "no", "n", "off"):
        return False
    return default

def die(msg: str, code: int = 1):
    print(f"[ERREUR] {msg}")
    sys.exit(code)

def info(msg: str):
    print(f"[INFO] {msg}")

def warn(msg: str):
    print(f"[WARN] {msg}")

# ---------- Entrée ----------
ACCOUNT = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1") or "1")
DRY_RUN = env_bool("DRY_RUN", False)
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "") or ""
UA_RAW = os.getenv("TIKTOK_UA", "").strip()

print(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")

# Validation cookie brut : on vérifie juste que ça ressemble à "k=v; k2=v2"
if not COOKIE_RAW or ("=" not in COOKIE_RAW) or (";" not in COOKIE_RAW):
    die("Impossible de parser TIKTOK_COOKIE (chaîne vide ou sans point-virgule). "
        "Colle **la valeur entière** de l'en-tête 'Cookie' depuis DevTools (onglet Network ➜ une requête vers tiktok.com ➜ Headers ➜ Request Headers ➜ Cookie).")

# Petit check : au moins un identifiant de session courant
if not any(key in COOKIE_RAW for key in ("sessionid", "sid_tt", "ssid_ucp_v1", "sessionid_ss")):
    warn("TIKTOK_COOKIE ne contient aucun des marqueurs ['sessionid','sid_tt','ssid_ucp_v1','sessionid_ss'] — il se peut qu'il soit expiré.")

if not VIDEO_PATH.exists():
    die(f"Fichier vidéo introuvable: {VIDEO_PATH}. Place un MP4 dans {VIDEO_PATH}")

if DRY_RUN:
    info("Mode DRY_RUN actif — aucun clic de publication ne sera effectué.")

# ---------- Publication ----------
def open_studio_and_upload(page):
    """
    Ouvre une des URLs Studio et envoie le fichier dans l'input caché.
    Retourne True si l'upload a pu être déclenché.
    """
    last_err = None
    for url in STUDIO_URLS:
        info(f"[NAV] Vers {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # Attends que la page soit stabilisée un minimum
            page.wait_for_timeout(1000)

            # L'input est caché: on l'attrape même s'il n'est pas visible, puis on force set_input_files
            file_input = page.locator(FILE_INPUT_SELECTOR).first
            file_input.wait_for(state="attached", timeout=15_000)
            info("Sélection champ fichier…")
            file_input.set_input_files(str(VIDEO_PATH), timeout=60_000)
            info("Upload déclenché ✅")
            return True
        except PWTimeout as e:
            last_err = e
            warn(f"Timeout sur {url}: {e}")
        except Exception as e:
            last_err = e
            warn(f"Echec sur {url}: {e}")
    if last_err:
        raise last_err
    return False

def try_fill_caption(page, caption: str):
    for sel in CAPTION_CANDIDATES:
        try:
            el = page.locator(sel).first
            el.wait_for(state="attached", timeout=5_000)
            # Certaines zones ont besoin d'un clic avant fill
            try:
                el.click(timeout=2_000)
            except Exception:
                pass
            el.fill(caption, timeout=5_000)
            info("Légende insérée.")
            return True
        except Exception:
            continue
    warn("Avertissement: impossible de remplir la légende (textarea non trouvé).")
    return False

def try_click_publish(page):
    if DRY_RUN:
        info("DRY_RUN=True ➜ on ne clique pas sur 'Publier'.")
        return True
    # Plusieurs variantes possibles selon langue/AB-test
    for label in PUBLISH_BUTTON_TEXTS:
        try:
            # Essaye un bouton textuel
            btn = page.get_by_role("button", name=label).first
            btn.wait_for(state="visible", timeout=8_000)
            page.evaluate("el => el.scrollIntoView({block:'center'})", btn.element_handle())
            btn.click(timeout=8_000)
            info("Bouton 'Publier' cliqué.")
            return True
        except Exception:
            # Essaye via texte brut
            try:
                btn2 = page.locator(f"button:has-text('{label}')").first
                btn2.wait_for(state="attached", timeout=5_000)
                page.evaluate("el => el.scrollIntoView({block:'center'})", btn2.element_handle())
                btn2.click(timeout=5_000, force=True)
                info("Bouton 'Publier' cliqué (fallback).")
                return True
            except Exception:
                continue
    warn("Avertissement: bouton 'Publier' non cliquable/détecté.")
    return False


def publish_once(pw):
    # Crée le navigateur avec User-Agent et **Cookie header** global
    launch_opts = dict(headless=True, args=["--disable-dev-shm-usage"])
    browser = pw.chromium.launch(**launch_opts)

    # Construit les en-têtes — on injecte **Cookie brut** ici
    headers = {"cookie": COOKIE_RAW}
    context_opts = {
        "extra_http_headers": headers,
        "ignore_https_errors": True,
        "viewport": {"width": 1366, "height": 768},
        "timezone_id": "Europe/Paris",
        "locale": "fr-FR",
    }
    if UA_RAW:
        context_opts["user_agent"] = UA_RAW

    context = browser.new_context(**context_opts)
    page = context.new_page()

    try:
        ok_upload = open_studio_and_upload(page)
        if not ok_upload:
            die("Impossible de démarrer l’upload (input file introuvable).")

        # Attends quelques secondes que TikTok traite le fichier
        page.wait_for_timeout(5_000)

        try_fill_caption(page, CAPTION_TEXT)

        # Selon l’UI, attendre que l’upload/wrapping se termine (optionnel)
        # On surveille un indicateur d’état s’il existe, sinon on continue.
        # (On évite d’être trop strict pour ne pas replanter.)
        try:
            # Exemples d’indicateurs (ajuste si tu en vois un stable)
            selectors_done = [
                'text=Téléversement réussi',
                'text=Traitement terminé',
                '[data-e2e="post-ready"]',
            ]
            for s in selectors_done:
                page.wait_for_selector(s, timeout=8_000)
                info(f"Indicateur détecté: {s}")
                break
        except Exception:
            pass

        try_click_publish(page)

        # Attendre un peu pour capter les éventuels toasts de succès
        page.wait_for_timeout(5_000)

        return True
    finally:
        context.close()
        browser.close()


def main():
    print("— Post 1/1 —")
    with sync_playwright() as pw:
        ok = publish_once(pw)
        if not ok:
            die("Publication échouée.")

    print("Run terminé ✅")


if __name__ == "__main__":
    main()
