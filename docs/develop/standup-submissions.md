# STANDUP submissions

MyEditor can hand a text in to a STANDUP publication: an EINUNDZWANZIG STANDUP site with an editorial team (its Redaktion). The writer submits, the team answers, and the two talk it through until the work is published. Everything is end to end encrypted; the submission server stores and forwards, and can read titles and states, never the work, the conversation or a key.

> [!NOTE]
> This is not switched on for everyone yet. The association runs the submission server and has not named its address, so no address is built into the app. Until then, **Nostr > Submit to a Publication…** and **Nostr > Submissions** are hidden, and the user guide does not describe them. Developers turn them on as below.

## Try it against a local STANDUP

Set the server's address in the environment before you start MyEditor from source:

```sh
MYEDITOR_STANDUP_SERVER=https://standup.example python main.py
```

For a STANDUP running on your own computer, allow plain HTTP to `127.0.0.1` only:

```sh
MYEDITOR_STANDUP_LOCAL_HTTP=1 MYEDITOR_STANDUP_SERVER=http://127.0.0.1:8000 python main.py
```

An address is an origin and nothing else: `https://host` or `https://host:port`, no path and no trailing slash (`nostr/standup/servers.py`). With an address set and an account in use, the two commands appear in the Nostr menu, and the Drafts panel gets a **Submissions** view.

## What the writer sees

- **Submit to a Publication…** hands in the text in front, as Publish as Article does: title, summary and cover, then the publication to send it to.
- **Submissions** (**⇧⌘Y** / **Ctrl+Shift+Y**) lists each submission with its state ("Your turn", "In Progress", "In the inbox") and its conversation with the team. From there the writer can **Hand In New Version…**, **Withdraw…**, **Take the Correction**, **Ask a Question**, and at the end **Confirm Share…** and **Publish…**.
- A submission whose sheet was closed goes on in the background (**Continue in Background**); the status line says how it ends.

## Where the code is

| Part | Where |
|---|---|
| The shared contract with STANDUP | STANDUP's `docs/editorial-contract.md` (v1); its test vectors, STANDUP's `docs/editorial-fixtures.json`, are copied into `tests/fixtures/standup_editorial_fixtures.json` |
| Formats, crypto, gift-wrapped messages (NIP-59), the server's HTTP API, trust in the Redaktion's key | `nostr/standup/` (no user interface) |
| Windows, sheets and the Submissions view | `nostr/ui/standup_*.py`, `nostr/ui/submission*.py` |
| Whether the commands are offered | `MainWindow._offer_submitting` in `main_window.py` |

Section numbers in the comments of `nostr/standup/` are the contract's. Change the contract in STANDUP first, then copy its fixtures here.
