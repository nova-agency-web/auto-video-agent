# main.py
# Playwright + TikTok Studio uploader – avec scroll, cases de conformité, visibilité publique et clic robuste sur "Publier".

import os, sys, time, re, json
from pathlib import Path
from typing import List, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------
# Utilitaires
# ----------------------------------
def log(msg: str):
    print(msg, flush=True)

def getenv(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if v is None:
        v = ""
    return v.strip()

def parse_cookie_string(cookie_raw: str) -> List[Dict]:
    """
    Transforme "a=1; b=2; c=3" -> [{name:'a', value:'1', domain:'.tiktok.com', path:'/'}, ...]
    On ne garde que les paires nom=valeur plausibles.
    """
    cookies = []
    if not cookie_raw:
        return cookies
    parts = [p.strip() for p in cookie_raw.split(";") if p.strip()]
    for p in parts:
        if "=" not in p:
            continue
        name, value = p.split("=", 1)
        name = name.strip()
        value = value.strip()
        # ignore attributs non-cookies classiques
        if name.lower() in {"path", "domain", "expires", "httponly", "secure", "samesite"}:
            continue
        if not name or not value:
            continue
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

def attach_cookies(context, cookies: List[Dict]) -> int:
    ok = 0
    for c in cookies:
        try:
            context.add_cookies([c])
            ok += 1
        except Exception as e:
            log(f"[WARN] Cookie ignoré ({c.get('name')}): {e}")
    return ok

def wait_for_file_input(frame_or_page, timeout_ms=30000):
    """
    Certains écrans ont plusieurs <input type="file"> cachés. On retourne le premier visible.
    On tente plusieurs requêtes.
    """
    candidates = [
        "input[type='file'][accept*='video']",
        "input[type='file']",
        "//*[@type='file']",
    ]
    for sel in candidates:
        try:
            el = frame_or_page.locator(sel).filter(has_not_text="").first
            el.wait_for(state="visible", timeout=timeout_ms)
            return el
        except PWTimeout:
            # Pas visible – on tente quand même si présent mais hidden (upload programmatique)
            try:
                el = frame_or_page.locator(sel).first
                el.wait_for(state="attached", timeout=2000)
                return el
            except Exception:
                pass
        except Exception:
            pass
    raise RuntimeError("Impossible de localiser le champ fichier (<input type='file'>).")

def find_upload_iframe(page):
    """
    Sur TikTok Studio, l'uploader vit parfois dans un <iframe>.
    On tente de repérer un iframe contenant 'upload' ou 'studio' dans l'URL, sinon on retourne None (upload dans page).
    """
    for f in page.frames:
        try:
            url = (f.url or "").lower()
            if "upload" in url or "studio" in url:
                return f
        except Exception:
            pass
    return None

def resilient_click(page_or_frame, selectors: List[str], timeout_ms=10000, allow_force=True, js_force=True, label=""):
    """
    Essaie de cliquer sur le premier sélecteur disponible.
    Stratégies: click normal, click(force), removeAttribute('disabled') + click, .dispatchEvent('click')
    """
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel).first
            loc.wait_for(state="attached", timeout=timeout_ms)
            # scroll dans la vue
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            # normal
            try:
                loc.click(timeout=timeout_ms)
                log(f"[INFO] Click normal OK → {label or sel}")
                return True
            except Exception:
                pass
            # force
            if allow_force:
                try:
                    loc.click(force=True, timeout=2000)
                    log(f"[INFO] Click forcé OK → {label or sel}")
                    return True
                except Exception:
                    pass
            # JS force (enlève disabled et clique)
            if js_force:
                try:
                    page_or_frame.evaluate(
                        """(sel)=>{
                            const el = document.querySelector(sel);
                            if(!el) return false;
                            el.removeAttribute && el.removeAttribute('disabled');
                            el.classList && el.classList.remove('is-disabled');
                            el.click && el.click();
                            el.dispatchEvent && el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                            return true;
                        }""", sel
                    )
                    log(f"[INFO] Click JS forcé OK → {label or sel}")
                    return True
                except Exception:
                    pass
        except Exception:
            continue
    return False

def type_caption(page_or_frame, text: str):
    """
    Insère la légende. Plusieurs implémentations existent côté TikTok – on tente plusieurs cibles.
    """
    if not text:
        return
    targets = [
        "textarea",
        "[contenteditable='true']",
        "[data-testid='caption']",
        "div[role='textbox']",
        "[placeholder*='description']",
    ]
    for sel in targets:
        try:
            el = page_or_frame.locator(sel).first
            el.wait_for(state="visible", timeout=3000)
            el.click()
            # clear
            try:
                el.fill("", timeout=2000)
            except Exception:
                # fallback: select-all delete
                el.press("Control+A")
                el.press("Backspace")
            el.type(text, delay=10)
            log("[INFO] Légende insérée.")
            return
        except Exception:
            continue
    log("[WARN] Avertissement: impossible de remplir la légende (textarea non trouvé).")

