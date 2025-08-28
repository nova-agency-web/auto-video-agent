# main.py
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ------------------------------
# Utilitaires de log
# ------------------------------
def log(msg: str):
    print(f"[INFO] {msg}")

def warn(msg: str):
    print(f"[WARN] {msg}")

def err(msg: str):
    print(f"[ERREUR] {msg}")


# ------------------------------
# Lecture de la config
# ------------------------------
ACCOUNT = os.getenv("ACCOUNT", "trucs-malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"

# Secrets
COOKIE_RAW = os.getenv("TIKTOK_COOKIE", "").strip()
UA_RAW = os.getenv("TIKTOK_UA", "").strip()

# Vidéo & légende (adapte si tu passes ça en arguments/vars)
VIDEO_PATH = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "Publication automatique via agent 🤖")


# ------------------------------
# Cookies helpers
# ------------------------------
def _normalize_cookie(c: Dict[str, Any]) -> Dict[str, Any]:
    """
    Playwright attend les champs: name, value, domain, path (oblig),
    expires (epoch), httpOnly, secure, sameSite ('Lax'|'Strict'|'None').
    On filtre/convertit doucement pour éviter 'Invalid cookie fields'.
    """
    out = {}

    # Obligatoires
    out["name"] = str(c.get("name", ""))
    out["value"] = str(c.get("value", ""))

    # Domain/path: essaye domain sinon host, mets leading dot si besoin
    domain = c.get("domain") or c.get("host", "")
    if not domain:
        # Si le cookie vient de DevTools 'Cookies' -> il a forcément domain
        # Sinon on le jette.
        raise ValueError("Cookie sans domain")

    out["domain"] = domain
    out["path"] = c.get("path", "/")

    # Optionnels
    if "expires" in c and isinstance(c["expires"], (int, float)):
        out["expires"] = int(c["expires"])

    if "httpOnly" in c:
        out["httpOnly"] = bool(c["httpOnly"])
    if "secure" in c:
        out["secure"] = bool(c["secure"])

    # sameSite conversion si présent
    ss = c.get("sameSite") or c.get("same_site")
    if isinstance(ss, str):
        t = ss.lower()
        if "lax" in t:
            out["sameSite"] = "Lax"
        elif "strict" in t:
            out["sameSite"] = "Strict"
        elif "none" in t:
            out["sameSite"] = "None"

    return out


def parse_cookies(raw: str) -> List[Dict[str, Any]]:
    """
    Accepte un JSON DevTools (liste d’objets) ou une seule ligne JSON.
    Ignore ce qui n’a pas domain/name/value.
    """
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Peut-être plusieurs JSON collés -> essaie list
        try:
            data = json.loads(raw.strip().strip("[]"))
            if isinstance(data, dict):
                data = [data]
        except Exception as e:
            err(f"Impossible de parser TIKTOK_COOKIE: {e}")
            return []

    if isinstance(data, dict):
        data = [data]

    cookies = []
    for c in data:
        try:
            nc = _normalize_cookie(c)
            cookies.append(nc)
        except Exception:
            # On ignore les entrées non valides
            pass
    return cookies


# ------------------------------
# Actions UI: publication
# ------------------------------
def _dismiss_overlays(page):
    # Ferme les overlays modaux qui bloquent les clics
    candidates = [
        '[data-tux-overlay] button:has-text("×")',
        '[role="dialog"] button:has-text("×")',
        'button[aria-label="Close"]',
    ]
    for sel in candidates:
        try:
            if page.locator(sel).first.is_visible():
                page.locator(sel).first.click()
        except Exception:
            pass


def _check_compliance_checkboxes(page):
    # Coche toute case visible (ex : “je confirme…”) si présente
    try:
        boxes = page.locator('input[type="checkbox"]')
        cnt = boxes.count()
        touched = 0
        for i in range(cnt):
            cb = boxes.nth(i)
            try:
                if cb.is_visible() and not cb.is_checked():
                    cb.check()
                    touched += 1
            except Exception:
                continue
        if touched:
            log(f"Cases de conformité cochées: {touched} ✅")
        else:
            warn("Aucune case de conformité détectée/nécessaire.")
    except Exception:
        warn("Impossible de scanner les cases de conformité (on continue).")


def _find_publish_button(page):
    # Essaye divers libellés FR/EN
    labels = [
        "Publier",
        "Post",
        "Publish",
        "Partager",  # fallback éventuel
    ]
    for text in labels:
        btn = page.locator(f'button:has-text("{text}")').first
        try:
            if btn.is_visible():
                return btn
        except Exception:
            pass
    return None


def publish_now(page):
    _dismiss_overlays(page)
    _check_compliance_checkboxes(page)

    btn = _find_publish_button(page)
    if not btn:
        raise RuntimeError("Bouton 'Publier' introuvable.")

    try:
        btn.scroll_into_view_if_needed()
    except Exception:
        pass

    # si le bouton est désactivé, on ré-essaye quelques secondes
    deadline = time.time() + 25
    clicked = False
    while time.time() < deadline:
        try:
            if btn.is_enabled():
                btn.click()
                log("Bouton 'Publier' cliqué ✅")
                clicked = True
                break
        except Exception:
            pass
        time.sleep(1)
    if not clicked:
        raise RuntimeError("Bouton 'Publier' toujours inactif après délais.")


