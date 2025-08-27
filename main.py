# données/main.py
import os
import string
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"

# Cookies vraiment utiles pour une session TikTok web
ALLOW_COOKIES = {
    "sessionid", "sessionid_ss",
    "sid_tt", "sid_guard",
    "sid_ucp_v1", "ssid_ucp_v1",
    "uid_tt", "uid_tt_ss",
    "msToken", "odin_tt",
    "s_v_web_id", "ttwid",
    "tt_csrf_token", "cmpl_token",
    # ces deux-là ne sont pas toujours requis, mais utiles
    "multi_sids", "last_login_method",
}


def clean_user_agent(raw: str) -> str:
    """Nettoie un UA pour Playwright (pas de guillemets, pas de \n/\r, que des imprimables)."""
    if not raw:
        return ""
    # strip espaces et guillemets
    ua = raw.strip().strip('"').strip("'")
    # remplace \r\n par espace
    ua = ua.replace("\r", " ").replace("\n", " ")
    # ne garder que les caractères imprimables
    allowed = set(string.printable)
    ua = "".join(ch if ch in allowed else " " for ch in ua)
    # normaliser espaces multiples
    ua = " ".join(ua.split())
    return ua


def parse_cookie_header(header: str):
    """
    Transforme 'a=b; c=d; ...' en objets cookies Playwright valides.
    - ignore les paires sans '=' ou nom vide
    - filtre sur ALLOW_COOKIES (case-insensitive)
    - ajoute domain/path requis
    - duplique pour tiktok.com et www.tiktok.com
    """
    if not header:
        return []

    pairs = [p.strip() for p in header.split(";") if p.strip()]
    items = []
    for p in pairs:
        if "=" not in p:
            continue
        name, value = p.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if name.lower() not in {n.lower() for n in ALLOW_COOKIES}:
            continue

        # deux domaines pour couvrir tous les sous-domaines Studio
        items.append({"name": name, "value": value, "domain": ".tiktok.com", "path": "/"})
        items.append({"name": name, "value": value, "domain": "www.tiktok.com", "path": "/"})

    # dédoublonne (même name/domain/path)
    uniq = {}
    for c in items:
        uniq[(c["name"], c["domain"], c["path"])] = c
    return list(uniq.values())


def publish_to_tiktok(cookie_raw: str, caption: str, video_path: str, ua_raw: str) -> bool:
    ua = clean_user_agent(ua_raw) or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )

    cookies = parse_cookie_header(cookie_raw)
    if not cookies:
        raise RuntimeError(
            "Aucun cookie valide n’a été trouvé. "
            "Copie/colle la ligne 'cookie:' complète de la requête 'upload' (onglet Network) "
            "dans le secret TIKTOK_COOKIE."
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(locale="fr-FR", user_agent=ua)
        context.add_cookies(cookies)

        page = context.new_page()
        page.goto(TIKTOK_UPLOAD_URL, wait_until="domcontentloaded")

        # si on est redirigé vers la connexion, la session n’est pas valide
        if "login" in page.url or "signin" in page.url.lower():
            raise RuntimeError("Session non valide (redirigé vers la connexion). Cookies expirés/invalides.")

        # 1) Upload
        file_input = "input[type='file']"
        page.set_input_files(file_input, video_path)

        # 2) Légende (sélecteurs possibles — l’un des deux passera suivant les versions UI)
        caption_filled = False
        for sel in [
            '[data-e2e="video-caption-input"] textarea',
            'textarea[placeholder*="Ajouter"]',
            'div[contenteditable="true"]',
        ]:
            try:
                page.wait_for_selector(sel, timeout=15000)
                page.fill(sel, caption)
                caption_filled = True
                break
            except PWTimeout:
                continue
            except Exception:
                continue

        if not caption_filled:
            print("⚠️ Zone de légende introuvable — la publication peut quand même fonctionner.")

        # 3) Publier
        published = False
        for sel in [
            'button:has-text("Publier")',
            'button:has-text("Post")',
            '[data-e2e="publish-button"]',
        ]:
            try:
                page.click(sel, timeout=20000)
                published = True
                break
            except Exception:
                continue

        if not published:
            raise RuntimeError("Bouton 'Publier' introuvable. Interface changée ?")

        # 4) Confirmation (best-effort)
        try:
            page.wait_for_selector("text=Publié", timeout=120000)
            print("✅ Publication confirmée.")
        except PWTimeout:
            print("ℹ️ Confirmation non détectée — la publication peut quand même être envoyée.")

        context.close()
        browser.close()
        return True


def main():
    cookie_raw = os.getenv("TIKTOK_COOKIE", "")
    ua_raw = os.getenv("TIKTOK_UA", "")
    caption = "Test upload vidéo automatique"
    video = "assets/test.mp4"

    if not os.path.exists(video):
        raise FileNotFoundError(f"Vidéo introuvable: {video}")

    ok = publish_to_tiktok(cookie_raw, caption, video, ua_raw)
    print("Résultat:", "SUCCÈS ✅" if ok else "ÉCHEC ❌")


if __name__ == "__main__":
    main()
