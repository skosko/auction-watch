import os
from datetime import date

import resend


def send_digest(html: str, recipient: str, subject: str | None = None) -> dict:
    resend.api_key = os.environ["RESEND_API_KEY"]
    subject = subject or f"Auction Watch — {date.today():%b %d, %Y}"
    return resend.Emails.send(
        {
            "from": "Auction Watch <onboarding@resend.dev>",
            "to": [recipient],
            "subject": subject,
            "html": html,
        }
    )


def cat_image_url() -> str:
    return "https://cataas.com/cat?tags=sleeping"
