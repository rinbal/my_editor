# Drafts

A Nostr draft is a private copy of your work, kept on your relays and encrypted to your own key, so only you can read it. Open MyEditor on another computer with the same account, or another app that reads Nostr drafts, and your drafts are there. There is no cloud account and no separate service.

## Save and open drafts

- **Save a tab as a draft**: choose **File > Save As…** (**⇧⌘S** / **Ctrl+Shift+S**). While an account is in use, MyEditor asks **Where do you want to save?**: **Save to disk** or **Save as private Nostr draft**. **Remember this choice for this tab** skips the question next time.
- **Save again**: **⌘S** / **Ctrl+S** on a draft's tab saves the draft again, without a question, just like a file.
- **The Drafts panel**: **Nostr > Drafts** (**⇧⌘D** / **Ctrl+Shift+D**) shows your drafts beside the text, with a search field. Double-click a draft to open it in a tab. Right-click it for **Publish…**, **Copy Event ID** and **Delete Draft**.
- **Refresh**: **Nostr > Refresh Drafts** (**⌘R** / **F5**) reads the list again.

An article draft keeps the article's details too: its title, summary, cover, hashtags and the date it first went out. Publishing from a draft offers them all.

## When your signer app is asleep

With a signer app, it is the signer that opens your drafts. While it does not answer (the phone is asleep, or the app is closed), the Drafts panel lists the drafts that are open and says the rest in one line, for example "48 drafts are locked until your signer app answers.", with **Try Again**. MyEditor asks again every minute, and when you come back to it; the drafts open as soon as your signer answers.

If you dismissed a request in your signer app by accident, the draft shows as failed. Double-click it, or choose **Retry Decryption**.

## Choose where drafts are kept

Drafts are saved on your usual relays. To keep them on relays of your own choosing, for example a relay you run yourself, choose **Nostr > Draft Relays…**, add up to ten relays, and click **Save**.

- MyEditor first copies every draft to the new relays and checks that it can read them back. Only then does the new choice take effect. If a draft cannot be copied, nothing changes and the sheet says what to do.
- The choice is kept on your relays, encrypted, so your other devices and apps that read it (Amethyst does) keep drafts in the same place.
- **Use My Usual Relays** goes back to your usual relays.
- If a relay you left stops answering, MyEditor keeps looking for drafts there in the background and moves them over once it answers. The sheet lists such relays under **Still looking for drafts on**; **Stop Looking…** gives one up.

## How long drafts stay

- Drafts you save never expire.
- A draft made by an import that you never change is removed after 90 days. Once you change and save it, it stays like any other draft.
- A draft saved by an earlier version of MyEditor keeps the end date it was saved with until you save it again. Its row says so: "Removed on … unless you save it again".
- Deleting a draft reaches your other devices too, so it does not come back.

[How MyEditor speaks Nostr](../../develop/nostr.md#drafts) has the technical details.
