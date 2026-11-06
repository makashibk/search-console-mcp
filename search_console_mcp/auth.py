"""Credentials: loaded straight from the ADC file (no gcloud subprocess), one HTTP session per thread."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

import google.auth
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)

# Read-only scope: with this login Search Console data can be read but never changed.
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

LOGIN_HELP = (
    "Run the 'gcloud auth application-default login' command from the README (its --scopes must include "
    "https://www.googleapis.com/auth/webmasters.readonly), sign in, tick every checkbox, then retry. "
    "No restart is needed; the server reloads the credentials file."
)


class AuthError(Exception):
    pass


_lock = threading.Lock()
_creds = None
_generation = 0
_local = threading.local()


def credentials_path() -> Path:
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def credentials_type() -> str | None:
    try:
        return json.loads(credentials_path().read_text(encoding="utf-8")).get("type")
    except (OSError, ValueError):
        return None


def _load():
    path = credentials_path()
    if not path.is_file():
        raise AuthError(f"Credentials file not found: {path}. {LOGIN_HELP}")
    try:
        # Loading the file directly avoids google.auth.default(), which shells out to
        # gcloud for a project ID and can hang under stdio on Windows.
        creds, _ = google.auth.load_credentials_from_file(str(path), scopes=SCOPES)
    except DefaultCredentialsError as exc:
        raise AuthError(f"Could not read credentials file {path}: {exc}") from exc
    quota_project = os.environ.get("SEARCH_CONSOLE_MCP_QUOTA_PROJECT")
    if quota_project and hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(quota_project)
    return creds


def reload() -> None:
    global _creds, _generation
    with _lock:
        _creds = _load()
        _generation += 1
        log.info("credentials reloaded from %s", credentials_path())


def session() -> AuthorizedSession:
    global _creds, _generation
    with _lock:
        if _creds is None:
            _creds = _load()
            _generation += 1
        creds, generation = _creds, _generation
    current = getattr(_local, "session", None)
    if current is None or getattr(_local, "generation", None) != generation:
        current = AuthorizedSession(creds)
        _local.session = current
        _local.generation = generation
    return current


def describe_refresh_error(exc: RefreshError) -> str:
    text = str(exc)
    if "invalid_grant" in text or "expired or revoked" in text:
        return "Google login expired or was revoked. " + LOGIN_HELP
    if "scope" in text.lower():
        return "Your Google login does not include the Search Console scope. " + LOGIN_HELP
    return f"Google token refresh failed ({text}). " + LOGIN_HELP
