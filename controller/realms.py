"""Kopf handlers for the KeycloakRealm CRD."""

from __future__ import annotations

from typing import Any

import httpx
import kopf
from common import CRD_GROUP, CRD_VERSION, get_admin_token, resolve_connection_params

# ---------------------------------------------------------------------------
# Keycloak Admin API helpers
# ---------------------------------------------------------------------------


def _find_realm(url: str, realm: str, token: str) -> dict | None:
    """Return the realm representation or None if it does not exist."""
    resp = httpx.get(
        f"{url}/admin/realms/{realm}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _create_realm(
    url: str,
    realm: str,
    display_name: str,
    display_name_html: str,
    enabled: bool,
    token: str,
    *,
    registration_allowed: bool,
    login_with_email_allowed: bool,
    duplicate_emails_allowed: bool,
    reset_password_allowed: bool,
    edit_username_allowed: bool,
    brute_force_protected: bool,
    ssl_required: str,
) -> None:
    resp = httpx.post(
        f"{url}/admin/realms",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": realm,
            "realm": realm,
            "displayName": display_name,
            "displayNameHtml": display_name_html,
            "enabled": enabled,
            "registrationAllowed": registration_allowed,
            "loginWithEmailAllowed": login_with_email_allowed,
            "duplicateEmailsAllowed": duplicate_emails_allowed,
            "resetPasswordAllowed": reset_password_allowed,
            "editUsernameAllowed": edit_username_allowed,
            "bruteForceProtected": brute_force_protected,
            "sslRequired": ssl_required,
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _update_realm(
    url: str,
    realm: str,
    display_name: str,
    display_name_html: str,
    enabled: bool,
    token: str,
    *,
    registration_allowed: bool,
    login_with_email_allowed: bool,
    duplicate_emails_allowed: bool,
    reset_password_allowed: bool,
    edit_username_allowed: bool,
    brute_force_protected: bool,
    ssl_required: str,
) -> None:
    resp = httpx.put(
        f"{url}/admin/realms/{realm}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "displayName": display_name,
            "displayNameHtml": display_name_html,
            "enabled": enabled,
            "registrationAllowed": registration_allowed,
            "loginWithEmailAllowed": login_with_email_allowed,
            "duplicateEmailsAllowed": duplicate_emails_allowed,
            "resetPasswordAllowed": reset_password_allowed,
            "editUsernameAllowed": edit_username_allowed,
            "bruteForceProtected": brute_force_protected,
            "sslRequired": ssl_required,
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _realm_matches_spec(existing: dict, spec: kopf.Spec) -> bool:
    """Return True if the live realm already matches the desired spec."""
    return (
        existing.get("displayName", "") == spec.get("displayName", spec["realm"])
        and existing.get("displayNameHtml", "") == spec.get("displayNameHtml", "")
        and existing.get("enabled", True) == spec.get("enabled", True)
        and existing.get("registrationAllowed", False)
        == spec.get("registrationAllowed", False)
        and existing.get("loginWithEmailAllowed", True)
        == spec.get("loginWithEmailAllowed", True)
        and existing.get("duplicateEmailsAllowed", False)
        == spec.get("duplicateEmailsAllowed", False)
        and existing.get("resetPasswordAllowed", False)
        == spec.get("resetPasswordAllowed", False)
        and existing.get("editUsernameAllowed", False)
        == spec.get("editUsernameAllowed", False)
        and existing.get("bruteForceProtected", False)
        == spec.get("bruteForceProtected", False)
        and existing.get("sslRequired", "external")
        == spec.get("sslRequired", "external")
    )


def _upsert_realm(spec: kopf.Spec, logger: kopf.Logger) -> None:
    """Ensure the Keycloak realm exists and matches spec."""
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    kwargs: dict[str, Any] = {
        "display_name": spec.get("displayName", realm),
        "display_name_html": spec.get("displayNameHtml", ""),
        "enabled": spec.get("enabled", True),
        "registration_allowed": spec.get("registrationAllowed", False),
        "login_with_email_allowed": spec.get("loginWithEmailAllowed", True),
        "duplicate_emails_allowed": spec.get("duplicateEmailsAllowed", False),
        "reset_password_allowed": spec.get("resetPasswordAllowed", False),
        "edit_username_allowed": spec.get("editUsernameAllowed", False),
        "brute_force_protected": spec.get("bruteForceProtected", False),
        "ssl_required": spec.get("sslRequired", "external"),
    }

    try:
        existing = _find_realm(keycloak_url, realm, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(
            f"Keycloak realm lookup failed: {exc}", delay=30
        ) from exc

    try:
        if existing is None:
            _create_realm(keycloak_url, realm, token=token, **kwargs)
            logger.info("Created Keycloak realm %s", realm)
        elif not _realm_matches_spec(existing, spec):
            _update_realm(keycloak_url, realm, token=token, **kwargs)
            logger.info("Updated Keycloak realm %s", realm)
        else:
            logger.debug("Realm %s already matches spec", realm)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(
            f"Keycloak realm upsert failed: {exc}", delay=30
        ) from exc


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@kopf.on.create(
    CRD_GROUP, CRD_VERSION, "keycloakrealms", retries=5, backoff=30, timeout=300
)
def create_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    _upsert_realm(spec, logger)
    return {"realm": spec["realm"], "ready": True}


@kopf.on.resume(CRD_GROUP, CRD_VERSION, "keycloakrealms")
def resume_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    _upsert_realm(spec, logger)
    return {"realm": spec["realm"], "ready": True}


@kopf.on.update(
    CRD_GROUP, CRD_VERSION, "keycloakrealms", field="spec", retries=3, backoff=15
)
def update_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    _upsert_realm(spec, logger)
    return {"realm": spec["realm"], "ready": True}


@kopf.on.delete(
    CRD_GROUP, CRD_VERSION, "keycloakrealms", retries=3, backoff=15, timeout=120
)
def delete_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_realm(keycloak_url, realm, token)
        if existing is None:
            logger.info("Realm %s already absent", realm)
            return
        httpx.delete(
            f"{keycloak_url}/admin/realms/{realm}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("Deleted Keycloak realm %s", realm)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(
            f"Keycloak realm delete failed: {exc}", delay=30
        ) from exc


@kopf.timer(CRD_GROUP, CRD_VERSION, "keycloakrealms", interval=300, initial_delay=60)
def check_drift(
    spec: kopf.Spec, logger: kopf.Logger, **_: Any
) -> dict[str, Any] | None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]

    try:
        token = get_admin_token(keycloak_url, username, password)
        existing = _find_realm(keycloak_url, realm, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Drift check failed: {exc}", delay=60) from exc

    if existing is None or not _realm_matches_spec(existing, spec):
        reason = "missing" if existing is None else "config mismatch"
        logger.warning("Drift detected: realm %s %s — remediating", realm, reason)
        _upsert_realm(spec, logger)
        return {"realm": realm, "ready": True, "drift": True, "driftReason": reason}

    return None
