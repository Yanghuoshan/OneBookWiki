var __assign = (this && this.__assign) || function () {
    __assign = Object.assign || function(t) {
        for (var s, i = 1, n = arguments.length; i < n; i++) {
            s = arguments[i];
            for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p))
                t[p] = s[p];
        }
        return t;
    };
    return __assign.apply(this, arguments);
};
var __spreadArray = (this && this.__spreadArray) || function (to, from, pack) {
    if (pack || arguments.length === 2) for (var i = 0, l = from.length, ar; i < l; i++) {
        if (ar || !(i in from)) {
            if (!ar) ar = Array.prototype.slice.call(from, 0, i);
            ar[i] = from[i];
        }
    }
    return to.concat(ar || Array.prototype.slice.call(from));
};
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, isAbsolute, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { request as httpRequest } from 'node:http';
var booksRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'books');
var bookIdPattern = /^[1-9]\d*$/;
var contentTypes = {
    '.json': 'application/json; charset=utf-8',
    '.md': 'text/markdown; charset=utf-8',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
};
function isWithin(root, candidate) {
    var path = relative(root, candidate);
    return path !== '' && !path.startsWith('..') && !isAbsolute(path);
}
function isDirectory(path) {
    try {
        return existsSync(path) && statSync(path).isDirectory();
    }
    catch (_a) {
        return false;
    }
}
function isFile(path) {
    try {
        return existsSync(path) && statSync(path).isFile();
    }
    catch (_a) {
        return false;
    }
}
function parseBookRequest(requestUrl) {
    var encodedPath = requestUrl.split('?', 1)[0].replace(/^\/+/, '');
    var requestPath;
    try {
        requestPath = decodeURIComponent(encodedPath);
    }
    catch (_a) {
        return null;
    }
    if (!requestPath || /[\\\0]/.test(requestPath))
        return null;
    var segments = requestPath.split('/');
    if (segments.some(function (segment) { return !segment || segment === '.' || segment === '..'; }))
        return null;
    var first = segments[0], remaining = segments.slice(1);
    var candidateRoot = resolve(booksRoot, first);
    var hasExplicitBook = bookIdPattern.test(first) && isWithin(booksRoot, candidateRoot) && isDirectory(candidateRoot);
    return hasExplicitBook ? { bookId: first, relativePath: remaining } : null;
}
function apiProxyMiddleware() {
    return function (req, res) {
        var targetUrl = req.url || '/';
        var options = {
            hostname: 'localhost',
            port: 8000,
            path: '/api' + targetUrl,
            method: req.method,
            headers: __assign(__assign({}, req.headers), { host: 'localhost:8000' }),
        };
        var proxyReq = httpRequest(options, function (proxyRes) {
            res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
            proxyRes.pipe(res);
        });
        proxyReq.on('error', function () {
            res.statusCode = 502;
            res.end('Bad Gateway');
        });
        req.pipe(proxyReq);
    };
}
function bookStaticMiddleware() {
    return function (request, response, next) {
        var requestUrl = request.url || '/';
        var parsed = parseBookRequest(requestUrl);
        if (!parsed || !bookIdPattern.test(parsed.bookId)) {
            next();
            return;
        }
        var bookRoot = resolve(booksRoot, parsed.bookId);
        var filePath = resolve.apply(void 0, __spreadArray([bookRoot], parsed.relativePath, false));
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
            configureServer: function (server) {
                server.middlewares.use('/book', bookStaticMiddleware());
                server.middlewares.use('/api', apiProxyMiddleware());
            },
            configurePreviewServer: function (server) {
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
