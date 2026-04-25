"""Shared helpers for the keycloak-provisioner operator."""

from __future__ import annotations

import base64
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import kopf
import kubernetes

KEYCLOAK_URL = os.environ["KEYCLOAK_URL"]
KEYCLOAK_ADMIN_USERNAME = os.environ["KEYCLOAK_ADMIN_USERNAME"]
KEYCLOAK_ADMIN_PASSWORD = os.environ["KEYCLOAK_ADMIN_PASSWORD"]

CRD_GROUP = "keycloak.drewsonne.github.io"
CRD_VERSION = "v1"

# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_cache_lock = threading.Lock()


def get_admin_token(url: str, username: str, password: str) -> str:
    """Return a cached admin access token, refreshing when within 10 s of expiry."""
    import httpx

    cache_key = (url, username)
    now = time.monotonic()
    with _token_cache_lock:
        cached = _token_cache.get(cache_key)
        if cached is not None:
            token, expires_at = cached
            if now < expires_at:
                return token

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
    payload = resp.json()
    token = payload["access_token"]
    expires_in: int = payload.get("expires_in", 60)
    expires_at = now + expires_in - 10  # 10-second buffer

    with _token_cache_lock:
        _token_cache[cache_key] = (token, expires_at)

    return token


# ---------------------------------------------------------------------------
# Status conditions
# ---------------------------------------------------------------------------


