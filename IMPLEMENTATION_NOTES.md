# Implementation Notes — Winning Wave (Ads-Optimized Edition)

This is the **ads-optimized rewrite** of the Winning Wave bot. The original was a copy of the WishWheel bot with branding changes. This version adds first-class ad campaign tracking, conversion funnel analytics, anti-fraud detection, and auto-nudge — all features that were missing and caused the 269-clicks/0-starts issue on the previous Telegram Ads campaign.

---

## What's New (vs. original Winning Wave bot)

### 1. Campaign Registry (`winning_wave_campaigns` table)

New table stores metadata for every ad campaign you run:

```sql
token TEXT PRIMARY KEY              -- 'tgads_v1'
platform TEXT NOT NULL              -- 'telegram_ads', 'mangoads', etc.
campaign_name TEXT NOT NULL         -- 'September Test'
creative TEXT                       -- '$5 free play'
target_channel TEXT                 -- 'fish-game-channels'
started_at TIMESTAMPTZ              -- when campaign began
ended_at TIMESTAMPTZ                -- when /campaigns end was called
expected_clicks INTEGER             -- your goal
actual_clicks INTEGER               -- from Telegram Ads dashboard
is_active BOOLEAN                   -- false after /campaigns end
```

Manage via `/campaigns` command in the support group.

### 2. Conversion Funnel Events (`winning_wave_events` table)

Every important user action is now logged with campaign attribution:

| Event | When it fires |
|---|---|
| `bot_start` | User presses /start (with attribution token) |
| `bonus_selected` | User clicks a bonus button |
| `game_selected` | User clicks a game button |
| `first_message` | User sends their first message |
| `nudge_sent` | Auto-nudge fired (no message within delay) |
| `nudge_acted` | User messaged after receiving a nudge |
| `topic_closed` | Staff closed the topic |
| `broadcast_received` | User received a broadcast message |

This lets `/adstats` show the full funnel per campaign: `clicks → starts → bonus → game → message`.

### 3. First-Touch / Last-Touch Attribution

Users table now tracks both:

- `first_touch_source` + `first_touch_campaign` + `first_touch_at` — set once on first /start, never overwritten
- `last_touch_source` + `last_touch_campaign` + `last_touch_at` — updates on every /start

This handles the case where a user clicks ad A today, then clicks ad B next week. You can attribute them to either campaign depending on your reporting model.

### 4. Explicit `/start` Logging

Every `/start` event now logs a structured line to Railway:

```
START_EVENT | user_id=12345 | username=@foo | args=['src_tgads_v1'] | game=none | source=📣 Telegram Ads | campaign=tgads_v1 | suspicious=False
```

This is **grep-friendly**. To verify your ad is driving real traffic:

```bash
# In Railway logs
grep "START_EVENT" railway.log
grep "START_EVENT.*tgads_v1" railway.log
grep "SUSPICIOUS" railway.log
```

### 5. Conversion-Optimized Welcome

Old welcome:
```
👋 Welcome to Winning Wave Support!
🎁 Choose your bonus:
[120% Signup Bonus] [Redeemable Freeplay]
```

New welcome (matches ad promise):
```
🌊 WELCOME TO WINNING WAVE!
🎁 FIRST 100 PLAYERS: $5 FREE PLAY
💸 CashApp • Crypto • Chime payouts
🎮 9 games: Fire Kirin, Orion Stars, Juwa + 6 more

👇 CLAIM YOUR BONUS BELOW 👇
[$5 Free Play (First 100)]    ← top button, primary CTA
[120% Signup Bonus]
[Redeemable Freeplay]
```

Key changes:
- Ad promise ("$5 free play for first 100") is now visible immediately
- Payment methods (CashApp/Crypto/Chime) shown upfront — trust signal
- Game count (9) creates choice perception
- $5 free play is the top button (highest conversion)
- Strong downward-arrow CTA

### 6. Auto-Nudge

If a user presses Start but doesn't message within 90 seconds (configurable), the bot sends:

