# Winning Wave — Ad Campaign Setup Guide

This guide walks you through setting up **ad campaigns** for the Winning Wave bot, with proper tracking so you can see exactly which campaigns drive real customers.

---

## 🎯 The Big Idea

Every Telegram Ad you run should use a **unique destination URL** with a campaign token:

```
https://t.me/winningwavesupportbot?start=src_<TOKEN>
```

When a user clicks the ad and presses Start, the bot:

1. Logs the `/start` event to Railway logs with full attribution (grep `START_EVENT`)
2. Stores `first_touch_campaign` and `last_touch_campaign` in the database
3. Shows the user a conversion-optimized welcome that mirrors what your ad promised
4. Schedules an auto-nudge (if they don't message within 90s)
5. Reports the conversion in `/stats` and `/adstats`

Without the `?start=src_<TOKEN>` parameter, Telegram Ads dashboard **cannot** attribute bot starts to your ad. This was the root cause of the 269-clicks/0-starts issue on the previous WishWheel campaign.

---

## 📋 Campaign Token Naming Convention

Use **lowercase**, **underscore-separated** tokens. The first prefix identifies the platform:

| Prefix | Platform | Example Token | Example URL |
|---|---|---|---|
| `tgads_` | Official Telegram Ads | `tgads_v1` | `?start=src_tgads_v1` |
| `tgads_` | Telegram Ads variant 2 | `tgads_v2` | `?start=src_tgads_v2` |
| `mangoads_` | MangoAds reseller | `mangoads_q4` | `?start=src_mangoads_q4` |
| `richads_` | RichAds reseller | `richads_sep` | `?start=src_richads_sep` |
| `chan_` | Direct channel buy | `chan_casinofans` | `?start=src_chan_casinofans` |
| `search_` | Telegram Search ad | `search_orion` | `?start=src_search_orion` |
| `fb_` | Facebook referral | `fb_sep_promo` | `?start=src_fb_sep_promo` |
| `ig_` | Instagram | `ig_reel_01` | `?start=src_ig_reel_01` |
| `tt_` | TikTok | `tt_viral_01` | `?start=src_tt_viral_01` |
| `yt_` | YouTube | `yt_shorts_01` | `?start=src_yt_shorts_01` |

### Rules
- Tokens must be **unique** across all campaigns (they're the primary key in the DB)
- Tokens can contain `a-z`, `0-9`, `_`, and `-` only (Telegram deep-link rules)
- Max length: 64 characters
- Once a campaign ends, **don't reuse** the token — create a new one

---

## 🚀 Step-by-Step: Launching Your First Ad Campaign

### Step 1 — Register the campaign in the bot

In your **Winning Wave support group**, run:

```
/campaigns add tgads_v1 telegram_ads "September Test - $5 Free" "$5 free play for first 100" "fish-game-channels" 500
```

This creates a record with:
- **Token:** `tgads_v1`
- **Platform:** `telegram_ads`
- **Name:** September Test - $5 Free
- **Creative:** $5 free play for first 100
- **Target:** fish-game-channels (your targeting notes)
- **Expected clicks:** 500

The bot will reply with the exact destination URL to use in your Telegram Ad.

### Step 2 — Create the Telegram Ad

1. Go to `ads.telegram.org`
2. Create new sponsored message
3. **Destination URL:** `https://t.me/winningwavesupportbot?start=src_tgads_v1` (the bot gave you this)
4. **Ad copy** (recommended):

```
🌊 Winning Wave — $5 FREE PLAY
🎮 Play Fire Kirin, Orion Stars, Juwa + 6 more games
💸 Instant CashApp / Crypto / Chime cashouts
🎁 First 100 players: $5 FREE
👉 Press START to claim
```

5. Target fish-game / sweepstakes channels (NOT competitor casinos like Stake.us, Ignition)
6. Set CPM to 0.5–1.0 TON (better quality than 0.18)
7. Launch

### Step 3 — Update actual clicks from Telegram Ads dashboard

After your ad has been running, Telegram Ads dashboard will show "Clicks" count. Update the bot:

```
/campaigns clicks tgads_v1 269
```

Now the bot has both expected_clicks (your goal) and actual_clicks (real number from Telegram).

### Step 4 — Monitor performance

Run these commands in the support group:

```
/adstats              — Overview of all campaigns with conversion funnel
/adstats tgads_v1     — Detailed breakdown of one campaign
/stats                — Overall bot statistics including ad performance
/campaigns            — List all registered campaigns
```

### Step 5 — End the campaign

When the ad stops running:

```
/campaigns end tgads_v1
```

This marks it inactive. You can still query its historical data via `/adstats tgads_v1`.

---

## 📊 What the Conversion Funnel Tells You

For each campaign, the bot tracks:

```
Clicks (from Telegram Ads dashboard)
  ↓
bot_start (user pressed Start in Telegram)
  ↓
bonus_selected (user picked a bonus)
  ↓
game_selected (user picked a game)
  ↓
first_message (user sent their first message)
```

### Key metrics

| Metric | What it means | Healthy range |
|---|---|---|
| **Click→Start** | Of people who clicked the ad, how many pressed Start | 30-50% |
| **Start→Message** | Of people who started, how many actually messaged | 40-60% |
| **Nudge→Acted** | Of nudges sent, how many converted to a message | 15-25% |
| **Suspicious %** | Users flagged for rapid /start spam | <5% |

### Diagnosing problems

| Symptom | Likely cause | Fix |
|---|---|---|
| High clicks, low starts | Ad copy mismatch with bot welcome, or bot traffic | Align ad copy with welcome message |
| High starts, low messages | Welcome confusing, no clear CTA | Simplify welcome, make CTA obvious |
| Many nudges, few acted | Wrong audience targeting | Change channel targeting |
| High suspicious % | Click fraud / bot traffic | Pause campaign, switch reseller |

---

## 🎁 Bonus Options Available

The bot's welcome shows these 3 options (matching what most ads promise):

```
💵 $5 Free Play (First 100)      ← Most-clicked, matches ad copy
💰 120% Signup Bonus
🎁 Redeemable Freeplay
```

If your ad promises something different (e.g., "$10 match bonus"), update the `BONUS_OPTIONS` dict in `bot.py`:

```python
BONUS_OPTIONS = {
    "signup120": "💰 120% Signup Bonus",
    "freeplay":  "🎁 Redeemable Freeplay",
    "f5free":    "💵 $5 Free Play (First 100)",
    "m10match":  "💵 $10 Match Bonus",   # NEW
}
```

And update `bonus_keyboard()` to include it.

---

## 🛡️ Anti-Fraud

The bot flags users as **suspicious** if they press `/start` >= 3 times within 60 seconds (configurable via `SUSPICIOUS_START_THRESHOLD` and `SUSPICIOUS_START_WINDOW` env vars).

Suspicious users are:
- Flagged in the database (`is_suspicious = TRUE`)
- Visible in `/id` output with `⚠️ Suspicious: YES`
- Counted separately in `/adstats` output
- Still processed normally (not blocked) — you decide whether to serve them

### What to do with suspicious users

If you see a campaign with high suspicious % (e.g., 20%+), that's a sign of click fraud. Options:

1. Pause the campaign in Telegram Ads
2. Switch to a different reseller (MangoAds, RichAds)
3. Tighten your channel targeting
4. Increase CPM (cheap inventory has more bot traffic)

---

## 🔔 Auto-Nudge

When a user presses Start but doesn't send a message within 90 seconds, the bot automatically sends:

> 👋 Hey! Still there?
> 🎁 Your $5 Free Play is waiting — just send a message and our team will hook you up!
> 💬 You can also pick a bonus below 👇
> [Bonus buttons]

This typically recovers 15-25% of users who would otherwise bounce.

### Tuning

| Env var | Default | Effect |
|---|---|---|
| `NUDGE_ENABLED` | `true` | Set to `false` to disable nudges entirely |
| `NUDGE_DELAY_SECONDS` | `90` | How long to wait before nudging |

A shorter delay (60s) catches more users but feels pushy. A longer delay (120s) feels natural but loses some users.

---

## 🩺 Health Endpoint (Optional)

If you want Railway (or an external uptime monitor like UptimeRobot) to do HTTP health checks:

1. Set `HEALTH_PORT=8080` in Railway Variables
2. Railway will detect the listening port
3. GET `https://your-app.up.railway.app/health` returns:
   ```json
   {"status": "ok", "brand": "Winning Wave", "timestamp": "2025-09-13T..."}
   ```

If `HEALTH_PORT` is 0 (default), no HTTP server is started — the bot runs in pure polling mode.

---

## 🚦 Quick Reference — All Staff Commands

| Command | Where | Purpose |
|---|---|---|
| `/id` | In a customer topic | Show customer info + attribution |
| `/close` | In a customer topic | Close the topic |
| `/stats` | Support group | Overall bot stats + ad performance |
| `/adstats` | Support group | Detailed per-campaign funnel |
| `/adstats <token>` | Support group | Single-campaign deep dive |
| `/campaigns` | Support group | List all campaigns |
| `/campaigns add <token> <platform> <name> ...` | Support group | Register new campaign |
| `/campaigns end <token>` | Support group | Mark campaign ended |
| `/campaigns clicks <token> <count>` | Support group | Update actual clicks |
| `/broadcast <msg>` | Support group | Send message to all customers |
| `/addstaff <id> [name]` | Support group | Add staff member |
| `/removestaff <id>` | Support group | Remove staff member |
| `/staff` | Support group | List all staff |
| `/groupid` | Support group | Get numeric group ID (setup only) |

---

## ❓ FAQ

**Q: Can I run multiple campaigns at the same time?**
A: Yes! Each gets its own token (`tgads_v1`, `tgads_v2`, `mangoads_q4`, etc.). All are tracked separately in `/adstats`.

**Q: What if a user clicks ad A, then later clicks ad B?**
A: We store both `first_touch_campaign` (A) and `last_touch_campaign` (B). `/adstats` shows both attribution models — pick whichever you prefer for reporting.

**Q: I forgot to register the campaign before launching the ad. Can I add it after?**
A: Yes. Register it now with `/campaigns add ...`, then update actual clicks. Historical `/start` events that already happened won't be retro-attributed, but future ones will.

**Q: The bot says my campaign token has invalid characters.**
A: Tokens must match `^[a-z0-9_-]+$`. Lowercase only. No spaces, no special chars.

**Q: Can I delete a campaign?**
A: No — only end it (`/campaigns end <token>`). This preserves historical data. Ended campaigns disappear from the active list but remain queryable via `/adstats`.

**Q: My ad got rejected by Telegram Ads moderation. What now?**
A: Telegram's official policy prohibits "gambling, gaming, or casino-based activities involving real money." Workarounds:
1. Use a reseller (MangoAds, RichAds) — looser moderation
2. Soften ad copy: "sweepstakes" → "promotional games", "casino" → "fish games & slots", "real money" → "real prizes"
3. Try direct channel buys instead of Telegram Ads platform
4. Run Telegram Search ads on keywords like "orion stars login" (less strict)
