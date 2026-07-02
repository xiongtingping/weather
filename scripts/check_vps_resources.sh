#!/bin/bash
# VPS资源检查脚本

echo "=================================="
echo "VPS资源检查脚本"
echo "=================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 1. 系统信息
echo "=== 1. 系统信息 ==="
echo "主机名: $(hostname)"
echo "操作系统: $(uname -s)"
echo "内核版本: $(uname -r)"
echo "架构: $(uname -m)"
if [ -f /etc/os-release ]; then
    echo "发行版: $(grep PRETTY_NAME /etc/os-release | cut -d'"' -f2)"
fi
echo ""

# 2. CPU信息
echo "=== 2. CPU信息 ==="
if command -v lscpu &> /dev/null; then
    echo "CPU型号: $(lscpu | grep 'Model name' | cut -d':' -f2 | xargs)"
    echo "CPU核心数: $(lscpu | grep '^CPU(s):' | awk '{print $2}')"
    echo "CPU架构: $(lscpu | grep 'Architecture' | awk '{print $2}')"
else
    echo "CPU核心数: $(nproc)"
fi
echo "CPU负载 (1/5/15分钟): $(uptime | awk -F'load average:' '{print $2}')"
echo ""

# 3. 内存信息
echo "=== 3. 内存信息 ==="
if command -v free &> /dev/null; then
    free -h
    echo ""
    total_mem=$(free -m | awk 'NR==2{print $2}')
    used_mem=$(free -m | awk 'NR==2{print $3}')
    free_mem=$(free -m | awk 'NR==2{print $4}')
    mem_usage=$(awk "BEGIN {printf \"%.1f\", ($used_mem/$total_mem)*100}")
    
    echo "内存使用率: ${mem_usage}%"
    if (( $(echo "$mem_usage > 80" | bc -l) )); then
        echo -e "${RED}⚠️  警告: 内存使用率过高！${NC}"
    elif (( $(echo "$mem_usage > 60" | bc -l) )); then
        echo -e "${YELLOW}⚠️  注意: 内存使用率较高${NC}"
    else
        echo -e "${GREEN}✅ 内存使用率正常${NC}"
    fi
else
    echo "无法获取内存信息（free命令不可用）"
fi
echo ""

# 4. 磁盘空间
echo "=== 4. 磁盘空间 ==="
df -h | grep -E '^/dev/|Filesystem'
echo ""
root_usage=$(df -h / | awk 'NR==2{print $5}' | sed 's/%//')
if [ "$root_usage" -gt 80 ]; then
    echo -e "${RED}⚠️  警告: 根分区使用率 ${root_usage}% (>80%)${NC}"
elif [ "$root_usage" -gt 60 ]; then
    echo -e "${YELLOW}⚠️  注意: 根分区使用率 ${root_usage}% (>60%)${NC}"
else
    echo -e "${GREEN}✅ 磁盘空间充足${NC}"
fi
echo ""

# 5. 网络端口占用
echo "=== 5. 网络端口占用 ==="
echo "常用端口监听状态:"
if command -v netstat &> /dev/null; then
    netstat -tulpn 2>/dev/null | grep LISTEN | awk '{print $4, $7}' | sort -u
elif command -v ss &> /dev/null; then
    ss -tulpn | grep LISTEN | awk '{print $5, $7}' | sort -u
else
    echo "无法检查端口（netstat/ss命令不可用）"
fi
echo ""

# 6. 运行中的进程
echo "=== 6. 运行中的进程 (Top 10 CPU) ==="
if command -v ps &> /dev/null; then
    ps aux --sort=-%cpu | head -11
else
    echo "无法获取进程信息"
fi
echo ""

# 7. 运行中的进程 (Top 10 内存)
echo "=== 7. 运行中的进程 (Top 10 内存) ==="
if command -v ps &> /dev/null; then
    ps aux --sort=-%mem | head -11
else
    echo "无法获取进程信息"
fi
echo ""

# 8. Node.js进程
echo "=== 8. Node.js/Python进程 ==="
if command -v ps &> /dev/null; then
    echo "Node.js进程:"
    ps aux | grep -E '[n]ode|[n]pm' || echo "  无Node.js进程"
    echo ""
    echo "Python进程:"
    ps aux | grep -E '[p]ython' || echo "  无Python进程"