def set_condition(
    patch: kopf.Patch,
    body: kopf.Body,
    *,
    condition_type: str,
    status: str,
    reason: str,
    message: str = "",
) -> None:
    """Merge a status condition into patch.status['conditions'].

    Preserves lastTransitionTime when the condition status has not changed.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing: list[dict[str, Any]] = list(
        (body.get("status") or {}).get("conditions", [])
    )
    prev = next((c for c in existing if c["type"] == condition_type), None)
    last_transition = (
        now
        if prev is None or prev.get("status") != status
        else prev["lastTransitionTime"]
    )
    new_condition: dict[str, Any] = {
        "type": condition_type,
        "status": status,
        "reason": reason,
        "message": message,
        "lastTransitionTime": last_transition,
        "observedGeneration": body.get("metadata", {}).get("generation", 0),
    }
    updated = [c for c in existing if c["type"] != condition_type]
    updated.append(new_condition)
    patch.status["conditions"] = updated


# ---------------------------------------------------------------------------
# Owner references
# ---------------------------------------------------------------------------

_KIND_PLURALS: dict[str, str] = {
    "KeycloakRealm": "keycloakrealms",
    "KeycloakRealmGroup": "keycloakrealmgroups",
    "KeycloakUser": "keycloakusers",
    "KeycloakClient": "keycloakclients",
}


def set_realm_owner_reference(
    body: kopf.Body,
    namespace: str,
    realm_name: str,
    logger: kopf.Logger,
) -> None:
    """Patch this CR's ownerReferences to point to the matching KeycloakRealm.

    Enables k8s GC to cascade-delete groups/users when the realm CR is removed.
    Silently skips if no matching KeycloakRealm is found.
    """
    custom = kubernetes.client.CustomObjectsApi()
    try:
        realm_list = custom.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="keycloakrealms",
        )
    except kubernetes.client.exceptions.ApiException as exc:
        logger.warning("Could not list KeycloakRealm objects: %s", exc)
        return

    realm_obj = next(
        (
            r
            for r in realm_list.get("items", [])
            if r.get("spec", {}).get("realm") == realm_name
        ),
        None,
    )
    if realm_obj is None:
        logger.debug(
            "No KeycloakRealm found for realm %r in namespace %s — skipping owner ref",
            realm_name,
            namespace,
        )
        return

    owner_uid = realm_obj["metadata"]["uid"]
    existing_owners: list[dict[str, Any]] = body.get("metadata", {}).get(
        "ownerReferences", []
    )
    if any(o.get("uid") == owner_uid for o in existing_owners):
        return  # already set

    owner_ref = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "KeycloakRealm",
        "name": realm_obj["metadata"]["name"],
        "uid": owner_uid,
        "blockOwnerDeletion": True,
        "controller": True,
    }
    cr_name = body["metadata"]["name"]
    cr_plural = _KIND_PLURALS.get(str(body.get("kind", "")), "")

    try:
        custom.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural=cr_plural,
            name=cr_name,
            body={"metadata": {"ownerReferences": [owner_ref]}},
        )
        logger.info(
            "Set ownerReference: %s/%s → KeycloakRealm/%s",
            cr_plural,
            cr_name,
            realm_obj["metadata"]["name"],
        )
    except kubernetes.client.exceptions.ApiException as exc:
        logger.warning("Failed to set ownerReference: %s", exc)


# ---------------------------------------------------------------------------
# Connection parameters
# ---------------------------------------------------------------------------


def resolve_connection_params(
    spec: kopf.Spec,
) -> tuple[str, str, str]:
    """Return (url, username, password) for the Keycloak admin API.

    If the CR specifies ``keycloakAdminSecret``, read credentials from that
    secret.  Otherwise fall back to the global env vars.
    """
    override_secret = spec.get("keycloakAdminSecret")
    if override_secret:
        v1 = kubernetes.client.CoreV1Api()
        try:
            secret = v1.read_namespaced_secret(
                name=override_secret["name"],
                namespace=override_secret["namespace"],
            )
        except kubernetes.client.exceptions.ApiException as exc:
            raise kopf.TemporaryError(
                f"Cannot read keycloak admin secret "
                f"{override_secret['namespace']}/{override_secret['name']}: {exc}",
                delay=30,
            ) from exc
        data = secret.data or {}
        username = (
            base64.b64decode(data["username"]).decode()
            if "username" in data
            else KEYCLOAK_ADMIN_USERNAME
        )
        password = base64.b64decode(data["password"]).decode()
        return KEYCLOAK_URL, username, password
    return KEYCLOAK_URL, KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD


# ---------------------------------------------------------------------------
# Kubernetes Secret helpers
# ---------------------------------------------------------------------------


def get_existing_client_secret(
    namespace: str,
    secret_name: str,
) -> str | None:
    """Read the client_secret from an existing Kubernetes Secret, or return None."""
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
        raw = (secret.data or {}).get("client_secret")
        if raw:
            return base64.b64decode(raw).decode()
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise kopf.TemporaryError(
                f"Failed reading secret {secret_name}: {exc}",
                delay=15,
            ) from exc
    return None


def ensure_secret(
    namespace: str,
    secret_name: str,
    body: kopf.Body,
    data: dict[str, str],
    logger: kopf.Logger,
) -> None:
    """Create or update a Kubernetes Secret with the supplied data.

    Skips the patch if the secret already exists with identical values to
    avoid churning the secret's resourceVersion, which would require pod
    restarts to pick up the new env vars.
    """
    v1 = kubernetes.client.CoreV1Api()
    secret_body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(name=secret_name),
        string_data=data,
    )
    # Owner references cannot cross namespaces
    cr_namespace = body["metadata"]["namespace"]
    if namespace == cr_namespace:
        kopf.adopt(secret_body)

    try:
        v1.create_namespaced_secret(namespace=namespace, body=secret_body)
        logger.info("Created secret %s", secret_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 409:
            existing = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            existing_data = {
                k: base64.b64decode(v).decode()
                for k, v in (existing.data or {}).items()
            }
            if existing_data == data:
                logger.debug(
                    "Secret %s already up-to-date, skipping patch",
                    secret_name,
                )
                return
            v1.patch_namespaced_secret(
                name=secret_name,
                namespace=namespace,
                body={"stringData": data},
            )
            logger.info("Updated existing secret %s", secret_name)
        else:
            raise kopf.TemporaryError(
                f"Kubernetes API error creating secret: {exc}",
                delay=15,
            ) from exc


def delete_secret(
    secret_name: str,
    secret_ns: str,
    logger: kopf.Logger,
) -> None:
    """Delete a Kubernetes Secret, ignoring 404."""
    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.delete_namespaced_secret(name=secret_name, namespace=secret_ns)
        logger.info("Deleted secret %s", secret_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise kopf.TemporaryError(
                f"Failed to delete secret: {exc}",
                delay=15,
            ) from exc
        logger.info("Secret %s already absent", secret_name)
