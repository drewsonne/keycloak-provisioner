"""Kopf handlers for the KeycloakRealmGroup CRD."""

from __future__ import annotations

from typing import Any

import httpx
import kopf
from common import (
    CRD_GROUP,
    CRD_VERSION,
    get_admin_token,
    resolve_connection_params,
    set_realm_owner_reference,
)

# ---------------------------------------------------------------------------
# Keycloak Admin API helpers
# ---------------------------------------------------------------------------


def _find_group(url: str, realm: str, name: str, token: str) -> dict | None:
    """Return the group object by exact name, or None."""
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/groups",
        params={"search": name, "exact": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    groups = resp.json()
    # API may return partial matches even with exact=true on older Keycloak versions
    for group in groups:
        if group["name"] == name:
            return group
    return None


def _get_group(url: str, realm: str, group_id: str, token: str) -> dict:
    """Return the full group representation by ID."""
    resp = httpx.get(
        f"{url}/admin/realms/{realm}/groups/{group_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _create_group(
    url: str,
    realm: str,
    name: str,
    attributes: dict[str, list[str]],
    token: str,
) -> str:
    """Create a group and return its ID."""
    resp = httpx.post(
        f"{url}/admin/realms/{realm}/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name, "attributes": attributes},
        timeout=10.0,
    )
    resp.raise_for_status()
    # Keycloak returns 201 with Location header containing the new group ID
    location = resp.headers.get("Location", "")
    group_id = location.rstrip("/").split("/")[-1]
    if not group_id:
        # Fall back to a search if Location header is absent
        group = _find_group(url, realm, name, token)
        if group is None:
            raise kopf.PermanentError(f"Group {name!r} not found after creation")
        group_id = group["id"]
    return group_id


def _update_group(
    url: str,
    realm: str,
    group_id: str,
    name: str,
    attributes: dict[str, list[str]],
    token: str,
) -> None:
    resp = httpx.put(
        f"{url}/admin/realms/{realm}/groups/{group_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name, "attributes": attributes},
        timeout=10.0,
    )
    resp.raise_for_status()


def _set_role_mappings(
    url: str,
    realm: str,
    group_id: str,
    desired_roles: list[str],
    token: str,
    logger: kopf.Logger,
) -> None:
    """Reconcile realm-level role mappings for the group."""
    # Fetch all available realm roles
    all_roles_resp = httpx.get(
        f"{url}/admin/realms/{realm}/roles",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    all_roles_resp.raise_for_status()
    role_map = {r["name"]: r for r in all_roles_resp.json()}

    # Validate requested roles exist
    missing = [r for r in desired_roles if r not in role_map]
    if missing:
        raise kopf.PermanentError(
            f"Realm roles not found in {realm!r}: {missing}. "
            "Create the roles first or remove them from realmRoles."
        )

    # Current mappings
    current_resp = httpx.get(
        f"{url}/admin/realms/{realm}/groups/{group_id}/role-mappings/realm",
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
            f"{url}/admin/realms/{realm}/groups/{group_id}/role-mappings/realm",
            headers={"Authorization": f"Bearer {token}"},
            json=to_add,
            timeout=10.0,
        ).raise_for_status()
        logger.info("Added roles %s to group", [r["name"] for r in to_add])

    if to_remove:
        httpx.request(
            "DELETE",
            f"{url}/admin/realms/{realm}/groups/{group_id}/role-mappings/realm",
            headers={"Authorization": f"Bearer {token}"},
            json=to_remove,
            timeout=10.0,
        ).raise_for_status()
        logger.info("Removed roles %s from group", [r["name"] for r in to_remove])


def _group_matches_spec(existing: dict, spec: kopf.Spec) -> bool:
    """Return True if the live group already matches spec (name + attributes)."""
    desired_attrs: dict[str, list[str]] = {
        k: ([v] if isinstance(v, str) else v)
        for k, v in spec.get("attributes", {}).items()
    }
    return (
        existing.get("name") == spec["name"]
        and existing.get("attributes", {}) == desired_attrs
    )


def _upsert_group(spec: kopf.Spec, logger: kopf.Logger) -> str:
    """Ensure the group exists and matches spec. Returns the group ID."""
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    name = spec["name"]
    attributes: dict[str, list[str]] = {
        k: ([v] if isinstance(v, str) else v)
        for k, v in spec.get("attributes", {}).items()
    }
    desired_roles: list[str] = list(spec.get("realmRoles", []))

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_group(keycloak_url, realm, name, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Group lookup failed: {exc}", delay=30) from exc

    try:
        if existing is None:
            group_id = _create_group(keycloak_url, realm, name, attributes, token)
            logger.info("Created group %r in realm %r", name, realm)
        else:
            group_id = existing["id"]
            full = _get_group(keycloak_url, realm, group_id, token)
            if not _group_matches_spec(full, spec):
                _update_group(keycloak_url, realm, group_id, name, attributes, token)
                logger.info("Updated group %r in realm %r", name, realm)
            else:
                logger.debug("Group %r already matches spec", name)

        if desired_roles:
            _set_role_mappings(
                keycloak_url, realm, group_id, desired_roles, token, logger
            )
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Group upsert failed: {exc}", delay=30) from exc

    return group_id


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@kopf.on.create(
    CRD_GROUP, CRD_VERSION, "keycloakrealmgroups", retries=5, backoff=30, timeout=300
)
def create_fn(
    spec: kopf.Spec,
    body: kopf.Body,
    namespace: str,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any]:
    group_id = _upsert_group(spec, logger)
    set_realm_owner_reference(body, namespace, spec["realm"], logger)
    return {"groupId": group_id, "name": spec["name"], "ready": True}


@kopf.on.resume(CRD_GROUP, CRD_VERSION, "keycloakrealmgroups")
def resume_fn(
    spec: kopf.Spec,
    body: kopf.Body,
    namespace: str,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any]:
    group_id = _upsert_group(spec, logger)
    set_realm_owner_reference(body, namespace, spec["realm"], logger)
    return {"groupId": group_id, "name": spec["name"], "ready": True}


@kopf.on.update(
    CRD_GROUP, CRD_VERSION, "keycloakrealmgroups", field="spec", retries=3, backoff=15
)
def update_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> dict[str, Any]:
    group_id = _upsert_group(spec, logger)
    return {"groupId": group_id, "name": spec["name"], "ready": True}


@kopf.on.delete(
    CRD_GROUP, CRD_VERSION, "keycloakrealmgroups", retries=3, backoff=15, timeout=120
)
def delete_fn(spec: kopf.Spec, logger: kopf.Logger, **_: Any) -> None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    name = spec["name"]

    try:
        token = get_admin_token(keycloak_url, username, password)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Keycloak auth failed: {exc}", delay=30) from exc

    try:
        existing = _find_group(keycloak_url, realm, name, token)
        if existing is None:
            logger.info("Group %r already absent in realm %r", name, realm)
            return
        httpx.delete(
            f"{keycloak_url}/admin/realms/{realm}/groups/{existing['id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("Deleted group %r from realm %r", name, realm)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Group delete failed: {exc}", delay=30) from exc


@kopf.timer(
    CRD_GROUP,
    CRD_VERSION,
    "keycloakrealmgroups",
    interval=300,
    initial_delay=60,
    idle=30,
)
def check_drift(
    spec: kopf.Spec, logger: kopf.Logger, **_: Any
) -> dict[str, Any] | None:
    keycloak_url, username, password = resolve_connection_params(spec)
    realm = spec["realm"]
    name = spec["name"]

    try:
        token = get_admin_token(keycloak_url, username, password)
        existing = _find_group(keycloak_url, realm, name, token)
    except httpx.HTTPError as exc:
        raise kopf.TemporaryError(f"Drift check failed: {exc}", delay=60) from exc

    needs_remediation = False
    reason = ""

    if existing is None:
        needs_remediation = True
        reason = "missing"
    else:
        full = _get_group(keycloak_url, realm, existing["id"], token)
        if not _group_matches_spec(full, spec):
            needs_remediation = True
            reason = "config mismatch"
        else:
            desired_roles = set(spec.get("realmRoles", []))
            if desired_roles:
                roles_resp = httpx.get(
                    f"{keycloak_url}/admin/realms/{realm}/groups/{existing['id']}/role-mappings/realm",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
                roles_resp.raise_for_status()
                current_roles = {r["name"] for r in roles_resp.json()}
                if desired_roles != current_roles:
                    needs_remediation = True
                    reason = "role mismatch"

    if needs_remediation:
        logger.warning(
            "Drift detected: group %r %s in realm %r — remediating",
            name,
            reason,
            realm,
        )
        group_id = _upsert_group(spec, logger)
        return {
            "groupId": group_id,
            "name": name,
            "ready": True,
            "drift": True,
            "driftReason": reason,
        }

    return None
