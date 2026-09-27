"""Notifications: e-mail and Telegram messages about good deals."""

from __future__ import annotations

from .base import Notifier, NotifyError, build_notifiers, missing_settings
from .emailer import EmailNotifier
from .render import (
    deal_headline,
    email_subject,
    format_money,
    format_percent,
    render_email_html,
    render_telegram,
    render_text,
    sample_deals,
    verdict_label,
)
from .telegram import TelegramNotifier

__all__ = [
    "EmailNotifier",
    "Notifier",
    "NotifyError",
    "TelegramNotifier",
    "build_notifiers",
    "deal_headline",
    "email_subject",
    "format_money",
    "format_percent",
    "missing_settings",
    "render_email_html",
    "render_telegram",
    "render_text",
    "sample_deals",
    "verdict_label",
]
