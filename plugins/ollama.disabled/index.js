import fs from "node:fs";

import { patchOllamaProviderDefinition } from "./lib/cache-controls.js";
import bundledEntry from "/opt/homebrew/lib/node_modules/openclaw/dist/extensions/ollama/index.js";

const BUNDLED_ENTRY_PATH = "/opt/homebrew/lib/node_modules/openclaw/dist/extensions/ollama/index.js";

if (!bundledEntry || typeof bundledEntry.register !== "function") {
  throw new Error("Bundled Ollama entry is invalid: missing register(api) hook.");
}
if (!fs.existsSync(BUNDLED_ENTRY_PATH)) {
  throw new Error(`Bundled Ollama entry path is missing: ${BUNDLED_ENTRY_PATH}`);
}

const shadowEntry = {
  ...bundledEntry,
  id: "ollama",
  name: bundledEntry.name ?? "Ollama Provider",
  description:
    "Local shadow Ollama provider that preserves bundled behavior and injects request cache controls.",
  register(api) {
    const apiProxy = new Proxy(api, {
      get(target, property, receiver) {
        if (property === "registerProvider") {
          // Patch only the ollama provider definition; all other providers pass through untouched.
          return (provider) => target.registerProvider(patchOllamaProviderDefinition(provider));
        }
        if (property === "registerMemoryEmbeddingProvider") {
          return (provider) => {
            // Bundled initialization may already own this id in the same runtime process.
            if (provider?.id === "ollama") return;
            target.registerMemoryEmbeddingProvider(provider);
          };
        }
        return Reflect.get(target, property, receiver);
      },
    });
    return bundledEntry.register(apiProxy);
  },
};

export default shadowEntry;
