import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, isAbsolute, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Connect } from 'vite';
import react from '@vitejs/plugin-react';
import { request as httpRequest } from 'node:http';


const booksRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'books');
const bookIdPattern = /^[1-9]\d*$/;
const contentTypes: Record<string, string> = {
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
};

type BookRequest = { bookId: string; relativePath: string[] };

function isWithin(root: string, candidate: string): boolean {
  const path = relative(root, candidate);
  return path !== '' && !path.startsWith('..') && !isAbsolute(path);
}

function isDirectory(path: string): boolean {
  try {
    return existsSync(path) && statSync(path).isDirectory();
  } catch {
    return false;
  }
}

function isFile(path: string): boolean {
  try {
    return existsSync(path) && statSync(path).isFile();
  } catch {
    return false;
  }
}

function parseBookRequest(requestUrl: string): BookRequest | null {
  const encodedPath = requestUrl.split('?', 1)[0].replace(/^\/+/, '');
  let requestPath: string;
  try {
    requestPath = decodeURIComponent(encodedPath);
  } catch {
    return null;
  }
  if (!requestPath || /[\\\0]/.test(requestPath)) return null;

  const segments = requestPath.split('/');
  if (segments.some(segment => !segment || segment === '.' || segment === '..')) return null;
  const [first, ...remaining] = segments;
  const candidateRoot = resolve(booksRoot, first);
  const hasExplicitBook = bookIdPattern.test(first) && isWithin(booksRoot, candidateRoot) && isDirectory(candidateRoot);
  return hasExplicitBook ? { bookId: first, relativePath: remaining } : null;
}

function apiProxyMiddleware(): Connect.NextHandleFunction {
  return (req, res) => {
    const targetUrl = req.url || '/';
    const options = {
      hostname: 'localhost',
      port: 8000,
      path: '/api' + targetUrl,
      method: req.method,
      headers: { ...req.headers, host: 'localhost:8000' },
    };
    const proxyReq = httpRequest(options, (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
      proxyRes.pipe(res);
    });
    proxyReq.on('error', () => {
      res.statusCode = 502;
      res.end('Bad Gateway');
    });
    req.pipe(proxyReq);
  };
}

function bookStaticMiddleware(): Connect.NextHandleFunction {
  return (request, response, next) => {
    const requestUrl = (request as { url?: string }).url || '/';
    const parsed = parseBookRequest(requestUrl);
    if (!parsed || !bookIdPattern.test(parsed.bookId)) {
      next();
      return;
    }
    const bookRoot = resolve(booksRoot, parsed.bookId);
    const filePath = resolve(bookRoot, ...parsed.relativePath);
    if (!isWithin(bookRoot, filePath) || !isFile(filePath)) {
      next();
      return;
    }
    response.statusCode = 200;
    response.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream');
    response.end(readFileSync(filePath));
  };
}

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'onebookwiki-server',
      configureServer(server) {
        server.middlewares.use('/book', bookStaticMiddleware());
        server.middlewares.use('/api', apiProxyMiddleware());
      },
      configurePreviewServer(server) {
        server.middlewares.use('/book', bookStaticMiddleware());
        server.middlewares.use('/api', apiProxyMiddleware());
      },
    },
  ],
  server: {
    port: 5173,
    strictPort: false,
  },
});
