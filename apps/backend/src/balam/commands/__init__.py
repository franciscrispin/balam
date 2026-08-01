"""Slash-command handlers, one module per command group.

Every handler here has the same shape — a PTB ``(update, context)`` coroutine
reading its dependencies out of ``context.application.bot_data`` — so they share
nothing but that convention and can be registered independently. ``bot.py``
imports them and wires them to their ``CommandHandler``; nothing in this package
imports ``bot``.
"""
