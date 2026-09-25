# OIDC login with Keycloak or Zitadel

OIDC is optional. Local Jeen Insights accounts remain available: their password
is created and stored by Jeen Insights. An OIDC user's password belongs only to
Keycloak or Zitadel and must never be put in environment variables or a
Kubernetes Secret.

`SETUP_BOOTSTRAP_TOKEN` is neither kind of password. It is an out-of-band,
one-time gate for creating the first local administrator at `/setup`. Complete
bootstrap before relying on SSO role administration.

## Do 1 — create the OIDC applications

Choose the exact browser-facing URL and set it as `PUBLIC_APP_URL`; for example:

```text
https://insights.example.invalid
```

Do not add a trailing slash. Proxies must preserve the original scheme and
host.

### Keycloak

1. In the required realm, create an OpenID Connect client.
2. Enable authorization code flow, PKCE `S256`, and client authentication when
   using a confidential client.
3. Register exactly:

   ```text
   https://insights.example.invalid/auth/keycloak/callback
   ```

4. Record the realm issuer, client ID and (for a confidential client) secret.
   A realm issuer looks like
   `https://keycloak.example.invalid/realms/replace-with-realm`.

### Zitadel

1. In the target project, create a Web/OIDC application using authorization
   code with PKCE.
2. Register exactly:

   ```text
   https://insights.example.invalid/auth/zitadel/callback
   ```

3. Record the issuer, client ID and optional client secret.

The redirect path is always `/auth/<provider>/callback`. Microsoft Entra's
separate callback is `/auth/microsoft/callback`.

## Do 2 — create the user and roles

For Keycloak, add the user in the same realm, set username/email and a password,
and ensure the email claim is emitted. For Zitadel, create a human user with
login name, verified email (when policy requires it), and a password.

On first successful SSO login, Jeen Insights creates a matching local account
record but never copies the IdP password.

For the simplest rollout, leave provider role maps empty. New SSO accounts are
viewers and a local Insights administrator assigns roles in **Settings →
Users**. To make the IdP authoritative, create roles such as
`insights-admin` and `insights-editor`, assign them to users, and configure the
matching `OIDC_<PROVIDER>_ADMIN_ROLES` and
`OIDC_<PROVIDER>_EDITOR_ROLES` comma lists.

## Do 3 — configure, restart and test

Put non-secrets in the UI ConfigMap/Helm values and client secrets in the UI
Secret:

```env
PUBLIC_APP_URL=https://insights.example.invalid
OIDC_PROVIDERS=keycloak,zitadel
OIDC_KEYCLOAK_ISSUER=https://keycloak.example.invalid/realms/replace-with-realm
OIDC_KEYCLOAK_CLIENT_ID=jeen-insights
OIDC_KEYCLOAK_LABEL=Keycloak
OIDC_ZITADEL_ISSUER=https://zitadel.example.invalid
OIDC_ZITADEL_CLIENT_ID=replace-with-client-id
OIDC_ZITADEL_LABEL=Zitadel
OIDC_TLS_VERIFY=true
```

Provide `OIDC_KEYCLOAK_CLIENT_SECRET` and
`OIDC_ZITADEL_CLIENT_SECRET` only when the corresponding client is
confidential. PKCE remains enabled. `OIDC_KEYCLOAK_IDP_HINT` may select an
upstream Keycloak identity provider.

The chart's portable default reads these keys from
`Secret/jeen-insights-secrets` in the release namespace. For least privilege,
set `jeen-insights-ui.existingSecret.name` to a UI-only Secret containing the
client-secret keys; the API and analytics pods do not need them.

Restart the UI after Secret or ConfigMap changes, open `/login`, select the
provider, sign in, and confirm the browser returns to the registered callback
without a redirect mismatch. Then verify the local account and expected role.

## Private certificate authorities

Keep TLS verification enabled. Mount a PEM bundle into the UI container and set
`OIDC_CA_BUNDLE` to that in-container path. `OIDC_TLS_VERIFY=false` disables
issuer/JWKS TLS verification and is acceptable only for temporary local
diagnosis, never production.

With the Helm chart, add the CA as a ConfigMap or Secret outside the chart and
use `jeen-insights-ui.extraVolumes` and `extraVolumeMounts`. Store the path in
UI values, not the PEM contents. Client secrets belong in the UI's
`existingSecret`; end-user passwords belong nowhere in Kubernetes.

## Platform notes

- **AKS:** use Key Vault/External Secrets for confidential-client secrets.
  Workload Identity used by ESO does not replace the OIDC client credential.
- **EKS:** use AWS Secrets Manager/External Secrets; permit UI egress to the
  issuer and JWKS endpoints through NetworkPolicy/security controls.
- **OpenShift:** mount the private CA under the arbitrary UID allowed by
  `restricted-v2`. Fully air-gapped sites can use an internal IdP; public Entra
  or SaaS IdPs require approved egress.
- **Defence AKS:** OIDC client secrets are keys in the broader hand-managed
  `jeen-insights-secrets` Secret; that object also contains the deployment's
  database and application secrets. The separate `jeen-insights-idp-ca`
  ConfigMap contains `ca-bundle.pem`. Use the callback URLs derived from the
  defence `PUBLIC_APP_URL`; do not store user passwords in either object.

See [configuration](configuration.md) for every OIDC variable and precedence.
