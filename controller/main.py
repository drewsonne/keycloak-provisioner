"""keycloak-provisioner operator entry point.

Importing the handler modules registers their kopf decorators.
"""

from __future__ import annotations

import logging
from typing import Any

import clients  # noqa: F401 — registers KeycloakClient handlers
import kopf


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.peering.standalone = True
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "keycloak.drewsonne.github.io/finalizer"
