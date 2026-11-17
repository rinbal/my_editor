# How MyEditor speaks Nostr

The technical side of MyEditor's Nostr features: which NIPs and event kinds it uses, how it routes to relays, and what it does to keep keys and private data safe. The user pages ([Publishing to Nostr](../use/nostr/README.md)) say how to use them. Nostr stays opt-in throughout ([decision 2](../decisions/0002-nostr-is-opt-in.md)): nothing here runs until an account is in use.

| Feature | NIPs and kinds | Main modules |
|---|---|---|
| Notes and articles | kind 1, NIP-23 kind 30023, NIP-27 mentions, NIP-18 quotes, NIP-89 client tag | `nostr/publisher.py`, `nostr/publish_pipeline.py`, `markdown_writer.py` |
| Signing | NIP-46 over NIP-44 v2, NIP-49 backups | `nostr/bunker.py`, `nostr/local_signer.py`, `nostr/key_vault.py` |
| Relays | NIP-65 outbox model, NIP-42 sign-in | `nostr/outbox/`, `nostr/relay_auth.py` |
| Drafts | NIP-37 kind 31234, NIP-40 expiry | `nostr/drafts.py`, `nostr/draft_sync.py`, `nostr/draft_relays.py` |
| Deleting | NIP-09 | `nostr/deletion.py` |
| Media | Blossom (BUD-01 to BUD-04), kind 24242 auth, kind 10063 server list, NIP-92 `imeta` | `nostr/blossom/`, `nostr/media/` |
| Zaps | NIP-57 zap splits | `nostr/metadata.py` |
| Imports | RSS, Atom, JSON Feed; kind 30078 source list | `nostr/imports/`, `nostr/rss/` |
| Membership | the association's API, through the sidecar | `nostr/einundzwanzig_api/`, `sidecar/` |
| STANDUP submissions | see [standup-submissions.md](standup-submissions.md) | `nostr/standup/` |

## Publishing

- **Short notes** (kind 1, `Nostr > Publish as Note…`): the editor's content as plain text, since that is what apps show for a note. Bold and italic are left out; list markers and link addresses stay. Publish as Note is a sheet on the document's window with the note's text in a box, where it can still be changed before it goes out, as a share sheet composes what it shares: the document is not changed by it. A mention in the box shows as "@Name", whether it was in the text, pasted as a `nostr:npub1…` link, or chosen by typing `@` at the start of a word; `Backspace` takes it away whole, and it goes out (and is copied) as the person's `nostr:` link. **People** under the box shows, by name, everyone the note will tag.
- **Long-form articles** (NIP-23 kind 30023, `Nostr > Publish as Article…`): the body is the same Markdown a `.md` save writes (`markdown_writer.py`): bold, italic, strikethrough, inline code, links, headings, lists, quotes, code blocks, tables and task lists reach Nostr. Underline and colors have no Markdown and stay in local files. Lines typed one under the other become paragraphs of their own (NIP-23 asks for no hard line breaks), and Markdown typed by hand (`## Heading`, `[label](url)`, a code fence) is published as typed. Footnotes are kept as the text Markdown writes for them (`[^1]` and `[^1]: the note`) and published as such; the preview draws them as most reading apps do and says which apps show them as typed. An article draft or `.md` file that holds something the editor cannot show (raw HTML, an image without alt text, a footnote of two paragraphs, a tab in code, code inside a list item, aligned table columns) opens as its Markdown text and is published exactly as written, so nothing is lost. Title, summary, link name (the `d`-tag identifier), cover image, and hashtags come with it. Re-publishing with the same link name replaces the previous version, so an article stays addressable as one `naddr1…` link across edits.

