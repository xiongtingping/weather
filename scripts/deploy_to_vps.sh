#!/bin/bash
# VPS部署脚本

set -e  # 遇到错误立即退出

# 配置变量
VPS_IP="82.22.32.48"
VPS_PORT="22"
VPS_USER="root"  # 默认root，如果不是请修改
DEPLOY_DIR="/var/www/weather"
PROJECT_NAME="weather"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=================================="
echo "Weather项目VPS部署脚本"
echo "==================================${NC}"
echo ""

# 检查本地环境
echo -e "${YELLOW}[1/8] 检查本地环境...${NC}"
if [ ! -d "/Users/xiong/weather" ]; then
    echo -e "${RED}错误: 项目目录不存在${NC}"
    exit 1
fi

if ! command -v rsync &> /dev/null; then
    echo -e "${YELLOW}警告: rsync未安装，将使用scp（较慢）${NC}"
    USE_RSYNC=0
else
    USE_RSYNC=1
fi

echo -e "${GREEN}✅ 本地环境检查完成${NC}"
echo ""

# 测试SSH连接
echo -e "${YELLOW}[2/8] 测试SSH连接...${NC}"
if ssh -o ConnectTimeout=10 -o BatchMode=yes -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} exit 2>/dev/null; then
    echo -e "${GREEN}✅ SSH连接成功（使用密钥认证）${NC}"
else
    echo -e "${YELLOW}⚠️  需要密码认证，请准备好密码${NC}"
    echo "测试连接..."
    if ! ssh -o ConnectTimeout=10 -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} exit; then
        echo -e "${RED}错误: 无法连接到VPS${NC}"
        exit 1
    fi
fi
echo ""

# 运行资源检查
echo -e "${YELLOW}[3/8] 检查VPS资源...${NC}"
echo "上传检查脚本..."
scp -P ${VPS_PORT} /Users/xiong/weather/scripts/check_vps_resources.sh ${VPS_USER}@${VPS_IP}:/tmp/ 2>/dev/null || {
    echo -e "${RED}错误: 无法上传检查脚本${NC}"
    exit 1
}

echo "运行资源检查..."
ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} 'bash /tmp/check_vps_resources.sh' > /tmp/vps_check_result.txt

echo ""
echo "=== 资源检查结果 ==="
cat /tmp/vps_check_result.txt
echo ""

# 询问是否继续
read -p "是否继续部署？(y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "部署已取消"
    exit 0
fi
echo ""

# 备份现有项目（如果存在）
echo -e "${YELLOW}[4/8] 备份现有项目（如果存在）...${NC}"
ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} << 'ENDSSH'
if [ -d "/var/www/weather" ]; then
    echo "发现现有项目，创建备份..."
    BACKUP_NAME="weather_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
    tar -czf "/tmp/${BACKUP_NAME}" -C /var/www weather 2>/dev/null || true
    echo "备份已保存到: /tmp/${BACKUP_NAME}"
else
    echo "未发现现有项目，跳过备份"
fi
ENDSSH
echo -e "${GREEN}✅ 备份完成${NC}"
echo ""

# 创建部署目录
echo -e "${YELLOW}[5/8] 创建部署目录...${NC}"
ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} << 'ENDSSH'
mkdir -p /var/www/weather
mkdir -p /var/www/weather/data
mkdir -p /var/www/weather/data/logs
echo "✅ 目录创建完成"
ENDSSH
echo ""

# 上传项目文件
echo -e "${YELLOW}[6/8] 上传项目文件...${NC}"
echo "这可能需要几分钟，请耐心等待..."

if [ $USE_RSYNC -eq 1 ]; then
    echo "使用rsync上传（增量同步）..."
    rsync -avz --progress \
        --exclude 'node_modules' \
        --exclude '.venv' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.git' \
        --exclude 'data/archive.db' \
        --exclude 'data/logs/*.log' \
        --exclude '.DS_Store' \
        -e "ssh -p ${VPS_PORT}" \
        /Users/xiong/weather/ \
        ${VPS_USER}@${VPS_IP}:${DEPLOY_DIR}/
else
    echo "使用scp上传..."
    # 创建临时压缩包
    echo "压缩项目文件..."
    cd /Users/xiong
    tar -czf /tmp/weather_deploy.tar.gz \
        --exclude='weather/node_modules' \
        --exclude='weather/.venv' \
        --exclude='weather/__pycache__' \
        --exclude='weather/*.pyc' \
        --exclude='weather/.git' \
        --exclude='weather/data/archive.db' \
        --exclude='weather/data/logs/*.log' \
        weather/
    
    echo "上传压缩包..."
    scp -P ${VPS_PORT} /tmp/weather_deploy.tar.gz ${VPS_USER}@${VPS_IP}:/tmp/
    
    echo "解压文件..."
    ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} << 'ENDSSH'
cd /var/www
tar -xzf /tmp/weather_deploy.tar.gz
rm /tmp/weather_deploy.tar.gz
ENDSSH
    
    rm /tmp/weather_deploy.tar.gz
fi

echo -e "${GREEN}✅ 文件上传完成${NC}"
echo ""

# 安装依赖
echo -e "${YELLOW}[7/8] 安装依赖...${NC}"
ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP} << 'ENDSSH'
cd /var/www/weather

# 检查Python版本
if ! command -v python3 &> /dev/null; then
    echo "错误: Python3未安装"
    exit 1
fi

echo "Python版本: $(python3 --version)"

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建Python虚拟环境..."
    python3 -m venv .venv
fi

# 安装Python依赖
echo "安装Python依赖..."
source .venv/bin/activate
if [ -f "requirements.txt" ]; then
    pip install --upgrade pip
    pip install -r requirements.txt
else
    echo "警告: requirements.txt不存在"
fi

echo "✅ 依赖安装完成"
ENDSSH
echo ""

# 配置环境变量
echo -e "${YELLOW}[8/8] 配置环境变量...${NC}"
echo "请手动配置.env文件，包括："
echo "  - 数据库连接"
echo "  - API密钥"
echo "  - 端口配置（建议使用3001避免冲突）"
echo ""
echo "示例命令："
echo "  ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP}"
echo "  cd ${DEPLOY_DIR}"
echo "  vim .env"
echo ""

# 部署完成
echo -e "${GREEN}=================================="
echo "✅ 部署完成！"
echo "==================================${NC}"
echo ""
echo "下一步操作："
echo ""
echo "1. 配置环境变量:"
echo "   ssh -p ${VPS_PORT} ${VPS_USER}@${VPS_IP}"
echo "   cd ${DEPLOY_DIR}"
echo "   vim .env"
echo ""
echo "2. 初始化数据库:"
echo "   cd ${DEPLOY_DIR}"
echo "   source .venv/bin/activate"
echo "   python scan_weather_market.py --init-db"
echo ""
echo "3. 测试运行:"
echo "   python scan_weather_market.py --help"
echo ""
echo "4. 使用PM2启动服务:"
echo "   pm2 start scripts/auto_trade_worker.sh --name weather-worker"
echo "   pm2 start scripts/market_refresh_1m.sh --name weather-refresh"
echo "   pm2 save"
echo ""
echo "5. 查看日志:"
echo "   pm2 logs weather-worker"
echo "   tail -f data/logs/*.log"
echo ""
echo "完整部署文档: /Users/xiong/weather/VPS_DEPLOYMENT_GUIDE.md"
echo ""
