# Implementation Notes — Winning Wave

This build is isolated for **Winning Wave**.

- All customer-facing brand text uses the `BRAND` constant for this bot only.
- PostgreSQL table names use the `winning_wave` prefix; a shared PostgreSQL server cannot mix customers, topics, staff or statistics with another bot.
- Tokens, group IDs, staff IDs and the policy URL are environment variables, not hard-coded secrets.
- A support-group message can be copied to a customer **only** when its sender is in `AUTHORIZED_STAFF_IDS` or has been added by `/addstaff`. This fixes an important security weakness in the original code, where any non-bot group member could reply to a customer.
- The message-queue cleanup was made safe at the idle boundary, preventing a newly received customer message from being left in an orphaned queue.
- The public privacy policy is included as `PRIVACY_POLICY.md`; publish its HTTPS URL through BotFather.

Read `README.md`, update the policy placeholders, and test the complete customer-to-staff-to-customer flow before launch.
