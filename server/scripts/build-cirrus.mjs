import { createHash } from "node:crypto";
import { copyFile, mkdir, readdir, readFile, unlink, writeFile } from "node:fs/promises";

const source = new URL("../static/", import.meta.url);
const output = new URL("../.cirrus/", import.meta.url);
const publicFiles = new URL("public/", output);
const assetDirectory = new URL("assets/", publicFiles);

async function writeIfChanged(file, content) {
  const previous = await readFile(file, "utf8").catch((error) => {
    if (error.code !== "ENOENT") throw error;
  });
  if (previous !== content) await writeFile(file, content);
}

const styles = [
  "1-tokens.css",
  "2-layout.css",
  "3-components.css",
  "4-motion.css",
];
const stylesheet = (
  await Promise.all(
    styles.map((name) => readFile(new URL(`css/${name}`, source), "utf8")),
  )
).join("\n");

await mkdir(assetDirectory, { recursive: true });

const assets = {};
const names = new Set();
for (const [extension, content] of [
  ["css", stylesheet],
  ["js", await readFile(new URL("cirrus.js", source), "utf8")],
]) {
  const hash = createHash("sha256").update(content).digest("hex").slice(0, 10);
  const name = `cirrus.${hash}.${extension}`;
  await writeIfChanged(new URL(name, assetDirectory), content);
  names.add(name);
  assets[`cirrus.${extension}`] = `assets/${name}`;
}

for (const name of ["favicon.svg", "favicon-32.png", "apple-touch-icon.png"]) {
  await copyFile(new URL(name, source), new URL(name, publicFiles));
}

await writeIfChanged(
  new URL("sw.js", publicFiles),
  await readFile(new URL("sw.js", source), "utf8"),
);
await writeIfChanged(
  new URL("_headers", publicFiles),
  "/assets/cirrus.*\n  Cache-Control: public, max-age=31536000, immutable\n",
);
await writeIfChanged(
  new URL("assets.json", output),
  JSON.stringify(assets) + "\n",
);

for (const name of await readdir(assetDirectory)) {
  if (/^cirrus\.[0-9a-f]{10}\.(css|js)$/.test(name) && !names.has(name)) {
    await unlink(new URL(name, assetDirectory));
  }
}
