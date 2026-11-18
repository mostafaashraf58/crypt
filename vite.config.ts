import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
export default defineConfig({plugins:[react(),tailwindcss()],base:'./',server:{host:'127.0.0.1',port:1420,strictPort:true},build:{target:'es2022',rollupOptions:{output:{manualChunks(id){if(id.includes('lightweight-charts')||id.includes('fancy-canvas'))return 'charts';if(id.includes('node_modules/react')||id.includes('node_modules/scheduler'))return 'react';}}}}});
