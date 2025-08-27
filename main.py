import os, sys, random, time
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# --- Génération vidéo ---
from moviepy.editor import ColorClip, TextClip, CompositeVideoClip, concatenate_videoclips

DATA_DIR = "data"
SCRIPTS = os.path.join(DATA_DIR, "scripts.csv")
IDEAS_TXT = os.path.join(DATA_DIR, "ideas.txt")

ACCOUNT = os.getenv("ACCOUNT", "trucs-malins")
POSTS_TO_PUBLISH = int(os.getenv("POSTS_TO_PUBLISH", "1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
TIKTOK_COOKIE = os.getenv("TIKTOK_COOKIE", "").strip()

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
def need_exit(msg, code=1): log(f"ERREUR: {msg}"); sys.exit(code)

def load_csv(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=["id","titre","texte","cta","hashtags","product_slug","source_broll","music_hint","thumb_hint","account_bucket"])
    return pd.read_csv(path)

def load_ideas(path):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def choose_idea(ideas):
    if not ideas: return "3 astuces express pour booster ta productivité"
    now = datetime.now(timezone.utc)
    idx = (now.timetuple().tm_yday * 24 + now.hour) % len(ideas)
    return ideas[idx]

def ensure_video_from_text(text, out_path="assets/auto_video.mp4", duration=12):
    W, H = 720, 1280
    Path("assets").mkdir(exist_ok=True)
    colors = [(20,20,20), (30,30,60), (10,40,30)]
    segs = []
    lines = [text] if len(text) < 70 else [text[:70], text[70:140]+"…" if len(text)>140 else text[70:]]
    for i, col in enumerate(colors):
        clip = ColorClip(size=(W,H), color=col, duration=duration/3)
        tc = TextClip(lines[min(i, len(lines)-1)], fontsize=60, color="white", method="caption", size=(W-120,None), align="center")
        tc = tc.set_pos(("center","center")).set_duration(duration/3)
        segs.append(CompositeVideoClip([clip, tc]))
    final = concatenate_videoclips(segs, method="compose")
    final.write_videofile(out_path, fps=30, codec="libx264", audio=False, verbose=False, logger=None)
    return out_path

def build_caption(texte, cta, tags, link=""):
    parts = [p for p in [texte.strip(), cta.strip() if cta else "", link.strip() if link else "", tags.strip() if tags else ""] if p]
    return " ".join(parts)[:2100]

def resolve_item(row, ideas):
    titre = str(row.get("titre") or "").strip()
    texte = str(row.get("texte") or "").strip()
    cta   = str(row.get("cta") or "").strip()
    tags  = str(row.get("hashtags") or "").strip()
    src   = str(row.get("source_broll") or "").strip()

    if not titre: titre = choose_idea(ideas)
    if not texte: texte = titre

    if src and os.path.exists(src):
        video = src
    elif os.path.exists("assets/test.mp4"):
        video = "assets/test.mp4"
    else:
        log("Aucun b-roll fourni → génération automatique.")
        video = ensure_video_from_text(titre, out_path=f"assets/auto_{int(time.time())}.mp4")

    caption = build_caption(texte, cta, tags)
    return titre, caption, video

def publish_to_tiktok(cookie_sessionid: str, caption: str, video_path: str) -> bool:
    if not cookie_sessionid:
        need_exit("TIKTOK_COOKIE manquant (cookie 'sessionid').")
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([{
            "name": "sessionid", "value": cookie_sessionid,
            "domain": ".tiktok.com", "path": "/", "httpOnly": True, "secure": True,
        }])
        page = context.new_page()
        log("Ouverture de la page d'upload…")
        page.goto("https://www.tiktok.com/upload?lang=fr", wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector('[data-e2e="upload-right-panel"]', timeout=15000)
        except PWTimeout:
            browser.close(); need_exit("Non connecté (cookie invalide/expiré).")

        log(f"Upload de la vidéo: {video_path}")
        page.locator('input[type="file"]').set_input_files(video_path)
        try:
            page.wait_for_selector('[data-e2e="caption"]', timeout=90000)
            page.wait_for_selector('[data-e2e="post-button"]:not([disabled])', timeout=300000)
        except PWTimeout:
            log("Traitement long… on tente la saisie quand même.")

        try:
            cap = page.locator('[data-e2e="caption"] textarea')
            if not cap.count():
                cap = page.locator('textarea[placeholder*="Légende"]').first
            cap.fill(caption)
            log("Légende insérée.")
        except Exception as e:
            log(f"Impossible d’insérer la légende ({e}).")

        btn = page.locator('[data-e2e="post-button"]')
        if not btn.count():
            btn = page.get_by_role("button", name="Publier")
        btn.click()
        log("Clic sur Publier…")

        ok = False
        try:
            page.wait_for_url("**/upload/success**", timeout=120000)
            ok = True
        except PWTimeout:
            if page.locator('text=Publié').count() > 0:
                ok = True
        browser.close()
        log("Publication confirmée ✅" if ok else "Aucune confirmation claire ❓")
        return ok

def main():
    log(f"Compte: {ACCOUNT} | Posts: {POSTS_TO_PUBLISH} | DRY_RUN={DRY_RUN}")
    df = load_csv(SCRIPTS)
    ideas = load_ideas(IDEAS_TXT)

    rows = df.to_dict(orient="records") if not df.empty else [{} for _ in range(POSTS_TO_PUBLISH)]
    count = min(POSTS_TO_PUBLISH, len(rows)) if rows else POSTS_TO_PUBLISH

    for i in range(count):
        log(f"--- Post {i+1}/{count} ---")
        titre, caption, video = resolve_item(rows[i] if i < len(rows) else {}, ideas)
        log(f"Titre: {titre}")
        log(f"Fichier vidéo: {video}")
        if DRY_RUN:
            log("DRY_RUN=TRUE → simulation ok ✅")
            continue
        ok = publish_to_tiktok(TIKTOK_COOKIE, caption, video)
        log("Publication réelle ✅" if ok else "Publication non confirmée ❓")

    log("Run terminé ✅")

if __name__ == "__main__":
    main()