> 👋 Hey! Still there?
> 🎁 Your $5 Free Play is waiting — just send a message and our team will hook you up!
> 💬 You can also pick a bonus below 👇

This recovers 15-25% of users who would otherwise bounce.

Tunable via env: `NUDGE_ENABLED`, `NUDGE_DELAY_SECONDS`.

### 7. Anti-Fraud Detection

Users who press `/start` >= 3 times within 60 seconds are flagged as suspicious:

- `is_suspicious = TRUE` in the database
- Visible in `/id` output: `⚠️ Suspicious: YES`
- Counted in `/adstats` per-campaign suspicious metric
- Still processed normally (not blocked) — staff decides whether to engage

Tunable via env: `SUSPICIOUS_START_THRESHOLD`, `SUSPICIOUS_START_WINDOW`.

### 8. New `/campaigns` Command

Staff can manage campaigns without touching code:

```
/campaigns                                          — list all
/campaigns add <token> <platform> <name> [...]      — register new
/campaigns end <token>                              — mark ended
/campaigns clicks <token> <count>                   — update actual_clicks
```

### 9. New `/adstats` Command

Two modes:

- `/adstats` — Overview of all campaigns with funnel + conversion rates
- `/adstats <token>` — Deep dive into one campaign: full funnel, suspicious count, last 5 users

### 10. Enhanced `/stats`

Now includes:
- Ad campaign performance (top 10 by starts)
- Conversion funnel (last 30 days)
- Start→Message conversion rate
- Suspicious user count

### 11. Enhanced `/id`

Now shows first-touch and last-touch attribution:
```
🎯 1st Touch: 📣 Telegram Ads (tgads_v1)
🎯 Last Touch: 📣 Telegram Ads (tgads_v2)
⚠️ Suspicious: no
👀 Last seen: 2025-09-13 14:23:01+00:00
```

### 12. Optional Health Endpoint

Set `HEALTH_PORT=8080` to enable `GET /health` returning JSON status. Useful for Railway uptime checks or external monitoring (UptimeRobot, BetterStack).

### 13. Security Hardening (carried from original)

- Non-staff messages in support group are ignored (with warning log)
- All DB queries use parameterized values (no SQL injection)
- Table names are constants (not user input)
- Bot token never logged (httpx set to WARNING level)

---

## Backwards Compatibility

This rewrite is **safe to deploy over an existing Winning Wave deployment**:

- All existing tables (`winning_wave_users`, `winning_wave_topics`, `winning_wave_staff`) are preserved
- `init_db()` uses `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS`
- New columns (`first_touch_source`, `last_touch_source`, `is_suspicious`, etc.) are added with safe defaults
- Existing customers will have `NULL` for new attribution fields — that's fine, they'll populate on next /start
- New tables (`winning_wave_campaigns`, `winning_wave_events`) are created fresh

**No data migration required.** Just push the new `bot.py` and Railway will redeploy.

---

## Deployment Checklist

1. **Backup current bot.py** (in case you need to roll back)
2. **Copy new files to your GitHub repo:**
   - `bot.py` (replaces existing)
   - `.env.example` (replaces existing)
   - `Procfile` (unchanged)
   - `requirements.txt` (unchanged)
   - `CAMPAIGN_SETUP.md` (new — keep for reference)
   - `IMPLEMENTATION_NOTES.md` (this file — replaces existing)
3. **Railway will auto-redeploy** from GitHub
4. **Watch Railway logs** — you should see:
   ```
   PostgreSQL pool created for Winning Wave
   Database initialized: campaigns + events tables ready.
   Authorized Staff loaded: {5991692961}
   Bot commands registered. Winning Wave ready.
   Bot is running...
   ```
5. **Test in Telegram:**
   - Send `/start` to your bot → should see new welcome with $5 free play button
   - Check Railway logs → should see `START_EVENT | user_id=... | args=[]`
6. **Register your first campaign** in the support group:
   ```
   /campaigns add tgads_v1 telegram_ads "September Test" "$5 free play" "fish-game-channels" 500
   ```
