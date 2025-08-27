# main.py
import os
import csv
import time
from pathlib import Path
from typing import List, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ---------- Utils ----------
def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name, "")
    if isinstance(v, bool):
        return v
    v = str(v).strip().lower()
    if v in ("1", "true", "vrai", "yes", "y", "on"):
        return True
    if v in ("0", "false", "faux", "no", "n", "off"):
        return False
    return default


def parse_cookie_header(cookie_header: str) -> List[Dict]:
    """
    Convertit une ligne "Cookie: a=1; b=2; ..." en liste de cookies Playwright.
    Domaine par défaut: .tiktok.com
    """
    if not cookie_header:
        return []
    # Supprime un éventuel prefixe "Cookie:"
    if cookie_header.lower().startswith("cookie:"):
        cookie_header = cookie_header.split(":", 1)[1].strip()

    cookies = []
    parts = [p.strip() for p in cookie_header.split(";") if p.strip()]
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".tiktok.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def find_in_any_frame(page, selector: str, timeout_ms: int = 10000):
    """
    Cherche le premier élément visible correspondant au sélecteur dans
    la page principale puis dans toutes les iframes.
    """
    # Page principale
    try:
        el = page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        return el
    except PWTimeoutError:
        pass
    # Frames
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        try:
            el = fr.wait_for_selector(selector, timeout=timeout_ms, state="visible")
            return el
        except PWTimeoutError:
            continue
    raise PWTimeoutError(f"Selector not found in any frame: {selector}")


def choose_video_file() -> Path:
    candidates = [
        Path("assets/test.mp4"),
        Path("videoplayback.mp4"),
        Path("lecture vidéo.mp4"),  # au cas où
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        "Aucune vidéo trouvée. Place un MP4 dans 'assets/test.mp4' (recommandé)."
    )


def load_caption_from_csv(account_hint: str) -> str:
    """
    Essaie de charger une légende depuis data/scripts.csv.
    Format attendu: id,titre,texte,cta,hashtags,...
    Renvoie la première légende 'texte' trouvée (logique minimale).
    """
    csv_path = Path("data/scripts.csv")
    if not csv_path.exists():
        return ""
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # tente 'texte' sinon 'title' sinon concat minimale
                txt = (
                    row.get("texte")
                    or row.get("text")
                    or row.get("title")
                    or row.get("titre")
                    or ""
                )
                if txt.strip():
                    # Optionnel: petite touche selon le compte
                    hashtags = row.get("hashtags", "").strip()
                    if hashtags:
                        return f"{txt}\n\n{hashtags}"
                    return txt
    except Exception as e:
        log(f"Avertissement CSV: {e}")
    return ""


