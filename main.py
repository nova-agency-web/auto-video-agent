        # ----- Upload du fichier -----
        log("Recherche du bouton d’importation vidéo…")
        upload_selectors = [
            "button:has-text('Importer')",
            "button:has-text('Upload')",
            "div[role='button'][data-e2e='upload-button']",
            "[data-e2e='upload-button'] input[type='file']",
            "input[type='file']"
        ]

        file_input = None
        for sel in upload_selectors:
            try:
                el = find_in_any_frame(page, sel, timeout_ms=15000)
                # si c’est un vrai input file :
                if el.evaluate("e => e.tagName.toLowerCase()") == "input":
                    file_input = el
                    break
                else:
                    # Clique sur le bouton pour révéler un input file
                    el.click()
                    time.sleep(2)
                    try:
                        file_input = find_in_any_frame(page, "input[type='file']", timeout_ms=5000)
                        break
                    except Exception:
                        continue
            except Exception:
                continue

        if not file_input:
            log("ERREUR: Impossible de trouver un champ/bouton d’upload.")
            context.close(); browser.close()
            return False

        log(f"Upload vidéo: {video_path}")
        file_input.set_input_files(str(video_path))
