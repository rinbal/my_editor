# Translating MyEditor

MyEditor shows every word in the reader's language. English is built in; other languages live in `locale/<language>.po`, the standard gettext format that [Poedit](https://poedit.net), Weblate and most translation tools read and write. There is no compile step: the app reads the `.po` file when it starts.

The language follows the operating system (View > Language > System Language), or the one chosen in that menu. A change applies the next time MyEditor starts.

## Add a language

1. Copy `locale/de.po` to `locale/<code>.po` (the two-letter code, for example `fr.po`), and change its `Language:` and, if your language needs it, `Plural-Forms:` header.
2. Empty every `msgstr` and translate. Poedit opens the file directly.
3. Add the language's own name to `LANGUAGE_NAMES` in `i18n.py` (for example `"fr": "Français"`), and its code to `CFBundleLocalizations` in `packaging/my_editor.spec`, so macOS shows its own panels in it too.
4. Run `python scripts/i18n.py check`. It lists every text that is still missing or whose placeholders differ from the English.
5. Start MyEditor with `MYEDITOR_LANGUAGE=<code>` to see it without changing the setting, and look at every window for text that is cut off.

## Keep a translation up to date

After code changes, run:

```sh
python scripts/i18n.py update
```

It rewrites `locale/myeditor.pot` from the code and brings every `.po` up to date: new texts appear untranslated, translations of texts that still exist are kept, and texts the code no longer uses are dropped. The test suite runs `scripts/i18n.py check`, so a missing German translation or a wrong placeholder fails the tests.

## Rules for the code

- Wrap every text a person sees: `_("Save")`, `ngettext("{n} file", "{n} files", n)`, `pgettext("verb", "Open")` for a word that translates differently depending on what it means, and `N_("Save")` for a text written in one place (a constant) and shown in another, which then calls `_()` on it.
- Write placeholders as `{name}` and fill them after the lookup: `_("Saved {path}").format(path=path)`. Never pass an f-string to `_()`: it would be looked up with the values already in it and never found (`scripts/i18n.py check` reports it).
- Give translators whole sentences. Do not build a sentence from pieces (`_("Saved") + " " + name`); word order differs between languages.
- Do not translate what is not language: file extensions, protocol names in code, key names in settings files, log messages, the membership service's answers (`sidecar/`).
- `i18n.install()` runs before the modules that hold texts are imported (see `main.py`), so a text in a module constant is translated too.

## Make room for longer words

A German text is often a third longer than the English one. Every window must still show all of it:

- Labels that can grow wrap (`setWordWrap(True)`) instead of having a fixed width.
- Windows built on `nostr/ui/assistant.py` grow to fit their text by themselves.
- Check new windows with the pseudo language, which makes every text about 40 percent longer and marks it with brackets: `MYEDITOR_LANGUAGE=pseudo python main.py`. A bracket that is cut off is a text that would be cut off in a translation.

## German style guide

The German translation should read like a well-made German app, in the everyday language people in Germany, Austria and Switzerland use. It follows Apple's German conventions where they are common usage.

- Address the reader with **du** ("Möchtest du die Änderungen speichern?"), as the EINUNDZWANZIG community does, and in the full imperative: "Versuche es erneut", "Versuche es gleich noch einmal", never "Versuch es".
- Buttons and menu items use the infinitive: "Speichern", "Abbrechen", "Nicht jetzt", "Weiter", "Zurück". Capitalization follows German grammar (nouns capitalized), not English title case.
- A menu item or button that opens a window before acting ends in " …" with a space before it, as in macOS: "Speichern unter …", "Öffnen …".
- Quotation marks: „so“.
- No em dash and no en dash used as a dash: use a colon, parentheses, a comma or a new sentence.
- Plain words, no jargon: no NIP numbers, no "kind 1", no "bolt11" in the interface, exactly as in English.
- Numbers and amounts: "21.000 Sats", "5 GB", "1 GB pro Datei".

### Glossary

| English | Deutsch |
|---|---|
| account (never "profile" or "identity" for it; a profile is the name and picture people see) | Konto |
| article | Artikel |
| back up (an account) | (Konto) sichern; backup file: Sicherungsdatei |
| bold / italic / underline / strikethrough | Fett / Kursiv / Unterstrichen / Durchgestrichen |
| connect a signer app | Signer-App verbinden |
| draft | Entwurf |
| EINUNDZWANZIG | EINUNDZWANZIG |
| feed | Feed |
| fee (membership) | Mitgliedsbeitrag |
| file list | Dateiliste |
| folder; Add Folder to Sidebar; Remove from Sidebar | Ordner; Ordner zur Seitenleiste hinzufügen; Aus der Seitenleiste entfernen |
| find / replace | Suchen / Ersetzen |
| full screen; Enter / Exit Full Screen (the View menu) | Vollbild; Vollbildmodus aktivieren / Vollbildmodus beenden |
| heading | Überschrift |
| image | Bild |
| import | importieren |
| invoice (Lightning) | Rechnung (Lightning-Rechnung) |
| item (a file or folder, as the Finder counts them) | Objekt |
| key (Nostr) | Schlüssel |
| Keyboard Shortcuts | Tastaturkurzbefehle |
| Library (the sidebar's section); Recents | Bibliothek; Zuletzt benutzt |
| line numbers | Zeilennummern |
| link | Link |
| list (bulleted / numbered) | Aufzählung / nummerierte Liste |
| Media Library | Mediathek |
| media server | Medienserver |
| member / membership | Mitglied / Mitgliedschaft |
| mention | Erwähnung; to mention someone: jemanden erwähnen |
| note (kind 1) | Notiz |
| Nostr address | Nostr-Adresse |
| Open in {app}; Open With | In {app} öffnen; Öffnen mit |
| paper mode | Papiermodus |
| profile | Profil |
| publish | veröffentlichen |
| Quick Look | Übersicht |
| quit | beenden |
| quote | Zitat |
| Recent Files | Zuletzt geöffnet |
| relay / relay list | Relay (das Relay, die Relays) / Relay-Liste |
| restore | wiederherstellen |
| save / save as | speichern / speichern unter |
| sats | Sats |
| Save a Copy… (a spreadsheet MyEditor cannot save into) | Kopie sichern … |
| settings (the window) | Einstellungen |
| sheet (of a workbook); workbook; spreadsheet (the file) | Tabellenblatt; Arbeitsmappe; Tabelle |
| Show in Finder / Explorer / File Manager | Im Finder / Explorer / Dateimanager zeigen |
| sidebar | Seitenleiste |
| sign out | abmelden |
| signer app | Signer-App |
| statutes | Satzung |
| subscribe / subscription | abonnieren / Abonnement |
| syntax highlighting | Syntaxhervorhebung |
| tab | Tab |
| Trash (the Recycle Bin on Windows) | Papierkorb |
| appearance (automatic / light / dark) | Erscheinungsbild (automatisch / hell / dunkel) |
| update (software) | Update; install update: Update installieren |
| upload / download | hochladen / herunterladen |
| wallet | Wallet |
| watchtower (Lightning) | Lightning-Watchtower |
| Welcome | Willkommen |
| window | Fenster |

Key names in the Keyboard Shortcuts window follow German keyboards: Strg, Umschalt, Eingabe, Leertaste, Pos1, Ende, Esc, Tab.