- **The date an article first went out** stays with it through every edit: readers date the article by it. MyEditor takes it from the article's draft, or from what this computer remembers of its own publications (`~/.config/my_editor/article_first_published.json`), or asks the relays you publish to, the ones you read from and the indexers. An article none of them holds is new and dated today, as long as at least one of the relays you publish to (and an indexer) answered: one relay that is down does not hold up a new article. When none of the relays you publish to answers, nobody can tell whether the article went out before, so MyEditor asks you: **Try Again**, **Publish as New** (today's date) or **Cancel**. A draft saved after publishing keeps the date, so another device that opens the draft keeps it too.

Both flows display a `Published from MyEditor` client tag so readers that honour NIP-89 can show which app produced the note.

## Signing

**Connect Signer App** shows a NIP-46 `nostrconnect://` link as a code to scan, renewed while the window is open, and offers three other ways: **Connect with Bunker46** opens the person's Bunker46 (an `https` address kept in the settings) with the link, **Open Signer App on This Computer** hands the link to the system, and **Details** takes a pasted `bunker://` address. One way is in flight at a time (`nostr/ui/connect_dialog.py`).

Connected profiles are saved to `~/.config/my_editor/nostr_profiles.json` with `chmod 600`. The local channel keypair lives there too; your real `nsec` does not.

An account kept on this computer signs in MyEditor itself (`nostr/local_signer.py`), without a prompt. Backups are NIP-49 `ncryptsec` files, which other Nostr apps import too.

## Mentions and quotes

While a Nostr account is in use, type `@` at the start of a word in a document (or choose `Insert > Mention…`, also in the Nostr menu and the slash menu) to mention someone: a short list opens at the caret with the people you follow first ("Following"), then everyone a search finds. Type a name or a Nostr address to narrow it, and choose one: "@Name" takes the place of the `@`, as a link to the person; `Escape` keeps the `@`. `Backspace` right after a mention takes it away whole. A mention is published as the person's Nostr link (`nostr:npub1…`, or `nostr:nprofile1…` with a relay where they can be found), which every reader shows as their name, and gets a `p` tag so they hear about it (NIP-27); opened again, it shows the name. A person's Nostr address shows in the list only once their domain has confirmed it (`nostr/nip05_check.py`): anyone can write any address into a profile. An `@` inside a word (an email address) or in code stays an `@`.

The people a note or an article will tag are shown in the publish sheets under **People**, by name, from the event the publisher builds: those the text mentions, the authors of what it quotes, and an article's co-authors. Mentions are made in the text itself; there is no second list of them. Inline `nostr:n…` links you paste yourself are tagged too, each person once.

A note or article that quotes another one (a `nostr:` link to a note or an article in its text) carries a `q` tag for each (NIP-18: the note's id, a relay hint and its author; the article's address), and a `p` tag for each quoted author, once, also when mentioned. A link inside code is text and quotes nothing. `Insert > Note or Article…` puts such a link into the text as a paragraph of its own (NIP-27), where readers show the note or article as a card.

## Co-authors and zap splits

An article's details (title, summary, cover, hashtags, link name, the date it first went out) are one model for each tab (`nostr/article_details.py`), shown and changed in **Article Details** (`View > Article Details`) and in the Publish as Article sheet alike; both add and remove hashtags, the cover and co-authors the same way (`nostr/ui/tag_field.py`, `cover_field.py`, `coauthors.py`). A draft holds them in its inner event; a Markdown file in its front matter. A co-author is tagged `["p", <pubkey>, <relay>, "author"]`, the marker standup writes and reads back for "this person wrote it"; a co-author also mentioned in the text keeps one `p` tag, the one with the marker. With co-authors, the signer is tagged first the same way, since standup takes the first `author` tag as the writer. A co-author tag is credit for writing the article and nothing more (the owner's decision, "Credit, zaps optional"): sharing zaps is a separate choice. Zaps are shared only when you choose to: then each person, you included (with your first write relay), gets a NIP-57 `["zap", <pubkey>, <relay>, "1"]` tag of four parts, as readers of zap splits read them; a co-author whose profile names no lightning address (`lud16` or `lud06`, where a zap is paid) gets none (`nostr/metadata.can_receive_zaps`). Zaps another app shared keep the weights it gave them until you change them in Article Details.

