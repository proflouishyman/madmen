# Local Shadow Ollama Plugin

This plugin intentionally uses id `ollama` so OpenClaw can override the bundled
Ollama plugin when the path is listed in:

- `plugins.load.paths`

## What It Changes

- keeps bundled Ollama behavior
- patches provider registration so stream payloads can inject:
  - `keep_alive` from `params.ollama.keepAlive`
  - merged `options` from `params.ollama.options`

Core-managed request keys (`num_ctx`, `temperature`, `num_predict`) remain
authoritative when already present in payload options.

## Inputs

From merged extra params:

- `ollama.keepAlive` (`string | number`)
- `ollama.options` (`object`)

## Runtime Assumption

The plugin statically imports bundled Ollama entry from:

`/opt/homebrew/lib/node_modules/openclaw/dist/extensions/ollama/index.js`

If OpenClaw is installed in another location, update `index.js`.

## Tests

```bash
node --test /Users/louishyman/openclaw/plugins/ollama/test/cache-controls.test.mjs
```
