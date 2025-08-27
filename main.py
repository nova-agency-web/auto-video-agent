import os, sys, random
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DATA_DIR = "data"
SCRIPTS = os.path.join(DATA_DIR, "scripts.csv")
PRODUCTS = os.path.join(DATA_DIR, "products.csv")
SCHEDULE = os.path.join(DATA_DIR, "schedule.csv")

ACCOUNT = os.getenv("ACCOUNT", "trucs-malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
TIKTOK_COOKIE = os.getenv("TIKTOK_COOKIE", "").strip()

def log(msg): 
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def need_exit(msg, code=1):
    log(f"ERREUR: {msg}")
    sys.exit(code)

def load_csv(path):
    if not os.path.exists(path):
        need_exit(f"fichier introuvable: {path}")
    return pd.read_csv(path)

def pick_scripts(df_scripts, df_products, account, n):
    if "account_bucket" in df_scripts.columns:
        pool = df_scripts[df_scripts["account_bucket"].fillna("").str.contains(account, na=False)]
        if pool.empty: pool = df_scripts
    else:
        pool = df_scripts
    if "product_slug" in pool.columns and "product_slug" in df_products.columns:
        pool = pool.merge(df_products[["product_slug","affiliate_url"]], on="product_slug", how="left")
    pool = pool.sample(min(n, len(pool)), random_state=random.randint(1, 999999))
    return pool.to_dict(orient="records")

def build_caption(item):
    texte = str(item.get("texte","")).strip()
    cta = str(item.get("cta","")).strip()
    tags = str(item.get("hashtags","")).strip()
    link = str(item.get("affiliate_url","")).strip()
    parts = [texte]
    if cta: parts.append(cta)
    if link and "REMPLACER_PAR_VOTRE_LIEN" not in link: parts.append(link)
    if tags: parts.append(tags)
    return " ".join([p for p in parts if p])[:2100]

def resolve_video_path(item):
    path = str(item.get("source_broll","")).strip()
    if path and os.path.exists(path): return path
    fallback = "assets/test.mp4"
    if not os.path.exists(fallback):
        need_exit("Aucune vidéo trouvée. Placez un MP4 dans assets/test.mp4 ou renseignez source_broll.")
    return fallback

# -------- Publication TikTok via Playwright (cookie sessionid) --------
def publish_to_tiktok(cookie_sessionid: str, caption: str, video_path: str) -> bool:
    if not cookie_sessionid:
        need_exit("TIKTOK_COOKIE manquant (valeur du cookie 'sessionid').")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        # injecte la session
        context.add_cookies([{
            "name": "sessionid",
            "value": cookie_sessionid,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }])

        page = context.new_page()
        log("Ouverture de la page d'upload…")
        page.goto("https://www.tiktok.com/upload?lang=fr", wait_until="domcontentloaded", timeout=60000)

        # vérifie qu'on est connecté
        try:
            page.wait_for_selector('[data-e2e="upload-right-panel"]', timeout=15000)
        except PWTimeout:
            browser.close()
            need_exit("Non connecté. Cookie invalide/expiré. Reprenez 'sessionid' dans DevTools.")

        # upload vidéo
        log(f"Upload de la vidéo: {video_path}")
        file_input = page.locator('input[type="file"]')
        file_input.set_input_files(video_path)

        # attendre préparation & bouton actif
        try:
            page.wait_for_selector('[data-e2e="caption"]', timeout=90000)
            page.wait_for_selector('[data-e2e="post-button"]:not([disabled])', timeout=300000)
        except PWTimeout:
            log("Traitement long, tentative de saisie quand même.")

        # légende
        try:
            cap = page.locator('[data-e2e="caption"] textarea')
            if not cap.count():
                cap = page.locator('textarea[placeholder*="Légende"]').first
            cap.fill(caption)
            log("Légende insérée.")
        except Exception as e:
            log(f"Impossible d’insérer la légende proprement ({e}).")

        # publier
        post_btn = page.locator('[data-e2e="post-button"]')
        if not post_btn.count():
            post_btn = page.get_by_role("button", name="Publier")
        post_btn.click()
        log("Clic sur Publier…")

        # confirmation
        ok = False
        try:
            page.wait_for_url("**/upload/success**", timeout=120000)
            ok = True
        except PWTimeout:
            if page.locator('text=Publié').count() > 0:
                ok = True
        browser.close()
        if ok: log("Publication confirmée ✅")
        else: log("Aucune confirmation claire ❓")
        return ok
# ----------------------------------------------------------------------

def main():
    log(f"Compte: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    df_scripts = load_csv(SCRIPTS)
    df_products = load_csv(PRODUCTS) if os.path.exists(PRODUCTS) else pd.DataFrame()

    items = pick_scripts(df_scripts, df_products, ACCOUNT, POSTS_TO_PUBLISH)
    if not items:
        log("Aucun script à publier.")
        return

    for i, item in enumerate(items, 1):
        log(f"--- Post {i}/{len(items)} ---")
        titre = item.get("titre","")
        caption = build_caption(item)
        video_path = resolve_video_path(item)
        log(f"Préparation post: {titre}")
        log(f"Caption: {caption[:180]}{'…' if len(caption)>180 else ''}")
        log(f"Fichier vidéo: {video_path}")

        if DRY_RUN:
            log("DRY_RUN=TRUE → simulation ok ✅")
            continue

        ok = publish_to_tiktok(TIKTOK_COOKIE, caption, video_path)
        log("Publication réelle ✅" if ok else "Publication non confirmée ❓")

    log("Run terminé ✅")

if __name__ == "__main__":
    main()
