#!/usr/bin/env node
/**
 * PLY → .spz converter using Spark's transcodeSpz WASM function.
 * Usage: node convert.mjs <input.ply> [--max-sh 0|1|2|3]
 * Output: <input>.spz in the same directory as the input file.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { transcodeSpz } from "./node_modules/@sparkjsdev/spark/dist/spark.module.js";

const args = process.argv.slice(2);
if (args.length === 0 || args.includes("--help")) {
  console.log("Usage: node convert.mjs <input.ply> [--max-sh N]");
  process.exit(0);
}

let inputFile = null;
let maxSh = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--max-sh") {
    maxSh = Number(args[++i]);
  } else {
    inputFile = args[i];
  }
}

if (!inputFile) {
  console.error("Error: no input file specified");
  process.exit(1);
}

console.log(`Loading ${inputFile}...`);
const data = await fs.readFile(inputFile);
const fileBytes = new Uint8Array(data);

console.log(`Compressing (${(fileBytes.length / 1024 / 1024).toFixed(0)} MB input)...`);
const result = await transcodeSpz({
  inputs: [
    {
      fileBytes,
      pathOrUrl: inputFile,
      transform: { translate: [0, 0, 0], quaternion: [0, 0, 0, 1], scale: 1 },
    },
  ],
  maxSh,
  fractionalBits: 12,
  opacityThreshold: null,
});

const outPath = path.join(
  path.dirname(inputFile),
  path.basename(inputFile).replace(/\.[^.]+$/, ".spz")
);
await fs.writeFile(outPath, result.fileBytes);
const outMB = (result.fileBytes.length / 1024 / 1024).toFixed(0);
console.log(`Done → ${outPath} (${outMB} MB)`);
