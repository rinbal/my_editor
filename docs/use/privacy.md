# Privacy and safety

MyEditor is a local app. Your notes are files on your computer, there is no account to sign in to, and nothing about you or your writing is sent anywhere unless you publish it.

## Lock a note with a password

**File > Set Password…** locks the document with a password, as Pages does. It is encrypted in place, keeps its name and type, and asks for its password when it opens. Saving it again asks for nothing. **File > Change Password…** changes the password or removes it.

- The file uses the open [age](https://age-encryption.org) format, so `age -d` opens it without MyEditor.
- A forgotten password cannot be recovered, by MyEditor or by anyone else. Keep it somewhere safe.
- A locked document is not saved as PDF or RTF, which cannot be locked, and is not knit with R Markdown, because R reads the file itself.

## Backups while you type

Files are saved when you choose **File > Save**. On top of that, what you type is backed up as you go: two seconds after you stop typing, and every fifteen seconds while you type. After a crash, MyEditor offers each backup back exactly as it was the next time it opens.

- Backups live in `~/.cache/my_editor/backups/`. A locked document's backups are encrypted too, and ask for its password when they are offered back.
- The status bar stays quiet while backups work. If one cannot be written, for example because the disk is full, it says "Couldn't keep a backup of this document." with **Try Again**.
- **File > Revert to Saved…** throws away the changes since the last save, after asking.

When MyEditor updates itself, every open tab comes back afterwards with its unsaved changes, so there is nothing to save first.

## What goes online

Without a Nostr account, MyEditor connects to the internet only for these:

- **Updates.** Once a day, when it starts, it asks GitHub whether a new version is out. An update is downloaded only when you choose to install it, and is checked against the fingerprint GitHub published for it.
- **Downloads you ask for.** The speech model for dictation on this computer, pandoc for Word documents, and R, pandoc and TinyTeX for R Markdown. Each comes from its official source, and nothing is downloaded until you agree.
- **Pictures on the web.** A picture in your document that lives at a web address is loaded from there, as in any browser.
- **Dictation on Windows.** Windows Voice Typing uses Microsoft's online service. Dictation on this computer (**Settings > Dictation**) works offline, and your voice never leaves the computer.

Spell checking uses your system's own checker. Links you click open in your browser.

## With a Nostr account

Nostr is optional, and nothing of it is active until you create or connect an account. Once you do, MyEditor talks to Nostr relays and media servers for the things you use: publishing, drafts, the Media Library and imports.

- Drafts are encrypted to your own key before they leave the computer. Only you can read them.
- With a signer app, your private key never enters MyEditor. An account kept on this computer stores its key in a file only your user account can read.
- What you upload to a media server can be opened by anyone who has its link. MyEditor says so wherever you upload.

[Publishing to Nostr](nostr/README.md) explains each part.
