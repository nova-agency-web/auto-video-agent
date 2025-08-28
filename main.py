# main.py
import os
import json
import asyncio
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# --------- Configuration minimale ----------
VIDEO_PATH = "assets/videoplayback.mp4"
TIKTOK_STUDIO_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"
NAV_TIMEOUT = 60_000  # 60s
STEP_PAUSE = 500      # petites pauses entre actions (ms)


def log(*a):
    print(*a, flush=True)


# ---------- Cookies helpers ----------
def _cookie_url_for_domain(domain: str) -> str:
    d = domain.lstrip(".")
    # TikTok emploie tiktok.com ; https obligatoire pour les cookies "secure"
    return f"https://{d}"

def _normalize_cookie(c: Dict[str, Any]) -> Dict[str, Any]:
    # Playwright attend: name, value, url OU domain/path, expires(optional), httpOnly, secure, sameSite
    out = {
        "name": c["name"],
        "value": c["value"],
        "url": _cookie_url_for_domain(c.get("domain", "tiktok.com")),
    }
    if "expires" in c and c["expires"]:
        out["expires"] = int(c["expires"])
    if "httpOnly" in c:
        out["httpOnly"] = bool(c["httpOnly"])
    if "secure" in c:
        out["secure"] = bool(c["secure"])
    if "sameSite" in c and c["sameSite"]:
        s = c["sameSite"].lower()
        if s in ("lax", "strict", "none"):
            out["sameSite"] = s  # playwright accepte "lax/strict/none"
    return out

def load_cookies_from_env() -> List[Dict[str, Any]]:
    raw = os.getenv("TIKTOK_COOKIE", "").strip()
    if not raw:
        raise RuntimeError("TIKTOK_COOKIE est vide : fournis un tableau JSON de cookies.")
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("TIKTOK_COOKIE doit être un tableau JSON.")
        return [_normalize_cookie(c) for c in data if c.get("name") and c.get("value")]
    except Exception as e:
        raise RuntimeError(f"Impossible de parser TIKTOK_COOKIE : {e}")

def get_user_agent() -> str:
    ua = os.getenv("TIKTOK_UA", "").strip()
    if ua:
        return ua
    # UA par défaut type Chrome macOS récent
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )


# ---------- UI helpers ----------
async def tiny_pause(page, ms: int = STEP_PAUSE):
    await page.wait_for_timeout(ms)

