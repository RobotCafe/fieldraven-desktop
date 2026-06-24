/**
 * Convert a Gaussian Splat PLY file to .ksplat format.
 * Uses @mkkellogg/gaussian-splats-3d's PlyLoader which preserves full SH coefficients.
 *
 * Usage: node convert.mjs <input.ply> <output.ksplat> [compressionLevel=1] [shDegree=3]
 */
import fs from 'fs'
import { createRequire } from 'module'

const [,, inputPly, outputKsplat, compressionLevelStr = '1', shDegreeStr = '3'] = process.argv

if (!inputPly || !outputKsplat) {
  console.error('Usage: node convert.mjs <input.ply> <output.ksplat> [compressionLevel=1] [shDegree=3]')
  process.exit(1)
}

const compressionLevel = parseInt(compressionLevelStr)
const shDegree = parseInt(shDegreeStr)

// The library is browser-oriented — stub out globals it may touch at import time
if (typeof globalThis.document === 'undefined') {
  globalThis.document = { createElement: () => ({ getContext: () => null, style: {} }) }
}
if (typeof globalThis.window === 'undefined') {
  globalThis.window = globalThis
}

const require = createRequire(import.meta.url)

// Import the CJS build — the module build imports THREE as ESM peer which may not resolve
// cleanly in the scripts node_modules. The UMD/CJS build bundles its own THREE references.
const gs3d = require('@mkkellogg/gaussian-splats-3d')

const { PlyLoader } = gs3d

if (!PlyLoader) {
  console.error('ERROR: PlyLoader not found in @mkkellogg/gaussian-splats-3d')
  process.exit(1)
}

const startTime = Date.now()
console.log(`[ksplat] Reading ${inputPly}...`)
const plyData = fs.readFileSync(inputPly)
console.log(`[ksplat] PLY size: ${(plyData.byteLength / 1024 / 1024).toFixed(0)} MB`)

console.log(`[ksplat] Parsing PLY (SH degree ${shDegree}, compression level ${compressionLevel})...`)

// PlyLoader.loadFromFileData(plyFileData, minimumAlpha, compressionLevel, optimizeSplatData, outSphericalHarmonicsDegree)
// Returns a Promise<SplatBuffer>
const splatBuffer = await PlyLoader.loadFromFileData(
  plyData.buffer,
  5,              // minimumAlpha — remove very transparent splats
  compressionLevel,
  true,           // optimizeSplatData — use SplatBufferGenerator (bucket quantization)
  shDegree,       // outSphericalHarmonicsDegree — preserve full view-dependent color
)

if (!splatBuffer || !splatBuffer.bufferData) {
  console.error('ERROR: PLY conversion produced no output buffer')
  process.exit(1)
}

const outBuffer = Buffer.from(splatBuffer.bufferData)
fs.writeFileSync(outputKsplat, outBuffer)

const elapsed = ((Date.now() - startTime) / 1000).toFixed(1)
console.log(`[ksplat] Done in ${elapsed}s → ${outputKsplat} (${(outBuffer.byteLength / 1024 / 1024).toFixed(0)} MB)`)
