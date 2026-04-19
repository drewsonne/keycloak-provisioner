# keycloak-provisioner

A lightweight Kubernetes operator that provisions Keycloak realms, groups, users, and OAuth clients declaratively via CRDs.

Applications can request their own Keycloak configuration without manual Keycloak admin access or pre-deploy scripts.

---

## What it does

The operator manages four CRD kinds:

| Kind | What it provisions |
| --- | --- |
| `KeycloakRealm` | A Keycloak realm |
| `KeycloakRealmGroup` | A group within a realm, with optional realm-role assignments |
| `KeycloakUser` | A user within a realm, with optional group membership and role assignments |
| `KeycloakClient` | An OAuth client within a realm, with a generated secret stored in a K8s Secret |

All resources are reconciled continuously — the operator detects and repairs out-of-band drift every 5 minutes.

---

## Architecture

```
KeycloakRealm
  └── KeycloakRealmGroup   (ownerRef → Realm; cascade-deleted with realm)
  └── KeycloakUser         (ownerRef → Realm; cascade-deleted with realm)
  └── KeycloakClient       (ownerRef → Realm; cascade-deleted with realm)
        └── K8s Secret     (ownerRef → Client; GC'd with client)
```

Deleting a `KeycloakRealm` CR triggers K8s garbage collection of all child CRs, which in turn clean up their Keycloak resources before the realm itself is deleted.

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

### KeycloakRealm

Creates a Keycloak realm with configurable login, security, and SSL settings.

```yaml
apiVersion: keycloak.drewsonne.github.io/v1
kind: KeycloakRealm
metadata:
  name: my-realm
  namespace: keycloak
spec:
  realm: my-realm
  displayName: My Realm
  enabled: true
  loginWithEmailAllowed: true
  bruteForceProtected: true
  sslRequired: external        # none | external | all
```

`sslRequired` values:

| Value | Behaviour |
| --- | --- |
| `none` | SSL is not required for any client IP address |
| `external` | Non-private IP addresses must use SSL |
| `all` | All IP addresses must use SSL |

### KeycloakRealmGroup

Creates a group within a realm, with optional realm-role assignments and custom attributes.

```yaml
apiVersion: keycloak.drewsonne.github.io/v1
kind: KeycloakRealmGroup
metadata:
  name: my-realm-admins
  namespace: keycloak
spec:
  realm: my-realm
  name: admins
  realmRoles:
    - admin
  attributes:
    team: ["platform"]
```

### KeycloakUser

Creates a user within a realm, with optional group membership and realm-role assignments.

```yaml
apiVersion: keycloak.drewsonne.github.io/v1
kind: KeycloakUser
metadata:
  name: alice
  namespace: keycloak
spec:
  realm: my-realm
  username: alice
  email: alice@example.com
  firstName: Alice
  lastName: Smith
  enabled: true
  emailVerified: true
  groups:
    - admins
  realmRoles:
    - offline_access
```

### KeycloakClient

Creates an OAuth client within a realm, with a generated secret stored in a K8s Secret. Supports protocol mappers for token claim customisation.

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
  secretNamespace: my-app        # optional; defaults to CR namespace
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
```

---

## Per-CR credential override

Every CR accepts an optional `keycloakAdminSecret` field to use a different Keycloak admin account (useful for multi-tenant setups):

```yaml
spec:
  keycloakAdminSecret:
    name: my-other-admin-secret
    namespace: keycloak
```

---

## Output

`KeycloakClient` creates a Secret containing:

* `client_id` — the Keycloak client ID
* `client_secret` — the OAuth client secret

```bash
kubectl get secret keycloak-my-app-secret -n my-app -o yaml
```

---

## Behaviour notes

* **Idempotent** — safe to reapply; existing Keycloak resources are updated, not recreated
* **Secret preservation** — if a `KeycloakClient` Secret already exists, the `client_secret` is preserved rather than regenerated
* **Protocol mappers** — added on client creation only; not reconciled on update
* **Drift detection** — a timer runs every 5 minutes per resource to detect and repair out-of-band changes (e.g. manual edits in the Keycloak UI); drift is visible in `kubectl describe` as a K8s Warning event and in the `Drift` printer column
* **Cascade deletion** — deleting a `KeycloakRealm` CR triggers cleanup of all child CRs (groups, users, clients) before the realm is removed from Keycloak
* **User/group ordering** — if a `KeycloakUser` references a group that doesn't exist yet, the controller retries automatically until the group is ready

---

## Observability

```bash
kubectl get keycloakrealms
kubectl get keycloakclients
kubectl get keycloakrealmgroups
kubectl get keycloakusers
```

Printer columns include `Ready`, `Drift`, and `Age`. Drift events are also emitted as Kubernetes Warning events visible via `kubectl describe`.

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