# ---------- Publication ----------
def publish_to_tiktok(cookie_raw: str, caption: str, video_path: Path, ua_raw: str, dry_run: bool) -> bool:
    """
    Publie une vidéo via TikTok Studio Upload.
    """
    if not video_path.exists():
        log(f"ERREUR: vidéo introuvable: {video_path}")
        return False

    upload_url = "https://www.tiktok.com/tiktokstudio/upload"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-background-timer-throttling",
            ],
        )

        context_args = {
            "viewport": {"width": 1366, "height": 900},
            "java_script_enabled": True,
        }

        # UA personnalisé si fourni
        if ua_raw and ua_raw.strip():
            context_args["user_agent"] = ua_raw.strip()

        context = browser.new_context(**context_args)

        # Cookies
        cookies = parse_cookie_header(cookie_raw)
        if not cookies:
            log("ERREUR: Cookie TikTok manquant ou invalide.")
            context.close(); browser.close()
            return False

        try:
            context.add_cookies(cookies)
            log(f"Injection cookies ({len(cookies)} entrées)…")
        except Exception as e:
            log(f"ERREUR cookies: {e}")
            context.close(); browser.close()
            return False

        page = context.new_page()

        # Charge la page Upload
        log("Ouverture TikTok Studio Upload…")
        page.goto(upload_url, wait_until="load", timeout=120_000)

        # Parfois, il faut recharger après cookies
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except PWTimeoutError:
            pass

        # ----- Upload du fichier -----
        file_input_selectors = [
            "input[type='file']",
            "[data-e2e='upload-input'] input[type='file']",
        ]
        file_input = None
        for sel in file_input_selectors:
            try:
                file_input = find_in_any_frame(page, sel, timeout_ms=10_000)
                break
            except PWTimeoutError:
                continue

        if not file_input:
            log("ERREUR: Impossible de localiser le champ fichier (input[type='file']).")
            context.close(); browser.close()
            return False

        try:
            file_input.set_input_files(str(video_path))
            log(f"Upload vidéo: {video_path}")
        except Exception as e:
            log(f"ERREUR upload: {e}")
            context.close(); browser.close()
            return False

        # ---------- Attente de la fin d'upload ----------
        # TikTok varie beaucoup; on attend un certain temps + surveille l'apparition d'éléments.
        # Si on ne trouve pas 'upload terminé', on poursuit prudemment.
        try:
            page.wait_for_timeout(2000)
            # quelques marqueurs fréquents:
            done_markers = [
                "[data-e2e='cover-selector']",     # apparition de l'éditeur
                "text=Miniature",                   # FR
                "text=Thumbnail",                   # EN
                "text=Résolution", "text=Resolution"
            ]
            upload_done = False
            t0 = time.time()
            while time.time() - t0 < 120:
                for fr in page.frames:
                    for sel in done_markers:
                        try:
                            el = fr.query_selector(sel)
                            if el and el.is_visible():
                                upload_done = True
                                break
                        except Exception:
                            continue
                    if upload_done:
                        break
                if upload_done:
                    break
                time.sleep(1)

            if not upload_done:
                log("Avertissement: aucun indicateur 'upload terminé' détecté (on continue).")
        except Exception:
            pass

        # ---------- Légende ----------
        if not caption.strip():
            caption = "Posté automatiquement ⚙️ #auto #tiktok"

        # --- Bloc robuste légende (textarea OU contenteditable) ---
        def try_fill(el):
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                is_contenteditable = el.evaluate("e => e.getAttribute('contenteditable') === 'true'")
                if tag == "textarea":
                    el.fill(caption)
                    return True
                if is_contenteditable:
                    el.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(caption)
                    return True
                # fallback
                el.click()
                page.keyboard.type(caption)
                return True
            except Exception:
                return False

        caption_candidates = [
            "textarea[placeholder*='description']",
            "textarea[placeholder*='Description']",
            "textarea[data-e2e='caption']",
            "div[contenteditable='true'][data-text='true']",
            "div[role='textbox'][contenteditable='true']",
            "[data-e2e='caption'] div[contenteditable='true']",
            "[data-e2e='caption-editor'] div[contenteditable='true']",
            "div[contenteditable='true']",
            "textarea",
        ]

        caption_set = False
        for sel in caption_candidates:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=12_000)
                if try_fill(el):
                    caption_set = True
                    log("Légende insérée.")
                    break
            except PWTimeoutError:
                continue
            except Exception:
                continue

        if not caption_set:
            log("Avertissement: impossible de remplir la légende (aucun champ compatible trouvé).")

        if dry_run:
            log("DRY_RUN=TRUE → simulation OK ✅ (aucun clic sur Publier).")
            context.close(); browser.close()
            return True

        # ---------- Bouton Publier ----------
        publish_selectors = [
            "button:has-text('Publier maintenant')",
            "button:has-text('Publier')",
            "button:has-text('Post')",
            "[data-e2e='post-button']",
            "button[type='submit']",
        ]

        def find_publish_button():
            for sel in publish_selectors:
                try:
                    return find_in_any_frame(page, sel, timeout_ms=8000)
                except PWTimeoutError:
                    continue
            return None

        clicked = False
        deadline = time.time() + 120
        while time.time() < deadline:
            btn = find_publish_button()
            if btn:
                try:
                    # attends qu'il soit activable
                    t_ready = time.time() + 30
                    while time.time() < t_ready:
                        disabled_attr = btn.get_attribute("disabled")
                        aria_disabled = btn.get_attribute("aria-disabled")
                        if (not disabled_attr) and (aria_disabled not in ("true", "1")):
                            btn.click()
                            clicked = True
                            break
                        time.sleep(1)
                    if clicked:
                        break
                except Exception:
                    pass
            time.sleep(2)

        if not clicked:
            log("ERREUR: Bouton 'Publier' non cliquable/détecté.")
            context.close(); browser.close()
            return False

        log("Clic sur Publier…")

        # ---------- Confirmation ----------
        success_markers = [
            "text=Publication programmée",
            "text=Publication réussie",
            "text=Votre vidéo a été publiée",
            "text=Posted",
            "[data-e2e='post-success']",
        ]
        success = False
        t2 = time.time()
        while time.time() - t2 < 120:
            for fr in page.frames:
                for sel in success_markers:
                    try:
                        el = fr.query_selector(sel)
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

        context.close(); browser.close()
        return True


# ---------- Entrée principale ----------
def main():
    account = os.getenv("ACCOUNT", "").strip()
    posts_to_publish = max(1, int(str(os.getenv("POSTS_TO_PUBLISH", "1")).strip() or "1"))
    dry_run = env_bool("DRY_RUN", default=True)

    tiktok_cookie = os.getenv("TIKTOK_COOKIE", "").strip()
    tiktok_ua = os.getenv("TIKTOK_UA", "").strip()

    log(f"Compte ciblé: {account or '(non spécifié)'} | Posts: {posts_to_publish} | DRY_RUN={dry_run}")

    # Choix de la vidéo
    try:
        video_path = choose_video_file()
    except Exception as e:
        log(f"ERREUR: {e}")
        return

    # Légende depuis CSV si possible
    caption = load_caption_from_csv(account)
    if not caption:
        caption = "Automatisation de post 🎯 #tiktok #auto"

    for i in range(posts_to_publish):
        log("─── Post {}/{} ───".format(i + 1, posts_to_publish))
        ok = publish_to_tiktok(tiktok_cookie, caption, video_path, tiktok_ua, dry_run)
        if not ok:
            log("ERREUR: publication échouée.")
            break
        time.sleep(2)

    log("Run terminé ✅")


if __name__ == "__main__":
    main()
