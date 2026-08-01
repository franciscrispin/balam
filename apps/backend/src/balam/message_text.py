"""Turn a Telegram message into the text the agent actually sees.

Telegram carries the owner's *gestures* — forwarding something, replying to a
particular message, quoting part of it — as structured metadata, and drops all of
it when a bot reads ``text``/``caption``. Without this module the agent would see
a bare message and lose the context the owner meant to supply, so the gestures
are rendered back into a short bracketed header instead.

Everything here is a pure function of the message object. They duck-type their
input (``getattr`` rather than ``isinstance``) so a stripped-down test double
works as well as a real :class:`telegram.Message`.
"""

from __future__ import annotations

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
    if reply is not None:
        if getattr(reply, "forum_topic_created", None) is not None:
            return None
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and getattr(reply, "message_id", None) == thread_id:
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
