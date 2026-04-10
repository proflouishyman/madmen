# Ollama Static-Prompt Reuse Override (OpenClaw Shadow Plugin)

Date: `2026-04-10`

## Goal

Improve practical reuse of static prompt prefixes (for example large `SOUL.md` blocks)
when using Ollama through OpenClaw, without forking OpenClaw.

## Design

- Keep upstream OpenClaw untouched.
- Add a local plugin with id `ollama` (same id as bundled plugin).
- Load the local plugin via `plugins.load.paths` so it shadows the bundled plugin.
- Reuse bundled Ollama plugin behavior, but patch provider stream wrapping to inject:
  - top-level `keep_alive` (when not already present in payload)
  - additional Ollama `options` from params

## New Params Contract

For Ollama models, use:

- `agents.defaults.models["ollama/<model>"].params.ollama.keepAlive`
  - Type: `string | number`
- `agents.defaults.models["ollama/<model>"].params.ollama.options`
  - Type: `object`

Example:

```json
{
  "agents": {
    "defaults": {
      "models": {
        "ollama/gemma4:26b": {
          "params": {
            "ollama": {
              "keepAlive": "45m",
              "options": {
                "num_batch": 16,
                "main_gpu": 0
              }
            }
          }
        }
      }
    }
  }
}
```

## Precedence Rules

1. `payload.keep_alive` stays unchanged if already present.
2. If `payload.keep_alive` is absent, `params.ollama.keepAlive` is injected.
3. `params.ollama.options` are merged into `payload.options`.
4. Core-managed keys remain authoritative when already set by OpenClaw request building:
   - `num_ctx`
   - `temperature`
   - `num_predict`

## Plugin Path

Local plugin root:

`/Users/louishyman/openclaw/plugins/ollama`

## Rollout

1. Add plugin path in runtime config:
   - `plugins.load.paths: ["/Users/louishyman/openclaw/plugins/ollama"]`
2. Add `params.ollama.*` for pilot model(s).
3. Restart OpenClaw gateway.
4. Validate plugin source:
   - `openclaw plugins list --json`
   - confirm plugin `id=ollama` source points to local path.
5. Benchmark before/after:
   - `python3 /Users/louishyman/openclaw/scripts/benchmark_ollama_soul_cache.py ...`

## Rollback

Immediate rollback is config-only:

- remove `/Users/louishyman/openclaw/plugins/ollama` from `plugins.load.paths`
- restart gateway

OpenClaw returns to bundled Ollama plugin behavior.

## Tests

Run:

```bash
node --test /Users/louishyman/openclaw/plugins/ollama/test/cache-controls.test.mjs
```

Coverage includes:

- `keep_alive` injection
- options merge behavior
- no-params no-op behavior
- integration path using bundled `createConfiguredOllamaStreamFn` with mocked fetch
