#!/bin/bash
# DSL + OpenClaw 全量备份到 OneDrive v1
# 每日 02:00 运行 (在 backup.sh 之前)
set -e

DATE=$(date +%Y%m%d)
TIME=$(date +%H%M)
ONEDRIVE_ROOT="$HOME/Library/CloudStorage/OneDrive-个人"
BACKUP_DIR="${ONEDRIVE_ROOT}/DSL_Backup/${DATE}_${TIME}"
KEEP_DAYS=14
LOG_FILE="/tmp/dsl_full_backup_${DATE}.log"

echo "============================================================" | tee -a "$LOG_FILE"
echo "💾 DSL+OpenClaw 全量备份到 OneDrive" | tee -a "$LOG_FILE"
echo "   日期: ${DATE} ${TIME}" | tee -a "$LOG_FILE"
echo "   目录: ${BACKUP_DIR}" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"

# 检查 OneDrive 可用
if [ ! -d "$ONEDRIVE_ROOT" ]; then
    echo "❌ OneDrive 目录不存在: $ONEDRIVE_ROOT" | tee -a "$LOG_FILE"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# v4.5.18: ProgressTracker for Dashboard
PT_SCRIPT="$HOME/.openclaw/workspace/dsl-quant-trading-hybrid/common/progress_tracker.py"
if [ -f "$PT_SCRIPT" ]; then
  python3 -c "
import sys; sys.path.insert(0, '$HOME/.openclaw/workspace/dsl-quant-trading-hybrid')
from common.progress_tracker import ProgressTracker
ProgressTracker('dsl_backup', total_steps=4)
" 2>/dev/null
fi

# ─── A. DSL 项目（代码 + 配置 + 数据） ───
DSL_ROOT="$HOME/.openclaw/workspace/dsl-quant-trading-hybrid"
echo "" | tee -a "$LOG_FILE"
echo "A. DSL 量化交易系统" | tee -a "$LOG_FILE"

# A1. 核心代码（GitHub已有，但备份更全）
mkdir -p "${BACKUP_DIR}/dsl/code"
cp -r "${DSL_ROOT}/scripts" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ scripts" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/core" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ core" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/config" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ config" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/common" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ common" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/strategies" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ strategies" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/markets" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ markets" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/web_dashboard" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ web_dashboard" | tee -a "$LOG_FILE"
cp -r "${DSL_ROOT}/tests" "${BACKUP_DIR}/dsl/code/" 2>/dev/null && echo "  ✅ tests" | tee -a "$LOG_FILE"

# A2. 关键数据文件（GitHub没有）
mkdir -p "${BACKUP_DIR}/dsl/data"
[ -f "${DSL_ROOT}/data/paper_trading.db" ] && cp "${DSL_ROOT}/data/paper_trading.db" "${BACKUP_DIR}/dsl/data/" && echo "  ✅ paper_trading.db" | tee -a "$LOG_FILE"
[ -f "${DSL_ROOT}/data/paper_trading_ledger.json" ] && cp "${DSL_ROOT}/data/paper_trading_ledger.json" "${BACKUP_DIR}/dsl/data/" && echo "  ✅ paper_trading_ledger.json" | tee -a "$LOG_FILE"
[ -f "${DSL_ROOT}/data/simulation_portfolio.json" ] && cp "${DSL_ROOT}/data/simulation_portfolio.json" "${BACKUP_DIR}/dsl/data/" && echo "  ✅ simulation_portfolio.json" | tee -a "$LOG_FILE"
[ -f "${DSL_ROOT}/data/feedback_log.jsonl" ] && cp "${DSL_ROOT}/data/feedback_log.jsonl" "${BACKUP_DIR}/dsl/data/" && echo "  ✅ feedback_log.jsonl" | tee -a "$LOG_FILE"
[ -f "${DSL_ROOT}/data/error_stats.json" ] && cp "${DSL_ROOT}/data/error_stats.json" "${BACKUP_DIR}/dsl/data/" && echo "  ✅ error_stats.json" | tee -a "$LOG_FILE"