## Audio and video

`Insert > Audio or Video…` makes the file an asset of the document, as a picture is (`AssetManager.adopt_media`: named by its hash, uploaded through the asset queue, kept through a crash or a restart), shown as a card (`nostr/ui/media_card.py`). Its type comes from one table (`media_files.py`), never the system's. Once uploaded it is written as its address, with its extension so readers can tell audio from video (`media_files.address_with_extension`), and described by an `imeta` tag (NIP-92: its type, hash and size); until then it is written nowhere, nothing is published while one is in the text, and a file saved meanwhile is edited again when the upload is done. It stays a paragraph of its own however it comes in (`HtmlEditor._set_media_apart` for pastes and drops).

## Relays and routing

Relay choice follows the **NIP-65 outbox model**, decided in one place (`nostr/outbox`):

- A note or article goes to **your write relays** from your kind 10002 list first, then a membership relay if you have one, then the **read relays of everyone it mentions** (or the relay hint in the mention when their list is unknown). Your relay list goes along to those people's relays, so readers there can find the rest of what you write. While your list is unknown, or has fewer than two write relays, a small curated set fills in.
- NIP-65 asks for all of a mentioned person's read relays; MyEditor sends to the first two of each (`INBOX_PER_MENTION`) and to ten for all mentions together (`INBOX_TOTAL_CAP`), so a note naming many people does not fan out to dozens of relays. The slots go round: everyone's first read relay, then everyone's second, so the people named last are reached too. The relay lists of everyone a note mentions are looked up in one request.
- **Private drafts**, the synced feed list and the private media library are written to your write and read relays, a membership relay, and the signer relays older versions stored drafts on (at most ten; a long relay list never pushes out the last two). While your list is still unknown, the curated set stands in for your own relays. Reading asks every relay writing goes to plus that curated set, so a draft saved before your list was known is still found once it is; deleting a draft reaches all of them too. Drafts can be kept on relays of your own choosing instead (Nostr > Draft Relays…).
- **Someone else's article** (an `naddr`, an `npub`'s articles) is read from **their** write relays, never from yours.
- Relay lists are looked up on your own relays and on indexers, verified by signature, and cached: for 30 minutes when found, while your own list is also kept on disk so routing is right from the first second after a launch.
- Relays named by other people (their relay lists, relay hints in mentions and `naddr`/`nevent` links) are used only when they are public `wss://` relays: plain `ws://`, `localhost`, private and link-local addresses, `.local`/`.lan` style names and `.onion` are skipped, so a stranger's event cannot make MyEditor connect into your own network. Your own relay list is used as you wrote it.
- Your own profile and contact list are read the same way, and only a validly signed event counts.
- **Relays that ask you to sign in** (NIP-42). Some relays take or show events only after you sign in to them, for example a relay that keeps your drafts private. When one of your own relays (your relay list, the relays your drafts are kept on, a membership relay) refuses a request until you sign in, MyEditor signs in with your account and asks again; the status bar says `Signing in to wss://…`. A signer app asks you to approve that, as for anything it signs. MyEditor never signs in unasked, and never to a relay you didn't choose, since signing in tells the relay who you are. A sign-in lasts as long as the connection. If your signer app declines, or the relay still refuses, the status bar says so and the relay's refusal stands. The relays of a signer app's channel are signed in to with the channel's own key, which says nothing about your account.

The publisher uses eager-first-accept semantics: as soon as one relay acknowledges the event, the dialog shows the result; remaining relays continue in the background and the final count lands in the status bar (e.g. `Published to Nostr: 6/7 relays · note1xxxxx…`).

## Security model

