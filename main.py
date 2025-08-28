import os
import json
import asyncio
from playwright.async_api import async_playwright

# ---------- Utilitaires ----------
def normalize_cookie(cookie):
    """
    Corrige automatiquement les champs des cookies pour éviter les erreurs Playwright.
    """
    # Forcer le domaine
    if not cookie.get("domain", "").startswith("."):
        cookie["domain"] = f".{cookie['domain']}"

    # Normaliser sameSite
    if "sameSite" in cookie:
        val = str(cookie["sameSite"]).capitalize()
        if val not in ["Lax", "Strict", "None"]:
            val = "Lax"
        cookie["sameSite"] = val
    else:
        cookie["sameSite"] = "Lax"

    # Corriger secure si sameSite=None
    if cookie["sameSite"] == "None":
        cookie["secure"] = True

    # Vérifier expires (doit être un entier ou absent)
    if "expires" in cookie:
        try:
            cookie["expires"] = int(cookie["expires"])
        except:
            cookie.pop("expires", None)

    return cookie

async def inject_cookies(context):
    raw = os.getenv("TIKTOK_COOKIE", "[]")
    try:
        cookies = json.loads(raw)
        cookies = [normalize_cookie(c) for c in cookies]
        await context.add_cookies(cookies)
        print(f"✅ {len(cookies)} cookies injectés avec succès")
    except Exception as e:
        print("❌ Erreur lors de l'injection des cookies:", e)
        raise

# ---------- Publication ----------
async def publish_once(video_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # Injecter cookies
        await inject_cookies(context)

        page = await context.new_page()
        await page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000)

        print(f"📂 Upload vidéo : {video_path}")
        input_file = await page.wait_for_selector('input[type="file"]', timeout=30000)
        await input_file.set_input_files(video_path)

        # Attendre un peu pour simuler l'upload
        await page.wait_for_timeout(10000)

        # Cliquer sur Publier
        publier_btn = await page.wait_for_selector('button:has-text("Publier")', timeout=30000)
        await publier_btn.click()

        print("🚀 Vidéo publiée avec succès (simulation)")

        await browser.close()

# ---------- Main ----------
async def main():
    video_path = os.getenv("VIDEO_PATH", "assets/test.mp4")
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Vidéo introuvable : {video_path}")

    await publish_once(video_path)

if __name__ == "__main__":
    asyncio.run(main())