async def ensure_visible_file_input(page) -> Optional[str]:
    """
    Sur certaines versions, l'input peut être masqué par un overlay.
    On le rend visible via JS puis on renvoie le sélecteur.
    """
    sel = "input[type='file']"
    try:
        # essaie direct
        loc = page.locator(sel).first
        if await loc.count() > 0:
            # force visible si caché
            await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (el) {
                        el.style.display = 'block';
                        el.style.visibility = 'visible';
                        el.removeAttribute('hidden');
                    }
                }""",
                sel,
            )
            return sel
    except Exception:
        pass
    return None


async def click_compliance_checkboxes(page):
    """
    Coche les cases de conformité si présentes (certaines versions l’exigent).
    On clique tous les inputs visibles non cochés dans la zone publication.
    """
    try:
        # Checkboxes visibles et non cochées
        boxes = page.locator("input[type='checkbox']:not(:checked)")
        n = await boxes.count()
        for i in range(n):
            try:
                b = boxes.nth(i)
                if await b.is_visible():
                    await b.scroll_into_view_if_needed()
                    await b.click(force=True)
                    await tiny_pause(page, 200)
            except Exception:
                continue
    except Exception:
        pass


async def find_publish_button(page):
    """
    Renvoie un locator candidat pour le bouton "Publier".
    Plusieurs sélecteurs possibles, on les teste dans l'ordre.
    """
    candidates = [
        "button:has-text('Publier')",
        "button[aria-label*='Publier']",
        "button:has-text('Post')",
        "[data-e2e*='publish'] button",
        "[data-e2e*='publish']",
        "button.tux-btn-primary:has-text('Publier')",
    ]
    for css in candidates:
        loc = page.locator(css)
        if await loc.count() > 0:
            return loc.first
    return None


async def publish_now(page) -> bool:
    """
    Fait défiler, coche les cases, attend que 'Publier' devienne cliquable, puis clique.
    """
    log("[INFO] Tentative de publication…")

    await click_compliance_checkboxes(page)

    # On essaie jusqu’à 120 cycles (~1 min) de voir un bouton cliquable
    for attempt in range(120):
        btn = await find_publish_button(page)
        if btn:
            try:
                await btn.scroll_into_view_if_needed()
                disabled_attr = await btn.get_attribute("disabled")
                is_enabled = await btn.is_enabled()
                is_visible = await btn.is_visible()
                if is_visible and is_enabled and not disabled_attr:
                    await btn.click()
                    log("[INFO] Clic sur 'Publier' ✅")
                    return True
            except Exception:
                pass

        await tiny_pause(page, 500)

        # Re-coche si de nouvelles cases apparaissent
        await click_compliance_checkboxes(page)

    log("[WARN] Bouton 'Publier' non cliquable après délai.")
    return False


async def wait_until_video_loaded(page, max_wait_s: int = 90):
    """
    Attends des indices que l’upload/processing est au moins reconnu.
    On se contente d’un délai progressif + vérifs de petites UI, sans bloquer indéfiniment.
    """
    waited = 0
    while waited < max_wait_s:
        # Divers petits signaux côté Studio (indépendants de la locale)
        selectors = [
            "video",  # un aperçu apparait parfois
            "canvas",  # preview
            "text=Couverture",  # (FR)
            "text=Cover",       # (EN)
        ]
        try:
            for s in selectors:
                if await page.locator(s).first.count() > 0:
                    return True
        except Exception:
            pass

        await tiny_pause(page, 1000)
        waited += 1
    return False


async def upload_and_publish(page, video_abs: str) -> bool:
    log("[INFO] Navigation vers TikTok Studio Upload")
    await page.goto(TIKTOK_STUDIO_UPLOAD, timeout=NAV_TIMEOUT)
    await tiny_pause(page, 1000)

    # Tente de fermer les bannières cookies si besoin
    try:
        # boutons possibles
        for t in ["Tout accepter", "Accepter tout", "I agree", "Accept all"]:
            loc = page.get_by_role("button", name=t)
            if await loc.count() > 0:
                await loc.click()
                await tiny_pause(page, 500)
                break
    except Exception:
        pass

    log("[INFO] Recherche du champ fichier…")
    sel = await ensure_visible_file_input(page)
    if not sel:
        raise RuntimeError("Impossible de localiser un input file visible pour l’upload.")

    # Déclenchement upload
    await page.set_input_files(sel, video_abs)
    log("[INFO] Upload déclenché ✅")
    await tiny_pause(page, 1500)

    # (Optionnel) Remplir la légende si un textarea est présent
    try:
        ta = page.locator("textarea").first
        if await ta.count() > 0 and await ta.is_visible():
            await ta.fill("Vidéo postée automatiquement 🚀")
            log("[INFO] Légende ajoutée")
    except Exception:
        log("[WARN] Impossible de remplir la légende (textarea non trouvé).")

    # Attends un minimum que l'UI reconnaisse la vidéo
    _ = await wait_until_video_loaded(page, max_wait_s=90)

    # Publier
    ok = await publish_now(page)
    return ok


async def run():
    # Vérifs de base
    if not os.path.exists(VIDEO_PATH):
        raise FileNotFoundError(f"Vidéo introuvable : {VIDEO_PATH}")

    cookies = load_cookies_from_env()
    ua = get_user_agent()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=ua,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
        )
        # Injection des cookies AVANT d’ouvrir la page
        try:
            await context.add_cookies(cookies)
            log(f"[INFO] Injection cookies ({len(cookies)} entrées)…")
        except Exception as e:
            log(f"[ERREUR] Cookies invalides : {e}")
            await browser.close()
            raise

        page = await context.new_page()

        ok = await upload_and_publish(page, os.path.abspath(VIDEO_PATH))

        # Laisse quelques secondes pour laisser partir la requête publication
        await tiny_pause(page, 3000)

        await context.close()
        await browser.close()

        if ok:
            log("[INFO] Publication envoyée (ou en file d’attente) ✅")
        else:
            log("[WARN] Publication non confirmée.")

if __name__ == "__main__":
    asyncio.run(run())
