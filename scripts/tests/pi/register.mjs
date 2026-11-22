// `node --import ./register.mjs` entry point: installs loader.mjs.
import { register } from "node:module";

register("./loader.mjs", import.meta.url);
