"""Kopf handlers for the KeycloakClient CRD."""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import kopf
from common import (
    CRD_GROUP,
    CRD_VERSION,
    delete_secret,
    ensure_secret,
    get_existing_client_secret,
    resolve_connection_params,
)

# ---------------------------------------------------------------------------
# Keycloak Admin API helpers
# ---------------------------------------------------------------------------


def _get_admin_token(url: str, username: str, password: str) -> str:
    resp = httpx.post(
        f"{url}/realms/master/protocol/openid-connect/token",
        data={
            "username": username,
            "password": password,
            "grant_type": "password",
            "client_id": "admin-cli",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _find_client(url: str, realm: str, client_id: str, token: str) -> dict | None:
    """Return the Keycloak client object or None if not found."""
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/clients",
        params={"clientId": client_id},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    clients = resp.json()
    return clients[0] if clients else None


def _get_client_secret(url: str, realm: str, client_uuid: str, token: str) -> str:
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/clients/{client_uuid}/client-secret",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["value"]


def _create_client(
    url: str,
    realm: str,
    client_id: str,
    redirect_uris: list[str],
    web_origins: list[str],
    client_secret: str,
    token: str,
) -> None:
    resp = httpx.post(
        f"{url}/admin/realms/{realm}/clients",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "clientId": client_id,
            "enabled": True,
            "clientAuthenticatorType": "client-secret",
            "secret": client_secret,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
            "protocol": "openid-connect",
            "publicClient": False,
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _update_client(
    url: str,
    realm: str,
    client_uuid: str,
    redirect_uris: list[str],
    web_origins: list[str],
    token: str,
) -> None:
    resp = httpx.put(
        f"{url}/admin/realms/{realm}/clients/{client_uuid}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _add_protocol_mappers(
    url: str,
    realm: str,
    client_uuid: str,
    mappers: list[dict],
    token: str,
) -> None:
    for mapper in mappers:
        resp = httpx.post(
            f"{url}/admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": mapper["name"],
                "protocol": "openid-connect",
                "protocolMapper": mapper["mapperType"],
                "consentRequired": False,
                "config": dict(mapper.get("config", {})),
            },
            timeout=10.0,
        )
        resp.raise_for_status()


def _upsert_client(
    spec: kopf.Spec,
    existing_client_secret: str | None,
    logger: kopf.Logger,
) -> tuple[str, str]:
    """Ensure the Keycloak client exists. Returns (client_uuid, client_secret)."""
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    client_id = spec["clientId"]
    redirect_uris = list(spec.get("redirectUris", []))
    web_origins = list(spec.get("webOrigins", []))
    protocol_mappers = list(spec.get("protocolMappers", []))

    try:
        token = _get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_client(keycloak_url, realm, client_id, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak lookup failed: {exc}", delay=30) from exc

    if existing:
        client_uuid = existing["id"]
        client_secret = _get_client_secret(keycloak_url, realm, client_uuid, token)
        logger.info("Keycloak client %s already exists", client_id)
        return client_uuid, client_secret

    # Generate a new secret or reuse the one already in the K8s Secret
    client_secret = existing_client_secret or secrets.token_urlsafe(32)

    try:
        _create_client(
            keycloak_url,
            realm,
            client_id,
            redirect_uris,
            web_origins,
            client_secret,
            token,
        )
        existing = _find_client(keycloak_url, realm, client_id, token)
        if existing is None:
            msg = f"Client {client_id} not found after creation"
            raise kopf.TemporaryError(msg, delay=30)
        client_uuid = existing["id"]
        if protocol_mappers:
            _add_protocol_mappers(
                keycloak_url,
                realm,
                client_uuid,
                protocol_mappers,
                token,
            )
        logger.info("Created Keycloak client %s", client_id)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak create failed: {exc}", delay=30) from exc

    return client_uuid, client_secret


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@kopf.on.create(
    CRD_GROUP,
    CRD_VERSION,
    "keycloakclients",
    retries=5,
    backoff=30,
    timeout=300,
)
def create_fn(
    spec: kopf.Spec,
    name: str,
    namespace: str,
    body: kopf.Body,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any]:
    secret_name = spec["secretName"]
    secret_ns = spec.get("secretNamespace", namespace)
    existing_secret = get_existing_client_secret(secret_ns, secret_name)

    client_uuid, client_secret = _upsert_client(spec, existing_secret, logger)

    ensure_secret(
        secret_ns,
        secret_name,
        body,
        {"client_id": spec["clientId"], "client_secret": client_secret},
        logger,
    )
    return {"clientId": spec["clientId"], "clientUuid": client_uuid, "ready": True}


@kopf.on.resume(CRD_GROUP, CRD_VERSION, "keycloakclients")
def resume_fn(
    spec: kopf.Spec,
    name: str,
    namespace: str,
    body: kopf.Body,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any]:
    secret_name = spec["secretName"]
    secret_ns = spec.get("secretNamespace", namespace)
    existing_secret = get_existing_client_secret(secret_ns, secret_name)

    client_uuid, client_secret = _upsert_client(spec, existing_secret, logger)

    ensure_secret(
        secret_ns,
        secret_name,
        body,
        {"client_id": spec["clientId"], "client_secret": client_secret},
        logger,
    )
    return {"clientId": spec["clientId"], "clientUuid": client_uuid, "ready": True}


@kopf.on.update(
    CRD_GROUP,
    CRD_VERSION,
    "keycloakclients",
    field="spec",
    retries=3,
    backoff=15,
)
def update_fn(
    spec: kopf.Spec,
    name: str,
    namespace: str,
    body: kopf.Body,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any]:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    client_id = spec["clientId"]
    redirect_uris = list(spec.get("redirectUris", []))
    web_origins = list(spec.get("webOrigins", []))

    try:
        token = _get_admin_token(keycloak_url, username, password)
        existing = _find_client(keycloak_url, realm, client_id, token)
        if existing:
            _update_client(
                keycloak_url,
                realm,
                existing["id"],
                redirect_uris,
                web_origins,
                token,
            )
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak update failed: {exc}", delay=30) from exc

    secret_name = spec["secretName"]
    secret_ns = spec.get("secretNamespace", namespace)
    existing_secret = get_existing_client_secret(secret_ns, secret_name)
    client_uuid, client_secret = _upsert_client(spec, existing_secret, logger)

    ensure_secret(
        secret_ns,
        secret_name,
        body,
        {"client_id": client_id, "client_secret": client_secret},
        logger,
    )
    return {"clientId": client_id, "clientUuid": client_uuid, "ready": True}


@kopf.on.delete(
    CRD_GROUP,
    CRD_VERSION,
    "keycloakclients",
    retries=3,
    backoff=15,
    timeout=120,
)
def delete_fn(
    spec: kopf.Spec,
    name: str,
    namespace: str,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    client_id = spec["clientId"]
    secret_name = spec["secretName"]
    secret_ns = spec.get("secretNamespace", namespace)

    try:
        token = _get_admin_token(keycloak_url, username, password)
        existing = _find_client(keycloak_url, realm, client_id, token)
        if existing:
            httpx.delete(
                f"{keycloak_url}/admin/realms/{realm}/clients/{existing['id']}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            ).raise_for_status()
            logger.info("Deleted Keycloak client %s", client_id)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak delete failed: {exc}", delay=30) from exc

    delete_secret(secret_name, secret_ns, logger)


@kopf.timer(CRD_GROUP, CRD_VERSION, "keycloakclients", interval=300, initial_delay=60)
def check_drift(
    spec: kopf.Spec,
    namespace: str,
    body: kopf.Body,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any] | None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    client_id = spec["clientId"]
    secret_name = spec["secretName"]
    secret_ns = spec.get("secretNamespace", namespace)

    try:
        token = _get_admin_token(keycloak_url, username, password)
        existing = _find_client(keycloak_url, realm, client_id, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Drift check failed: {exc}", delay=60) from exc

    existing_secret = get_existing_client_secret(secret_ns, secret_name)

    if not existing or not existing_secret:
        logger.warning(
            "Drift detected: client_exists=%s secret_exists=%s — remediating",
            bool(existing),
            bool(existing_secret),
        )
        new_uuid, client_secret = _upsert_client(spec, existing_secret, logger)
        ensure_secret(
            secret_ns,
            secret_name,
            body,
            {"client_id": spec["clientId"], "client_secret": client_secret},
            logger,
        )
        return {
            "clientId": spec["clientId"],
            "clientUuid": new_uuid,
            "ready": True,
            "drift": True,
        }

    return None
