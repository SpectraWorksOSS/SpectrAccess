# Provider credentials

spectrAccess uses your OS keyring for personal logins and accepts a credential
source from programs. It never reads credential environment variables, .netrc,
or provider-client login files. Non-credential settings such as cache paths and
CAMS forecast options still work through environment variables.

## Personal logins

Install spectrAccess, then run:

```console
spectraccess login cdse
spectraccess status
spectraccess logout cdse
```

Login prompts for your account and a hidden password or token. No command flag
takes a secret. Run login again to replace an entry; it asks for confirmation.
Status lists providers, stored account names, and the active backend, never
secret values. Logout removes the entry. In Python or a notebook, the same
operations are `spectraccess.login("cdse")`, `spectraccess.status()`, and
`spectraccess.logout("cdse")`.

Windows Credential Manager, macOS Keychain, and Linux Secret Service store
entries under `spectraccess:<provider>`. The username is your account name
(or `token` for token credentials), and the password field holds the secret.
You can inspect and edit entries in your OS credential manager. Use login to
change accounts so the old entry is removed.

## Programs

Pass `credentials=` as a callable returning a `Credential`. The callable is
invoked for each authenticated operation, so secret-manager rotation takes
effect on the next call in the same process. spectrAccess stores no keyring
entry from a handed-over source. A `Credential` object also works directly.

For example, a program using AWS Secrets Manager can hand over a source:

```python
import json
import boto3
from spectraccess import Credential
from spectraccess.connectors.sentinel2_cdse import Sentinel2CDSEConnector

secrets = boto3.client("secretsmanager")

def cdse_credentials():
    value = secrets.get_secret_value(SecretId="satellite/cdse")
    account = json.loads(value["SecretString"])
    return Credential("password", account["password"], account["username"])

connector = Sentinel2CDSEConnector(credentials=cdse_credentials)
```

Your program controls its secret manager and authentication to it. Existing
explicit username/password or ADS token arguments also work; they are treated
as handed-over credentials. Do not put secret values into scripts or repositories.

## Servers, CI and Colab

A server or Colab notebook may have no usable OS keyring. Hand over a credential
source, or configure a backend using the
[keyring documentation](https://keyring.readthedocs.io/en/latest/#configuring).
The owner can choose an encrypted-file or secret-manager backend. Login refuses
backends with priority below 1 and explains how to configure a backend or hand
over a source. Select a backend appropriate for your storage needs; spectrAccess
uses the backend selected by keyring.

## Accounts

| Provider | What to supply | Sign up and prepare access |
| --- | --- | --- |
| `cdse` | Account username and password | [Copernicus Data Space](https://dataspace.copernicus.eu/); discovery is public |
| `ads` | Personal access token | [Atmosphere Data Store](https://ads.atmosphere.copernicus.eu/how-to-api); accept the dataset terms |
| `earthdata` | Earthdata username/password or a token | [NASA Earthdata Login](https://urs.earthdata.nasa.gov/); authorize LP DAAC access |
| `usgs` | EarthExplorer username and M2M application token in the password prompt | [USGS registration](https://ers.cr.usgs.gov/register/); request M2M access |
| `radcalnet` | Portal username and password | [RadCalNet portal](https://www.radcalnet.org/); create a free account |
| `eumetsat` | Consumer key as account, consumer secret at the password prompt | [EUMETSAT API keys](https://api.eumetsat.int/api-key); create an account and obtain a key/secret pair |

Earthdata discovery uses earthaccess without login. Downloads use spectrAccess's
credential session: bearer authentication for tokens, or basic authentication
for username/password, including the Earthdata Login redirect. Credentials are
removed when redirecting to other hosts.

Missing credentials raise `CredentialMissing` with the provider and login command.
HTTP 401 or 403 failures raise `CredentialRejected`; update the keyring entry or
your source. Other provider errors retain scrubbed diagnostics. No live login
probe is performed by login or status.

## Migration to 0.2

Run `spectraccess login <provider>` once, or pass a credential source in code.
Credential environment variables and provider-client login files are no longer
read. `CredentialConfig`, `RadCalNetCredentials`, and `core.session` are removed;
use `Credential` and `spectraccess.core.credentials`. `CredentialSession` is
also exported from `spectraccess.core`.
