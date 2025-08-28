# ---------- Helpers robustes pour publier ----------

def _click_all_close_buttons(page_or_frame, log_prefix=""):
    # Fermer les overlays/modales les plus courantes
    candidates = [
        'button:has-text("OK")',
        'button:has-text("Ok")',
        'button:has-text("D\'accord")',
        'button:has-text("Compris")',
        'button:has-text("J\'ai compris")',
        'button:has-text("Fermer")',
        'button:has-text("Continuer")',
        'button:has-text("Confirmer")',
        'button[aria-label="Fermer"]',
        'button[aria-label="Close"]',
        '[data-e2e="close"]',
        '[role="dialog"] button',
    ]
    for sel in candidates:
        try:
            btns = page_or_frame.locator(sel)
            count = btns.count()
            if count:
                for i in range(count):
                    if btns.nth(i).is_visible():
                        try:
                            btns.nth(i).click(timeout=500, force=True)
                        except Exception:
                            pass
        except Exception:
            pass

def _dismiss_overlays(page, max_rounds=6):
    # Supprimer / masquer les overlays interceptant les clics
    # (TUXModal-overlay est typique de TikTok UI)
    for _ in range(max_rounds):
        found = False
        for sel in [
            ".TUXModal-overlay",
            "[data-tux-overlay]",
            "[role='dialog']",
            ".tux-modal",
        ]:
            try:
                loc = page.locator(sel)
                if loc.count():
                    found = True
                    try:
                        _click_all_close_buttons(page, "overlay")
                    except Exception:
                        pass
                    # En dernier recours, on enlève l’overlay par JS
                    try:
                        page.evaluate(
                            """(sel) => {
                                for (const el of document.querySelectorAll(sel)) {
                                  el.style.pointerEvents = 'none';
                                  el.style.display = 'none';
                                }
                              }""",
                            sel,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        if not found:
            break
        page.wait_for_timeout(300)

def _check_compliance_checkboxes(page):
    # Coche toutes les cases visibles non cochées (conformité / politiques)
    try:
        boxes = page.locator('input[type="checkbox"]')
        n = boxes.count()
        for i in range(n):
            cb = boxes.nth(i)
            try:
                if cb.is_visible() and cb.is_enabled():
                    # Vérifie si déjà coché
                    checked = cb.evaluate("el => el.checked")
                    if not checked:
                        cb.scroll_into_view_if_needed(timeout=500)
                        cb.click(timeout=800, force=True)
            except Exception:
                pass
    except Exception:
        pass

def _find_publish_button(page):
    # Plusieurs variantes FR/EN dans Studio
    btn = page.locator(
        'button:has-text("Publier"), button:has-text("Post"), button[data-e2e="post_button"]'
    )
    if btn.count() == 0:
        # variante sidebar
        btn = page.locator('[data-tux-tooltip]:has-text("Publier"), [data-tux-tooltip]:has-text("Post")')
    return btn

def _enable_and_click_publish(page, max_attempts=15):
    btn = _find_publish_button(page)
    if btn.count() == 0:
        return False, "Bouton 'Publier' introuvable"

    for attempt in range(1, max_attempts + 1):
        try:
            _dismiss_overlays(page)
            _check_compliance_checkboxes(page)

            # Re-récupère (si DOM a changé)
            btn = _find_publish_button(page)
            if btn.count() == 0:
                page.wait_for_timeout(300)
                continue

            b = btn.first
            b.scroll_into_view_if_needed(timeout=1200)

            # S’il est désactivé, on attend un peu (upload/validation)
            try:
                disabled = b.evaluate(
                    "el => el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true'"
                )
            except Exception:
                disabled = False

            if disabled:
                page.wait_for_timeout(600)
                continue

            # Petit hover puis click “classique”
            try:
                b.hover(timeout=800)
            except Exception:
                pass

            try:
                b.click(timeout=1200)
                return True, "Cliqué normalement"
            except Exception:
                # Click forcé
                try:
                    b.click(timeout=800, force=True)
                    return True, "Cliqué en force"
                except Exception:
                    # JS click en dernier recours
                    try:
                        page.evaluate("(el)=>el.click()", b)
                        return True, "Cliqué via JS"
                    except Exception:
                        pass

        except Exception:
            pass

        # Si on arrive ici, on réessaie après un petit délai
        page.wait_for_timeout(500)

    return False, "Toujours inactif / bloqué par overlay / non trouvable"

def publish_now(page):
    """À appeler après l’upload et l’insertion de la légende."""
    # 1) On ferme tout ce qui peut masquer le bouton
    _dismiss_overlays(page)

    # 2) On tente l’activation + clic
    ok, why = _enable_and_click_publish(page)
    print(f"[PUBLISH] Résultat: ok={ok} ({why})")
    return ok
