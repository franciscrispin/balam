"""Turn a Telegram message into the text the agent actually sees.

Telegram carries the sender's *gestures* — forwarding something, replying to a
particular message, quoting part of it — as structured metadata, and drops all of
it when a bot reads ``text``/``caption``. Without this module the agent would see
a bare message and lose the context the sender meant to supply, so the gestures
are rendered back into a short bracketed header instead. :func:`sender_prefix`
adds the one piece Telegram *does* keep but the agent still never sees: which
person in the chat is speaking.

One more gesture matters in a ``respond_to: mentions`` context, where people talk
among themselves and the bot stays quiet: whether a message is *aimed at the bot*
at all. :func:`addresses_bot` reads the three ways Telegram lets someone address
a bot in a group — an ``@mention``, a reply to one of its messages, a slash
command — and :func:`strip_bot_mention` removes the ``@handle`` again, since to
the agent it is routing, not content.

Everything here is a pure function of the message object. They duck-type their
input (``getattr`` rather than ``isinstance``) so a stripped-down test double
works as well as a real :class:`telegram.Message`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from telegram import Message, MessageEntity


def strip_bot_mention_from_command(text: str, bot_username: str | None) -> str:
    """Drop a leading ``@<bot>`` from a slash command so the agent sees a clean
    ``/goal``. Telegram appends ``@<bot>`` to commands in groups (``/goal@thisbot``);
    only the first token is touched, and only when it addresses *this* bot."""
    if not text.startswith("/") or not bot_username:
        return text
    head, sep, tail = text.partition(" ")
    cmd, at, mention = head.partition("@")
    if at and mention.lower() == bot_username.lower():
        return f"{cmd}{sep}{tail}" if sep else cmd
    return text


def forwarded_slash_command(message: Message) -> bool:
    """True when this message is a bot command Balam forwards to the agent — a
    ``BOT_COMMAND`` entity at offset 0, exactly what telegram's ``filters.COMMAND``
    matches to route it through the catch-all handler. Balam's own commands are
    caught by their ``CommandHandler`` first, so anything reaching here is a Claude
    slash command (e.g. ``/goal``) on its way to the agent."""
    ents = getattr(message, "entities", None) or []
    return (
        bool(getattr(message, "text", None))
        and bool(ents)
        and ents[0].offset == 0
        and (ents[0].type == MessageEntity.BOT_COMMAND)
    )


def _entity_text(source: str, entity: Any) -> str:
    """The text an entity covers. The Bot API counts ``offset``/``length`` in
    UTF-16 code units, so slice the UTF-16 encoding rather than the ``str`` —
    an emoji ahead of the mention would otherwise shift the window."""
    data = source.encode("utf-16-le")
    start = entity.offset * 2
    end = (entity.offset + entity.length) * 2
    return data[start:end].decode("utf-16-le", errors="ignore")


def mentions_bot(message: Any, *, bot_id: int | None, bot_username: str | None) -> bool:
    """Whether the message ``@mentions`` this bot in its text or its caption.

    Covers both entity shapes: a plain ``mention`` (``@balambot``, matched on the
    username, case-insensitively) and a ``text_mention`` (a user linked by id,
    which clients produce when the account has no username — rare for a bot,
    but free to honour).
    """
    username = (bot_username or "").lower()
    sources = (
        (getattr(message, "text", None), getattr(message, "entities", None)),
        (getattr(message, "caption", None), getattr(message, "caption_entities", None)),
    )
    for source, entities in sources:
        for entity in entities or []:
            if entity.type == MessageEntity.TEXT_MENTION:
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and user.id == bot_id:
                    return True
            elif entity.type == MessageEntity.MENTION and source and username:
                if _entity_text(source, entity).lstrip("@").lower() == username:
                    return True
    return False


def _is_topic_anchor(reply: Any, message: Any) -> bool:
    """Whether ``reply`` is forum bookkeeping rather than something the sender
    chose to reply to. In a forum supergroup a message carries the topic's own
    service message (topic created/edited) or anchor as ``reply_to_message``;
    the person never replied to it, so it must not read as a reply — least of
    all as a reply *to the bot*, which created the topic."""
    if getattr(reply, "forum_topic_created", None) is not None:
        return True
    thread_id = getattr(message, "message_thread_id", None)
    return thread_id is not None and getattr(reply, "message_id", None) == thread_id


def replies_to_bot(message: Any, *, bot_id: int | None) -> bool:
    """Whether the message is a genuine reply to one of the bot's own messages."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None or bot_id is None or _is_topic_anchor(reply, message):
        return False
    user = getattr(reply, "from_user", None)
    return getattr(user, "id", None) == bot_id


