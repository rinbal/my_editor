# Publish notes and articles

Write in MyEditor as always, then choose how it goes out: as a short **note**, or as a long-form **article** with a title, a summary, a cover and hashtags. Both need an account in use ([Your account](account.md)). With a signer app, it asks you to approve each post.

## Publish a note

Choose **Nostr > Publish as Note…** (**⌥⌘P** / **Ctrl+Shift+P**). A sheet slides over the window with the note's text in a box. Notes are plain text, as Nostr apps show them, so bold and italic are left out; list markers and link addresses stay. You can still change the text in the box; your document stays as it is. **People** under the box shows, by name, everyone the note will tag. Click **Publish**.

## Publish an article

Choose **Nostr > Publish as Article…** (**⇧⌘A** / **Ctrl+Shift+A**). The sheet shows the article's details and a **Preview** card of how readers see it in their lists:

- **Title**: the article's own, else its first heading.
- **Summary**: one sentence, shown under the title in feeds.
- **Hashtags**: **Return** or a comma makes a tag, **×** or **Backspace** removes one.
- **Cover image**: **Choose…** from your [Media Library](media.md), drop a picture file on it, or **Remove** it. **Adjust…** frames it; 16:9 at 1600 × 900 pixels suits most readers.
- **People**: everyone the article tags, by name: people mentioned in the text, authors it quotes, and co-authors.
- **Show Details**: the article's **Link name**, the last part of its web address. It is made from the title until you type one.

The article's text is your document exactly as written: headings, bold, italic, links, lists, quotes, code, tables, footnotes and photo groups. Underline and colors have no Markdown and stay in your local file.

**Publishing again updates the article.** An article keeps its link name once it is out, so publishing it again replaces the old version at the same address, and readers keep the date it first went out.

## Article Details

**View > Article Details** (**⌥⌘I** / **Ctrl+F5**) opens the same details in a panel beside the document, as Pages has its inspector: title, summary, cover, hashtags, authors, and under **Advanced** the link name and when it first went out. What you type there belongs to the article at once, and the publish sheet shows the same.

A Markdown file keeps these details in its front matter, the block at its top that Jekyll, Hugo and Obsidian use. MyEditor changes only the lines you changed, in the file's own style, and leaves everything else in that block as written.

## Co-authors and zaps

**Add Co-Author…** names a person as a co-author. That is credit and nothing more. If you also want to share the zaps readers send, turn on **Share zaps equally with co-authors**: then everyone, you included, gets an equal share. A co-author without a Lightning address in their profile cannot receive zaps, and a line under the option says so. [Decision 4](../../decisions/0004-co-author-credit-zaps-optional.md) explains why.

## Mention people and quote posts

- **Mention**: type `@` at the start of a word, or choose **Insert > Mention…**. A list opens with the people you follow first; type a name to narrow it and choose one. "@Name" goes into the text as a link to the person, who hears about it. **Backspace** right after a mention removes it whole.
- **Quote a note or an article**: choose **Insert > Note or Article…** and paste its link (a `nostr:` link, or the address njump.me, Primal or YakiHonne shows it at). Readers show it as a card.
- **Audio and video**: **Insert > Audio or Video…**, or drop the file on the window. It shows as a card and goes to your media servers as a picture does; readers show a player. Nothing is published while an upload is still on its way.

## Pictures

Pictures you put into a document stay on this computer until you publish, then go to your media servers. **Settings > Upload files: Right Away** uploads them as soon as you add them instead. Camera and location details are removed from a cover before it is uploaded.

## What you published

Choose **Nostr > Published** (**⇧⌘B** / **Ctrl+Shift+B**) to see your notes and articles, newest first, in the Drafts panel's **Published** view. **Load More Notes** at the end brings older notes. **Nostr > Refresh Drafts** (**⌘R** / **F5**) reads the list again. Right-click a row, or press the Menu key, for:

- **Edit Article**: opens it in a tab. Publishing it again replaces it and keeps its address and first date.
- **Open in Browser**: shows it on njump.me, a page any browser can open.
- **Copy Link**: copies that address, to share.
- **Delete from Nostr…**: asks first, then asks the relays to remove it. People and apps that already saved a copy may still have it.

## If something goes wrong

When no relay takes a post, the sheet says so in one sentence and offers **Try Again**; nothing is lost. When only some relays answer, MyEditor carries on quietly: one relay is enough for readers to find your post.
