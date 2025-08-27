# main.py
#
# Version "robuste" :
# - Upload via TikTok Studio
# - Remplit la légende
# - Coche les cases de conformité si présentes (Divulgation / branded content)
# - S'assure que la visibilité = Public
# - Fait défiler jusqu'à "Publier", attend que le traitement soit OK, clique avec reprises
# - Journalise en FR pour lecture facile dans GitHub Actions

import os
import sys
import time
from contextlib import suppress
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TIKTOK_STUDIO_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"

# ====== Paramètres via variables d'env (défauts sûrs) ======
ACCOUNT         = os.getenv("ACCOUNT", "compte")  # juste pour logs
DRY_RUN         = os.getenv("DRY_RUN", "FALSE").upper() == "TRUE"
POSTS_TO_PUBLISH= int(os.getenv("POSTS_TO_PUBLISH", "1"))
VIDEO_PATH      = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT    = os.getenv("CAPTION_TEXT", "Video auto 🚀")
UA_RAW          = os.getenv("TIKTOK_UA", "").strip()
COOKIE_RAW      = os.getenv("TIKTOK_COOKIE", "").strip()  # facultatif si déjà connecté dans le runner

# ====== Utilitaires ======
def log(msg: str):
    print(msg, flush=True)

def try_click(locator, timeout=2000):
    with suppress(Exception):
        locator.click(timeout=timeout, trial=True)
        locator.click(timeout=timeout)

def visible(locator, timeout=1500) -> bool:
    with suppress(Exception):
        locator.wait_for(state="visible", timeout=timeout)
        return True
    return False

def enabled(locator, timeout=1500) -> bool:
    with suppress(Exception):
        locator.wait_for(state="visible", timeout=timeout)
        return locator.is_enabled()
    return False

def set_public_if_needed(page):
    """
    Force la visibilité sur 'Tout le monde / Public' si un sélecteur existe.
    On couvre à la fois UI Studio FR et EN.
    """
    # Bouton/ligne d'option Visibilité
    # Cas FR : "Tout le monde peut voir cette publication"
    # Cas EN : "Who can view this post" / "Everyone"
    candidates = [
        page.get_by_text("Tout le monde peut voir cette publication", exact=False),
        page.get_by_text("Who can view this post", exact=False),
        page.get_by_role("button", name="Tout le monde", exact=False),
        page.get_by_role("button", name="Everyone", exact=False),
    ]
    for loc in candidates:
        if visible(loc):
            # Si un menu s'ouvre, on choisit "Tout le monde / Everyone"
            try_click(loc)
            choice = (
                page.get_by_text("Tout le monde", exact=False).or_(
                    page.get_by_text("Everyone", exact=False)
                )
            )
            if visible(choice, 1200):
                try_click(choice)
            break

def tick_compliance_if_needed(page):
    """
    Coche les cases de conformité / divulgation si la UI l'exige (selon pays/compte).
    On cherche du texte FR/EN fréquent et coche les checkbox/toggles à proximité.
    """
    possible_labels = [
        "Divulgation de contenu",
        "Contenu de marque",
        "Branded content",
        "Sponsored",
        "Paid partnership",
        "Publicité",
    ]
    for label in possible_labels:
        label_loc = page.get_by_text(label, exact=False)
        if visible(label_loc, 800):
            # Cherche un toggle/checkbox proche dans le même bloc
            container = label_loc.locator("xpath=ancestor::*[self::div or self::section][1]")
            # toggles/checkbox classiques
            toggle = container.locator("input[type=checkbox], div[role=switch], button[role=switch], div:has(input[type=checkbox])")
            with suppress(Exception):
                if toggle.count() > 0:
                    # clique le premier interactif
                    try_click(toggle.first, timeout=1500)

def wait_processing_done(page, max_wait_s=240):
    """
    Attend que TikTok ait fini l'analyse/traitement de la vidéo.
    Signaux possibles :
      - disparition du spinner
      - apparition de "traitement terminé" / "processing complete"
      - activation du bouton 'Publier'
    """
    start = time.time()
    while time.time() - start < max_wait_s:
        # 1) si bouton Publier devient enabled, on sort
        publish_btn = get_publish_button(page)
        if publish_btn and enabled(publish_btn, 1000):
            return True

        # 2) bulles de statut fréquentes (FR/EN)
        done_texts = [
            "Traitement terminé", "Traitement fini", "Prêt à publier",
            "Processing complete", "Ready to post"
        ]
        for t in done_texts:
            if visible(page.get_by_text(t, exact=False), 800):
                return True

        time.sleep(1.5)
    return False

def get_publish_button(page):
    """
    Récupère un locator fiable pour Publier (FR/EN).
    """
    options = [
        page.get_by_role("button", name="Publier", exact=False),
        page.get_by_role("button", name="Post", exact=False),
        page.get_by_text("Publier").locator("xpath=ancestor::button[1]"),
        page.get_by_text("Post").locator("xpath=ancestor::button[1]"),
    ]
    for loc in options:
        with suppress(Exception):
            if loc.count() > 0 and visible(loc.first, 600):
                return loc.first
    return None

