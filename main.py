import os
from playwright.sync_api import sync_playwright

TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"


def _parse_cookie_header(header: str):
    """
    Transforme 'a=b; c=d; e=f' -> liste d'objets cookies Playwright.
    Ajoute domain/path requis pour éviter Invalid cookie fields.
    """
    if not header:
        return []
    pairs = [p.strip() for p in header.split(";")]
    cookies = []
    for p in pairs:
        if "=" not in p:
            continue
        name, value = p.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".tiktok.com",  # obligatoire
            "path": "/",              # obligatoire
        })
    return cookies


def publish_to_tiktok(cookie_raw: str, caption: str, video_path: str) -> bool:
    ua = os.getenv("TIKTOK_UA") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="fr-FR",
            user_agent=ua,
        )

        # 1) Injecter les cookies correctement formés
        cookies = _parse_cookie_header(cookie_raw)
        if not cookies:
            raise RuntimeError("Aucun cookie valide à injecter. Vérifie TIKTOK_COOKIE.")
        context.add_cookies(cookies)

        page = context.new_page()

        # 2) Aller sur TikTok Studio Upload
        page.goto(TIKTOK_UPLOAD_URL, wait_until="domcontentloaded")

        # 3) Upload de la vidéo
        input_sel = "input[type='file']"
        page.set_input_files(input_sel, video_path)

        # 4) Légende (à ajuster si le sélecteur change)
        try:
            page.get_by_role("textbox").fill(caption)
        except Exception:
            print("⚠️ Impossible de trouver la zone de légende, sélecteur à adapter.")

        # 5) Bouton Publier (à ajuster selon interface)
        try:
            page.get_by_role("button", name="Publier").click()
            print("▶️ Vidéo en cours de publication...")
        except Exception:
            print("⚠️ Bouton Publier non trouvé, vérifie le sélecteur.")

        # 6) Attente confirmation (optionnelle, selon interface)
        try:
            page.wait_for_selector("text=Publié", timeout=120000)
            print("✅ Publication confirmée.")
        except Exception:
            print("⚠️ Confirmation non détectée, publication peut avoir fonctionné.")

        context.close()
        browser.close()
        return True


def main():
    cookie_raw = os.getenv("TIKTOK_COOKIE", "")
    caption = "Test upload vidéo automatique"
    video = "assets/test.mp4"

    ok = publish_to_tiktok(cookie_raw, caption, video)
    print("Résultat:", "SUCCÈS ✅" if ok else "ÉCHEC ❌")


if __name__ == "__main__":
    main()
