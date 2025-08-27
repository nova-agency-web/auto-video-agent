import os
import sys
import json
from playwright.sync_api import sync_playwright

# --------- CONFIGURATION ---------
ACCOUNT = os.getenv("ACCOUNT", "trucs-malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "FALSE").upper() == "TRUE"

# Secrets injectés depuis GitHub
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "")
UA_RAW = os.getenv("TIKTOK_UA", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/128.0.0.0 Safari/537.36")

VIDEO_PATH = "assets/test.mp4"
CAPTION_TEXT = "Test upload vidéo automatique"


# --------- UPLOAD ROBUSTE ---------
def upload_video_via_studio(page, video_path: str):
    print("[UPLOAD] Navigation et sélection du champ vidéo…")

    if "tiktokstudio/upload" not in page.url:
        page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="networkidle")
    page.wait_for_load_state("networkidle")

    # 1) Cliquer CTA pour révéler l’input
    for label in ["Sélectionner une vidéo", "Importer", "Téléverser", "Select a video", "Upload"]:
        btn = page.locator(f'button:has-text("{label}")')
        try:
            if btn.count():
                btn.first.click()
                page.wait_for_timeout(500)
                break
        except Exception:
            pass

    # 2) Essayer plusieurs sélecteurs possibles
    candidates = [
        'input[data-e2e="upload-video-input"]',
        'input[type="file"][accept*="video"]',
        'input[type="file"]',
        'input#upload-input',
        'input[name="upload-file"]',
        'label input[type="file"]'
    ]
    file_input = None
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=3000)
            file_input = loc
            break
        except Exception:
            continue

    # 3) Si introuvable, démasquer via JS
    if file_input is None:
        page.evaluate("""
            () => {
                const el =
                  document.querySelector('input[data-e2e="upload-video-input"]') ||
                  document.querySelector('input[type="file"][accept*="video"]') ||
                  document.querySelector('input[type="file"]');
                if (el) {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.removeAttribute('hidden');
                }
            }
        """)
        try:
            file_input = page.locator('input[type="file"]').first
            file_input.wait_for(state="attached", timeout=3000)
        except Exception:
            raise RuntimeError("Impossible de localiser le champ fichier (input[type='file']).")

    # 4) Charger la vidéo
    file_input.set_input_files(video_path)
    print(f"[UPLOAD] Fichier vidéo sélectionné: {video_path}")

    # 5) Attendre la fin d’upload (différents signaux)
    done_signals = [
        '[data-e2e="upload-done"]',
        'text=Upload complete',
        'text=Téléversement terminé',
        'text=Upload terminé',
        '[data-e2e="video-cover"]',
    ]
    uploaded = False
    for sig in done_signals:
        try:
            page.locator(sig).first.wait_for(state="visible", timeout=120_000)
            uploaded = True
            print(f"[UPLOAD] Signal détecté: {sig}")
            break
        except Exception:
            continue

    if not uploaded:
        print("[UPLOAD] Aucun signal explicite détecté. On continue après délai…")
        page.wait_for_timeout(10_000)


# --------- PUBLIER VIDEO ---------
def publish_to_tiktok(cookies_raw, caption, video, ua_raw):
    cookies = []
    for pair in cookies_raw.split(";"):
        if "=" in pair:
            name, value = pair.strip().split("=", 1)
            cookies.append({"name": name, "value": value, "domain": ".tiktok.com", "path": "/"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=ua_raw)
        context.add_cookies(cookies)
        page = context.new_page()

        print("[NAV] Vers TikTok Studio Upload…")
        page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="networkidle")
        page.wait_for_load_state("networkidle")

        # Upload robuste
        upload_video_via_studio(page, video)

        # Légende
        print("[CAPTION] Remplissage légende…")
        caption_sel = (
            '[data-e2e="caption"] textarea, '
            '[data-e2e="caption"] div[contenteditable="true"], '
            'textarea[placeholder*="Légende"], '
            'div[contenteditable="true"][aria-label*="Légende"], '
            'div[contenteditable="true"][data-e2e="caption"]'
        )
        try:
            cap = page.locator(caption_sel).first
            cap.fill(caption)
            print("[CAPTION] Légende insérée.")
        except Exception:
            print("[CAPTION] Échec remplissage légende.")

        # Bouton Publier
        print("[PUBLISH] Recherche du bouton Publier…")
        publish_candidates = [
            'button:has-text("Publier")',
            'button:has-text("Post")',
            '[data-e2e="post-button"]',
        ]
        published = False
        for pc in publish_candidates:
            try:
                btn = page.locator(pc).first
                if btn.count():
                    btn.click()
                    published = True
                    print(f"[PUBLISH] Clic sur: {pc}")
                    break
            except Exception:
                continue

        if not published:
            raise RuntimeError("ERREUR: Bouton 'Publier' introuvable.")

        page.wait_for_timeout(5000)
        browser.close()
        return True


# --------- MAIN ---------
def main():
    print(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    for i in range(POSTS_TO_PUBLISH):
        print(f"— Post {i+1}/{POSTS_TO_PUBLISH} —")
        if DRY_RUN:
            print("[SIMULATION] Publication non effectuée.")
            continue
        ok = publish_to_tiktok(COOKIE_RAW, CAPTION_TEXT, VIDEO_PATH, UA_RAW)
        if ok:
            print("[SUCCÈS] Publication terminée.")


if __name__ == "__main__":
    main()
