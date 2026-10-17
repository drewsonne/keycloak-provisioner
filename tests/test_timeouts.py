from unittest import mock

import common
import httpx
import kubernetes
import pytest
import realms
import urllib3


def test_hung_keycloak_call_raises_timeout(httpx_mock):
    # TimeoutException is an httpx.HTTPError, which every handler turns
    # into kopf.TemporaryError (retry).
    httpx_mock.add_exception(httpx.ReadTimeout("hung"))
    with pytest.raises(httpx.HTTPError):
        realms._find_realm("http://kc", "r", "tok")


def test_shared_client_has_timeout():
    assert common.http.timeout.read == common.REQUEST_TIMEOUT


def test_kubernetes_call_passes_timeout_and_hang_raises():
    api = mock.Mock()
    api.read_namespaced_secret.side_effect = urllib3.exceptions.ReadTimeoutError(
        None, "", "timed out"
    )
    spec = {"keycloakAdminSecret": {"name": "s", "namespace": "n"}}
    with (
        mock.patch.object(kubernetes.client, "CoreV1Api", return_value=api),
        pytest.raises(urllib3.exceptions.ReadTimeoutError),
    ):
        common.resolve_connection_params(spec)
    kwargs = api.read_namespaced_secret.call_args.kwargs
    assert kwargs["_request_timeout"] == common.REQUEST_TIMEOUT
