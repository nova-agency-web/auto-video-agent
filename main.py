import asyncio
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ------------ Configuration simple ------------
ASSETS_DIR = Path("assets")
# Priorité 1 : variable VIDEO_PATH (ex: assets/test.mp4)
# Priorité 2 : premier .mp4 dans assets/
VIDEO_PATH = os.getenv("VIDEO_PATH", "").strip()
CAPTION_TEXT = os.getenv("CAPTION_TEXT", "").strip()  # optionnel
TARGET_URL = "https://www.tiktok.com/tiktokstudio/upload"
HEADLESS = True  # mettre False pour debug local (pas en Actions)

# ------------ Helpers logs ------------
def info(msg: str) -> None:
    print(f"***INFO*** {msg}")

def warn(msg: str) -> None:
    print(f"***WARN*** {msg}")

def err(msg: str) -> None:
    print(f"***ERREUR*** {msg}")

# ------------ Cookies ------------
def _coerce_expires(v: Any) -> Optional[int]:
    """
    Playwright attend un int (seconds since epoch) ou rien.
    Si v est falsy / non numérique -> None (on supprime la clé).
    """
    if v is None:
        return None
    try:
        # Certains exports donnent des strings
        ival = int(v)
        # S'il ressemble à des ms (trop grand), on divise
        if ival > 2_000_000_000_000:
            ival = ival // 1000
        return ival
    except Exception:
        return None

def parse_cookie_env(raw: str) -> List[Dict[str, Any]]:
    """
    Accepte :
    - JSON array de cookies [{name, value, domain, ...}]
    - OU un blob { "cookies": [...] }
    - OU une string "name=value; name2=value2" (fallback minimal)
    """
    cookies: List[Dict[str, Any]] = []
    if not raw:
        return cookies

    raw = raw.strip()
    # JSON ?
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
            arr = data["cookies"] if isinstance(data, dict) and "cookies" in data else data
            for c in arr:
                if not c.get("name") or not c.get("value"):
                    continue
                cookie = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain") or ".tiktok.com",
                    "path": c.get("path") or "/",
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", True)),
                }
                # sameSite
                same = (c.get("sameSite") or "").lower()
                if same in ("lax", "strict", "none"):
                    cookie["sameSite"] = same  # type: ignore

                exp = _coerce_expires(c.get("expires"))
                if exp is not None:
                    cookie["expires"] = exp  # type: ignore

                cookies.append(cookie)
        except Exception as e:
            err(f"Impossible de parser TIKTOK_COOKIE JSON: {e}")

    else:
        # Fallback "name=value; name2=value2"
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        for p in parts:
            if "=" not in p:
                continue
            name, value = p.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".tiktok.com",
                "path": "/",
                "secure": True,
            })

    return cookies

async def inject_cookies(context, cookies: List[Dict[str, Any]]) -> int:
    if not cookies:
        warn("Aucun cookie fourni.")
        return 0

    accepted = []
    ignored = []
    for c in cookies:
        # Filtre minimal
        if not c.get("name") or not c.get("value"):
            ignored.append(c)
            continue
        # expires déjà nettoyé dans parse_cookie_env
        accepted.append(c)

    if not accepted:
        warn("Aucun cookie valide après nettoyage.")
        return 0

    await context.add_cookies(accepted)
    info(f"Injection cookies ({len(accepted)})…")
    if ignored:
        warn(f"Cookies ignorés ({len(ignored)}) (clés manquantes).")
    return len(accepted)

# ------------ Upload helpers ------------
async def try_input_upload(page, video_abs: str, timeout_ms: int = 45000) -> bool:
    """
    Cherche un input[type=file] acceptant la vidéo (visible ou caché), et upload.
    """
    selectors = [
        "input[type='file'][accept*='video']",
        "input[type='file']",
    ]
    for sel in selectors:
        try:
            input_el = page.locator(sel).first
            await input_el.set_input_files(video_abs, timeout=timeout_ms)
            info("Upload (input direct) déclenché ✅")
            return True
        except PWTimeout:
            # Le locator n'a pas accepté dans les temps -> on tente le suivant
            continue
        except Exception:
            continue
    return False

