// Produces a self-contained, local-only preview from the verified Vite build.
import {build} from 'esbuild';
import fs from 'node:fs/promises';
import path from 'node:path';
const destination=path.resolve(process.argv[2]||'dist-offline/Axiom-Preview.html');
const index=await fs.readFile('dist/index.html','utf8');
const jsFile=index.match(/src="([^"]+\.js)"/)?.[1];
const cssFile=index.match(/href="([^"]+\.css)"/)?.[1];
if(!jsFile||!cssFile)throw new Error('Run npm run build first.');
const jsPath=path.resolve('dist',jsFile),cssPath=path.resolve('dist',cssFile);
const result=await build({entryPoints:[jsPath],bundle:true,write:false,format:'iife',minify:true,target:'es2022',legalComments:'inline',logLevel:'error'});
let css=await fs.readFile(cssPath,'utf8');
const references=[...css.matchAll(/url\(([^)]+)\)/g)];
for(const match of references){const relative=match[1].replace(/^['"]|['"]$/g,'');if(relative.startsWith('data:'))continue;const asset=await fs.readFile(path.resolve(path.dirname(cssPath),relative));const mime=relative.endsWith('.woff2')?'font/woff2':'font/woff';css=css.replaceAll(match[0],`url(data:${mime};base64,${asset.toString('base64')})`);}
const script=result.outputFiles[0].text.replace(/<\/script/gi,'<\\/script');
const html=`<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#111419"><title>Axiom — Offline Trading Workstation Preview</title><!-- Self-contained offline preview. See source docs and THIRD-PARTY-NOTICES.txt for licenses. Fixture prices are simulated. --><style>${css}</style></head><body><div id="root"></div><script>${script}</script></body></html>`;
await fs.mkdir(path.dirname(destination),{recursive:true});await fs.writeFile(destination,html);console.log(`Offline preview written: ${destination} (${Buffer.byteLength(html)} bytes)`);
