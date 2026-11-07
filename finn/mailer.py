"""Reading orders from the inbox (IMAP) and sending replies (SMTP).

Design choices:
- Plain IMAP/SMTP instead of the Gmail API: works with any mailbox, no OAuth setup.
- An email is only marked as read AFTER the reply was sent. If anything crashes,
  the email stays unread and is picked up again on the next poll (no lost orders).
- Loop protection: never answer our own mails, auto-replies or mailer daemons.
"""

from __future__ import annotations

import imaplib
import os
import smtplib
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

FINN_EMAIL = os.environ.get("FINN_EMAIL", "")
APP_PASSWORD = os.environ.get("FINN_APP_PASSWORD", "")
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")

INBOX_DIR = Path("inbox")  # where attachments are saved


@dataclass
class Order:
    """One incoming email, reduced to what we need."""

    imap_id: bytes          # id inside the mailbox, used to mark it as read later
    sender: str             # who we answer to
    subject: str
    message_id: str         # needed so our reply lands in the same thread
    references: str
    body: str
    attachments: list[Path] = field(default_factory=list)  # saved .xlsx files


def is_automatic(msg) -> bool:
    """True for mails we must never answer (avoids endless reply loops)."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender == FINN_EMAIL.lower():
        return True
    if "mailer-daemon" in sender or "noreply" in sender or "no-reply" in sender:
        return True
    if msg.get("Auto-Submitted", "no").lower() != "no":  # auto-replies, out-of-office
        return True
    if msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):  # mass mail
        return True
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):  # newsletters, mailing lists
        return True
    return False


def parse_order(imap_id: bytes, raw: bytes) -> Order | None:
    """Turn a raw email into an Order and save its .xlsx attachments."""
    msg = message_from_bytes(raw, policy=policy.default)
    if is_automatic(msg):
        return None

    # Reply-To wins over From if the sender set it
    sender = parseaddr(msg.get("Reply-To") or msg.get("From", ""))[1]

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""

    order = Order(
        imap_id=imap_id,
        sender=sender,
        subject=msg.get("Subject", ""),
        message_id=msg.get("Message-ID", ""),
        references=msg.get("References", ""),
        body=body,
    )

    INBOX_DIR.mkdir(exist_ok=True)
    for part in msg.iter_attachments():
        name = part.get_filename() or ""
        if name.lower().endswith((".xlsx", ".xlsm")):
            # prefix with the mailbox id so two orders never overwrite each other
            path = INBOX_DIR / f"{imap_id.decode()}_{Path(name).name}"
            path.write_bytes(part.get_payload(decode=True))
            order.attachments.append(path)
    return order


def fetch_unread() -> list[Order]:
    """Return all unread emails in the inbox, WITHOUT marking them as read."""
    orders = []
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(FINN_EMAIL, APP_PASSWORD)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        for imap_id in data[0].split():
            # BODY.PEEK reads the mail without setting the "read" flag
            _, msg_data = imap.fetch(imap_id, "(BODY.PEEK[])")
            order = parse_order(imap_id, msg_data[0][1])
            if order is None:
                mark_as_read(imap_id, imap)  # automatic mail: ignore it for good
            else:
                orders.append(order)
    return orders


def mark_as_read(imap_id: bytes, imap: imaplib.IMAP4_SSL | None = None) -> None:
    """Mark an email as read, i.e. as handled."""
    if imap is not None:
        imap.store(imap_id, "+FLAGS", "\\Seen")
        return
    with imaplib.IMAP4_SSL(IMAP_HOST) as conn:
        conn.login(FINN_EMAIL, APP_PASSWORD)
        conn.select("INBOX")
        conn.store(imap_id, "+FLAGS", "\\Seen")


def build_reply(order: Order, text: str, html: str | None = None) -> EmailMessage:
    """Build a reply that the mail client shows in the same thread."""
    reply = EmailMessage()
    reply["From"] = FINN_EMAIL
    reply["To"] = order.sender
    subject = order.subject
    reply["Subject"] = subject if subject.lower().startswith(("re:", "aw:")) else f"Re: {subject}"
    reply["Message-ID"] = make_msgid()
    # These two headers are what makes it a reply in the same thread:
    reply["In-Reply-To"] = order.message_id
    reply["References"] = f"{order.references} {order.message_id}".strip()

    reply.set_content(text)                       # plain-text version
    if html:
        reply.add_alternative(html, subtype="html")  # nicer version for normal clients
    return reply


def send(reply: EmailMessage) -> None:
    """Send an email via SMTP (SSL, port 465)."""
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
        smtp.login(FINN_EMAIL, APP_PASSWORD)
        smtp.send_message(reply)


if __name__ == "__main__":
    # Test: answer every unread mail with a short confirmation.
    #   python -m finn.mailer
    for order in fetch_unread():
        names = ", ".join(p.name for p in order.attachments) or "no .xlsx attachment"
        print(f"Mail from {order.sender}: {order.subject!r} -> {names}")
        send(build_reply(order, f"Hallo,\n\nAuftrag erhalten. Anhang: {names}\n\nFinn"))
        mark_as_read(order.imap_id)
        print("  replied and marked as read")
