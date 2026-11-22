// Module resolve hook for the pi extension smoke test (Node stdlib only).
//
// The extensions import the pi SDK (`@earendil-works/pi-coding-agent`) and
// their own `./lib/run-hook` without a file extension, the way pi's own
// loader accepts them. Neither resolves under plain Node, and installing the
// SDK would break this repo's no-dependencies rule. So:
//   - the SDK specifier resolves to ./pi-stub.mjs (the two type guards the
//     extensions call at runtime; everything else they import is a type),
//   - an extensionless relative import that names a sibling .ts file
//     resolves to that file.
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SDK = "@earendil-works/pi-coding-agent";
const STUB = new URL("./pi-stub.mjs", import.meta.url).href;

export async function resolve(specifier, context, nextResolve) {
  if (specifier === SDK) {
    return { url: STUB, shortCircuit: true };
  }
  if (
    (specifier.startsWith("./") || specifier.startsWith("../")) &&
    !/\.[cm]?[jt]s$/.test(specifier) &&
    context.parentURL
  ) {
    const candidate = new URL(`${specifier}.ts`, context.parentURL);
    if (existsSync(fileURLToPath(candidate))) {
      return nextResolve(candidate.href, context);
    }
  }
  return nextResolve(specifier, context);
}
