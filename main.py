# main.py
# --- Agent TikTok Studio : upload + légende + publication ---
# Utilise les cookies collés dans le secret GitHub TIKTOK_COOKIE
# et (optionnel) un User-Agent via TIKTOK_UA.

import os
import re
import json
from typing import Dict, List
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# -------------------------
# Config via variables d'env
# -------------------------
ACCOUNT          = os.getenv("ACCOUNT", "trucs→malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN          = os.getenv("DRY_RUN", "FALSE").strip().lower() in ("1","true","yes")
COOKIE_RAW       = os.getenv("TIKTOK_COOKIE", "").strip()
UA_RAW           = os.getenv("TIKTOK_UA", "").strip()

# Vidéo & légende : adapte si besoin
VIDEO_PATH       = os.getenv("VIDEO_PATH", "assets/test.mp4")
CAPTION_TEXT     = os.getenv("CAPTION_TEXT", "Test upload vidéo automatique")

UPLOAD_URL       = "https://www.tiktok.com/tiktokstudio/upload"

# Les clés cookies qu’on accepte d’injecter (évite l’erreur “Invalid cookie fields”)
COOKIE_WHITELIST = {
    "sessionid", "sessionid_ss", "sid_guard", "sid_tt",
    "msToken", "odin_tt", "s_v_web_id"
}

# --------------------------------
# Helpers : logs et parsing cookies
# --------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)

def parse_cookie_string(cookie_str: str) -> List[Dict]:
    """
    Transforme "a=1; b=2; ..." -> [{name:"a", value:"1", domain:".tiktok.com", path:"/"}, ...]
    On ne garde que les clés whiteliste.
    """
    cookies = []
    if not cookie_str:
        return cookies

    # Scinde par ';' mais conserve les valeurs contenant '='
    parts = [p.strip() for p in cookie_str.split(";") if p.strip()]
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name in COOKIE_WHITELIST:
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".tiktok.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            })
    return cookies

def robust_get_by_text(page, text_regex: str, role: str = "button"):
    """
    Renvoie le premier locator par rôle + texte (regex, insensible à la casse).
    """
    return page.get_by_role(role, name=re.compile(text_regex, re.I))

# --------------
# Workflow agent
# --------------
def publish_to_tiktok(cookie_raw: str, caption: str, video_path: str, ua_raw: str) -> bool:
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Fichier vidéo introuvable : {video_path}")

    cookies = parse_cookie_string(cookie_raw)
    if not cookies:
        raise ValueError("Aucun cookie valide à injecter (vérifie le secret TIKTOK_COOKIE).")

    user_agent = ua_raw.strip() if ua_raw else (
        # UA par défaut : Chrome 128 macOS (ok côté TikTok au 27/08/2025)
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        context = browser.new_context(user_agent=user_agent, locale="fr-FR", timezone_id="Europe/Paris")
        page = context.new_page()

        # Injecte cookies AVANT navigation
        log(f"[LOGIN] Injection cookies ({len(cookies)} entrées)…")
        context.add_cookies(cookies)

        # Navigation
        log(f"[NAV] Vers {UPLOAD_URL}")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=120_000)

        # Assure-toi qu’on est bien loggé (présence d’un élément studio)
        try:
            page.wait_for_url(re.compile(r"tiktokstudio"), timeout=30_000)
        except PlaywrightTimeoutError:
            log("[AVERT] URL studio non confirmée, on continue quand même…")

        # Parfois TikTok affiche des popups (autoriser cookies, etc.)
        try:
            robust_get_by_text(page, r"(Tout accepter|Accepter|OK|J’accepte)").first.click(timeout=3_000)
        except Exception:
            pass

        # ----- Upload -----
        log(f"[UPLOAD] Sélection champ fichier…")
        file_input = page.locator("input[type='file']").first
        try:
            file_input.wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeoutError:
            # fallback : cherche une autre frame ou un input masqué
            inputs = page.locator("input[type='file']")
            if inputs.count() == 0:
                raise RuntimeError("Impossible de localiser le champ fichier (input[type='file']).")
            file_input = inputs.first

        log(f"[UPLOAD] Fichier : {video_path}")
        file_input.set_input_files(video_path)

        # Attends l’upload (TikTok n’a pas toujours un indicateur stable)
        # On attend la miniature / prévisualisation ou on temporise.
        uploaded_ok = False
        for _ in range(10):
            page.wait_for_timeout(2000)
            # Heuristiques : présence d’un thumbnail ou d’un titre auto
            if page.locator("video, img").first.is_visible(timeout=0):
                uploaded_ok = True
                break
        if not uploaded_ok:
            log("[AVERT] Aucun indicateur 'upload terminé' détecté (on continue).")

        # ----- Légende -----
        log("[CAPTION] Insertion légende…")
        # Plusieurs variantes possibles :
        caption_locators = [
            "textarea[placeholder*='légende' i]",
            "textarea[placeholder*='Caption' i]",
            "[data-e2e='caption'] textarea",
            "textarea"
        ]
        caption_set = False
        for sel in caption_locators:
            try:
                t = page.locator(sel).first
                if t.count() > 0:
                    t.click(timeout=2000)
                    t.fill(caption)
                    caption_set = True
                    break
            except Exception:
                continue
        if not caption_set:
            log("[AVERT] Avertissement : impossible de remplir la légende (textarea non trouvé).")

        # ----- Publier -----
        btn = None
        for pattern in (r"\bPublier\b", r"\bPublish\b"):
            candidate = robust_get_by_text(page, pattern, role="button")
            if candidate.count() > 0:
                btn = candidate.first
                break

        if not btn:
            log("[ERREUR] Bouton 'Publier' introuvable.")
            return False

        if DRY_RUN:
            log("[DRY-RUN] Simulation activée → on ne clique PAS sur Publier.")
            browser.close()
            return True

        log("[ACTION] Clic sur 'Publier'…")
        try:
            btn.click(timeout=10_000, force=True)
        except Exception:
            # dernier recours : click JS
            page.evaluate("(el) => el.click()", btn)

        # Confirmation (heuristique : message de succès, changement d’URL, etc.)
        success = False
        for _ in range(15):
            page.wait_for_timeout(1000)
            # Cherche message de confirmation ou un état "posté"
            if page.locator("text=Publié").first.count() > 0:
                success = True
                break
            # parfois l’UI n’affiche pas de toast ; on checke l’absence de bloc upload
            if page.locator("input[type='file']").count() == 0:
                success = True
                break

        log("[OK] Publication confirmée ✅" if success else "[ERREUR] Publication non confirmée ❌")

        browser.close()
        return success


def main():
    log(f"[INFO] Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    if not COOKIE_RAW:
        raise SystemExit("Secret TIKTOK_COOKIE manquant. Renseigne-le puis relance.")

    for i in range(POSTS_TO_PUBLISH):
        log(f"—— Post {i+1}/{POSTS_TO_PUBLISH} ——")
        try:
            ok = publish_to_tiktok(COOKIE_RAW, CAPTION_TEXT, VIDEO_PATH, UA_RAW)
            if not ok:
                raise RuntimeError("Publication échouée.")
        except Exception as e:
            log(f"[ERREUR] {type(e).__name__}: {e}")
            raise
    log("Run terminé ✅")


if __name__ == "__main__":
    main()
