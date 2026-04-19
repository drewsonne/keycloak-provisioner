"""Kopf handlers for the KeycloakUser CRD."""

from __future__ import annotations

from typing import Any

import httpx
import kopf
from common import CRD_GROUP, CRD_VERSION, get_admin_token, resolve_connection_params
from groups import _find_group

# ---------------------------------------------------------------------------
# Keycloak Admin API helpers
# ---------------------------------------------------------------------------


def _find_user(url: str, realm: str, username: str, token: str) -> dict | None:
    """Return the user representation by exact username, or None."""
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/users",
        params={"username": username, "exact": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    users = resp.json()
    for user in users:
        if user["username"] == username:
            return user
    return None


def _create_user(
    url: str,
    realm: str,
    token: str,
    *,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    enabled: bool,
    email_verified: bool,
    attributes: dict[str, list[str]],
) -> str:
    """Create a user and return their ID."""
    resp = httpx.post(
        f"{url}/admin/realms/{realm}/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": username,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": enabled,
            "emailVerified": email_verified,
            "attributes": attributes,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    location = resp.headers.get("Location", "")
    user_id = location.rstrip("/").split("/")[-1]
    if not user_id:
        user = _find_user(url, realm, username, token)
        if user is None:
            raise kopf.PermanentError(f"User {username!r} not found after creation")
        user_id = user["id"]
    return user_id


def _update_user(
    url: str,
    realm: str,
    user_id: str,
    token: str,
    *,
    email: str,
    first_name: str,
    last_name: str,
    enabled: bool,
    email_verified: bool,
    attributes: dict[str, list[str]],
) -> None:
    resp = httpx.put(
        f"{url}/admin/realms/{realm}/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": enabled,
            "emailVerified": email_verified,
            "attributes": attributes,
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _sync_group_memberships(
    url: str,
    realm: str,
    user_id: str,
    desired_group_names: list[str],
    token: str,
    logger: kopf.Logger,
) -> None:
    """Reconcile group memberships: add missing, remove extra."""
    # Resolve desired group names → IDs, failing hard if any name is unknown
    desired_ids: dict[str, str] = {}
    for name in desired_group_names:
        group = _find_group(url, realm, name, token)
        if group is None:
            raise kopf.PermanentError(
                f"Group {name!r} not found in realm {realm!r}. "
                "Create the KeycloakRealmGroup resource first."
            )
        desired_ids[group["id"]] = name

    # Current memberships
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/users/{user_id}/groups",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    current_ids = {g["id"]: g["name"] for g in resp.json()}

    to_add = set(desired_ids) - set(current_ids)
    to_remove = set(current_ids) - set(desired_ids)

    for group_id in to_add:
        httpx.put(
            f"{url}/admin/realms/{realm}/users/{user_id}/groups/{group_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("Added user to group %r", desired_ids[group_id])

    for group_id in to_remove:
        httpx.delete(
            f"{url}/admin/realms/{realm}/users/{user_id}/groups/{group_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("Removed user from group %r", current_ids[group_id])


def _sync_realm_roles(
    url: str,
    realm: str,
    user_id: str,
    desired_roles: list[str],
    token: str,
    logger: kopf.Logger,
) -> None:
    """Reconcile realm-level role assignments for the user."""
    all_roles_resp = httpx.get(
        f"{url}/admin/realms/{realm}/roles",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    all_roles_resp.raise_for_status()
    role_map = {r["name"]: r for r in all_roles_resp.json()}

    missing = [r for r in desired_roles if r not in role_map]
    if missing:
        raise kopf.PermanentError(
            f"Realm roles not found in {realm!r}: {missing}. "
            "Create the roles first or remove them from realmRoles."
        )

    current_resp = httpx.get(
        f"{url}/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    current_resp.raise_for_status()
    current_roles = {r["name"] for r in current_resp.json()}
    desired_set = set(desired_roles)

    to_add = [role_map[r] for r in desired_set - current_roles]
    to_remove = [role_map[r] for r in current_roles - desired_set if r in role_map]

    if to_add:
        httpx.post(
            f"{url}/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
            headers={"Authorization": f"Bearer {token}"},
            json=to_add,
            timeout=10.0,
        ).raise_for_status()
        logger.info("Added realm roles %s to user", [r["name"] for r in to_add])

    if to_remove:
        httpx.request(
            "DELETE",
            f"{url}/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
            headers={"Authorization": f"Bearer {token}"},
            json=to_remove,
            timeout=10.0,
        ).raise_for_status()
        logger.info("Removed realm roles %s from user", [r["name"] for r in to_remove])


def _user_matches_spec(existing: dict, spec: kopf.Spec) -> bool:
    """Return True if the live user already matches desired spec fields."""
    desired_attrs: dict[str, list[str]] = {
        k: ([v] if isinstance(v, str) else v)
        for k, v in spec.get("attributes", {}).items()
    }
    return (
        existing.get("email", "") == spec.get("email", "")
        and existing.get("firstName", "") == spec.get("firstName", "")
        and existing.get("lastName", "") == spec.get("lastName", "")
        and existing.get("enabled", True) == spec.get("enabled", True)
        and existing.get("emailVerified", False) == spec.get("emailVerified", False)
        and existing.get("attributes", {}) == desired_attrs
    )


def _upsert_user(spec: kopf.Spec, logger: kopf.Logger) -> str:
    """Ensure the user exists and matches spec. Returns user ID."""
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    user_username = spec["username"]
    attributes: dict[str, list[str]] = {
        k: ([v] if isinstance(v, str) else v)
        for k, v in spec.get("attributes", {}).items()
    }
    desired_groups: list[str] = list(spec.get("groups", []))
    desired_roles: list[str] = list(spec.get("realmRoles", []))

    kwargs: dict[str, Any] = {
        "email": spec.get("email", ""),
        "first_name": spec.get("firstName", ""),
        "last_name": spec.get("lastName", ""),
        "enabled": spec.get("enabled", True),
        "email_verified": spec.get("emailVerified", False),
        "attributes": attributes,
    }

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_user(keycloak_url, realm, user_username, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"User lookup failed: {exc}", delay=30) from exc

    try:
        if existing is None:
            user_id = _create_user(
                keycloak_url, realm, token, username=user_username, **kwargs
            )
            logger.info("Created user %r in realm %r", user_username, realm)
        else:
            user_id = existing["id"]
            if not _user_matches_spec(existing, spec):
                _update_user(keycloak_url, realm, user_id, token, **kwargs)
                logger.info("Updated user %r in realm %r", user_username, realm)
            else:
                logger.debug("User %r already matches spec", user_username)

        if desired_groups:
            _sync_group_memberships(
                keycloak_url, realm, user_id, desired_groups, token, logger
            )
        if desired_roles:
            _sync_realm_roles(
                keycloak_url, realm, user_id, desired_roles, token, logger
            )
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"User upsert failed: {exc}", delay=30) from exc

    return user_id


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@kopf.on.create(
    CRD_GROUP, CRD_VERSION, "keycloakusers", retries=5, backoff=30, timeout=300
)
def create_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    user_id = _upsert_user(spec, logger)
    return {"userId": user_id, "username": spec["username"], "ready": True}


@kopf.on.resume(CRD_GROUP, CRD_VERSION, "keycloakusers")
def resume_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    user_id = _upsert_user(spec, logger)
    return {"userId": user_id, "username": spec["username"], "ready": True}


@kopf.on.update(
    CRD_GROUP, CRD_VERSION, "keycloakusers", field="spec", retries=3, backoff=15
)
def update_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    user_id = _upsert_user(spec, logger)
    return {"userId": user_id, "username": spec["username"], "ready": True}


@kopf.on.delete(
    CRD_GROUP, CRD_VERSION, "keycloakusers", retries=3, backoff=15, timeout=120
)
def delete_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    user_username = spec["username"]

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_user(keycloak_url, realm, user_username, token)
        if existing is None:
            logger.info("User %r already absent in realm %r", user_username, realm)
            return
        httpx.delete(
            f"{keycloak_url}/admin/realms/{realm}/users/{existing['id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("Deleted user %r from realm %r", user_username, realm)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"User delete failed: {exc}", delay=30) from exc


@kopf.timer(CRD_GROUP, CRD_VERSION, "keycloakusers", interval=300, initial_delay=60)
def check_drift(
    spec: kopf.Spec, logger: kopf.Logger, **_: Any
) -> dict[str, Any] | None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    user_username = spec["username"]

    try:
        token = get_admin_token(keycloak_url, username, password)
        existing = _find_user(keycloak_url, realm, user_username, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Drift check failed: {exc}", delay=60) from exc

    needs_remediation = False
    reason = ""

    if existing is None:
        needs_remediation = True
        reason = "missing"
    elif not _user_matches_spec(existing, spec):
        needs_remediation = True
        reason = "config mismatch"
    else:
        # Check group memberships separately
        try:
            desired_groups: list[str] = list(spec.get("groups", []))
            if desired_groups:
                current_resp = httpx.get(
                    f"{keycloak_url}/admin/realms/{realm}/users/{existing['id']}/groups",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
                current_resp.raise_for_status()
                current_group_names = {g["name"] for g in current_resp.json()}
                if set(desired_groups) != current_group_names:
                    needs_remediation = True
                    reason = "group membership mismatch"
        except httpx.HTTPError as exc:
            raise kopf.TemporaryError(
                f"Group drift check failed: {exc}", delay=60
            ) from exc

    if needs_remediation:
        logger.warning(
            "Drift detected: user %r %s in realm %r — remediating",
            user_username,
            reason,
            realm,
        )
        user_id = _upsert_user(spec, logger)
        return {
            "userId": user_id,
            "username": user_username,
            "ready": True,
            "drift": True,
            "driftReason": reason,
        }

    return None
