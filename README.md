# keycloak-provisioner

A lightweight Kubernetes operator that provisions Keycloak OAuth clients using a Custom Resource Definition (CRD).

It allows applications to declaratively request their own Keycloak client and credentials without manual Keycloak admin access or pre-deploy scripts.

---

## What it does

When you create a `KeycloakClient` resource, the operator will:

* Create a Keycloak OAuth client in the specified realm
* Add any configured protocol mappers (e.g. realm-roles mapper)
* Store the `client_id` and `client_secret` in a Kubernetes Secret

---

## Architecture

* Keycloak (any version with Admin REST API)
* This operator (runs in Kubernetes)
* CRD (`KeycloakClient`)
* Kubernetes Secrets for OAuth credentials

---

## Installation

Add the Helm repository:

```bash
helm repo add keycloak-provisioner https://drewsonne.github.io/keycloak-provisioner
helm repo update
```

Install the chart:

```bash
helm install keycloak-provisioner keycloak-provisioner/keycloak-provisioner \
  --namespace keycloak \
  --set keycloak.url=http://keycloak.keycloak.svc.cluster.local \
  --set keycloak.adminSecret.name=keycloak-admin-credentials
```

---

## Requirements

* A running Keycloak instance accessible from within the cluster
* A Kubernetes Secret containing Keycloak admin credentials:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-admin-credentials
  namespace: keycloak
stringData:
  username: admin
  password: <admin-password>
```

---

## Configuration

Default values:

```yaml
image:
  repository: drewsonne/keycloak-provisioner
  tag: latest

keycloak:
  url: http://keycloak-keycloakx-http.keycloak.svc.cluster.local
  adminSecret:
    name: keycloak-admin-credentials

namespace: keycloak
```

---

## Usage

Create a Keycloak OAuth client:

```yaml
apiVersion: keycloak.drewsonne.github.io/v1
kind: KeycloakClient
metadata:
  name: my-app
  namespace: my-app
spec:
  clientId: my-app
  realm: my-realm
  redirectUris:
    - https://my-app.example.com/oauth-callback
  webOrigins:
    - https://my-app.example.com
  secretName: keycloak-my-app-secret
  protocolMappers:
    - name: realm-roles
      mapperType: oidc-usermodel-realm-role-mapper
      config:
        multivalued: "true"
        userinfo.token.claim: "true"
        id.token.claim: "true"
        access.token.claim: "true"
        claim.name: "realm_access.roles"
        jsonType.label: "String"
  keycloakAdminSecret:
    name: keycloak-admin-credentials
    namespace: keycloak
```

---

## Output

The operator creates a Secret with:

```bash
kubectl get secret keycloak-my-app-secret -n my-app -o yaml
```

Contents:

* `client_id` — the Keycloak client ID
* `client_secret` — the OAuth client secret

---

## Behaviour notes

* Idempotent: safe to reapply the same resource
* If a client already exists in Keycloak, its secret is retrieved (not regenerated)
* If the K8s Secret already exists, the existing `client_secret` is preserved
* Protocol mappers are added on creation only (not reconciled on update)

---

## Development

Build locally:

```bash
docker build -t drewsonne/keycloak-provisioner:dev .
```

Run locally (requires cluster access):

```bash
docker run --rm \
  -e KEYCLOAK_URL=http://keycloak.keycloak.svc.cluster.local \
  -e KEYCLOAK_ADMIN_USERNAME=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=... \
  drewsonne/keycloak-provisioner:dev
```

---

## License

GPLv3
