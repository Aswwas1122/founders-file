#!/usr/bin/env bash
# Regenerates GraphViz.compiled.js from GraphViz.jsx.
#
# Run this after editing GraphViz.jsx. The app loads GraphViz.compiled.js
# directly (see templates/index.html) — GraphViz.jsx itself is source only
# and is never served to the browser or compiled at runtime. This keeps
# Visualization working with a strict CSP (script-src 'self', no
# 'unsafe-inline'): Babel-standalone's in-browser compilation works by
# injecting the compiled output as a new inline <script>, which such a CSP
# always blocks, so JSX must be compiled ahead of time instead.
#
# Requires Node + npm. Run from anywhere; paths below are relative to this
# script's own directory.
set -euo pipefail
cd "$(dirname "$0")"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

( cd "$TMP_DIR" && npm install --no-save @babel/core @babel/preset-react >/dev/null )

NODE_PATH="$TMP_DIR/node_modules" node -e '
const babel = require("@babel/core");
const fs = require("fs");
const src = fs.readFileSync("GraphViz.jsx", "utf8");
babel.transform(src, {
  presets: [["@babel/preset-react", { runtime: "classic", pragma: "React.createElement" }]],
  filename: "GraphViz.jsx",
}, (err, result) => {
  if (err) { console.error(err); process.exit(1); }
  fs.writeFileSync("GraphViz.compiled.js", result.code);
  console.log("Wrote GraphViz.compiled.js (" + result.code.length + " bytes)");
});
'