def addresses_bot(messages: Iterable[Any], *, bot_id: int | None, bot_username: str | None) -> bool:
    """Whether any of ``messages`` is aimed at the bot — the gate for a
    ``respond_to: mentions`` context (:class:`balam.contexts.ContextConfig`).

    Three gestures count, mirroring what Telegram itself delivers to a bot in
    privacy mode: an ``@mention`` in the text or caption, a reply to one of the
    bot's messages, or a slash command. ``messages`` is one message in the
    ordinary case and a whole album in the buffered one, where the mention sits
    in whichever caption the client attached it to.
    """
    return any(
        forwarded_slash_command(m)
        or mentions_bot(m, bot_id=bot_id, bot_username=bot_username)
        or replies_to_bot(m, bot_id=bot_id)
        for m in messages
    )


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    """Remove every ``@<bot>`` token from ``text``, with the space that led into
    it, so the agent sees ``what's the status`` rather than ``@balambot what's
    the status``. Only this bot's handle, only as a whole token: ``@balambot2``
    and ``mail@balambot`` are left alone. Everything else — line breaks, code,
    indentation — is untouched."""
    if not text or not bot_username:
        return text
    pattern = re.compile(rf"[ \t]*(?<![\w@])@{re.escape(bot_username)}(?!\w)", re.IGNORECASE)
    return pattern.sub("", text).strip()


def _user_label(user: Any) -> str | None:
    """``Full Name (@username)`` for a Telegram user, name-only when there is no
    handle, ``@handle`` when there is no name, ``None`` for nothing usable."""
    if user is None:
        return None
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None)
    username = getattr(user, "username", None)
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    return f"@{username}" if username else None


def _forward_origin_label(origin: Any) -> str | None:
    """Best-effort label for who a forwarded message originally came from.

    Telegram exposes the origin as one of four ``MessageOrigin`` shapes; we
    duck-type them (rather than ``isinstance``) so a stripped-down test double
    works too, and check the most specific field first:

    * visible user            → ``sender_user``       (name + @handle)
    * hidden-account user     → ``sender_user_name``  (name only, no handle)
    * group / on-behalf chat  → ``sender_chat``       (+ ``author_signature``)
    * channel post            → ``chat``              (+ ``author_signature``)
    """
    if origin is None:
        return None
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        return _user_label(sender_user)
    hidden = getattr(origin, "sender_user_name", None)
    if hidden:
        return hidden
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        who = getattr(chat, "title", None) or getattr(chat, "username", None)
        signature = getattr(origin, "author_signature", None)
        if who and signature:
            return f"{who} ({signature})"
        return who or signature
    return None


def _quote_snippet(text: str | None, limit: int = 200) -> str | None:
    """Collapse whitespace and truncate a quoted excerpt to one header line."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _reply_context_line(message: Any) -> str | None:
    """``[Replying to <who>: "<quoted>"]`` for a genuine reply, else ``None``.

    Prefers ``message.quote`` — the exact portion the owner highlighted (Bot API
    7.0) — over the whole replied message. Skips forum bookkeeping: in a forum
    supergroup, service messages (topic created/edited) and the topic's own anchor
    message are threading metadata, not something the owner replied to, so they
    never reach the agent as a "reply".
    """
    reply = getattr(message, "reply_to_message", None)
    quote = getattr(message, "quote", None)
    if reply is None and quote is None:
        return None
    if reply is not None and _is_topic_anchor(reply, message):
        return None

    who = None
    if reply is not None:
        who = _user_label(getattr(reply, "from_user", None))
        if who is None:
            sender_chat = getattr(reply, "sender_chat", None)
            who = getattr(sender_chat, "title", None) if sender_chat is not None else None

    quoted = _quote_snippet(getattr(quote, "text", None)) if quote is not None else None
    if quoted is None and reply is not None:
        quoted = _quote_snippet(getattr(reply, "text", None) or getattr(reply, "caption", None))

    if who and quoted:
        return f'[Replying to {who}: "{quoted}"]'
    if who:
        return f"[Replying to {who}]"
    if quoted:
        return f'[Replying to: "{quoted}"]'
    return None


def forward_reply_prefix(message: Any) -> str:
    """A short bracketed header telling the agent a message was forwarded and/or is
    a reply, so the owner's forward/reply gestures survive the bot layer — Telegram
    otherwise drops that metadata and only ``text``/``caption`` reach the agent.

    Returns ``""`` for an ordinary message, so those are passed through unchanged.
    """
    lines: list[str] = []
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        label = _forward_origin_label(origin)
        lines.append(f"[Forwarded from {label}]" if label else "[Forwarded message]")
    reply_line = _reply_context_line(message)
    if reply_line:
        lines.append(reply_line)
    return "\n".join(lines) + "\n" if lines else ""


def sender_prefix(message: Any, *, owner_id: int) -> str:
    """A bracketed header naming who sent the message, when it wasn't the owner.

    A topic is one session shared by everyone in the chat (ADR-0008/0009), so two
    humans' messages otherwise arrive at the agent as one indistinguishable
    voice — it cannot say "you asked me to" about the right person, and cannot
    address either of them by name.

    Returns ``""`` for the owner, so a single-user deployment's prompts are
    unchanged; an allowlisted guest gets ``[From Bob (@bob)]``.
    """
    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    if user_id is None or user_id == owner_id:
        return ""
    return f"[From {_user_label(user) or f'Telegram user {user_id}'}]\n"


def command_remainder(text: str, *, args_consumed: int = 0) -> str:
    """Everything after the leading ``/command`` and ``args_consumed`` argument
    tokens, as the owner typed it.

    ``context.args`` is a whitespace split, so a multi-line prompt would come
    back collapsed onto one line; commands that forward a prompt to the agent
    take it from the raw text instead.
    """
    rest = text.lstrip()
    for _ in range(1 + args_consumed):
        parts = rest.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
    return rest.strip()
