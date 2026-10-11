"""Winning Wave: real anonymous support-group admins retain staff capabilities.

Telegram sets sender_chat to the group and uses @GroupAnonymousBot as the
fake sender. A foreign channel sender must never obtain staff permissions.
"""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _setup(actor_id=1087968824, group_sender=-1005555, is_bot=True, text="Hello"):
    runtime = importlib.import_module("crm_runtime")
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=actor_id,is_bot=is_bot),
        sender_chat=SimpleNamespace(id=group_sender) if group_sender is not None else None,
        is_automatic_forward=False, message_thread_id=77,
        text=text,caption=None,message_id=33,
        reply_text=AsyncMock())
    update = SimpleNamespace(message=msg, effective_message=msg,
                             callback_query=None,
                             effective_user=msg.from_user,
                             effective_chat=SimpleNamespace(id=-1005555))
    db = SimpleNamespace(roles=AsyncMock(return_value={}),
                         by_topic=AsyncMock(return_value={"user_id":555}),
                         broadcast_draft=AsyncMock(return_value=(42,127)),
                         broadcast_confirm=AsyncMock(return_value=127))
    cfg=SimpleNamespace(group_id=-1005555,owner_ids=set(),seed_staff=set(),
                        staff_id_sources={})
    state=SimpleNamespace(cfg=cfg,db=db,roles={},
                          delivery_wakeup=MagicMock())
    bot=SimpleNamespace(get_chat_member=AsyncMock(
        return_value=SimpleNamespace(status="member")))
    context=SimpleNamespace(application=SimpleNamespace(bot_data={"state":state}),
                            bot=bot,args=["Hello","team"])
    return runtime, update, context, state, msg


def test_anonymous_admin_broadcast_is_authorized_and_queued():
    runtime, update, context, state, msg = _setup()
    asyncio.run(runtime.broadcast(update,context))
    state.db.broadcast_draft.assert_awaited_once_with(1087968824,"Hello team",None)
    state.db.broadcast_confirm.assert_awaited_once_with(42,1087968824,True)
    state.delivery_wakeup.set.assert_called_once()
    msg.reply_text.assert_awaited_once()
    assert "127" in msg.reply_text.await_args.args[0]


def test_anonymous_admin_message_goes_to_existing_customer_topic(monkeypatch):
    runtime, update, context, state, msg = _setup()
    enqueue=AsyncMock()
    monkeypatch.setattr(runtime,"enqueue_incoming",enqueue)
    asyncio.run(runtime.group_message(update,context))
    state.db.by_topic.assert_awaited_once_with(77)
    enqueue.assert_awaited_once_with(state,555,"staff",msg)


def test_foreign_channel_imitating_staff_is_not_routed(monkeypatch):
    runtime, update, context, state, msg = _setup(group_sender=-1009999)
    enqueue=AsyncMock()
    monkeypatch.setattr(runtime,"enqueue_incoming",enqueue)
    asyncio.run(runtime.group_message(update,context))
    enqueue.assert_not_awaited()


def test_non_admin_user_cannot_bypass_with_sender_chat(monkeypatch):
    runtime, update, context, state, msg = _setup(actor_id=1234567,
                                                     group_sender=-1005555,
                                                     is_bot=False)
    assert asyncio.run(runtime.staff_access(update,context)) is None
    context.bot.get_chat_member.assert_awaited_once()


def test_automatically_forwarded_post_is_not_admin():
    runtime, update, context, state, msg = _setup()
    msg.is_automatic_forward=True
    assert asyncio.run(runtime.staff_access(update,context)) is None


def test_anon_staffcheck_reports_verified_group_actor():
    runtime, update, context, state, msg = _setup()
    asyncio.run(runtime.staffcheck(update,context))
    answer=msg.reply_text.await_args.args[0]
    assert "Verified anonymous support-group administrator" in answer
    assert "admin" in answer.lower()
    assert "NOT AUTHORIZED" not in answer
