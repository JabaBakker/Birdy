"""Google-credentials voor gcal.py en gdrive.py.

Voorkeursroute: OAuth als Jaap (GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET +
GOOGLE_REFRESH_TOKEN, eenmalig verkregen via scripts/google_consent.py). Zolang die
env-variabelen er nog niet zijn, valt dit terug op het oude service-account-bestand,
zodat de agenda blijft werken tijdens de overgang. Drive vereist OAuth (de map
"Birdy 2.0" staat in een persoonlijke My Drive).
"""
from __future__ import annotations

import os
import sys

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _service_account_file() -> str:
    return os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "secrets", "service-account.json"),
    )


def oauth_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
    )


def configured() -> bool:
    return oauth_configured() or os.path.exists(_service_account_file())


def google_credentials(scopes: list[str]):
    """Credentials-object voor de Google-API-clients; sys.exit met nette melding als
    er niets gekoppeld is (de agent toont die melding dan in de chat)."""
    if oauth_configured():
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=scopes,
        )

    key_file = _service_account_file()
    if os.path.exists(key_file):
        if DRIVE_SCOPE in scopes:
            sys.exit(
                "Drive werkt niet via het service-account; de OAuth-koppeling ontbreekt nog "
                "(GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN, zie scripts/google_consent.py). "
                "Meld dit kort in de chat in plaats van het opnieuw te proberen."
            )
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(key_file, scopes=scopes)

    sys.exit(
        "Google is nog niet gekoppeld (geen OAuth-variabelen en geen service-account.json). "
        "Meld dit kort in de chat in plaats van het opnieuw te proberen."
    )
