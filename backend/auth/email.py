from __future__ import annotations

import smtplib
from email.message import EmailMessage


class InvitationMailer:
    def __init__(self, host: str, port: int, *, sender: str = "noreply@finscope.local"):
        self.host, self.port, self.sender = host, port, sender

    def send(self, *, recipient: str, invitation_url: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, recipient, "FinScope invitation"
        message.set_content(f"Open this one-time invitation link:\n{invitation_url}\n")
        with smtplib.SMTP(self.host, self.port, timeout=10) as client:
            client.send_message(message)
