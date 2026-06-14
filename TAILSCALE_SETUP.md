# Control KalshiBot remotely with Tailscale

Tailscale is a free, secure mesh VPN. Once both your **home PC** and your **laptop**
are signed into the *same* Tailscale account, the laptop can open the bot's web UI
exactly as if it were on your home network — from anywhere, no port forwarding, no
public exposure. (You already get hands-off *code* updates via the auto-updater;
Tailscale is for *clicking buttons / viewing the UI* remotely.)

Total time: ~5 minutes. You do this once per machine.

---

## 1. Home PC (the machine that runs the bot)

1. Download & install Tailscale: https://tailscale.com/download/windows
2. Launch it → **Log in** → sign in (Google / Microsoft / GitHub — pick one you'll
   also use on the laptop).
3. After login, the Tailscale tray icon shows **Connected**. That's it.
4. Allow the bot's port through Windows Firewall (one time). Open **PowerShell as
   Administrator** and paste:
   ```powershell
   New-NetFirewallRule -DisplayName "KalshiBot 5003" -Direction Inbound -Protocol TCP -LocalPort 5003 -Action Allow
   ```
   (Without this, Windows may block the laptop from reaching port 5003.)
5. Start the bot with `run_bot.bat` (located in your bot folder, e.g. `%USERPROFILE%\bots\kalshibot\run_bot.bat`). In the CMD window
   you'll now see a line like:
   ```
   📱 Remote (Tailscale): http://100.x.y.z:5003
   ```
   **Write down that `100.x.y.z` address** — that's your home PC on the tailnet.

---

## 2. Laptop (the machine you take with you)

1. Install Tailscale: https://tailscale.com/download
2. Log in with **the same account** you used on the home PC.
3. Done. To reach the bot from anywhere, open a browser to:
   ```
   http://100.x.y.z:5003
   ```
   (the address the home PC printed). You can also use the home PC's Tailscale
   *machine name*, e.g. `http://your-pc-name:5003`, which you can see in the
   Tailscale admin console at https://login.tailscale.com/admin/machines.

---

## Tips

- **Bookmark** `http://100.x.y.z:5003` on the laptop and phone for one-tap access.
- The home PC must be **powered on and running `run_bot.bat`** for the UI to answer.
- Tailscale IPs are stable — they don't change when your home IP changes.
- The bot already binds to `0.0.0.0:5003`, so nothing else needs configuring.
- Phone works too: install the Tailscale app, sign in, open the `100.x.y.z:5003`
  URL in your mobile browser.

## Troubleshooting

- **Laptop can't load the page:** confirm both machines show **Connected** in
  Tailscale, and that you ran the firewall command in step 1.4 on the home PC.
- **No `📱 Remote (Tailscale)` line in CMD:** Tailscale isn't running/connected on
  the home PC, or it was installed after the bot started — restart `run_bot.bat`.
- **Want it locked down further:** Tailscale ACLs (admin console) can restrict which
  devices may reach port 5003.

---

## Running the laptop as a BACKUP (and not double-trading)

**Only ONE bot may trade a Kalshi account at a time.** Normally the home PC is
the single bot and the laptop just *views* it over Tailscale. The laptop should
only *run* a bot if the home PC has died.

To make that safe, set the laptop's bot to "know about" the home PC so it can warn
you. On the **laptop**, before launching, set this environment variable to the home
PC's Tailscale URL (nothing is hardcoded — you fill in your own address):

```bat
set KALSHIBOT_PEER_URL=http://100.x.y.z:5003
```

(Put that line at the top of the laptop's `run_bot.bat`, above `:loop`.)

With it set, the laptop's UI will:
- **Warn before you start** a bot if the home PC is already trading — you can Cancel
  and leave the home PC running.
- **Prompt you to stop** the laptop bot if the home PC comes back online and resumes
  trading, so you can hand control back and manage it remotely over Tailscale.

The **home PC leaves `KALSHIBOT_PEER_URL` unset**, so it never prompts — no one needs
to be sitting at it to click anything.
