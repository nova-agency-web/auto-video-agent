# main.py
# -*- coding: utf-8 -*-

import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError


# ----------------------------
# Utilitaires
# ----------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v in ("1", "true", "vrai", "yes", "y"):
        return True
    if v in ("0", "false", "faux", "no", "n"):
        return False
    return default


def parse_cookie_header_to_items(cookie_header: str) -> List[dict]:
    """
    Transforme une chaîne 'Cookie:' (ex: 'name=value; name2=value2; ...')
    en liste de cookies Playwright.
    On met par défaut le domaine en .tiktok.com et path=/.
    """
    items = []
    # découpage *simple* sur ';' – suffisamment robuste pour nos cookies TikTok
    for part in [p.strip() for p in cookie_header.split(";") if p.strip()]:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        items.append({
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })
    return items


def find_in_any_frame(page, selector: str, timeout_ms: int = 10000):
    """
    Attend qu'un élément correspondant à 'selector' existe dans au moins
    un frame et le retourne.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                el = frame.wait_for_selector(selector, timeout=500, state="attached")
                if el:
                    return el
            except Exception as e:
                last_err = e
                continue
        time.sleep(0.1)
    if last_err:
        raise last_err
    raise PwTimeoutError(f"Selector not found in any frame: {selector}")


def wait_for_any_selector(page, selectors: List[str], timeout_ms: int = 10000):
    """
    Attend qu'au moins un des sélecteurs apparaisse dans n'importe quel frame
    et retourne le tuple (selector, element_handle).
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=800)
                if el:
                    return sel, el
            except Exception as e:
                last_err = e
                continue
        time.sleep(0.1)
    if last_err:
        raise last_err
    raise PwTimeoutError(f"Aucun des sélecteurs suivants n'a été trouvé: {selectors}")


# ----------------------------
# Publication TikTok
# ----------------------------
def publish_to_tiktok(cookie_raw: str, caption: str, video_path: Path, ua_raw: Optional[str], dry_run: bool) -> bool:
    upload_url = "https://www.tiktok.com/tiktokstudio/upload"

    with sync_playwright() as p:
        log("Ouverture Playwright (Chromium)…")
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context_args = {}

        user_agent = (ua_raw or "").strip()
        if user_agent:
            context_args["user_agent"] = user_agent

        context = browser.new_context(**context_args)

        # Ajout cookies AVANT la navigation
        cookies = parse_cookie_header_to_items(cookie_raw)
        if not cookies:
            log("ERREUR: Aucun cookie parsé ; vérifie le secret TIKTOK_COOKIE.")
            context.close(); browser.close()
            return False

        log(f"Injection cookies ({len(cookies)} entrées)…")
        context.add_cookies(cookies)

        page = context.new_page()

        # Naviguer vers la page d'upload
        log(f"Navigation: {upload_url}")
        page.goto(upload_url, wait_until="domcontentloaded", timeout=120_000)

        # Si TikTok affiche une page de consentement ou login, on échouera au moment de chercher l'UI d'upload.
        # ----- Upload du fichier -----
        log("Recherche du bouton/champ d’importation…")
        upload_selectors = [
            "button:has-text('Importer')",
            "button:has-text('Upload')",
            "div[role='button'][data-e2e='upload-button']",
            "[data-e2e='upload-button']",
            # cas où l'input est directement présent (peu probable) :
            "input[type='file']",
        ]

        file_input = None
        # on essaie de cliquer sur un bouton d'abord
        for sel in upload_selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=12_000)
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                if tag == "input":
                    file_input = el
                    break
                else:
                    # Cliquez pour déclencher l'apparition de l'input
                    try:
                        el.click()
                    except Exception:
                        # parfois nécessite force=True
                        try:
                            el.click(force=True)
                        except Exception:
                            pass
                    time.sleep(2)
                    # rechercher un input file nouvellement ouvert
                    try:
                        file_input = find_in_any_frame(page, "input[type='file']", timeout_ms=6_000)
                        break
                    except Exception:
                        continue
            except Exception:
                continue

        if not file_input:
            log("ERREUR: Impossible de trouver un champ/bouton d’upload (input[file]).")
            context.close(); browser.close()
            return False

        log(f"Upload vidéo: {video_path}")
        if not video_path.exists():
            log(f"ERREUR: fichier vidéo introuvable: {video_path}")
            context.close(); browser.close()
            return False

        # Envoie le fichier
        file_input.set_input_files(str(video_path))

        # Attendre que l'upload démarre/finisse — heuristiques
        # On surveille la disparition du bouton, l’apparition d’une miniature, ou un libellé de progression.
        possible_upload_done = [
            "text=Upload complete",
            "text=Téléversement réussi",
            "[data-e2e='upload-success']",
            "[data-e2e='video-cover'] img",
            "[class*='thumbnail'] img",
        ]
        try:
            wait_for_any_selector(page, possible_upload_done, timeout_ms=180_000)
            log("Upload terminé (signal heuristique).")
        except Exception:
            log("Avertissement: aucun indicateur 'upload terminé' détecté (on continue).")

        # ----- Légende -----
        log("Remplissage légende…")
        caption_selectors = [
            "[data-e2e='caption-input'] textarea",
            "[data-e2e='caption-input']",
            "textarea[placeholder*='légende']",
            "textarea[placeholder*='caption']",
            "div[contenteditable='true'][data-editor]",
            "div[contenteditable='true']",
        ]
        caption_box = None
        for sel in caption_selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=8_000)
                caption_box = el
                break
            except Exception:
                continue

        if not caption_box:
            log("Avertissement: impossible de remplir la légende (textarea non trouvé).")

        else:
            try:
                # Certaines zones sont contenteditable; on tente d'abord 'fill', sinon 'type'
                try:
                    caption_box.fill(caption)
                except Exception:
                    caption_box.click()
                    # vider éventuellement
                    caption_box.press("ControlOrMeta+A")
                    caption_box.type(caption, delay=5)
                log("Légende insérée.")
            except Exception:
                log("Avertissement: échec lors de l'insertion de la légende.")

        # Si simulation, on s’arrête ici
        if dry_run:
            log("DRY_RUN=TRUE → simulation terminée ✅ (aucune publication réelle).")
            context.close(); browser.close()
            return True

        # ----- Publier -----
        log("Recherche du bouton 'Publier'…")
        publish_selectors = [
            "button:has-text('Publier')",
            "button:has-text('Post')",
            "[data-e2e='post-button']",
        ]
        publish_btn = None
        for sel in publish_selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=10_000)
                publish_btn = el
                break
            except Exception:
                continue

        if not publish_btn:
            log("ERREUR: Bouton 'Publier' non trouvable/détecté.")
            context.close(); browser.close()
            return False

        try:
            # S'il est disabled, on patiente un peu (parfois besoin que l'analyse/scan finisse)
            end_time = time.time() + 90
            while time.time() < end_time:
                disabled = publish_btn.evaluate("(b)=>b.disabled === true", timeout=1000)
                if not disabled:
                    break
                time.sleep(1.0)

            # Cliquer
            publish_btn.click()
            log("Clic sur 'Publier'…")
        except Exception:
            try:
                publish_btn.click(force=True)
                log("Clic forcé sur 'Publier'.")
            except Exception:
                log("ERREUR: bouton 'Publier' non cliquable/détecté.")
                context.close(); browser.close()
                return False

        # Attendre un signal de succès (heuristique)
        success_markers = [
            "text=Publication réussie",
            "text=Post successful",
            "[data-e2e='post-success']",
            "text=Publié",
        ]
        try:
            wait_for_any_selector(page, success_markers, timeout_ms=60_000)
            log("Publication confirmée ✅")
        except Exception:
            log("Avertissement: pas de confirmation explicite détectée (vérifie dans l'app).")

        context.close()
        browser.close()
        return True


