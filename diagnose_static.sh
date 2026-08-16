#!/bin/bash
# OneBookWiki 静态文件诊断脚本（在 Mac 服务器上运行）

echo "=== OneBookWiki 静态文件诊断 ==="
echo ""

# 1. 检查工作目录
echo "1. 当前目录："
pwd
echo ""

# 2. 检查前端构建文件
echo "2. 检查前端构建文件："
if [ -d "frontend/dist" ]; then
    echo "✅ frontend/dist 存在"
    echo "内容："
    ls -lh frontend/dist/
    echo ""
    echo "Assets 目录："
    ls -lh frontend/dist/assets/
else
    echo "❌ frontend/dist 不存在"
fi
echo ""

# 3. 检查服务器是否在运行
echo "3. 检查服务器进程："
ps aux | grep "[u]vicorn server.main:app" | head -5
echo ""

# 4. 测试后端直接访问
echo "4. 测试后端直接访问："
echo "   GET /"
curl -s -o /dev/null -w "Status: %{http_code}\n" http://localhost:8000/
echo ""

echo "   GET /api/health"
curl -s http://localhost:8000/api/health
echo ""
echo ""

echo "   GET /assets/index-pEbt8zyJ.js (前 10 字节)"
curl -s http://localhost:8000/assets/index-pEbt8zyJ.js 2>&1 | head -c 100
echo ""
echo ""

# 5. 检查实际的 JS 文件名
echo "5. 实际的 assets 文件："
ACTUAL_JS=$(ls frontend/dist/assets/*.js 2>/dev/null | head -1 | xargs basename)
if [ -n "$ACTUAL_JS" ]; then
    echo "   找到: $ACTUAL_JS"
    echo "   测试访问: GET /assets/$ACTUAL_JS"
    curl -s -o /dev/null -w "Status: %{http_code}, Size: %{size_download} bytes\n" http://localhost:8000/assets/$ACTUAL_JS
else
    echo "   ❌ 未找到 JS 文件"
fi
echo ""

# 6. 查看服务器日志（最后 20 行）
echo "6. 服务器最后 20 行日志（如果可见）："
echo "   (请在运行服务器的终端查看)"
echo ""

# 7. 建议的修复步骤
echo "=== 建议的修复步骤 ==="
echo ""
echo "如果 curl http://localhost:8000/assets/xxx.js 返回 404："
echo ""
echo "步骤 1: 停止服务器 (Ctrl+C)"
echo ""
echo "步骤 2: 检查 frontend/dist 是否是最新构建"
echo "   cd frontend && npm run build"
echo ""
echo "步骤 3: 重新启动服务器"
echo "   ONEBOOKWIKI_ENV=production ./start.sh"
echo ""
echo "步骤 4: 查看启动日志，应该看到："
echo "   [onebookwiki] Production mode: serving frontend from ..."
echo "   [onebookwiki] Mounting /assets from ..."
echo ""
echo "如果仍然 404，运行此脚本并将输出发给技术支持。"
