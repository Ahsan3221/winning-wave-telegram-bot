# Winning Wave — Anonymous Administrator Authorization Fix

## Why staff commands were rejected

When a Telegram group administrator has **Remain Anonymous** enabled, the
Telegram Bot API sends the command with `sender_chat` set to the support-group
chat. The apparent `from_user` is Telegram's shared
`GroupAnonymousBot` pseudo-user (ID `1087968824`) rather than the actual
person. The prior Winning Wave CRM compared that fake ID with Railway staff IDs
and rejected it.

## Fix

- Accept a command *only if* the Telegram update proves that it is a message
  posted as **this exact configured support group**: matching
  `sender_chat.id == SUPPORT_GROUP_ID` and
  `chat.id == SUPPORT_GROUP_ID`, with Telegram's standard anonymous-admin
  pseudo-user.
- Reject linked-channel posts, automatically forwarded posts, and arbitrary
  anonymous-looking messages. Callback queries cannot borrow the original
  message's anonymous group identity.
- Preserve the original role checks for staff posting as their personal
  accounts: Railway IDs, active CRM staff, and Telegram group admins.
- Permit verified anonymous-admin **staff commands and topic replies**. The
  earlier reply handler incorrectly ignored Telegram's anonymous admin
  pseudo-user because `from_user.is_bot` was true.
- Make `/staffcheck` explicitly identify a verified anonymous group admin
  rather than instruct the person to turn anonymity off.

## Attribution limitation

Telegram does not provide the real human's user ID for an anonymous group
message. Broadcasts executed as the group are attributed to the shared
`1087968824` Telegram pseudo-user, not to the individual admin. To record
which employee sent a broadcast, the employee must post as their personal
account instead. Do not assume this pseudo-user ID uniquely identifies someone.

## Scope

Only **Winning Wave** was changed by this patch; no other repository, payment
flow, game, existing CRM data, or broadcast selection/delay was altered.

## Verification

Run `/staffcheck` and then a safe `/bstatus` test in the Winning Wave
support group while posting anonymously. For delivery tests, use a deliberate
small targeted audience. GitHub CI is offline; Railway's running deployment
must be verified separately.