async def try_filechooser_upload(page, video_abs: str, timeout_ms: int = 45000) -> bool:
    """
    Click sur un bouton qui ouvre un file chooser (avec page.wait_for_event('filechooser')).
    On cible quelques libellés typiques de TikTok Studio.
    """
    labels = [
        "button:has-text('+ Importer')",
        "button:has-text('Importer')",
        "button:has-text('Upload')",
        "button:has-text('Select file')",
        "[data-e2e='upload-button']",
        "[data-e2e='sidebar-upload']",
    ]
    for lab in labels:
        try:
            async with page.expect_file_chooser(timeout=timeout_ms) as fc:
                await page.locator(lab).first.click()
            chooser = await fc.value
            await chooser.set_files(video_abs)
            info("Upload via file chooser déclenché ✅")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    return False

async def wait_processing_and_optionals(page) -> None:
    """
    Attend un minimum que l’UI apparaisse et tente d’insérer la légende.
    Pas bloquant : on ne lève pas d’exception.
    """
    # Attendre que l’éditeur se stabilise un peu
    try:
        await page.wait_for_timeout(1500)
        # Légende
        if CAPTION_TEXT:
            # plusieurs tentatives de zones de texte
            candidates = [
                "textarea",
                "[role='textbox']",
                "div[contenteditable='true']",
            ]
            for sel in candidates:
                try:
                    ta = page.locator(sel).first
                    await ta.fill(CAPTION_TEXT, timeout=2000)
                    info("Légende insérée ✅")
                    break
                except Exception:
                    continue
    except Exception:
        pass

# ------------ Main ------------
async def main() -> None:
    # --------- tracer toujours ----------
    trace_path = "trace.zip"

    # --------- détecter la vidéo ----------
    video_abs = ""
    if VIDEO_PATH:
        vp = Path(VIDEO_PATH)
        if vp.exists():
            video_abs = str(vp.resolve())
    if not video_abs:
        mp4s = sorted(ASSETS_DIR.glob("*.mp4"))
        if mp4s:
            video_abs = str(mp4s[0].resolve())

    if not video_abs:
        raise FileNotFoundError(f"Vidéo introuvable ! (VIDEO_PATH/ assets/*.mp4)")

    info(f"Vidéo cible : {video_abs}")

    cookie_raw = os.getenv("TIKTOK_COOKIE", "")
    ua = os.getenv("TIKTOK_UA", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context_args: Dict[str, Any] = {}
        if ua:
            context_args["user_agent"] = ua

        context = await browser.new_context(**context_args)

        # Trace on (record everything)
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = await context.new_page()

        try:
            # TikTok a besoin de cookies posés avant navigation
            cookies = parse_cookie_env(cookie_raw)
            if cookies:
                # On doit avoir un contexte attaché à un domaine pour add_cookies ? Non avec Playwright on peut add direct.
                await inject_cookies(context, cookies)
            else:
                warn("Aucun cookie injecté -> session risque d’être non loguée.")

            info(f"NAV >>> {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

            # Essayer upload par input direct
            ok = await try_input_upload(page, video_abs)
            if not ok:
                # fallback : ouvrir un file chooser
                ok = await try_filechooser_upload(page, video_abs)

            if not ok:
                err("Bouton pour ouvrir le sélecteur de fichiers introuvable.")
                return

            await wait_processing_and_optionals(page)

            # Ici on laisse le traitement quelques secondes (selon ton flux réel)
            await page.wait_for_timeout(4000)

            # Si tu veux tenter de publier automatiquement plus tard,
            # tu pourras ajouter une fonction publish_now(page) et l’appeler ici.

        finally:
            # Sauvegarde du trace quoi qu'il arrive
            try:
                await context.tracing.stop(path=trace_path)
                info("Trace Playwright sauvegardée ➜ trace.zip")
            except Exception as e:
                warn(f"Impossible de sauvegarder le trace: {e}")
            await context.close()
            await browser.close()

# Runner
if __name__ == "__main__":
    try:
        asyncio.run(main())
        info("Run terminé ✅")
    except Exception as e:
        err(f"Process terminé avec erreur: {e}")
        # IMPORTANT: écrire tout de même un trace vide si jamais
        if not Path("trace.zip").exists():
            try:
                with open("trace.zip", "wb") as f:
                    pass
            except Exception:
                pass
        # code de sortie non-zero pour signaler l'échec au workflow
        raise