- **With a signer, no private key in the editor.** All signing goes through NIP-46 over NIP-44 v2 encryption. Every `sign_event` call surfaces an approval prompt in your signer.
- **An account kept on this computer** has its key in `~/.config/my_editor/nostr_keys.json` (`chmod 600`, folder `chmod 700`), separate from the profiles. It is never shown on screen, and Sign Out deletes it, after an alert that says what a missing backup costs. A damaged key file is set aside, never overwritten.
- **No secret key goes out by mistake.** While a Nostr account is in use, a paste or drop of a Nostr secret key (`nsec1…`, or an encrypted `ncryptsec1…`) into a document is stopped with a warning; you may paste its public key instead (the link to the key's owner). Nothing is published with a secret key in it. When you choose Publish, MyEditor looks at the note or article as it would go out (its text and link addresses, the title, summary and tags, whatever was typed into the publish sheet) and offers to publish it with each key replaced by its public key, or, for an encrypted key, asks you to remove it. A key in an article's or a draft's link name is shown under the link name before anything is sent, with Publish or Save dimmed (the link name is published as it is, and is never rewritten; one made from the title leaves a key out), and Edit Profile shows a key in a field under that field, with Save dimmed. Underneath all of these, nothing with a secret key in its text or tags is signed at all, by a key kept on this computer or by a signer app, so no other way can get one out; a draft's encrypted text is never affected. Plain-text and code files, and documents without an account, paste as they are.
- **Connection spoof protection.** The `nostrconnect://` flow generates a one-time secret that the editor verifies against the signer's response before completing the handshake.
- **Profile file permissions.** The on-disk profile store is restricted to the owner. The local channel keypair stored there only authorizes the existing bunker session; it cannot sign anything itself.

## Drafts

In-progress work is saved as a **NIP-37 draft**: a kind 31234 event whose body is NIP-44 encrypted to your own Nostr key. Only you can decrypt it, and the encryption happens inside your signer so the editor never holds the plaintext key.

**Cross-device by design.** Drafts live on your own relays, and every device reads them from exactly where they were written. Sign in with the same Nostr profile on another device and the editor pulls those drafts straight back into the panel. No cloud account, no separate service.

Where drafts are kept is itself a NIP-37 setting on the person's relays, encrypted to their own key, so other apps that read it (Amethyst does) keep drafts in the same place. Changing it copies every draft, already signed, to the new relays and reads each one back before the new setting is published. Relays left behind that do not answer are kept in a list and asked again in the background until they do.

- Drafts you save with this version never expire. An imported draft you don't change is removed after 90 days (NIP-40); once you change and save it, it stays like any other draft.
- A draft saved with an earlier version of MyEditor keeps the end date it was saved with (90 days after that save) until you save it again. The Drafts list shows that date in the draft's row ("Removed on ... unless you save it again"); open the draft and save it to keep it.
- Notes are tagged with a private UUID; articles use a stable link name, so the draft and its eventual published article share the same address.
- Deleting a draft publishes an empty replacement so your other devices see it removed; it never expires, so a relay that kept an old copy cannot bring the draft back.

## Media (Blossom)

Keep images, video, audio and common files on your own Blossom media servers, browse them in a library that works like a Finder window or Photos, and put them into notes and articles. Files include every document format MyEditor opens or saves: PDF, plain text, Markdown, R Markdown, HTML and RTF. CSV and JSON, Office, OpenDocument and Apple iWork documents, spreadsheets and presentations, archives and EPUB books are supported too. File tiles and Quick Look use the operating system's file-type icons when available, with recognizable document, spreadsheet, presentation, archive and book glyphs as a fallback. Extension labels distinguish individual formats. Servers may restrict accepted types or file sizes; upload failures appear on the file's tile with Retry. Every upload is authorised by a kind 24242 event your signer signs; no separate API key, no third-party account.

Everything MyEditor uploads can be opened by anyone who has its link: a media server serves a file to whoever asks for its address. That is the normal case, so no tile is marked for it; the library says it once, in the quiet line at its foot ("Anyone with the link can see the files you add here."), and the questions before a pasted or dropped image is uploaded say it too. The exception is marked: files kept privately (encrypted, in the kind 34578 library other apps such as Lotus write) carry a small lock in the corner of their thumbnail ("Only you can see this"); MyEditor shows their pictures by opening them in memory, never on disk.

Private video, audio, PDF and text previews also open in memory: the shared cache keeps only their encrypted envelopes. Files larger than 128 MiB are not decrypted for a preview; **Save As…** remains available. Previewing never makes a public copy. A file whose privacy has not been checked is not handed to these viewers.

**Media Servers…** (in the library's action menu) lists your servers in the order uploads use them: new files go to the first, the others keep copies. **Make Primary**, **Remove** (after asking; files already there stay there) and **Add Server** (by its address) change the list, which is saved in `~/.config/my_editor/blossom_servers.json`. The servers your Nostr account lists (its kind 10063 list) and MyEditor does not use yet are offered there; on a first run with nothing set up, that list is used as it is. **Share with Other Apps…** publishes your list as your account's kind 10063, after asking, so other Nostr apps find your files.

The defaults, chosen for two independent operators with a published per-file limit:

| URL | Per file | |
|---|---|---|
| `https://nostr.download` | 100 MiB | first |
| `https://blossom.primal.net` | about 100 MiB (unpublished) | copies |

**EINUNDZWANZIG members** also get the members' server, `https://blossom.einundzwanzig.space` (1 GiB per file, 5 GiB per member), while the membership is active. It is listed in Media Servers under the association's name, its files appear in the library even when another app uploaded them, and its storage line is the one at the foot of the library.

What happens across servers stays in the background: an upload goes to the first server that can take the file (each server's published limit decides, before anything is sent) and is then copied to the others one at a time, through BUD-04 mirroring or, where a server cannot mirror, a direct upload; a server whose allowance is full is left out. When a server does not keep a copy, the library says so once, when the file is added. A delete goes to every server the file is on; when one cannot be reached, the file stays in the library, saying it is still on one of your servers, until a later Delete reaches it.

- **No separate credential.** Every privileged Blossom request carries an `Authorization: Nostr <base64(signed event)>` header where the event is a kind 24242 you signed via NIP-46. Your signer prompts you to approve the first upload / list call in a session.
- **No CORS proxy.** Requests go straight from the desktop app to the Blossom servers.
- **Content-addressed cache.** Thumbnails and previewed bytes are cached at `~/.config/my_editor/blossom_cache/<sha256>` and verified against the hash before use; a file loaded by an address that names its hash (the last 64-hex run, BUD-03) is refused when the bytes are another file.
- **Your own copy on each server.** A server that already has the bytes (someone else uploaded them) still gets the upload, so the file is in your own listing, yours to delete, and does not vanish when the other owner deletes theirs. Only a server your own fresh listing shows holding the file is skipped.

## Imports

Auto-discovery reads `<link rel="alternate" type="application/rss+xml">` from the page head, falls back to the well-known `/feed/` path for WordPress / Ghost / Substack, and explains itself in plain language when no feed is found.

Supports RSS 2.0, Atom, and JSON Feed (WordPress, Ghost, Hugo, Jekyll, Substack, Bear, Mataroa, and friends).

Nothing an import reads may point into your own network: feeds, pages, podcast chapters, site icons and images on `localhost`, local-only names (`router`, `nas.local`, `.lan`), private or link-local addresses, or names that resolve to one are refused, and every redirect is checked the same way before it is followed (five at most). Each connection then goes to the very address that was checked, so a name whose answer changes after the check cannot lead into your network either (DNS rebinding). Behind a proxy the address is still checked, and the proxy makes the connection.

### Nostr-native publishers

Some publishers (Habla, Yakihonne, Pareto, self-hosted Nostr-aware blogs) emit feeds where the body is a teaser and the real article lives on Nostr as a kind:30023 long-form event. When the feed's link is a `nostr:naddr...` URI, or contains a bech32 naddr embedded in an HTTP URL (njump.me, habla.news, yakihonne.com, etc.), the importer fetches the event from your NIP-65 read relays plus the relay hints encoded in the address and uses its prose as the draft body. If the fetch times out or the event is empty, the feed-provided teaser is published instead so the draft always ships.

### Sources shared with EINUNDZWANZIG STANDUP

The sources you subscribe to are one private list on your relays, the same list EINUNDZWANZIG STANDUP keeps (a kind 30078 event, `d` = `einundzwanzig:feed-sources`, encrypted to yourself with NIP-44). A source followed in one app shows up in the other, together with its defaults for new drafts (copy images, fetch the full article).

- Both apps' edits are kept: before saving, MyEditor reads the list again and merges (sources added in either app stay, sources removed in either app go, a title or a default changed here wins over the other app's unchanged one).
- Whatever MyEditor does not understand (sources only STANDUP reads, keys it does not know) is written back unchanged.
- A list saved with the older encryption, NIP-04, is read too: with a key kept on this computer, or through your signer app when it offers that (MyEditor asks for it when it pairs). MyEditor writes the list back with NIP-44 only.
- A list MyEditor cannot read (while your signer app does not answer, or one it cannot decrypt) is never written over; your changes stay on this computer until it can be read, and the Imports window says so above the list.
- The list MyEditor kept before (`d` = `myeditor:feed-sources`) is merged in once and not written again.
- Quitting waits a few seconds for changes that are still on their way. Changes that could not be sent yet are kept with the list on this computer and sent at the next start.

### Importing again never overwrites a draft

Each item's draft identifier is derived from its feed id the same way EINUNDZWANZIG STANDUP derives it, so the same post gets the same identifier in both apps. Before anything is fetched or signed, MyEditor asks your relays which of the chosen posts already have a draft (also one you deleted), an article or a deletion request, and leaves those alone: a draft you edited after importing it stays exactly as it is, and a deleted one does not come back. When no relay answers, the import does not start. The question is asked once more right before each draft is signed, in case the other app made it in the meantime.

## EINUNDZWANZIG membership

- The window names every service the association lists for its members, in its order: the members' relay, a Nostr address, a Lightning watchtower, media storage, the community group and Buzz (experimental). The association publishes that list only as text on its member portal, so `MEMBER_SERVICES` in `nostr/einundzwanzig.py` is a copy, with the source file and commit it was taken from; a link opens the association's own page with every detail and setup guide. MyEditor sets up the relay, the address and the media server itself; the member page links to the association's guide for the others.
- Which host does what: `wss://nostr.einundzwanzig.space` is the members' relay for ordinary notes and articles (it goes in the relay list); `https://blossom.einundzwanzig.space` is the media server and has no relay that goes with it; the community group and Buzz relays need a login and never go in a relay list.
- The fee shows in sats first, then CHF, then EUR. The association's own amount is exact; the others are converted with today's Bitcoin price from mempool.space (asked once while the window is open; mempool.space sees this computer's address, but no account and no reason for asking) and say "about". The invoice's sats are always exact.

The association's API needs a client key that must not ship inside the app, so membership requests go through a small service that holds it: the membership sidecar in [`sidecar/`](../../sidecar/README.md).
- A build names its sidecar in `constants.MEMBERSHIP_SERVICE_URL`. Official builds name the maintainers' sidecar, `https://e21.rinbal.de`.
- A self-hosted build names its own sidecar, or none. `MYEDITOR_MEMBERSHIP_SERVICE` in the environment names one without rebuilding (an `https://` address, or `http://` on this computer).
- Without a sidecar, with one that has no key, or with one whose key the association refuses, the window says joining in the app isn't available right now and offers the association's website instead.
- Members get their relay and media server either way, recognized from the association's public member list.
- Running a sidecar (Docker with Caddy, or systemd behind Caddy or nginx), its limits and what it logs are in [`sidecar/README.md`](../../sidecar/README.md). Its server's clock must be synchronized (NTP), because signatures are valid for a minute only.
