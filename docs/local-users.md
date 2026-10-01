# Local users

A local user signs in on `/login` with an email and a password that Jeen Insights
stores. The password is chosen when the account is created. No environment
variable sets it.

Microsoft Entra and generic OIDC (Keycloak, Zitadel) are separate. Those
passwords stay in the identity provider. See
[`deployment/oidc.md`](../deployment/oidc.md).

## First administrator

A fresh install has no Insights admin. Until one exists, open `/setup`. After
an active Insights admin exists, `/setup` redirects to login.

`SETUP_BOOTSTRAP_TOKEN` unlocks that page. It is a one-time setup code, not the
admin password. Set it on the UI process before the first start (in the UI
secret on Kubernetes). If it is blank, the UI generates a token once and logs:

```text
FIRST-RUN SETUP TOKEN — enter this on the /setup page …
```

On `/setup`, enter:

| Field | Rule |
| --- | --- |
| Setup token | The `SETUP_BOOTSTRAP_TOKEN` value, or the token from the UI log |
| Full name | Required |
| Email | Required. Stored in lowercase. This is the login name |
| Password | At least 12 characters |
| Confirm password | Must match |

Submit **Create admin & continue**. The UI signs that person in. The account is
an Insights admin and a Metadata viewer. Change the Metadata role later if this
person should also administer Schema Modeler.

Setup accepts 10 attempts per hour. A wrong token is rejected and does not
create an account.

## Sign in

On `/login`, use the email and the password chosen at creation. A disabled
account cannot sign in.

## Add more users

An Insights admin opens **Settings → Users**, fills in the form, and chooses
**Add user**.

| Field | Rule |
| --- | --- |
| Full name | Required |
| Email | Required, unique, stored in lowercase |
| Password | At least 8 characters |
| Insights | `admin`, `editor`, or `viewer`. The form starts on Editor |
| Metadata | `admin`, `editor`, or `viewer`. The form starts on Viewer |

Insights and Metadata roles are independent. Changing one does not change the
other. Insights roles live in `insights_user_app_roles`. The Metadata role is
`auth_users.role`, which Schema Modeler reads. A missing Insights role is
treated as viewer.

The same create is available while signed in as an Insights admin:

```http
POST /api/users
Content-Type: application/json

{
  "name": "Ada Lovelace",
  "email": "ada@example.invalid",
  "password": "choose-a-password",
  "role": "editor",
  "metadata_role": "viewer"
}
```

`role` is the Insights role. Omit `role` or `metadata_role` and the API uses
`viewer` for that one. The response is `201` with the new account, or `409`
when the email is already registered.

## Change a role

In **Settings → Users**, use the Insights or Metadata dropdown on that row.
Your own dropdowns stay disabled, so you cannot change your own roles there.

```http
PATCH /api/users/<id>/role
Content-Type: application/json

{ "app": "insights", "role": "admin" }
```

`app` is `insights` or `metadata`. `role` is `admin`, `editor`, or `viewer`.

## Remove a user

Use **Remove user** on the row, or:

```http
DELETE /api/users/<id>
```

You cannot delete the account you are signed in as. Removal deletes the login
account. It does not move that person's conversations, pins, or saved analyses
onto a new account. Creating the same email again makes a new account.

## Passwords after creation

There is no screen or API to change or reset a local password. The password
set on `/setup` or **Add user** is the one that signs in.

To give someone a new password, an Insights admin removes that account and
creates it again. That is a new account, as above.

## What does not set a password

These variables only tell tests and stress runs which existing password to
type. They do not create or update an account:

| Variable | Used by |
| --- | --- |
| `LIVE_EMAIL`, `LIVE_PASSWORD` | Live end-to-end tests and the stress runner. Defaults are `admin` / `admin` |
| `SETTINGS_TEST_PASSWORD` | `tests/integration/test_settings_selenium.py`. Default `ChangeMe123!` |
| `JEEN_ADMIN_PASSWORD` | `tests/integration/test_entra_login_local.py`. Default `ChangeMe123!` |
| `STRESS_USER_PASSWORD` | Password for the viewer accounts the stress runner creates |

Older databases may still have a seeded `admin` / `ChangeMe123!` login.
Current installs do not seed that account. Migration `014_harden_default_admin`
removes it when the password was never changed, and `/setup` is how the first
admin is created instead.