# ----------------------------
# Entrée principale
# ----------------------------
def main():
    account = os.getenv("ACCOUNT", "trucs malins")
    posts_to_publish = int(os.getenv("POSTS_TO_PUBLISH", "1"))
    dry_run = env_bool("DRY_RUN", False)

    # Secrets
    cookie_raw = os.getenv("TIKTOK_COOKIE", "").strip()
    ua_raw = os.getenv("TIKTOK_UA", "").strip()  # optionnel

    # Vidéo d'entrée : priorise assets/test.mp4, sinon un mp4 à la racine (ex: videoplayback.mp4)
    candidates = [
        Path("assets/test.mp4"),
        Path("test.mp4"),
        Path("videoplayback.mp4"),
        Path("lecture vidéo.mp4"),   # au cas où
    ]
    video_path = None
    for c in candidates:
        if c.exists():
            video_path = c
            break

    print(f"[{time.strftime('%H:%M:%S')}] Compte ciblé: {account} | Posts: {posts_to_publish} | DRY_RUN={dry_run}")

    if not video_path:
        print(f"[{time.strftime('%H:%M:%S')}] ERREUR: Aucune vidéo trouvée. Place un MP4 dans assets/test.mp4 (ou test.mp4 / videoplayback.mp4).")
        sys.exit(1)

    if not cookie_raw:
        print(f"[{time.strftime('%H:%M:%S')}] ERREUR: TIKTOK_COOKIE manquant (Secrets du repo).")
        sys.exit(1)

    # Légende de test — tu peux la remplacer par ce que tu lis de ton CSV si besoin
    base_caption = "Test upload vidéo automatique"

    ok_global = True
    for i in range(1, posts_to_publish + 1):
        print(f"[{time.strftime('%H:%M:%S')}] —— Post {i}/{posts_to_publish} ——")
        caption = base_caption
        ok = publish_to_tiktok(cookie_raw=cookie_raw, caption=caption, video_path=video_path, ua_raw=ua_raw, dry_run=dry_run)
        if not ok:
            print(f"[{time.strftime('%H:%M:%S')}] ERREUR: publication échouée.")
            ok_global = False
            # on n'interrompt pas forcément la boucle si tu envoies plusieurs posts
            # break

    print(f"[{time.strftime('%H:%M:%S')}] Run terminé {'✅' if ok_global else '❌'}")
    sys.exit(0 if ok_global else 1)


if __name__ == "__main__":
    main()
