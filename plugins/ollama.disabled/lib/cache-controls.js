const CORE_MANAGED_OPTION_KEYS = new Set(["num_ctx", "temperature", "num_predict"]);
const RETRYABLE_STREAM_ERROR_RE =
  /(stream ended without a final response|aborted|timeout|timed out|connection reset|eof|500\b|502\b|503\b|504\b)/i;

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

function toFinitePositiveInt(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  const rounded = Math.floor(value);
  return rounded > 0 ? rounded : undefined;
}

function toFiniteNonNegativeInt(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  const rounded = Math.floor(value);
  return rounded >= 0 ? rounded : undefined;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function extractErrorMessageFromEvent(event) {
  if (!isRecord(event)) return "";
  const error = event.error;
  if (isRecord(error)) {
    if (typeof error.errorMessage === "string") return error.errorMessage;
    const content = error.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      const text = content
        .filter((part) => isRecord(part) && part.type === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join(" ");
      if (text) return text;
    }
  }
  return "";
}

function isRetryableStreamEventError(event) {
  if (!isRecord(event) || event.type !== "error") return false;
  const message = extractErrorMessageFromEvent(event);
  return RETRYABLE_STREAM_ERROR_RE.test(message);
}

function hasMaterialOutputEvent(event) {
  if (!isRecord(event)) return false;
  return (
    event.type === "text_delta" ||
    event.type === "text_start" ||
    event.type === "thinking_delta" ||
    event.type === "thinking_start" ||
    event.type === "tool_call" ||
    event.type === "done"
  );
}

function mergeAbortSignals(signals) {
  const active = asArray(signals).filter((signal) => signal && typeof signal.aborted === "boolean");
  if (active.length === 0) return { signal: undefined, dispose: () => {} };
  if (active.length === 1) return { signal: active[0], dispose: () => {} };

  const controller = new AbortController();
  const onAbort = (event) => {
    const source = event?.target;
    if (!controller.signal.aborted) controller.abort(source?.reason ?? "aborted");
  };
  for (const signal of active) {
    if (signal.aborted) {
      controller.abort(signal.reason ?? "aborted");
      break;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    dispose: () => {
      for (const signal of active) signal.removeEventListener("abort", onAbort);
    },
  };
}

function buildTimeoutSignal(timeoutMs) {
  const ms = toFinitePositiveInt(timeoutMs);
  if (!ms) return { signal: undefined, cancel: () => {} };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(`timeout:${ms}`), ms);
  return {
    signal: controller.signal,
    cancel: () => clearTimeout(timer),
  };
}

function readIntEnv(name) {
  const raw = process.env[name];
  if (typeof raw !== "string" || !raw.trim()) return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : undefined;
}

function resolvePollyDefaultReliabilityControls(model) {
  const provider = typeof model?.provider === "string" ? model.provider : "";
  if (!provider || !provider.startsWith("ollama-polly")) return null;

  const localTimeoutSeconds = readIntEnv("POLLY_LOCAL_TIMEOUT_SECONDS");
  const timeoutFromSeconds =
    typeof localTimeoutSeconds === "number" && localTimeoutSeconds > 0
      ? localTimeoutSeconds * 1000
      : undefined;
  const requestTimeoutMs = toFinitePositiveInt(
    readIntEnv("OPENCLAW_POLLY_REQUEST_TIMEOUT_MS") ?? timeoutFromSeconds ?? 3000,
  );
  const maxRetries = clamp(
    toFiniteNonNegativeInt(readIntEnv("OPENCLAW_POLLY_MAX_RETRIES") ?? 0) ?? 0,
    0,
    3,
  );
  const retryBackoffMs =
    toFiniteNonNegativeInt(readIntEnv("OPENCLAW_POLLY_RETRY_BACKOFF_MS") ?? 100) ?? 100;

  return {
    requestTimeoutMs,
    maxRetries,
    retryBackoffMs,
  };
}

export function resolveOllamaReliabilityControls(extraParams) {
  if (!isRecord(extraParams)) return null;
  const rawOllama = isRecord(extraParams.ollama) ? extraParams.ollama : extraParams;
  const rawReliability = isRecord(rawOllama.reliability) ? rawOllama.reliability : rawOllama;
  const requestTimeoutMs = toFinitePositiveInt(
    rawReliability.requestTimeoutMs ?? rawReliability.request_timeout_ms,
  );
  const maxRetries = toFiniteNonNegativeInt(rawReliability.maxRetries ?? rawReliability.max_retries);
  const retryBackoffMs = toFiniteNonNegativeInt(
    rawReliability.retryBackoffMs ?? rawReliability.retry_backoff_ms,
  );

  if (requestTimeoutMs === undefined && maxRetries === undefined && retryBackoffMs === undefined) return null;

  return {
    requestTimeoutMs,
    maxRetries: clamp(maxRetries ?? 1, 0, 3),
    retryBackoffMs: retryBackoffMs ?? 250,
  };
}

function resolveRuntimeReliabilityControls(extraParams, model) {
  const configured = resolveOllamaReliabilityControls(extraParams);
  const pollyDefaults = resolvePollyDefaultReliabilityControls(model);
  if (!configured && !pollyDefaults) return null;
  if (!configured) return pollyDefaults;
  if (!pollyDefaults) return configured;
  return {
    requestTimeoutMs: configured.requestTimeoutMs ?? pollyDefaults.requestTimeoutMs,
    maxRetries: configured.maxRetries ?? pollyDefaults.maxRetries,
    retryBackoffMs: configured.retryBackoffMs ?? pollyDefaults.retryBackoffMs,
  };
}

export function resolveOllamaCacheControls(extraParams) {
  if (!isRecord(extraParams)) return null;
  const raw = isRecord(extraParams.ollama) ? extraParams.ollama : extraParams;

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

// Purpose: Apply payload-level cache controls and optional reliability protections around the stream call.
export function wrapStreamFnWithOllamaCacheControls(streamFn, extraParams, createEventStream) {
  if (typeof streamFn !== "function") return streamFn;
  const cacheControls = resolveOllamaCacheControls(extraParams);

  return (model, context, options) => {
    const originalOnPayload =
      options && typeof options.onPayload === "function" ? options.onPayload : undefined;

    const wrappedOptions = {
      ...(options ?? {}),
      onPayload: (payload, payloadModel) => {
        // onPayload executes before fetch serialization in the bundled transport.
        injectOllamaCacheControls(payload, cacheControls);
        if (originalOnPayload) originalOnPayload(payload, payloadModel);
      },
    };

    const reliability = resolveRuntimeReliabilityControls(extraParams, model);
    if (!reliability || typeof createEventStream !== "function") {
      return streamFn(model, context, wrappedOptions);
    }

    const output = createEventStream();
    const run = async () => {
      const maxAttempts = (reliability.maxRetries ?? 0) + 1;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        const timeout = buildTimeoutSignal(reliability.requestTimeoutMs);
        const mergedSignal = mergeAbortSignals([wrappedOptions.signal, timeout.signal]);
        const attemptOptions = {
          ...wrappedOptions,
          signal: mergedSignal.signal,
        };

        const bufferedEvents = [];
        let sawMaterialOutput = false;
        let shouldRetry = false;

        try {
          const stream = streamFn(model, context, attemptOptions);
          for await (const event of stream) {
            bufferedEvents.push(event);
            if (hasMaterialOutputEvent(event)) sawMaterialOutput = true;

            // Retry only when the stream fails before any meaningful output.
            if (
              !sawMaterialOutput &&
              isRetryableStreamEventError(event) &&
              attempt < maxAttempts &&
              !(wrappedOptions.signal && wrappedOptions.signal.aborted)
            ) {
              shouldRetry = true;
              break;
            }
          }
        } catch {
          if (attempt < maxAttempts && !(wrappedOptions.signal && wrappedOptions.signal.aborted)) {
            shouldRetry = true;
          }
        } finally {
          mergedSignal.dispose();
          timeout.cancel();
        }

        if (shouldRetry) {
          if (reliability.retryBackoffMs > 0) await sleep(reliability.retryBackoffMs);
          continue;
        }

        for (const event of bufferedEvents) output.push(event);
        output.end();
        return;
      }

      // Invariant: loop exits only via return; this is a fallback guard.
      output.end();
    };

    queueMicrotask(() => void run());
    return output;
  };
}

export function patchOllamaProviderDefinition(provider) {
  if (!provider) return provider;
  const providerId = typeof provider.id === "string" ? provider.id : "";
  const providerApi = typeof provider.api === "string" ? provider.api : "";
  if (providerId !== "ollama" && providerApi !== "ollama") return provider;
  const originalWrapStreamFn = provider.wrapStreamFn;

  return {
    ...provider,
    wrapStreamFn: (ctx) => {
      const maybeWrapped =
        typeof originalWrapStreamFn === "function" ? originalWrapStreamFn(ctx) : undefined;
      const baseStreamFn = maybeWrapped ?? ctx.streamFn;
      return wrapStreamFnWithOllamaCacheControls(baseStreamFn, ctx.extraParams, ctx.createEventStream);
    },
  };
}

export const __testing = {
  CORE_MANAGED_OPTION_KEYS,
};