# A3. 置信度校准数据
[ -d "${DSL_ROOT}/confidence_data" ] && cp -r "${DSL_ROOT}/confidence_data" "${BACKUP_DIR}/dsl/" && echo "  ✅ confidence_data/" | tee -a "$LOG_FILE"

# A4. 训练模型（大文件，关键）
mkdir -p "${BACKUP_DIR}/dsl/models"
if [ -d "${DSL_ROOT}/models/pool" ]; then
    model_count=$(ls "${DSL_ROOT}/models/pool" 2>/dev/null | wc -l)
    cp -r "${DSL_ROOT}/models/pool" "${BACKUP_DIR}/dsl/models/" 2>/dev/null && echo "  ✅ models/pool/ (${model_count} files)" | tee -a "$LOG_FILE"
fi

# A5. 自适应参数
[ -f "${DSL_ROOT}/config/adaptive_params.yaml" ] && cp "${DSL_ROOT}/config/adaptive_params.yaml" "${BACKUP_DIR}/dsl/" && echo "  ✅ adaptive_params.yaml" | tee -a "$LOG_FILE"
[ -f "${DSL_ROOT}/VERSION" ] && cp "${DSL_ROOT}/VERSION" "${BACKUP_DIR}/dsl/" && echo "  ✅ VERSION" | tee -a "$LOG_FILE"

# A6. .env.local（密钥文件）
[ -f "${DSL_ROOT}/.env.local" ] && cp "${DSL_ROOT}/.env.local" "${BACKUP_DIR}/dsl/" && echo "  ✅ .env.local" | tee -a "$LOG_FILE"

# ─── B. OpenClaw 工作区（Memory、黑天鹅等） ───
WORKSPACE="$HOME/.openclaw/workspace"
echo "" | tee -a "$LOG_FILE"
echo "B. OpenClaw 工作区" | tee -a "$LOG_FILE"

mkdir -p "${BACKUP_DIR}/openclaw"
# B1. Memory 文件（长期记忆）
for f in MEMORY.md LESSONS.md SOUL.md USER.md IDENTITY.md; do
    [ -f "${WORKSPACE}/${f}" ] && cp "${WORKSPACE}/${f}" "${BACKUP_DIR}/openclaw/" && echo "  ✅ ${f}" | tee -a "$LOG_FILE"
done

# B2. 每日记忆
[ -d "${WORKSPACE}/memory" ] && cp -r "${WORKSPACE}/memory" "${BACKUP_DIR}/openclaw/" && echo "  ✅ memory/" | tee -a "$LOG_FILE"

# B3. 黑天鹅分析
[ -d "${WORKSPACE}/memory/black-swan" ] && cp -r "${WORKSPACE}/memory/black-swan" "${BACKUP_DIR}/openclaw/" && echo "  ✅ black-swan/" | tee -a "$LOG_FILE"

# ─── C. OpenClaw 系统配置（Gateway、Cron） ───
echo "" | tee -a "$LOG_FILE"
echo "C. OpenClaw 系统配置" | tee -a "$LOG_FILE"
OPENCLAW_HOME="$HOME/.openclaw"
mkdir -p "${BACKUP_DIR}/openclaw/system"

# C1. openclaw.json（Gateway配置）
[ -f "${OPENCLAW_HOME}/openclaw.json" ] && cp "${OPENCLAW_HOME}/openclaw.json" "${BACKUP_DIR}/openclaw/system/" && echo "  ✅ openclaw.json" | tee -a "$LOG_FILE"

# C2. Cron jobs
[ -f "${OPENCLAW_HOME}/cron/jobs.json" ] && cp "${OPENCLAW_HOME}/cron/jobs.json" "${BACKUP_DIR}/openclaw/system/" && echo "  ✅ cron/jobs.json" | tee -a "$LOG_FILE"

# C3. OpenClaw系统配置（排除35GB的agents目录，skills可重装）
mkdir -p "${BACKUP_DIR}/openclaw/system/config"

# openclaw.json, models.json, cron
for f in openclaw.json models.json .env.local; do
    [ -f "${OPENCLAW_HOME}/${f}" ] && cp "${OPENCLAW_HOME}/${f}" "${BACKUP_DIR}/openclaw/system/config/" && echo "  ✅ ${f}" | tee -a "$LOG_FILE"
