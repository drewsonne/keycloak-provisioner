"""Shared helpers for the keycloak-provisioner operator."""

from __future__ import annotations

import base64
import os

import kopf
import kubernetes

KEYCLOAK_URL = os.environ["KEYCLOAK_URL"]
KEYCLOAK_ADMIN_USERNAME = os.environ["KEYCLOAK_ADMIN_USERNAME"]
KEYCLOAK_ADMIN_PASSWORD = os.environ["KEYCLOAK_ADMIN_PASSWORD"]

CRD_GROUP = "keycloak.drewsonne.github.io"
CRD_VERSION = "v1"


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
        username = base64.b64decode(data["username"]).decode()
        password = base64.b64decode(data["password"]).decode()
        return KEYCLOAK_URL, username, password
    return KEYCLOAK_URL, KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD


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
            # Check if the existing secret already has the correct values;
            # skip the patch if so to avoid unnecessary resourceVersion churn.
            existing = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            existing_data = {
                k: base64.b64decode(v).decode()
                for k, v in (existing.data or {}).items()
            }
            if existing_data == data:
                logger.debug("Secret %s already up-to-date, skipping patch", secret_name)
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
