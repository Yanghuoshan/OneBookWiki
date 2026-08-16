# OneBookWiki 生产部署指南

## 问题诊断

如果你的域名能访问首页但无法加载 JS/CSS 资源，通常是以下原因之一：

### 1. 检查服务器日志

在 Mac 上运行：
```bash
tail -f /var/log/nginx/onebookwiki-error.log
tail -f /var/log/nginx/onebookwiki-access.log
```

查看是否有 `/assets/` 路径的请求到达 Nginx。

### 2. 检查 Nginx 配置

查看当前 Nginx 配置：
```bash
sudo nginx -t
cat /etc/nginx/sites-enabled/onebookwiki
# 或
cat /etc/nginx/conf.d/onebookwiki.conf
```

**关键点**：必须正确代理 `/assets/` 路径。

### 3. 浏览器开发者工具

打开 `https://duckorganon.dpdns.org/`，按 F12 打开开发者工具：

- **Network 标签页**：查看资源请求状态
  - 如果看到 `/assets/index-xxx.js` 显示 404/502 → Nginx 配置问题
  - 如果请求卡住或超时 → 代理超时配置问题
  - 如果请求被 CORS 阻止 → 需要检查 FastAPI CORS 配置

- **Console 标签页**：查看 JavaScript 错误
  - 如果显示 "Failed to load module" → 资源加载失败
  - 如果显示 CORS 错误 → 跨域问题

### 4. 直接测试后端

在服务器上测试后端是否正常响应：
```bash
# 测试 HTML
curl -I http://localhost:8000/

# 测试静态资源（替换为实际文件名）
curl -I http://localhost:8000/assets/index-pEbt8zyJ.js

# 测试 API
curl http://localhost:8000/api/books
```

如果这些都返回 200，说明后端正常，问题在 Nginx。

## 解决方案

### 方案 1：更新 Nginx 配置（推荐）

1. 复制 `nginx.conf.example` 到 Nginx 配置目录：
```bash
sudo cp nginx.conf.example /etc/nginx/sites-available/onebookwiki
sudo ln -sf /etc/nginx/sites-available/onebookwiki /etc/nginx/sites-enabled/onebookwiki
```

2. 修改配置文件中的：
   - `server_name` 改为你的域名
   - SSL 证书路径
   - 后端地址（如果不是 127.0.0.1:8000）

3. 测试并重载配置：
```bash
sudo nginx -t
sudo nginx -s reload
```

### 方案 2：清除浏览器缓存

如果之前访问过旧版本，可能有缓存问题：

1. Chrome/Edge: Ctrl+Shift+Delete，清除缓存
2. 或者用隐私模式/无痕模式访问
3. 或者清除该域名的缓存（开发者工具 → Application → Clear storage）

### 方案 3：检查防火墙

确保服务器防火墙允许 8000 端口（如果直接访问）或 80/443 端口：
```bash
# macOS
sudo pfctl -sr | grep 8000

# Linux (ufw)
sudo ufw status

# Linux (iptables)
sudo iptables -L -n | grep 8000
```

### 方案 4：检查 FastAPI 日志

OneBookWiki 日志应该显示所有请求。如果 Nginx 日志有请求但 FastAPI 没有，说明代理未生效。

查看完整日志：
```bash
# 在运行服务器的终端查看
# 应该看到类似这样的日志：
# INFO:     220.161.73.18:0 - "GET /assets/index-pEbt8zyJ.js HTTP/1.1" 200 OK
```

## 常见 Nginx 配置错误

### 错误 1：没有代理 /assets

```nginx
# ❌ 错误：缺少 /assets 配置
location / {
    proxy_pass http://127.0.0.1:8000;
}
```

```nginx
# ✅ 正确：明确配置 /assets
location /assets/ {
    proxy_pass http://127.0.0.1:8000/assets/;
}
location / {
    proxy_pass http://127.0.0.1:8000;
}
```

### 错误 2：代理路径末尾斜杠不一致

```nginx
# ❌ 错误：路径不匹配
location /assets/ {
    proxy_pass http://127.0.0.1:8000/assets;  # 缺少末尾斜杠
}
```

```nginx
# ✅ 正确：保持一致
location /assets/ {
    proxy_pass http://127.0.0.1:8000/assets/;
}
```

### 错误 3：缺少必要的 proxy_set_header

```nginx
# ❌ 错误：缺少 headers
location / {
    proxy_pass http://127.0.0.1:8000;
}
```

```nginx
# ✅ 正确：设置完整 headers
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 快速诊断脚本

在服务器上运行：

```bash
#!/bin/bash
echo "=== OneBookWiki 诊断 ==="
echo ""

echo "1. 检查后端是否运行："
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/ || echo "❌ 后端未运行"

echo ""
echo "2. 检查静态资源："
ASSET_FILE=$(ls -1 frontend/dist/assets/index-*.js 2>/dev/null | head -1 | xargs basename)
if [ -n "$ASSET_FILE" ]; then
    curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/assets/$ASSET_FILE
else
    echo "❌ 未找到前端构建文件"
fi

echo ""
echo "3. 检查 Nginx 配置："
sudo nginx -t 2>&1 | grep -q "successful" && echo "✅ Nginx 配置正确" || echo "❌ Nginx 配置错误"

echo ""
echo "4. 检查 Nginx 进程："
pgrep nginx > /dev/null && echo "✅ Nginx 运行中" || echo "❌ Nginx 未运行"

echo ""
echo "5. 当前监听端口："
netstat -tuln | grep -E ':(80|443|8000)' || lsof -iTCP -sTCP:LISTEN | grep -E '(80|443|8000)'
```

## 重新构建前端

如果怀疑前端构建有问题：

```bash
cd frontend
rm -rf dist node_modules
npm install
npm run build
```

然后重启服务器：
```bash
ONEBOOKWIKI_ENV=production ./start.sh
```

## 联系支持

如果以上方法都无法解决，请提供以下信息：

1. Nginx 配置文件内容
2. OneBookWiki 服务器日志（最近 50 行）
3. Nginx access.log 和 error.log（访问首页时的日志）
4. 浏览器开发者工具 Network 标签的截图
5. `curl -v http://localhost:8000/` 的完整输出
