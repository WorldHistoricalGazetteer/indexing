// Drive the PLATO tools engine from Node, exactly as its own tests do, so a converter's output is
// checked and converted by the same code as https://pelagios.org/plato-tools/.
//
//   PLATO_TOOLS=~/PycharmProjects/plato-tools node plato_run.mjs <in> check
//   PLATO_TOOLS=... node plato_run.mjs <in> convert <ntriples|plato-jsonl|plato-json|lpf|tables> <out>
//
// PLATO_TOOLS is a checkout of github.com/pelagios/plato-tools with `npm install` run in it.
import { readFileSync, writeFileSync, createWriteStream, openAsBlob } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const T = process.env.PLATO_TOOLS;
if (!T) { console.error('Set PLATO_TOOLS to a plato-tools checkout.'); process.exit(2); }
const req = createRequire(`${T}/package.json`);
const mod = async (name) => import(pathToFileURL(req.resolve(name)).href);
const src = async (path) => import(pathToFileURL(`${T}/${path}`).href);

const sqlite3InitModule = (await mod('@sqlite.org/sqlite-wasm')).default;
const XLSX = await mod('xlsx');
const { loadResources } = await src('src/engine/resources.js');
const { prepare, run } = await src('src/engine/pipeline.js');
const { detect } = await src('src/engine/input.js');
const { openSqlite } = await src('src/lib/store.js');

const [, , inPath, action, target, outPath] = process.argv;
const res = prepare(await loadResources(async (f) => readFileSync(`${T}/public/plato/${f}`, 'utf8')));
const outs = {};
const env = {
  resources: res, csvMeta: res.csvMeta, xlsx: XLSX,
  openDb: () => openSqlite(sqlite3InitModule, { memory: true }),
  output: async (name) => {
    const parts = [];
    return { write: (s) => parts.push(s), writeBytes: (b) => parts.push(b), close: async () => { outs[name] = parts; return { name, size: 0 }; } };
  },
};
// A Blob made from a Buffer streams as ONE chunk, so the text decoder builds a single string and
// throws ERR_ENCODING_INVALID_ENCODED_DATA once the input passes V8's maximum string length, at
// about 512 MB. openAsBlob is backed by the file and streams it, so size stops mattering.
const blob = await openAsBlob(inPath);
const input = await detect([new File([blob], inPath.split('/').pop())]);
const r = await run({ input, action, target, options: {} }, env);
const by = {};
for (const i of r.report.items) by[i.severity] = (by[i.severity] || 0) + 1;
console.log(action, target || '', 'from', input.format, JSON.stringify(r.report.counts), JSON.stringify(by));
for (const i of r.report.items.slice(0, 12)) console.log('  ', i.severity, i.kind || '', (i.message || '').slice(0, 300), i.count ? `x${i.count}` : '');
if (outPath) {
  const p = outs[Object.keys(outs)[0]];
  // Write chunk by chunk. Joining them into one string throws RangeError: Invalid string length
  // once the output passes V8's maximum string size, which a whole-corpus conversion does.
  const ws = createWriteStream(outPath);
  for (const chunk of p) ws.write(typeof chunk === 'string' ? chunk : Buffer.from(chunk));
  await new Promise((res, rej) => { ws.end(); ws.on('finish', res); ws.on('error', rej); });
  console.log('  wrote', outPath);
}
process.exit(by.error ? 1 : 0);
