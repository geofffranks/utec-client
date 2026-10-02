# utec-client

> **Fork notice:** This is an independently maintained fork of [LF2b2w/utec-py](https://github.com/LF2b2w/utec-py), the original repository by [@LF2b2w](https://github.com/LF2b2w). The upstream project currently appears to be unmaintained, so this fork exists to keep the library working alongside the companion [Uhome-HA](https://github.com/LF2b2w/Uhome-HA) integration. It is not vendor-official. Any changes made here will be offered back to the upstream repository should it become responsive again. See [MIGRATION.md](MIGRATION.md) if you are coming from `utec_py` — the import name changed.

Python API client for U-Tec/U-Home devices. Primarily for Home Assistant, but usable anywhere.

## Install

```sh
pip install utec-client
```

Imports use the `utec_client` namespace:

```python
from utec_client.api import UHomeApi
from utec_client.auth import AbstractAuth
from utec_client.devices.lock import Lock
from utec_client.devices.light import Light
from utec_client.devices.sensor import Sensor
from utec_client.devices.switch import Switch
from utec_client.exceptions import ApiError
```

## API overview

### `UHomeApi` (`utec_client.api`)

U-Tec's API is a single endpoint (`https://api.utec.com/action`) that uses
variable header information in the JSON payload to reach different API
interfaces. `UHomeApi` handles packaging the variables responsible for the
different interfaces/actions correctly within the request payload.

### `AbstractAuth` (`utec_client.auth`)

Abstract base class handling most of the OAuth2 process: generating the auth
request URL, token exchange, token management, expiry validation, and token
refresh. Subclass it and implement the abstract token-retrieval method to
control how the initial auth request is handled. `UHomeApi` uses an
`AbstractAuth` implementation to perform authenticated requests.

### Device modules (`utec_client.devices`)

Device info is organized with minimal change from the raw API response
(`id`, `name`, `category`, `handleType`, `deviceInfo`, `capabilities`,
`customData`, `attributes`, `state`). Device type classes (`Light`, `Lock`,
`Sensor`, `Switch`) pre-format and pre-fill API requests for simple
integration downstream. Locks are the most fully supported device type;
switches and lights have limited functionality.

## Development

Requires Python 3.11–3.14.

```sh
pip install -e .[dev]
ruff check . && ruff format --check .
pytest                # includes the 90% branch-coverage floor
```

See [docs/RELEASES.md](docs/RELEASES.md) for the release process and
[MIGRATION.md](MIGRATION.md) for the `utec_py` → `utec_client` rename guide.

## License

MIT. Upstream authorship (Luke Wallis) is preserved; see [LICENSE](LICENSE).