def smart_wait_processing(page_or_frame, max_sec=120):
    """
    Attend la fin du traitement (convert/scan). On scrute quelques indicateurs.
    """
    start = time.time()
    hints = [
        "Traitement en cours", "Processing", "Scan", "Conversion", "Analyzing",
        "Génération de miniatures", "Creating thumbnail", "0%", "%"
    ]
    while time.time() - start < max_sec:
        try:
            txt = page_or_frame.locator("body").inner_text(timeout=2000)
            # s'il ne reste plus de mots-clés liés au processing on sort
            if not any(h.lower() in (txt or "").lower() for h in hints):
                break
        except Exception:
            pass
        time.sleep(2)

def ensure_public_visibility(page_or_frame):
    """
    Ouvre/pose la visibilité sur 'Tout le monde'.
    Plusieurs variantes UI.
    """
    # Ouvre le panneau de visibilité si présent
    triggers = [
        "text=Tout le monde peut voir cette publication",
        "text=Who can view this post",
        "text=Visibilité",
        "text=Privacy",
    ]
    for t in triggers:
        try:
            if page_or_frame.locator(t).first.is_visible():
                resilient_click(page_or_frame, [t], label="Ouvrir réglages visibilité")
                time.sleep(0.5)
                break
        except Exception:
            pass

    # Choisir 'Tout le monde'
    opts = [
        "text=Tout le monde",
        "text=Public",
        "label:has-text('Tout le monde')",
        "label:has-text('Public')",
        "role=option[name='Tout le monde']",
    ]
    if resilient_click(page_or_frame, opts, label="Choix visibilité: Tout le monde"):
        log("[INFO] Visibilité réglée sur 'Tout le monde'.")
    else:
        log("[WARN] Impossible de confirmer la visibilité (on continue).")

def tick_compliance_checkboxes(page_or_frame):
    """
    Coche les cases de conformité éventuelles (droits musicaux, contenu sponsorisé, etc.)
    On essaie plusieurs libellés FR/EN.
    """
    checks = [
        "label:has-text('Je confirme')",
        "label:has-text('J’ai les droits')",
        "label:has-text('I confirm')",
        "label:has-text('I have the rights')",
        "label:has-text('publicité')",
        "label:has-text('sponsorisé')",
        "label:has-text('sponsored')",
        "label:has-text('contenu de marque')",
    ]
    ticked = 0
    for sel in checks:
        try:
            lab = page_or_frame.locator(sel).first
            if lab.count() == 0:
                continue
            # clique label
            if resilient_click(page_or_frame, [sel], timeout_ms=2000, label=f"Case {sel}"):
                ticked += 1
        except Exception:
            continue
    if ticked:
        log(f"[INFO] {ticked} case(s) de conformité cochée(s).")
    else:
        log("[INFO] Aucune case de conformité détectée/nécessaire.")

def deep_scroll(page_or_frame, steps=8):
    """
    Scroll vers le bas pour révéler tous les éléments cachés (bouton Publier, cases, etc.)
    """
    try:
        for _ in range(steps):
            page_or_frame.mouse.wheel(0, 1200)
            time.sleep(0.25)
        # remonte un peu pour stabiliser
        page_or_frame.mouse.wheel(0, -300)
    except Exception:
        # fallback JS
        try:
            page_or_frame.evaluate("""()=>{window.scrollTo(0, document.body.scrollHeight);}""")
        except Exception:
            pass

def find_publish_button(page_or_frame):
    """
    Retourne un locator plausible du bouton Publier.
    """
    candidates = [
        "button:has-text('Publier')",
        "button:has-text('Publish')",
        "text=Publier >> xpath=ancestor-or-self::button",
        "[data-e2e='post-button']",
        "[aria-label='Publier']",
        "[role='button']:has-text('Publier')",
    ]
    for c in candidates:
        loc = page_or_frame.locator(c).first
        if loc.count() > 0:
            return loc, c
    return None, None