# ------------------------------
# Upload helpers
# ------------------------------
def _wait_upload_ready(page, timeout_ms=30000):
    """
    Attend que l’UI d’upload soit prête : soit l’input file visible,
    soit une zone qui accepte le file chooser.
    """
    # Selectors courants côté Studio
    candidates = [
        'input[type="file"]',
        '[role="button"]:has-text("Sélectionner")',
        'div:has(input[type="file"])',
        '[data-e2e="upload"]',
    ]
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc and loc.is_visible():
                    return sel
            except Exception:
                continue
        time.sleep(0.5)
    raise RuntimeError("Impossible de localiser un input file visible pour l’upload.")


def _set_video(page, video_abs: str):
    """
    Injecte la vidéo: priorité à l’input direct. Sinon file chooser.
    """
    # 1) input direct
    inputs = page.locator('input[type="file"]')
    try:
        if inputs.count() > 0:
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                try:
                    if inp.is_visible():
                        inp.set_input_files(video_abs)
                        log("Upload déclenché ✅ (input direct)")
                        return
                except Exception:
                    continue
    except Exception:
        pass

    # 2) file chooser
    # On clique un bouton qui ouvre le file chooser
    triggers = [
        'button:has-text("Importer")',
        'button:has-text("Importer ta vidéo")',
        '[data-e2e="upload"]:has(button)',
        '[role="button"]:has-text("Importer")',
    ]
    for sel in triggers:
        try:
            with page.expect_file_chooser(timeout=5000) as fc:
                page.locator(sel).first.click()
            chooser = fc.value
            chooser.set_files(video_abs)
            log("Upload déclenché ✅ (file chooser)")
            return
        except Exception:
            continue

    raise RuntimeError("Impossible de fournir le fichier : ni input file ni file chooser.")


def _wait_upload_finish(page, max_wait_sec=180):
    """
    Attend que TikTok considère la vidéo prête (textarea/légende dispo, ou barre de complétion finie).
    """
    start = time.time()
    # Heuristiques: textarea visible OU bouton Publier activable
    while time.time() - start < max_wait_sec:
        try:
            if page.locator("textarea").first.is_visible():
                return
        except Exception:
            pass
        try:
            btn = _find_publish_button(page)
            if btn and btn.is_enabled():
                return
        except Exception:
            pass
        time.sleep(1)
    warn("Traitement trop long — on tente la suite quand même.")


# ------------------------------
# Flux publication
# ------------------------------
def publish_once(pw, video_abs: str) -> bool:
    browser = pw.chromium.launch(
        headless=True,  # passe à False si tu veux voir
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context = browser.new_context(
        user_agent=UA_RAW or None,
        viewport={"width": 1440, "height": 900},
    )

    page = context.new_page()

    # Cookies
    cookies = parse_cookies(COOKIE_RAW)
    if not cookies:
        err("Impossible de parser TIKTOK_COOKIE (aucun cookie autorisé/valide trouvé).")
        browser.close()
        return False

    try:
        context.add_cookies(cookies)
    except Exception as e:
        err(f"Injection cookies: {e}")
        browser.close()
        return False
    log(f"Injection cookies ({len(cookies)} entrées)…")

    # Upload page
    UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
    log("Navigation vers TikTok Studio Upload")
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)

    # Attendre que l’upload UI soit prête
    try:
        _wait_upload_ready(page, timeout_ms=30000)
    except Exception as e:
        err(str(e))
        browser.close()
        return False

    # Donner la vidéo
    _set_video(page, video_abs)

    # Attendre la fin du traitement
    _wait_upload_finish(page, max_wait_sec=180)

    # Légende (optionnelle)
    try:
        t = page.locator("textarea").first
        if t.is_visible():
            t.fill(CAPTION_TEXT)
            log("Légende insérée ✅")
        else:
            warn("Impossible de remplir la légende (textarea non trouvé).")
    except Exception:
        warn("Impossible de remplir la légende (textarea non trouvé).")

    if DRY_RUN:
        warn("DRY_RUN=True : je n’appuie pas sur Publier.")
        browser.close()
        return True

    # Publier
    try:
        publish_now(page)
    except Exception as e:
        err(f"Échec de publication: {e}")
        browser.close()
        return False

    # Petit délai pour laisser partir la requête/transition
    page.wait_for_timeout(3000)

    browser.close()
    return True


# ------------------------------
# Entrée principale
# ------------------------------
def main():
    log(f"Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    video_abs = str(Path(VIDEO_PATH).resolve())
    if not Path(video_abs).exists():
        err(f"Fichier vidéo introuvable: {video_abs}")
        sys.exit(1)

    with sync_playwright() as pw:
        ok_count = 0
        for i in range(POSTS_TO_PUBLISH):
            print(f"— Post {i+1}/{POSTS_TO_PUBLISH} —")
            ok = publish_once(pw, video_abs)
            ok_count += 1 if ok else 0

        log(f"Run terminé ✅ ({ok_count}/{POSTS_TO_PUBLISH} réussis)")
        sys.exit(0 if ok_count == POSTS_TO_PUBLISH else 1)


if __name__ == "__main__":
    main()
