const CORE_MANAGED_OPTION_KEYS = new Set(["num_ctx", "temperature", "num_predict"]);

// Narrow guard used across payload/params handling to avoid array/null traps.
function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

// Accept Ollama keep_alive values in the same forms Ollama accepts.
function coerceKeepAlive(value) {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : undefined;
  }
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return undefined;
}

export function resolveOllamaCacheControls(extraParams) {
  if (!isRecord(extraParams)) return null;
  const raw = extraParams.ollama;
  if (!isRecord(raw)) return null;

  const keepAlive = coerceKeepAlive(raw.keepAlive ?? raw.keep_alive);
  const options = isRecord(raw.options) ? { ...raw.options } : undefined;
  if (keepAlive === undefined && options === undefined) return null;
  return { keepAlive, options };
}

export function mergeOllamaOptions(existingOptions, injectedOptions) {
  const base = isRecord(existingOptions) ? { ...existingOptions } : {};
  if (!isRecord(injectedOptions)) return base;

  const merged = { ...base, ...injectedOptions };
  // Core request builder owns these keys; plugin-level params must not override them.
  for (const key of CORE_MANAGED_OPTION_KEYS) {
    if (Object.hasOwn(base, key)) merged[key] = base[key];
  }
  return merged;
}

// Mutates the outgoing payload in-place to avoid changing upstream stream internals.
export function injectOllamaCacheControls(payload, controls) {
  if (!isRecord(payload) || !controls) return payload;

  if (!Object.hasOwn(payload, "keep_alive") && controls.keepAlive !== undefined) {
    payload.keep_alive = controls.keepAlive;
  }

  if (controls.options) {
    payload.options = mergeOllamaOptions(payload.options, controls.options);
  }
  return payload;
}

export function wrapStreamFnWithOllamaCacheControls(streamFn, extraParams) {
  if (typeof streamFn !== "function") return streamFn;
  const controls = resolveOllamaCacheControls(extraParams);
  if (!controls) return streamFn;

  return (model, context, options) => {
    const originalOnPayload =
      options && typeof options.onPayload === "function" ? options.onPayload : undefined;

    const wrappedOptions = {
      ...(options ?? {}),
      onPayload: (payload, payloadModel) => {
        // onPayload executes before fetch serialization in the bundled transport.
        injectOllamaCacheControls(payload, controls);
        if (originalOnPayload) originalOnPayload(payload, payloadModel);
      },
    };

    return streamFn(model, context, wrappedOptions);
  };
}

export function patchOllamaProviderDefinition(provider) {
  if (!provider || provider.id !== "ollama") return provider;
  const originalWrapStreamFn = provider.wrapStreamFn;

  return {
    ...provider,
    wrapStreamFn: (ctx) => {
      const maybeWrapped =
        typeof originalWrapStreamFn === "function" ? originalWrapStreamFn(ctx) : undefined;
      const baseStreamFn = maybeWrapped ?? ctx.streamFn;
      return wrapStreamFnWithOllamaCacheControls(baseStreamFn, ctx.extraParams);
    },
  };
}

export const __testing = {
  CORE_MANAGED_OPTION_KEYS,
  isRecord,
  coerceKeepAlive,
};
