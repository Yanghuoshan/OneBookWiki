import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Connect } from 'vite';
import react from '@vitejs/plugin-react';

const bookRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'books', 'zhenshi');
const contentTypes: Record<string, string> = {
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
};

function bookStaticMiddleware(): Connect.NextHandleFunction {
  return (request, response, next) => {
    const requestUrl = (request as { url?: string }).url || '/';
    const requestPath = decodeURIComponent(requestUrl.split('?', 1)[0]).replace(/^\/+/, '');
    const filePath = resolve(bookRoot, requestPath);
    if (!filePath.startsWith(`${bookRoot}${sep}`) || !existsSync(filePath) || !statSync(filePath).isFile()) {
      next();
      return;
    }
    response.statusCode = 200;
    response.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream');
    response.end(readFileSync(filePath));
  };
}

export default defineConfig({
  plugins: [{
    ...react(),
    configureServer(server) {
      server.middlewares.use('/book', bookStaticMiddleware());
    },
    configurePreviewServer(server) {
      server.middlewares.use('/book', bookStaticMiddleware());
    },
  }],
  server: {
    port: 5173,
    strictPort: false,
  },
});
