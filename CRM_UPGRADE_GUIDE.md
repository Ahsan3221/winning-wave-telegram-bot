# Winning Wave — CRM Upgrade (2026-10-11)

This update adds the Ace Arcade-style staff CRM to the **existing Winning Wave ads bot** while retaining the original ad funnel, event tracking, campaigns registry, dashboard, nudge, deep-link attribution, original `winning_wave_users`/`topics`/`staff` records and the Winning Wave channel branding.

## New staff commands (inside a customer's group topic)

| Command | Action |
|---|---|
| `/profile` or `/id` | Show customer profile |
| `/accounts` | Show saved usernames and passwords to authorized staff |
| `/setaccount Juwa player123 009988` | Manually add an account |
| `/setaccount Custom Game | player123 | 009988` | Add custom game name |
| `/delaccount Juwa` | Remove an account |
| `/findaccount player123` | Find existing account (case-insensitive) |
| `/label High Roller` | Add any label |
| `/unlabel high roller` | Remove label, case-insensitive |
| `/note text` | Add an internal note |
| `/history`, `/status`, `/assign` | Manage the customer |
| `/shortcuts`, `/use KEY` | Send a saved reply |
| `/health` | View background delivery queue (owner) |

## Automatically save account from staff chat
In the customer's support-group topic, send a *normal* account message:

```text
👤 Username:Michael333fk
🔑 Password: 009988
Log in and let me know if you need help ❤️✨
```

Bot detects `Firekirin`, saves `Michael333fk` and password, and queues the same staff message for the customer. `Dummy` is NOT a required prefix. The suffix `3s`, `33s`, `333s`, `3fk`, `3u`, `3v`, `3r`, `3j`, `3gv`, `3mw`, or `3cm` identifies the game. For a custom/non-suffixed account, add a third line `Game: My Custom Game`.

**Plain text passwords:** As requested, `winning_wave_accounts.account_password` stores game credentials without encryption. Restrict PostgreSQL access. Bot does not put passwords into the pinned profile or debug logs. An authorized staff member can use `/accounts` in the customer's topic.

## Preserved Winning Wave tools

- `/campaigns` and `/adstats`, including existing `winning_wave_campaigns` and `winning_wave_events` tables.
- Campaign deep links, first/last-touch attribution, suspicious-start detection and nudge events.
- `/menu` ad-performance dashboard.
- Existing `BOT_TOKEN`, `SUPPORT_GROUP_ID`, `DATABASE_URL`, `ALLOWED_GAMES`, `HEALTH_PORT`, `NUDGE_*` variables.
- Original customer topic and staff tables.

## Railway

1. Back up PostgreSQL separately; the GitHub source backup does not include database contents.
2. Set `OWNER_IDS=your_numeric_telegram_id` in Railway (new optional setting; owner-only commands stay unavailable if unset).
3. Confirm the existing bot's Telegram administration rights: send messages, manage topics, pin messages.
4. With GitHub auto-deploy enabled on `main`, Railway will deploy the updated source.
5. Test existing topic, `/profile`, `/label VIP`, `/accounts` and auto-capture template before using with customers.
6. Monitor Railway logs and verify `/campaigns`, `/adstats` and deep-link tracking.

### Rollback
Original code: [backup/pre-crm-upgrade-20261011](https://github.com/Ahsan3221/winning-wave-telegram-bot/tree/backup/pre-crm-upgrade-20261011). Reverting GitHub code does not revert PostgreSQL schema/data. A DB snapshot is recommended.

### Testing status
The migrated CRM helper modules passed 35 offline tests locally. The integration requires a Railway/PostgreSQL + Telegram staging test; CI validates syntax and offline tests only.