7. **Use the URL the bot gives you** as the destination in Telegram Ads

---

## Environment Variables Reference

### Required (bot won't start without these)

| Variable | Example | Purpose |
|---|---|---|
| `BOT_TOKEN` | `123456:ABC...` | Telegram bot token from BotFather |
| `SUPPORT_GROUP_ID` | `-1003960273507` | Numeric ID of your support supergroup |
| `DATABASE_URL` | `postgresql://...` | Railway PostgreSQL connection URL |
| `AUTHORIZED_STAFF_IDS` | `5991692961` | Comma-separated staff Telegram IDs |

### Optional (all have safe defaults)

| Variable | Default | Purpose |
|---|---|---|
| `NUDGE_ENABLED` | `true` | Enable/disable auto-nudge |
| `NUDGE_DELAY_SECONDS` | `90` | Seconds to wait before nudging |
| `SUSPICIOUS_START_THRESHOLD` | `3` | /start count that triggers suspicious flag |
| `SUSPICIOUS_START_WINDOW` | `60` | Window in seconds for the above |
| `HEALTH_PORT` | `0` | Port for HTTP health endpoint (0 = disabled) |
| `ALLOWED_GAMES` | (all 9) | Comma-separated games list override |

---

## File Structure

```
winning-wave-telegram-bot/
├── bot.py                    ← REWRITTEN — ads-optimized
├── Procfile                  ← unchanged
├── requirements.txt          ← unchanged
├── .env.example              ← updated with new optional vars
├── README.md                 ← original (still valid)
├── IMPLEMENTATION_NOTES.md   ← this file
├── CAMPAIGN_SETUP.md         ← NEW — full guide to ad campaigns
├── PRIVACY_POLICY.md         ← original (still valid)
└── BOTFATHER_DETAILS.md      ← original (still valid)
```

---

## What's NOT Changed

- **Branding**: Still "Winning Wave", `@winningwavesupportbot`, `@winningwaveofficial`
- **DB table prefixes**: Still `winning_wave_*` — no mixing with WishWheel
- **Staff workflow**: Still reply in forum topics, customers still get copied messages
- **Polling mode**: Still uses long-polling (proven reliable in production)
- **PostgreSQL dependency**: Still psycopg2-binary (no migration to psycopg3)
- **Python version**: Still 3.11+ compatible
- **Privacy policy**: Unchanged — already covers the new fields (campaigns, events)

---

## Troubleshooting

### Bot won't start
- Check Railway logs for `❌ BOT_TOKEN not set!` or `❌ SUPPORT_GROUP_ID not set!`
- Verify `DATABASE_URL` is set — Railway PostgreSQL service must be running

### `/campaigns` command doesn't work
- Make sure you're running it **inside the support group**, not in DM
- Make sure your Telegram ID is in `AUTHORIZED_STAFF_IDS` or you've been added via `/addstaff`

### New columns missing from old users
- That's expected. `first_touch_source` will be NULL for users who joined before the update. They'll populate on their next /start.

### `/adstats` shows 0 for everything
- You haven't registered any campaigns yet → use `/campaigns add ...`
- OR: your ad links didn't include `?start=src_<token>` parameter

### Nudges not sending
- Check `NUDGE_ENABLED` isn't `false`
- Check `NUDGE_DELAY_SECONDS` — default 90s, so wait at least 90s after /start
- Check Railway logs for `NUDGE_SENT | user_id=...`

### High suspicious count
- Likely bot traffic from a cheap ad placement
- Increase CPM in Telegram Ads (better quality inventory)
- Switch to a different reseller
- Tighten channel targeting

---

## Next Steps After Deployment

1. **Read `CAMPAIGN_SETUP.md`** — full guide to launching ad campaigns
2. **Register your first campaign** via `/campaigns add ...`
3. **Launch a small test ad** (€50-100 budget) using the URL the bot gives you
4. **Monitor with `/adstats`** every few hours
5. **Iterate**: kill underperforming campaigns, scale winners

Good luck! 🌊
