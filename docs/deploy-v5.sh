#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Memory Gateway v5.0 一键部署脚本
# 使用方法：复制到 1Panel 终端执行
# ═══════════════════════════════════════════════════════════
set -e

REPO_DIR="/opt/memory-gateway"
BRANCH="main"

echo "═══ Memory Gateway v5.0 部署 ═══"
echo ""

# 1. 进入仓库目录
if [ ! -d "$REPO_DIR" ]; then
    echo "❌ 仓库不存在: $REPO_DIR"
    echo "请先执行: cd /opt && git clone https://github.com/kiDemon/memory-gateway.git"
    exit 1
fi
cd "$REPO_DIR"

# 2. 备份当前版本
echo "📦 备份当前 server.py..."
cp server.py server.py.v4.bak.$(date +%Y%m%d_%H%M%S)
echo "   备份完成"

# 3. 拉取最新代码
echo "⬇️  拉取 v5.0 更新..."
git fetch origin $BRANCH 2>/dev/null || {
    echo "⚠️  git fetch 失败（网络问题？），尝试 scp 方式..."
    echo "   请手动将新的 server.py 和 requirements.txt 复制到 $REPO_DIR/"
    echo "   然后继续执行: docker compose down && docker compose up -d --build"
    exit 1
}
git reset --hard origin/$BRANCH

# 4. 停止旧容器
echo "🛑 停止旧容器..."
docker compose down

# 5. 重新构建并启动
echo "🔨 构建 v5.0 镜像（包含 embedding 模型，首次构建约 3-5 分钟）..."
docker compose up -d --build

# 6. 等待启动
echo "⏳ 等待服务就绪..."
sleep 8

# 7. 健康检查
echo "🏥 健康检查..."
HEALTH=$(curl -s http://localhost:8650/health 2>/dev/null || echo '{"status":"down"}')
VERSION=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")

if [ "$VERSION" = "5.0.0" ]; then
    echo "✅ 部署成功！Memory Gateway v5.0.0 已就绪"
else
    echo "⚠️  版本检查: 期望 5.0.0，实际: $VERSION"
    echo "请手动检查: curl http://localhost:8650/health"
fi

# 8. 显示新增端点
echo ""
echo "═══ V5 新增端点 ═══"
echo "  GET  /mcp/cache/stats        → 热缓存统计"
echo "  POST /mcp/search_hybrid      → 混合语义搜索"
echo "  POST /mcp/cleanup            → 记忆衰减清理"
echo "  POST /mcp/audit/search       → 检索审计日志"
echo ""
echo "═══ 日志 ═══"
echo "  docker logs -f memory-gateway"
