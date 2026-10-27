import fs from 'node:fs/promises';
const names=['react','react-dom','scheduler','lucide-react','lightweight-charts','fancy-canvas','@tauri-apps/api','@fontsource/dm-sans','@fontsource/jetbrains-mono'];
let text='Axiom Workstation — Third-party notices\n\nDistributed source uses the dependencies in package-lock.json. The offline preview embeds these runtime components and fonts.\n';
for(const name of names){const root=`node_modules/${name}`;try{const p=JSON.parse(await fs.readFile(`${root}/package.json`,'utf8'));text+=`\n\n===== ${name} ${p.version} (${p.license||'see license'}) =====\n`;const files=(await fs.readdir(root)).filter(x=>/^(license|notice|ofl)(\.|$)/i.test(x));for(const file of files)text+=`\n${file}\n${await fs.readFile(`${root}/${file}`,'utf8')}\n`;}catch{}}
await fs.writeFile('THIRD-PARTY-NOTICES.txt',text);