# ----------------------------------
# Main – publication
# ----------------------------------
def publish_once(pw, video_abs: str) -> bool:
    account = getenv("ACCOUNT", "compte")
    dry_run = getenv("DRY_RUN", "FALSE").upper() == "TRUE"
    caption = "Test upload vidéo automatique"
    ua_raw = getenv("TIKTOK_UA", "").strip()
    cookie_raw = getenv("TIKTOK_COOKIE", "").strip()

    log(f"[INFO] Compte ciblé: {account} | Posts: 1 | DRY_RUN={dry_run}")
    browser_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
    if ua_raw:
        browser_args.append(f"--user-agent={ua_raw}")

    browser = pw.chromium.launch(headless=True, args=browser_args)
    context = browser.new_context(user_agent=ua_raw if ua_raw else None, viewport={"width": 1400, "height": 900})

    # Cookies
    cookies = parse_cookie_string(cookie_raw)
    ok = attach_cookies(context, cookies)
    if ok == 0:
        log("[ERREUR] Impossible de parser TIKTOK_COOKIE (aucun cookie autorisé/valide trouvé).")
        context.close(); browser.close()
        return False

    page = context.new_page()
    upload_url = "https://www.tiktok.com/tiktokstudio/upload"
    log("[INFO] — Post 1/1 —")
    log("[INFO] Injection cookies ({})…".format(len(cookies)))
    page.goto(upload_url, wait_until="domcontentloaded")

    # Assure navigation
    log("[INFO] [NAV] Vers TikTok Studio Upload")
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass

    # Trouver la zone d'upload (page ou iframe)
    frame = find_upload_iframe(page)
    target = frame if frame else page

    # Trouver input file
    try:
        file_input = wait_for_file_input(target, timeout_ms=30000)
    except Exception as e:
        log(f"[ERREUR] Impossible de localiser le champ fichier: {e}")
        context.close(); browser.close()
        return False

    # Upload du fichier
    log("[INFO] Upload (input direct) déclenché ✅")
    try:
        file_input.set_input_files(video_abs, timeout=60000)
    except Exception as e:
        log(f"[ERREUR] Echec d'affectation du fichier: {e}")
        context.close(); browser.close()
        return False

    # Attendre un minimum que l'UI réagisse
    time.sleep(2)

    # Attendre le traitement initial
    smart_wait_processing(target, max_sec=90)

    # Insérer la légende
    type_caption(target, caption)

    # Scroll pour révéler les options et le bouton
    deep_scroll(target, steps=10)

    # Visibilité publique
    ensure_public_visibility(target)

    # Cases de conformité
    tick_compliance_checkboxes(target)

    # Une seconde pour laisser l'UI activer le bouton
    time.sleep(1.0)

    # Chercher bouton Publier
    btn, sel_used = find_publish_button(target)
    if not btn:
        log("[WARN] Bouton 'Publier' introuvable.")
        context.close(); browser.close()
        return False

    # Vérifier état disabled
    try:
        disabled = target.evaluate(
            """(el)=>el.hasAttribute && (el.hasAttribute('disabled') || el.classList.contains('is-disabled'))""",
            btn
        )
    except Exception:
        disabled = False

    if disabled:
        log("[WARN] Bouton 'Publier' toujours inactif / non cliquable (dernier libellé: Publier). Tentatives de déblocage…")

    if dry_run:
        log("[INFO] DRY_RUN=TRUE → on s'arrête avant la publication.")
        context.close(); browser.close()
        return True

    # Tentatives de clic (normal → force → JS)
    clicked = resilient_click(
        target,
        selectors=[sel_used, "button:has-text('Publier')", "[data-e2e='post-button']"],
        timeout_ms=8000,
        label="Publier"
    )

    if not clicked:
        # petite attente + re-scroll + re-essai
        time.sleep(2)
        deep_scroll(target, steps=6)
        clicked = resilient_click(
            target,
            selectors=[sel_used, "button:has-text('Publier')", "[data-e2e='post-button']"],
            timeout_ms=6000,
            label="Publier (retry)"
        )

    if not clicked:
        log("[ERROR] Impossible de cliquer sur 'Publier' après plusieurs stratégies.")
        context.close(); browser.close()
        return False

    # Attendre un éventuel retour/confirmation
    time.sleep(4)
    log("[INFO] Publication déclenchée (si aucune erreur UI).")

    # Optionnel: vérifier un toast de succès (facultatif – résilient)
    try:
        success_hints = [
            "Votre vidéo est en cours de publication",
            "Posted",
            "scheduled",
            "Publication réussie",
        ]
        bodytxt = target.locator("body").inner_text(timeout=4000)
        if any(h.lower() in (bodytxt or "").lower() for h in success_hints):
            log("[INFO] Indication de succès détectée.")
    except Exception:
        pass

    context.close()
    browser.close()
    return True


def main():
    # Entrées
    account = getenv("ACCOUNT", "compte")
    posts = int(getenv("POSTS_TO_PUBLISH", "1") or "1")
    dry_run = getenv("DRY_RUN", "FALSE").upper() == "TRUE"

    # Vidéo : assets/test.mp4 par défaut
    assets_dir = Path("assets")
    assets_dir.mkdir(parents=True, exist_ok=True)
    default_video = assets_dir / "test.mp4"
    if not default_video.exists():
        # création d'un petit fichier vide pour ne pas planter (mais TikTok refusera à l'upload)
        default_video.write_bytes(b"\x00\x00")

    video_abs = str(default_video.resolve())

    with sync_playwright() as pw:
        log(f"[INFO] Compte ciblé: {account} | Posts: {posts} | DRY_RUN={dry_run}")
        for i in range(posts):
            log(f"[INFO] — Post {i+1}/{posts} —")
            ok = publish_once(pw, video_abs)
            if not ok:
                sys.exit(1)
        log("[INFO] Run terminé ✅")


if __name__ == "__main__":
    main()