done

# Cron jobs
[ -f "${OPENCLAW_HOME}/cron/jobs.json" ] && cp "${OPENCLAW_HOME}/cron/jobs.json" "${BACKUP_DIR}/openclaw/system/config/" && echo "  ✅ cron/jobs.json" | tee -a "$LOG_FILE"

# Gateway日志（最后10MB，用于诊断）
[ -f "${OPENCLAW_HOME}/logs/gateway.log" ] && tail -c 10M "${OPENCLAW_HOME}/logs/gateway.log" > "${BACKUP_DIR}/openclaw/system/config/gateway_tail.log" 2>/dev/null && echo "  ✅ gateway.log (tail 10M)" | tee -a "$LOG_FILE"

# Skills list (不是全量备份，只记安装了哪些skill)
mkdir -p "${BACKUP_DIR}/openclaw/system"
ls "${OPENCLAW_HOME}/skills/" > "${BACKUP_DIR}/openclaw/system/installed_skills.txt" 2>/dev/null && echo "  ✅ installed_skills.txt" | tee -a "$LOG_FILE"

# Skill配置（排除大文件）
if [ -d "${OPENCLAW_HOME}/skills" ]; then
    mkdir -p "${BACKUP_DIR}/openclaw/system/skills"
    find "${OPENCLAW_HOME}/skills" -maxdepth 3 -name "*.json" -o -name "SKILL.md" -o -name "*.yaml" -o -name "*.yml" | while read f; do
        rel="${f#${OPENCLAW_HOME}/}"
        mkdir -p "${BACKUP_DIR}/openclaw/system/skills/$(dirname $rel)"
        cp "$f" "${BACKUP_DIR}/openclaw/system/skills/$rel" 2>/dev/null
    done
    echo "  ✅ skills/(SKILL.md+config only)" | tee -a "$LOG_FILE"
fi

# C4. 模型路由配置
[ -f "${OPENCLAW_HOME}/models.json" ] && cp "${OPENCLAW_HOME}/models.json" "${BACKUP_DIR}/openclaw/system/" && echo "  ✅ models.json" | tee -a "$LOG_FILE"

# C5. （skills已合并到C3中）

# ─── D. 打包压缩 ───
echo "" | tee -a "$LOG_FILE"
echo "D. 打包压缩..." | tee -a "$LOG_FILE"
cd "$ONEDRIVE_ROOT/DSL_Backup"
tar czf "full_backup_${DATE}_${TIME}.tar.gz" "${DATE}_${TIME}" 2>/dev/null
BACKUP_SIZE=$(du -h "full_backup_${DATE}_${TIME}.tar.gz" | cut -f1)
echo "  ✅ 打包完成: full_backup_${DATE}_${TIME}.tar.gz (${BACKUP_SIZE})" | tee -a "$LOG_FILE"

# 清理旧备份（保留 KEEP_DAYS 天）
find "$ONEDRIVE_ROOT/DSL_Backup" -name "full_backup_*.tar.gz" -mtime +${KEEP_DAYS} -delete 2>/dev/null
find "$ONEDRIVE_ROOT/DSL_Backup" -type d -name "${DATE}_*" -exec rm -rf {} + 2>/dev/null
echo "  ✅ 清理 ${KEEP_DAYS} 天前的旧备份" | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"
echo "✅ DSL+OpenClaw 全量备份完成 — ${DATE} ${TIME}" | tee -a "$LOG_FILE"
echo "   文件: full_backup_${DATE}_${TIME}.tar.gz (${BACKUP_SIZE})" | tee -a "$LOG_FILE"
echo "   位置: OneDrive/DSL_Backup/" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"
# v4.5.18: Mark backup complete for Dashboard
if [ -f "$PT_SCRIPT" ]; then
  python3 -c "
import sys; sys.path.insert(0, '$HOME/.openclaw/workspace/dsl-quant-trading-hybrid')
from common.progress_tracker import ProgressTracker
p = ProgressTracker('dsl_backup', total_steps=4)
p.complete()
" 2>/dev/null
fi