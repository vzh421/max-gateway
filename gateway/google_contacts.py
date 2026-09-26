"""Чтение контактов телефона из Google Контактов (People API), только чтение.

Сверено:
- People API — по discovery-документу (revision 20260923): GET v1/people/me/connections,
  personFields, pageSize ≤ 1000, pageToken/nextPageToken; Person.names[] (displayName,
  unstructuredName, familyName, givenName, middleName), Person.phoneNumbers[] (value,
  canonicalForm — E.164); область доступа contacts.readonly.
- OAuth — по https://accounts.google.com/.well-known/openid-configuration:
  authorization_endpoint, token_endpoint, grant refresh_token, PKCE S256.
НЕ проверено запуском: параметры access_type=offline и prompt=consent (нужны, чтобы Google
выдал refresh_token) и приём loopback-адреса в клиенте типа «Desktop app». Если
refresh_token не придёт, команда google-auth сообщит об этом.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .contacts import RawContact

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CONNECTIONS_URL = "https://people.googleapis.com/v1/people/me/connections"
SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
REDIRECT_URI = "http://127.0.0.1:8765/"
PAGE_SIZE = 1000


class GoogleAuthError(RuntimeError):
    pass


def make_auth_request(client_id: str) -> tuple[str, str, str]:
    """→ (url для браузера, code_verifier, state)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", verifier, state


def parse_redirect(url: str, state: str) -> str:
    """Код авторизации из адреса, на который браузер перешёл после согласия."""
    q = parse_qs(urlparse(url.strip()).query)
    if "error" in q:
        raise GoogleAuthError(f"Google вернул ошибку: {q['error'][0]}")
    if q.get("state", [""])[0] != state:
        raise GoogleAuthError("state не совпадает — вставьте адрес из этого же входа")
    code = q.get("code", [""])[0]
    if not code:
        raise GoogleAuthError("В адресе нет параметра code")
    return code


async def exchange_code(client: httpx.AsyncClient, client_id: str, client_secret: str, code: str, verifier: str) -> str:
    r = await client.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    if r.status_code != 200:
        raise GoogleAuthError(f"Обмен кода не удался: HTTP {r.status_code} {_error(r)}")
    refresh = r.json().get("refresh_token")
    if not refresh:
        raise GoogleAuthError("Google не выдал refresh_token (проверьте тип клиента OAuth: Desktop app)")
    return refresh


async def access_token(client: httpx.AsyncClient, client_id: str, client_secret: str, refresh_token: str) -> str:
    r = await client.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if r.status_code != 200:
        raise GoogleAuthError(f"Обновление доступа не удалось: HTTP {r.status_code} {_error(r)}. Повторите google-auth")
    return r.json()["access_token"]


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def save_refresh_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": token}, f)
    os.chmod(path, 0o600)


def load_refresh_token(path: Path) -> str:
    if not path.exists():
        raise GoogleAuthError(f"Нет {path}. Сначала: python -m gateway google-auth")
    return json.loads(path.read_text(encoding="utf-8"))["refresh_token"]


def person_to_contact(person: dict) -> RawContact | None:
    names: list[str] = []
    for n in person.get("names") or []:
        parts = " ".join(p for p in (n.get("familyName"), n.get("givenName"), n.get("middleName")) if p)
        for v in (n.get("displayName"), n.get("unstructuredName"), parts):
            if v and v not in names:
                names.append(v)
    phones = tuple(p.get("canonicalForm") or p.get("value") or "" for p in person.get("phoneNumbers") or [])
    phones = tuple(p for p in phones if p)
    if not phones:
        return None
    return RawContact(resource_name=person.get("resourceName", ""), names=tuple(names), phones=phones)


async def fetch_contacts(client: httpx.AsyncClient, token: str) -> list[RawContact]:
    result: list[RawContact] = []
    page_token: str | None = None
    while True:
        params = {"personFields": "names,phoneNumbers", "pageSize": PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        r = await client.get(CONNECTIONS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise GoogleAuthError(f"People API: HTTP {r.status_code} {_error(r)}")
        body = r.json()
        for person in body.get("connections") or []:
            c = person_to_contact(person)
            if c:
                result.append(c)
        page_token = body.get("nextPageToken")
        if not page_token:
            return result


async def load_google_contacts(client_id: str, client_secret: str, token_file: Path) -> list[RawContact]:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await access_token(client, client_id, client_secret, load_refresh_token(token_file))
        return await fetch_contacts(client, token)
