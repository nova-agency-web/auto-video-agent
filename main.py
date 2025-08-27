import os, sys, random
import pandas as pd
from datetime import datetime

DATA_DIR = "data"
SCRIPTS = os.path.join(DATA_DIR, "scripts.csv")
PRODUCTS = os.path.join(DATA_DIR, "products.csv")
SCHEDULE = os.path.join(DATA_DIR, "schedule.csv")

# Inputs via GitHub Actions (workflow_dispatch)
ACCOUNT = os.getenv("ACCOUNT", "trucs-malins")      # ex: trucs-malins | gadgets-utiles
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "2"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def load_csv(path):
    if not os.path.exists(path):
        log(f"ERREUR: fichier introuvable: {path}")
        sys.exit(1)
    return pd.read_csv(path)

def pick_scripts(df_scripts, df_products, account, n):
    # Filtre par bucket de compte si présent, sinon prend tout
    if "account_bucket" in df_scripts.columns:
        pool = df_scripts[df_scripts["account_bucket"].fillna("").str.contains(account, na=False)]
        if pool.empty: pool = df_scripts
    else:
        pool = df_scripts

    # Join vers produits pour vérifier que le slug existe
    if "product_slug" in pool.columns:
        pool = pool.merge(df_products[["product_slug", "affiliate_url"]], on="product_slug", how="left")
    pool = pool.sample(min(n, len(pool)), random_state=random.randint(1, 9999))
    return pool.to_dict(orient="records")

def simulate_publish(item):
    titre = item.get("titre", "")
    texte = item.get("texte", "")
    cta = item.get("cta", "")
    affiliate = item.get("affiliate_url", "AUCUN LIEN")
    hashtags = item.get("hashtags", "")
    log(f"Préparation post: {titre}")
    log(f"Texte: {texte}")
    log(f"CTA: {cta} | Lien: {affiliate} | Tags: {hashtags}")
    # ICI: intégrer la vraie publication TikTok si tu as déjà un module (cookie/API)
    # Exemple: tiktok_publish(video_path, caption, cookie=os.getenv('TIKTOK_COOKIE'))
    if DRY_RUN:
        log("DRY_RUN=TRUE → simulation ok ✅")
    else:
        # tiktok_publish(...)
        log("Publication réelle (placeholder) ✅")

def main():
    log(f"Compte ciblé: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    df_scripts = load_csv(SCRIPTS)
    df_products = load_csv(PRODUCTS)
    # schedule non bloquant — on l'ignore pour le PoC
    try:
        _ = load_csv(SCHEDULE)
    except SystemExit:
        pass

    posts = pick_scripts(df_scripts, df_products, ACCOUNT, POSTS_TO_PUBLISH)
    if not posts:
        log("Aucun script disponible.")
        sys.exit(0)

    for i, p in enumerate(posts, 1):
        log(f"--- Post {i}/{len(posts)} ---")
        simulate_publish(p)

    log("Run terminé ✅")

if __name__ == "__main__":
    main()
