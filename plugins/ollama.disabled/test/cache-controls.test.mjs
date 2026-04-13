import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import {
  injectOllamaCacheControls,
  mergeOllamaOptions,
  patchOllamaProviderDefinition,
  resolveOllamaCacheControls,
  resolveOllamaReliabilityControls,
  wrapStreamFnWithOllamaCacheControls,
} from "../lib/cache-controls.js";

test("resolveOllamaCacheControls reads keepAlive and options", () => {
  const controls = resolveOllamaCacheControls({
    ollama: {
      keepAlive: "15m",
      options: { num_batch: 8 },
    },
  });
  assert.deepEqual(controls, {
    keepAlive: "15m",
    options: { num_batch: 8 },
  });
});

test("resolveOllamaCacheControls accepts flattened keys", () => {
  const controls = resolveOllamaCacheControls({
    keepAlive: "10m",
    options: { num_batch: 4 },
  });
  assert.deepEqual(controls, {
    keepAlive: "10m",
    options: { num_batch: 4 },
  });
});

test("injectOllamaCacheControls injects keep_alive when payload omits it", () => {
  const payload = {
    model: "gemma4:26b",
    options: { num_ctx: 32768 },
  };
  injectOllamaCacheControls(payload, {
    keepAlive: "1h",
    options: undefined,
  });
  assert.equal(payload.keep_alive, "1h");
});

test("injectOllamaCacheControls preserves payload keep_alive when already present", () => {
  const payload = {
    model: "gemma4:26b",
    keep_alive: "2h",
  };
  injectOllamaCacheControls(payload, {
    keepAlive: "30m",
    options: undefined,
  });
  assert.equal(payload.keep_alive, "2h");
});

test("mergeOllamaOptions keeps core-managed keys authoritative", () => {
  const merged = mergeOllamaOptions(
    {
      num_ctx: 32768,
      temperature: 0.1,
      num_predict: 512,
      top_k: 40,
    },
    {
      num_ctx: 4096,
      temperature: 0.9,
      num_predict: 32,
      num_batch: 16,
      top_k: 5,
    },
  );

  assert.equal(merged.num_ctx, 32768);
  assert.equal(merged.temperature, 0.1);
  assert.equal(merged.num_predict, 512);
  assert.equal(merged.num_batch, 16);
  assert.equal(merged.top_k, 5);
});

test("no params keeps payload byte-identical", () => {
  const payload = {
    model: "gemma4:26b",
    options: { num_ctx: 32768, temperature: 0 },
  };
  const before = JSON.stringify(payload);
  injectOllamaCacheControls(payload, null);
  assert.equal(JSON.stringify(payload), before);
});

test("resolveOllamaReliabilityControls reads reliability config", () => {
  const controls = resolveOllamaReliabilityControls({
    ollama: {
      reliability: {
        requestTimeoutMs: 90000,
        maxRetries: 2,
        retryBackoffMs: 150,
      },
    },
  });
  assert.deepEqual(controls, {
    requestTimeoutMs: 90000,
    maxRetries: 2,
    retryBackoffMs: 150,
  });
});

test("resolveOllamaReliabilityControls accepts flattened keys", () => {
  const controls = resolveOllamaReliabilityControls({
    requestTimeoutMs: 3500,
    maxRetries: 0,
    retryBackoffMs: 100,
  });
  assert.deepEqual(controls, {
    requestTimeoutMs: 3500,
    maxRetries: 0,
    retryBackoffMs: 100,
  });
});

test("patchOllamaProviderDefinition patches custom providers using api=ollama", () => {
  const baseProvider = {
    id: "ollama-polly",
    api: "ollama",
    wrapStreamFn: ({ streamFn }) => streamFn,
  };
  const patched = patchOllamaProviderDefinition(baseProvider);
  assert.notEqual(patched, baseProvider);
  assert.equal(typeof patched.wrapStreamFn, "function");
});

function resolveInstalledRuntimeApiPath() {
  const roots = ["/opt/homebrew/lib/node_modules/openclaw", "/usr/local/lib/node_modules/openclaw"];
  for (const rootDir of roots) {
    const candidate = path.join(rootDir, "dist/extensions/ollama/runtime-api.js");
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function createCollectorStream() {
  const events = [];
  let ended = false;
  let resolveEnded;
  const endedPromise = new Promise((resolve) => {
    resolveEnded = resolve;
  });
  return {
    push(event) {
      events.push(event);
    },
    end() {
      ended = true;
      resolveEnded();
    },
    get events() {
      return events;
    },
    get ended() {
      return ended;
    },
    endedPromise,
  };
}

async function* streamFromEvents(events) {
  for (const event of events) yield event;
}

test("polly provider gets fallback reliability signal when params are missing", async () => {
  const base = (_model, _context, options) => {
    assert.ok(options?.signal);
    return streamFromEvents([{ type: "done" }]);
  };

  const wrapped = wrapStreamFnWithOllamaCacheControls(base, undefined, createCollectorStream);
  const out = wrapped(
    { api: "ollama", provider: "ollama-polly", id: "qwen2.5:7b-instruct" },
    {},
    {},
  );
  await out.endedPromise;
  assert.deepEqual(
    out.events.map((event) => event.type),
    ["done"],
  );
});

test(
  "integration: wrapped createConfiguredOllamaStreamFn injects keep_alive and options",
  { skip: !resolveInstalledRuntimeApiPath() },
  async () => {
    const runtimeApiPath = resolveInstalledRuntimeApiPath();
    const runtimeApiModule = await import(pathToFileURL(runtimeApiPath).href);
    const { createConfiguredOllamaStreamFn } = runtimeApiModule;
    assert.equal(typeof createConfiguredOllamaStreamFn, "function");

    const model = {
      id: "gemma4:26b",
      name: "gemma4:26b",
      api: "ollama",
      provider: "ollama",
      contextWindow: 32768,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      input: ["text"],
    };
    const context = {
      systemPrompt: "system",
      messages: [{ role: "user", content: [{ type: "text", text: "ping" }] }],
      tools: [],
    };

    let captured = null;
    const originalFetch = globalThis.fetch;
    const encoder = new TextEncoder();
    globalThis.fetch = async (url, init) => {
      captured = { url, init };
      const ndjson =
        '{"model":"gemma4:26b","created_at":"2026-04-10T00:00:00Z","message":{"role":"assistant","content":"ok"},"done":true,"prompt_eval_count":1,"eval_count":1}\n';
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(ndjson));
          controller.close();
        },
      });
      return new Response(body, { status: 200 });
    };

    try {
      const baseStreamFn = createConfiguredOllamaStreamFn({
        model,
        providerBaseUrl: "http://127.0.0.1:11434",
      });
      const wrappedStreamFn = wrapStreamFnWithOllamaCacheControls(baseStreamFn, {
        ollama: {
          keepAlive: "45m",
          options: {
            num_batch: 16,
            num_ctx: 1024,
          },
        },
      });

      const stream = wrappedStreamFn(model, context, { temperature: 0.2, maxTokens: 42 });
      for await (const event of stream) {
        if (event.type === "done" || event.type === "error") break;
      }
    } finally {
      globalThis.fetch = originalFetch;
    }

    assert.ok(captured);
    assert.equal(captured.url, "http://127.0.0.1:11434/api/chat");

    const payload = JSON.parse(captured.init.body);
    assert.equal(payload.keep_alive, "45m");
    assert.equal(payload.options.num_batch, 16);
    assert.equal(payload.options.num_ctx, 32768);
    assert.equal(payload.options.temperature, 0.2);
    assert.equal(payload.options.num_predict, 42);
  },
);
