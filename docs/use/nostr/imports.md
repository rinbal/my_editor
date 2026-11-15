# Imports

Imports bring posts from elsewhere into private Nostr drafts: your own blog, a website you follow, an export file or a single link. Nothing is published until you publish a draft yourself.

Open **Nostr > Imports…** (**⇧⌘N** / **Ctrl+Shift+N**), or the **Imports** row at the top of the Drafts panel, which also says how many new posts wait.

## Add a source

**Add** offers three ways:

- **Follow a Website…**: paste an address. The site's home page or any article on it is enough; MyEditor finds the feed itself. Before anything is kept, it shows what it found: the site's name, how many posts there are and how new the newest is. While MyEditor is open, it checks the site for new posts, which appear in the **Inbox**. Posts that were already there wait in **Older Posts**.
- **Import a File…**, or drop the file on the window: a WordPress, Ghost, Medium or Substack export, a feed file, a Markdown file, or a list of sources to follow (OPML).
- **Import a Link…**: one post, thread, Nostr article or Markdown file, imported once.

## Make drafts

Check the posts you want, or open one, and click **Create Drafts** (it says how many, for example **Create 3 Drafts**; **⌘Return** / **Ctrl+Return**). **Options…** chooses whether pictures are copied to your media server, and whether the whole article is fetched when a feed gives only its start.

Each draft keeps the post's title, summary, cover, hashtags and original date, and ends with a line naming where it was first published. Pictures, videos and code blocks come along as the post shows them; buttons, forms and tracking pixels stay behind.

**Skip** (or **Delete**) sets posts aside. **Undo** brings them back, and so does **Restore** in the Skipped list.

Importing again never overwrites a draft: a post that already has a draft, an article, or was deleted, is left alone.

## Each source's settings

Choose **Source Settings…** in a source's menu (the button beside it, or a right-click):

- Its name and address, and when it was last checked.
- **Check for new posts automatically**. When it is off, the source is read only when you open it or choose **Check Now**.
- Its defaults for new drafts: copy pictures, and fetch the whole article.
- **Unsubscribe…** removes the source and its waiting posts. Drafts made from it stay.

**Add > Export Sources…** writes every source to an OPML file, which other feed readers import.

## New posts on the app icon

**Nostr > Show New Posts on the App Icon** puts the number of waiting posts on MyEditor's icon, in the Dock, on the taskbar, or in a Linux launcher that shows counts. It is off until you turn it on. There are no other notifications.

## Shared with EINUNDZWANZIG STANDUP

Your list of sources is kept on your relays, encrypted to yourself, and is the same list EINUNDZWANZIG STANDUP uses. A source you follow in one app shows up in the other. Changes made in both are merged, so neither app overwrites the other.

## Kept on this computer

The Inbox is kept on this computer only, for your account. A waiting post keeps its text for 90 days at most, a skipped or imported one for 30 days. After that MyEditor keeps only its title and link, enough never to offer it as new again; **Open Original** reads it on its site.