def ensure_caption(page, caption: str):
    """
    Remplit/rafraîchit la légende pour débloquer la validation.
    Couverture FR/EN pour l'éditeur de légende.
    """
    candidates = [
        page.get_by_placeholder("Ajoute un titre accrocheur", exact=False),
        page.get_by_placeholder("Rédiger une description", exact=False),
        page.get_by_role("textbox", name="Description", exact=False),
        page.locator("textarea[placeholder*='description' i]"),
        page.locator("div[contenteditable='true']"),
    ]
    for ed in candidates:
        if visible(ed, 800):
            try:
                ed.click(timeout=1200)
                # injecte la légende (on efface au besoin)
                with suppress(Exception):
                    ed.fill("", timeout=800)
                ed.type(caption, delay=10)
                # blur pour forcer la validation
                page.keyboard.press("Tab")
                return True
            except Exception:
                continue
    return False

def upload_video_via_studio(page, file_path: str):
    """
    Upload via l'input direct sur la page Studio.
    """
    page.goto(TIKTOK_STUDIO_UPLOAD, timeout=60_000, wait_until="domcontentloaded")
    log("[INFO] [NAV] Vers TikTok Studio Upload")

    # 1) input file
    file_input = page.locator("input[type='file']")
    file_input.wait_for(state="visible", timeout=30_000)
    log("[INFO] Sélection champ fichier…")
    file_input.set_input_files(file_path)
    log("[INFO] Upload (input direct) déclenché ✅")

def finalize_and_publish(page, caption: str):
    """
    Tous les garde-fous avant clic 'Publier':
    - Légende
    - Visibilité Public
    - Cases Conformité si présentes
    - Scroll et retry jusqu'à activation du bouton
    """
    # Légende
    if ensure_caption(page, caption):
        log("[INFO] Légende insérée.")
    else:
        log("[WARN] Impossible de localiser l’éditeur de légende (textarea).")

    # Visibilité
    set_public_if_needed(page)

    # Conformité / divulgation
    tick_compliance_if_needed(page)

    # Attendre le traitement
    ready = wait_processing_done(page, max_wait_s=240)
    if not ready:
        log("[WARN] Traitement trop long ou bouton toujours inactif.")

    # Scroll jusqu’à "Publier" + reprises
    publish_btn = get_publish_button(page)
    if not publish_btn:
        log("[ERROR] Bouton 'Publier' introuvable.")
        return False

    # Boucle de reprises
    for attempt in range(1, 8):
        try:
            publish_btn.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        # Petite “impulsion” de validation : retaper un espace dans la légende si possible
        ensure_caption(page, caption + " ")

        if enabled(publish_btn, 1500):
            if DRY_RUN:
                log("[INFO] DRY_RUN=TRUE → on ne clique pas 'Publier'. ✅")
                return True
            try:
                publish_btn.click(timeout=2000)
                log("[INFO] Clic 'Publier' envoyé ✅")
                return True
            except Exception as e:
                log(f"[WARN] Clic 'Publier' échoué (tentative {attempt}) : {e}")

        # Attendre un peu / déclencher des micro-actions pour rafraîchir l'état
        time.sleep(2 + attempt)  # backoff progressif

    log("[WARN] Bouton 'Publier' non cliquable dans les temps.")
    return False

def main():
    if not os.path.exists(VIDEO_PATH):
        log(f"[ERROR] Fichier vidéo introuvable : {VIDEO_PATH}")
        sys.exit(1)

    log(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,  # en CI : True ; en local pour debug : False
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )

        context_kwargs = {}
        if UA_RAW:
            context_kwargs["user_agent"] = UA_RAW

        context = browser.new_context(**context_kwargs)

        # Cookies optionnels si fournis (format "k=v; k2=v2; ...")
        if COOKIE_RAW:
            try:
                cookies = []
                for part in [c.strip() for c in COOKIE_RAW.split(";") if c.strip()]:
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".tiktok.com",
                            "path": "/",
                            "httpOnly": False,
                            "secure": True,
                            "sameSite": "Lax"
                        })
                if cookies:
                    context.add_cookies(cookies)
                    log(f"[INFO] Injection cookies ({len(cookies)})…")
            except Exception as e:
                log(f"[WARN] Cookies ignorés (invalides pour DevTools) : {e}")

        page = context.new_page()

        ok_posts = 0
        for idx in range(POSTS_TO_PUBLISH):
            log(f"[INFO] — Post {idx+1}/{POSTS_TO_PUBLISH} —")

            try:
                upload_video_via_studio(page, VIDEO_PATH)

                ok = finalize_and_publish(page, CAPTION_TEXT)

                if ok:
                    ok_posts += 1
                    log("[INFO] Publication envoyée (ou simulée).")
                else:
                    log("[WARN] Publication non finalisée.")

            except PWTimeout as te:
                log(f"[ERROR] Timeout Playwright : {te}")
            except Exception as e:
                log(f"[ERROR] Exception inattendue : {e}")

        log(f"[INFO] Run terminé ✅ ({ok_posts}/{POSTS_TO_PUBLISH} réussis)")
        context.close()
        browser.close()

if __name__ == "__main__":
    main()
