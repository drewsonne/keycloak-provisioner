"""keycloak-provisioner operator entry point.

Importing the handler modules registers their kopf decorators.
"""

from __future__ import annotations

import logging
from typing import Any

import clients  # noqa: F401 — registers KeycloakClient handlers
import groups  # noqa: F401 — registers KeycloakRealmGroup handlers
import kopf
import realms  # noqa: F401 — registers KeycloakRealm handlers
import users  # noqa: F401 — registers KeycloakUser handlers


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.peering.standalone = True
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "keycloak.drewsonne.github.io/finalizer"