else
    echo "无法检查进程"
fi
echo ""

# 9. PM2进程
echo "=== 9. PM2进程 ==="
if command -v pm2 &> /dev/null; then
    pm2 list
else
    echo "PM2未安装"
fi
echo ""

# 10. Docker容器
echo "=== 10. Docker容器 ==="
if command -v docker &> /dev/null; then
    docker ps -a
else
    echo "Docker未安装"
fi
echo ""

# 11. 数据库服务
echo "=== 11. 数据库服务 ==="
echo "PostgreSQL:"
if command -v systemctl &> /dev/null; then
    systemctl status postgresql 2>/dev/null | grep -E 'Active:|Loaded:' || echo "  未安装或未运行"
else
    ps aux | grep -E '[p]ostgres' || echo "  未运行"
fi
echo ""
echo "Redis:"
if command -v systemctl &> /dev/null; then
    systemctl status redis 2>/dev/null | grep -E 'Active:|Loaded:' || echo "  未安装或未运行"
else
    ps aux | grep -E '[r]edis' || echo "  未运行"
fi
echo ""

# 12. Web服务器
echo "=== 12. Web服务器 ==="
echo "Nginx:"
if command -v systemctl &> /dev/null; then
    systemctl status nginx 2>/dev/null | grep -E 'Active:|Loaded:' || echo "  未安装或未运行"
else
    ps aux | grep -E '[n]ginx' || echo "  未运行"
fi
echo ""

# 13. 防火墙状态
echo "=== 13. 防火墙状态 ==="
if command -v ufw &> /dev/null; then
    sudo ufw status 2>/dev/null || echo "需要sudo权限"
elif command -v firewall-cmd &> /dev/null; then
    sudo firewall-cmd --state 2>/dev/null || echo "需要sudo权限"
else
    echo "未检测到防火墙"
fi
echo ""

# 14. 系统运行时间
echo "=== 14. 系统运行时间 ==="
uptime
echo ""

# 15. 最近登录
echo "=== 15. 最近登录 ==="
if command -v last &> /dev/null; then
    last -n 5
else
    echo "无法获取登录信息"
fi
echo ""

# 16. 资源建议
echo "=== 16. 部署建议 ==="
echo ""

# 内存建议
if [ -n "$total_mem" ]; then
    if [ "$total_mem" -lt 1024 ]; then
        echo -e "${RED}⚠️  内存不足1GB，不建议部署复杂应用${NC}"
    elif [ "$total_mem" -lt 2048 ]; then
        echo -e "${YELLOW}⚠️  内存1-2GB，建议使用轻量级配置${NC}"
    else
        echo -e "${GREEN}✅ 内存充足 (${total_mem}MB)${NC}"
    fi
fi

# 磁盘建议
if [ -n "$root_usage" ]; then
    available_gb=$(df -BG / | awk 'NR==2{print $4}' | sed 's/G//')
    if [ "$available_gb" -lt 5 ]; then
        echo -e "${RED}⚠️  可用磁盘空间不足5GB，建议清理${NC}"
    elif [ "$available_gb" -lt 10 ]; then
        echo -e "${YELLOW}⚠️  可用磁盘空间${available_gb}GB，注意监控${NC}"
    else
        echo -e "${GREEN}✅ 磁盘空间充足 (${available_gb}GB可用)${NC}"
    fi
fi

# 端口建议
echo ""
echo "推荐使用的端口:"
for port in 3000 3001 3002 5000 8000 8080; do
    if command -v netstat &> /dev/null; then
        if ! netstat -tuln 2>/dev/null | grep -q ":$port "; then
            echo -e "  ${GREEN}✅ $port (可用)${NC}"
        else
            echo -e "  ${RED}❌ $port (已占用)${NC}"
        fi
    elif command -v ss &> /dev/null; then
        if ! ss -tuln | grep -q ":$port "; then
            echo -e "  ${GREEN}✅ $port (可用)${NC}"
        else
            echo -e "  ${RED}❌ $port (已占用)${NC}"
        fi
    fi
done

echo ""
echo "=================================="
echo "检查完成！"
echo "=================================="
