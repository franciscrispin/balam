# Telegram Web — site-specific notes

Quirks worked out while driving `web.telegram.org` with this skill. Nothing
here is loaded by default — read it only when a task involves Telegram Web.
The skill itself stays site-agnostic; this file is just accumulated know-how.

## Which client

- `https://web.telegram.org/a/` — "WebA". `https://web.telegram.org/k/` —
  "WebK". Both are single-page apps and both keep the **login session in
  IndexedDB** (WebA: a DB named `tt-data`), not in cookies/localStorage.
  → `playwright-cli state-save`/`state-load` (cookies + localStorage only)
  does **not** carry a Telegram login. You must use a **persistent profile**
  (`headed-browser/profile.sh <name> https://web.telegram.org/a/`), which
  keeps the whole on-disk profile including IndexedDB.

## Logging in (the user does this, not the agent)

1. `headed-browser/profile.sh telegram https://web.telegram.org/a/` — opens
   the login screen (a rotating QR code + "Log in by phone number").
2. The user, in the noVNC window, either:
   - scans the QR with their phone: Telegram app → Settings → Devices →
     Link Desktop Device → point at the QR (it rotates every ~20–30 s, so
     they must look at the live noVNC view, not a screenshot), then approve
     on the phone; or
   - clicks "Log in by phone number", enters the number, then the code
     Telegram sends (and the 2FA password if set).
3. After that the session lives in the `telegram` profile on the VM. Future
   `profile.sh telegram …` runs start already logged in. Do **not** re-login
   unless it expired; the agent never types the user's credentials.
4. To revoke: Telegram → Settings → Devices. To wipe locally:
   `rm -rf ~/.cache/browser-use/profiles/telegram`.

## Navigating

- Chat folders show as a horizontal tab strip at the top of the chat list
  ("All", plus any custom folders). They are clickable `[cursor=pointer]`
  generics; clicking one filters the chat list to that folder.
- A chat is opened by clicking its entry in the chat list. The URL then
  becomes `https://web.telegram.org/a/#<chatId>` (and `#<chatId>_<msgId>`
  when jumped to a specific message). Verify the URL after clicking — it is
  the reliable signal that the right chat is open.
- `playwright-cli goto 'https://web.telegram.org/a/#<chatId>'` on an
  already-loaded tab does **not** reliably route to that chat (the SPA reset
  to `/a/` in testing). Click the chat-list entry instead.
- Chat-list previews sometimes end with an inline button like "Open" (opens a
  mini-app or jumps to a message, possibly in a *different* chat). Clicking a
  chat-list entry via a broad text locator can hit that instead — prefer the
  fresh `ref=eNN` for the chat link, and re-check the URL afterward.

## Sending a file to a chat

The attachment control is a **hover menu**, and the OS file dialog never
appears (headless X + Playwright intercepts the chooser — see the file-upload
note in `SKILL.md`). Flow that works:

1. Open the target chat; confirm the URL is `…/a/#<chatId>`.
2. Hover the paperclip and click the "File" item **in one action** (the menu
   closes between separate `playwright-cli` commands):
   ```sh
   playwright-cli run-code 'async () => {
     await page.getByRole("button", { name: "Add an attachment" }).hover();
     await page.waitForTimeout(300);
     await page.getByRole("menuitem", { name: "File", exact: true }).click({ timeout: 4000 });
   }'
   ```
   That raises a pending file-chooser modal.
3. `playwright-cli upload /absolute/path/on/the/VM/file.ext` — feeds the file
   to the chooser. (`scp` a local file to the VM first if it isn't there.)
4. Telegram opens a **"Send File"** dialog: file name + size, an "Add a
   caption…" textbox, and a send button. The send button has **no accessible
   name** (just an icon) — take a fresh `snapshot`, find the unnamed
   `button [ref=eNN]` at the end of the caption row, screenshot for the user,
   then click it to send. (To cancel instead: "Cancel attachments" button, or
   press Escape.)
5. Confirm: the "Send File" dialog closes and a file message appears in the
   chat. Sending is **not** automatic — step 4's click is what sends it, so
   confirm with the user before doing it (it is an irreversible outward
   action).

## Stuck file-chooser modals

If the paperclip → "File" path is clicked with nothing to satisfy it (e.g. a
user clicking the paperclip in the noVNC window), Telegram/Playwright leaves a
pending file-chooser modal that blocks `snapshot`/`eval`/`click`. Clear it by
`playwright-cli upload <file>` (then cancel the resulting preview) or, more
simply, reopen the browser (`playwright-cli close` then `profile.sh telegram
…`) — the login persists on disk.

## Window size

Telegram Web is responsive and lays out fine at the default Xvfb size. No
special handling needed beyond what `ensure.sh` already does (window fills the
screen, viewport tracks the window).
