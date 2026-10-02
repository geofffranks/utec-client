# Migrating from utec-py / `utec_py`

This project is an independently maintained fork of
[LF2b2w/utec-py](https://github.com/LF2b2w/utec-py) (by Luke Wallis). It is
**not** vendor-official, and it ships under a **new distribution name and
import namespace**. There is no compatibility alias: upgrading is a
deliberate, mechanical rename.

## What changed

| Before (upstream)             | After (this fork)         |
| ----------------------------- | ------------------------- |
| `pip install utec_py_LF2b2w`  | `pip install utec-client` |
| `import utec_py`              | `import utec_client`      |
| `utec_py.api.UHomeApi`        | `utec_client.api.UHomeApi`|
| `utec_py.auth.AbstractAuth`   | `utec_client.auth.AbstractAuth` |
| `utec_py.devices.light.Light` | `utec_client.devices.light.Light` |
| `utec_py.devices.lock.Lock`   | `utec_client.devices.lock.Lock` |
| `utec_py.devices.sensor.Sensor` | `utec_client.devices.sensor.Sensor` |
| `utec_py.devices.switch.Switch` | `utec_client.devices.switch.Switch` |
| `utec_py.exceptions`          | `utec_client.exceptions`  |

The public API surface is otherwise unchanged: the same classes and methods
exist under the new prefix, including `utec_client.api.UHomeApi`,
`utec_client.auth.AbstractAuth`, and the device modules. There are no new
top-level re-exports.

## How to migrate

1. Replace the dependency: `pip uninstall utec_py_LF2b2w && pip install utec-client`.
2. Rewrite imports (mechanical): `utec_py` → `utec_client`.
   Example: `sed -i 's/\butec_py\b/utec_client/g'` across your project — then
   review the diff.
3. Run your tests. Behavior is preserved, but the import break means this
   fork will never be a drop-in upgrade.

## Versioning

Releases are tagged `vX.Y.Z` and the published package version equals
`X.Y.Z`. The first fork release version is chosen by the maintainer at
release time; it is not derived from the upstream `0.2.4`.

## License

MIT, unchanged. Upstream authorship is preserved in the package metadata and the
license file ships inside the wheel and sdist.
