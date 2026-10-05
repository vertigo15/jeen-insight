# Trino mTLS

Insights does not keep its own copy of a Trino connection. Schema Modeler
writes the row in `settings_services`. When that row says
`authenticationMode` is `mtls`, Insights connects with the client certificate
and does not send the password.

Basic and JWT rows are unchanged.

## The two certificates

mTLS uses one certificate in each direction. They are signed by different
authorities, and mixing them up is the usual reason a connection fails.

| File | Who shows it | Who must trust the CA that signed it |
| --- | --- | --- |
| `client.crt` + `client.key` | Insights, to prove who it is | Trino. Trino's truststore is the CA that issued the client certificate. |
| `ca.crt` | Nobody. Insights uses it to check Trino. | This file **is** the CA that signed Trino's server certificate. |

`client.key` is the private key. It lives only in a Kubernetes Secret (or a
local file). It is never written into `connection_config`.

You can skip `ca.crt` only when Trino's server certificate is already trusted
by the API container's system trust store. A private or internal CA needs
`ca.crt`.

`verify: false` turns off the server check. Leave that for a lab. It does not
replace the client certificate.

## What Trino must already accept

On the Trino coordinator:

- HTTPS is on.
- Authentication includes `CERTIFICATE` (password can stay as a second method
  for other clients).
- The truststore is the CA that signed `client.crt`.
- The certificate user mapping reads the CN, commonly
  `CN=([^,]+).*`. That CN is the Trino user.

Insights sends `username` as `X-Trino-User`. Set it to that same CN.

## Files to mount

Put these PEM files in one directory. The default directory is
`/mnt/trino-mtls`, the same path Schema Modeler uses.

| Secret key | Path Insights reads |
| --- | --- |
| `client.crt` | `/mnt/trino-mtls/client.crt` |
| `client.key` | `/mnt/trino-mtls/client.key` |
| `ca.crt` | `/mnt/trino-mtls/ca.crt` |

Only the API pod queries Trino. Mount the Secret on `jeen-insights-api`.
The UI and the analytics sandbox do not need it.

Insights refuses a path outside that directory. To mount somewhere else, set
`TRINO_MTLS_MOUNT_PATH` on the API to that directory and use paths inside it.

## Kubernetes

Create the Secret from the PEM files. Reuse the Secret Schema Modeler already
mounts when it is the same client certificate.

```sh
kubectl --namespace jeen-insights create secret generic trino-mtls \
  --from-file=client.crt=./client.crt \
  --from-file=client.key=./client.key \
  --from-file=ca.crt=./ca.crt
```

Mount it in the environment values, on the API only:

```yaml
jeen-insights-api:
  extraVolumes:
    - name: trino-mtls
      secret:
        secretName: trino-mtls
  extraVolumeMounts:
    - name: trino-mtls
      mountPath: /mnt/trino-mtls
      readOnly: true
```

The chart runs the API as a non-root user. The default secret file mode is
readable by that user. Do not set a mode of `0440` unless the pod `fsGroup`
owns the files; otherwise the API cannot read the key.

On AKS, sync the same Key Vault objects Schema Modeler uses
(`client.crt`, `client.key`, `ca.crt`) into this Secret, then mount it as
above. Insights does not template the PEM contents.

## Connection row

Schema Modeler saves this shape. Insights reads it as-is.

```json
{
  "authenticationMode": "mtls",
  "username": "insights-trino",
  "host": "trino.example.com",
  "port": 443,
  "catalog": "hive",
  "databaseSchema": "analytics",
  "verify": true,
  "clientCertPath": "/mnt/trino-mtls/client.crt",
  "clientKeyPath": "/mnt/trino-mtls/client.key",
  "caCertPath": "/mnt/trino-mtls/ca.crt"
}
```

Rules Insights applies:

- `authenticationMode: mtls` selects the certificate. A password saved on the
  same row is ignored.
- `https` is required. An `http` scheme is rejected before any request.
- `clientCertPath` and `clientKeyPath` are required, absolute, and inside
  `/mnt/trino-mtls`.
- `username` is required. Use the client certificate CN.
- `caCertPath` is the Trino server CA. A legacy CA path stored in `verify`
  still works.
- `verify: false` skips the server-certificate check.
- With no `authenticationMode`, a certificate pair and no password is treated
  as mTLS. A password with no mode stays Basic.

## Check

After the API pod is up:

```sh
kubectl --namespace jeen-insights exec deploy/jeen-insights-api -- \
  ls -l /mnt/trino-mtls
```

You should see `client.crt`, `client.key`, and, when you use a private server
CA, `ca.crt`. Then run a question against that Trino connection in Insights.

The API log line for the runner includes `auth=mtls`.

## Rotation

Replace the Secret. The next query opens the files again, so a refreshed
mount is picked up without a code change. If the pod still has the old files,
restart the API deployment.

## Local API

Point the mount at a directory you control and save the same paths on the
connection:

```sh
export TRINO_MTLS_MOUNT_PATH=/path/to/trino-mtls
```

The directory must contain `client.crt` and `client.key` before the first
query.
