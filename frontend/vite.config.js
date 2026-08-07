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
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
var bookRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'books', 'zhenshi');
var contentTypes = {
    '.json': 'application/json; charset=utf-8',
    '.md': 'text/markdown; charset=utf-8',
};
function bookStaticMiddleware() {
    return function (request, response, next) {
        var requestUrl = request.url || '/';
        var requestPath = decodeURIComponent(requestUrl.split('?', 1)[0]).replace(/^\/+/, '');
        var filePath = resolve(bookRoot, requestPath);
        if (!filePath.startsWith("".concat(bookRoot).concat(sep)) || !existsSync(filePath) || !statSync(filePath).isFile()) {
            next();
            return;
        }
        response.statusCode = 200;
        response.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream');
        response.end(readFileSync(filePath));
    };
}
export default defineConfig({
    plugins: [__assign(__assign({}, react()), { configureServer: function (server) {
                server.middlewares.use('/book', bookStaticMiddleware());
            }, configurePreviewServer: function (server) {
                server.middlewares.use('/book', bookStaticMiddleware());
            } })],
    server: {
        port: 5173,
        strictPort: false,
    },
});
