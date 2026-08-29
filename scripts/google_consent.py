"""Eenmalige Google OAuth-consent voor Birdy (Calendar + Drive).

Draai dit op je eigen laptop (niet op de server — er opent een browser):

    pip install google-auth-oauthlib
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/google_consent.py

Log in als jaapbakker89@gmail.com en geef toestemming. Het script print daarna het
refresh token; zet dat samen met de client-id en het secret in de .env op de server:

    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...
    GOOGLE_REFRESH_TOKEN=...

Let op: het OAuth consent screen moet op "In production" staan, anders verloopt het
refresh token na 7 dagen.
"""
from __future__ import annotations

import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Installeer eerst: pip install google-auth-oauthlib")

    client_id = os.environ.get("GOOGLE_CLIENT_ID") or input("GOOGLE_CLIENT_ID: ").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or input("GOOGLE_CLIENT_SECRET: ").strip()
    if not client_id or not client_secret:
        sys.exit("Client-id en secret zijn verplicht (Google Cloud → Credentials → Desktop app).")

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        sys.exit(
            "Geen refresh token ontvangen. Trek de toegang in via "
            "https://myaccount.google.com/permissions en draai dit script opnieuw."
        )
    print("\nGelukt! Zet dit in de .env op de server:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print(
        "\nDaarna: docker compose up -d --force-recreate  (restart laadt .env niet opnieuw!)\n"
        "Werkt de agenda hierna, dan mag het service-account met pensioen "
        "(workspace/secrets/service-account.json verwijderen + uitschakelen in Google Cloud)."
    )


if __name__ == "__main__":
    main()
