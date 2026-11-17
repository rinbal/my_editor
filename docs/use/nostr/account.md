# Your Nostr account

A Nostr account is a pair of keys: a public key that is your name on the network, and a private key that works like a password. Nostr has no password reset, so whoever holds the private key holds the account. MyEditor offers two places for it: in a **signer app** you already trust, or **on this computer**.

## Connect a signer app

With a signer app, your private key stays in that app and never enters MyEditor. The app asks you before it signs each post.

Choose **Nostr > Connect Signer App…**. The window shows a code:

- **On a phone**: open Amber, Primal or Amethyst and scan the code, then approve the connection there. The code renews itself while the window is open.
- **Bunker46**: click **Connect with Bunker46**, enter your Bunker46 address once, and confirm in the browser page that opens.
- **A signer app on this computer**: click **Open Signer App on This Computer**.
- **An app that shows a `bunker://` address**: open **Details**, paste it into **Connection address**, and click **Connect**.

If you have no signer app yet, the same window offers **Create an Account** and **Restore from Backup**.

## Create an account

Choose **Nostr > Create Account…**. It takes a few steps:

1. **Name.** Optional. People see it, and you can change it later.
2. **Backup.** Right after your key is made, because nobody can reset it. **Save Backup File…** writes a small file with your private key protected by a password of at least 8 characters. Other Nostr apps can import it too. Saving it without a password, or skipping the backup, is a separate choice that asks first.
3. **Where the key lives.** **Keep the key on this computer**, and MyEditor signs for you. Or put it in Amber on an Android phone, guided step by step, after which this computer no longer needs it.
4. **Setup.** MyEditor tells the Nostr network about your new account and publishes your profile, showing each step. If that cannot finish now, **Finish Later** keeps the account and completes the rest the next time you open MyEditor.

## Restore an account

**Nostr > Restore Account…** opens a backup file, or takes a pasted private key with **Paste a Private Key Instead**, and asks for the backup's password. Your account comes back with its profile and the relays it already uses. Nothing on the network is replaced.

## Keep the account safe

- **Nostr > Back Up Account…** saves a new backup file at any time, for an account kept on this computer.
- **Nostr > Move Key to Signer App…** moves a key kept on this computer into Amber. Afterwards MyEditor signs through Amber.
- MyEditor never shows your private key on screen, except as the code for Amber to scan, and only when you ask. When you copy it, it is marked as a secret for clipboard managers and taken off the clipboard again after a minute.
- While an account is in use, pasting a private key into a document is stopped with a warning, and nothing with a private key in it is ever published.

## Profile and several accounts

**Nostr > Edit Profile…** changes your name, username, picture, banner, about text, website, Lightning address and Nostr address.

While an account is in use, its picture sits at the right end of the tab row. Click it to switch to another account, or choose **Add Account…**. A switch counts from the next post; the publish window shows which account it will use, and has its own account menu.

## Sign out

**Nostr > Sign Out** stops using the account in MyEditor, and the Nostr features leave the window. For an account kept on this computer, signing out deletes its key from this computer, so MyEditor first says what that costs if you have no backup.
