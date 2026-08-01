#!/usr/bin/env bash
# tmp/master.sh — 自主跑完 A2 (doc_sync) → B1 (backup) → B2 (extract_docs) → C (fix_signatures)
#
# 设计：
#   - 完全脱手：nohup + disown + 落盘 PID
#   - 每阶段 10h 硬上限（master 自己当 watchdog，不用 run-sync.sh）
#   - 每步写 log 到 data/pipeline/pipeline.log
#   - 失败时优雅退出 + 写明 error 到 FINAL_REPORT.md
#   - 走完 4 阶段后写最终报告
#
# 依赖：当前 session 已经 export MINIMAX_API_KEY（master 脚本作为 nohup 子进程会继承）

set -uo pipefail

ROOT="/Users/wenzhengfeng/code/agent/ragWithColdApiDocument"
cd "$ROOT" || { echo "❌ 找不到 ROOT: $ROOT" >&2; exit 1; }

LOG_DIR="$ROOT/data/pipeline"
mkdir -p "$LOG_DIR"
PIPELINE_LOG="$LOG_DIR/pipeline.log"
: > "$PIPELINE_LOG"

# === helpers ============================================================
log() {
  echo "[$(date +%Y-%m-%dT%H:%M:%S)] $*" | tee -a "$PIPELINE_LOG"
}
stage_start() { touch "$LOG_DIR/$1.running"; }
stage_end()   { rm -f "$LOG_DIR/$1.running"; }

# 阶段执行：$1=stage_name $2=timeout_sec $3=cmd
#   后台跑，超时强杀，写 PID
run_stage() {
  local name="$1" timeout_s="$2"; shift 2
  local cmd=( "$@" )
  local stdout="$LOG_DIR/${name}.stdout" stderr="$LOG_DIR/${name}.stderr"
  local pidfile="$LOG_DIR/${name}.pid"
  local deadline
  deadline=$(( $(date +%s) + timeout_s ))
  stage_start "$name"

  log "--- $name START (timeout=${timeout_s}s) ---"
  log "  cmd: ${cmd[*]}"

  # 后台跑
  nohup "${cmd[@]}" > "$stdout" 2> "$stderr" &
  local pid=$!
  echo "$pid" > "$pidfile"
  disown "$pid" 2>/dev/null || true
  log "  pid=$pid  pidfile=$pidfile  stdout=$stdout  stderr=$stderr"

  # 等：每 60s 探活
  while kill -0 "$pid" 2>/dev/null; do
    if (( $(date +%s) > deadline )); then
      log "  ⏰ $name 超时 ${timeout_s}s，强杀 SIGTERM"
      kill -TERM "$pid" 2>/dev/null || true
      sleep 10
      if kill -0 "$pid" 2>/dev/null; then
        log "  ⏰ 仍未退出，SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
      fi
      break
    fi
    sleep 60
  done
  wait "$pid" 2>/dev/null
  local rc=$?
  log "  $name 退出码=$rc"
  stage_end "$name"
  return $rc
}

# === A2: doc_sync apply ==================================================
log "============================================================"
log "  PIPELINE START  $(date)"
log "  ROOT=$ROOT"
log "  MINIMAX_API_KEY 长度=${#MINIMAX_API_KEY} (子进程继承)"
log "============================================================"

# 锁检查（之前残留的 lock 已清）
if [ -f data/doc_sync/sync.lock ]; then
  log "❌ 发现残留 lock data/doc_sync/sync.lock，拒绝启动"
  log "   请人工处理后再跑"
  exit 1
fi

if run_stage "a2_doc_sync" 36000 \
  uv run --no-progress python -m src.script.sync_docs sync \
    --config configs/document_sync.yaml \
    --apply \
    --trigger scheduled; then
  A2_RC=0
  log "✅ A2 正常退出"
else
  A2_RC=$?
  log "⚠️ A2 异常退出 rc=$A2_RC（仍然往下走 B 阶段——B 阶段只看现有 markdown）"
fi

# A2 status (从 latest.json 读)
A2_STATUS="unknown"
if [ -f data/doc_sync/latest.json ]; then
  A2_STATUS=$(python3 -c "
import json
try:
    d = json.load(open('data/doc_sync/latest.json'))
    print(d.get('status','?'))
except Exception as e:
    print(f'parse_err:{e}')
" 2>&1 | tail -1)
fi
log "A2 manifest status: $A2_STATUS"

# === B1: 备份 + manifest ===============================================
log "--- B1: backup yaml + write manifest ---"
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="yaml.backup-$TS"
if [ -d yaml ]; then
  cp -r yaml "$BACKUP_DIR" || { log "❌ 备份失败"; exit 1; }
  log "✅ 备份 yaml → $BACKUP_DIR ($(du -sh "$BACKUP_DIR" | cut -f1))"
  # 用 mavis-trash 删（移到回收站可恢复）
  find yaml -mindepth 1 -delete 2>/dev/null || rm -rf yaml
  log "✅ 清空 yaml/"
else
  log "(yaml 不存在，跳过备份)"
fi
find API参考 -name "*.md" -type f | sort > "$LOG_DIR/minimal-units.txt"
MD_COUNT=$(wc -l < "$LOG_DIR/minimal-units.txt" | tr -d ' ')
log "✅ 写入 manifest ($LOG_DIR/minimal-units.txt), $MD_COUNT 个文件"

# === B2: extract_docs batch ============================================
if run_stage "b2_extract_docs" 36000 \
  uv run --no-progress python -m src.script.extract_docs \
    --batch-file "$LOG_DIR/minimal-units.txt" \
    --output-dir yaml \
    --workers 4 \
    --overwrite; then
  B2_RC=0
  log "✅ B2 正常退出"
else
  B2_RC=$?
  log "⚠️ B2 异常退出 rc=$B2_RC（部分完成会写 _batch_state.json，fix_signatures 仍可跑）"
fi

B2_STATUS="unknown"
B2_SUCCESS=0
B2_FAILED=0
B2_SKIPPED=0
if [ -f yaml/_batch_state.json ]; then
  eval "$(python3 -c "
import json
d = json.load(open('yaml/_batch_state.json'))
print(f'B2_STATUS={d.get(\"status\",\"?\")}')
print(f'B2_SUCCESS={d.get(\"success_count\",0)}')
print(f'B2_FAILED={d.get(\"failed_count\",0)}')
print(f'B2_SKIPPED={d.get(\"skipped_count\",0)}')
" 2>/dev/null)"
fi
log "B2 batch state: status=$B2_STATUS success=$B2_SUCCESS failed=$B2_FAILED skipped=$B2_SKIPPED"

# === C: fix_signatures =================================================
log "--- C: fix_signatures ---"
if run_stage "c_fix_signatures" 3600 \
  uv run --no-progress python tmp/fix_signatures.py \
    --yaml-dir yaml \
    --md-dir API参考 \
    --report "$LOG_DIR/c_report.txt"; then
  C_RC=0
  log "✅ C 正常退出"
else
  C_RC=$?
  log "⚠️ C 异常退出 rc=$C_RC"
fi

C_FIXED="?"
C_REMAINING_EMPTY="?"
C_SCANNED="?"
if [ -f "$LOG_DIR/c_report.txt" ]; then
  C_FIXED=$(grep -E "^fixed:" "$LOG_DIR/c_report.txt" | awk '{print $2}' || echo "?")
  C_REMAINING_EMPTY=$(grep -E "^remaining_empty:" "$LOG_DIR/c_report.txt" | awk '{print $2}' || echo "?")
  C_SCANNED=$(grep -E "^scanned:" "$LOG_DIR/c_report.txt" | awk '{print $2}' || echo "?")
fi
log "C: scanned=$C_SCANNED fixed=$C_FIXED remaining_empty=$C_REMAINING_EMPTY"

# === Final report ======================================================
YAML_COUNT=$(find yaml -name "*.yaml" 2>/dev/null | wc -l | tr -d ' ')
MD_COUNT_NOW=$(find API参考 -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' ')

REPORT="$LOG_DIR/FINAL_REPORT.md"
cat > "$REPORT" <<EOF
# Pipeline 最终报告

**完成时间**: $(date '+%Y-%m-%d %H:%M:%S %Z')
**ROOT**: \`$ROOT\`

## 1. A2: doc_sync（全量抓取）
- exit code: $A2_RC
- manifest status: \`$A2_STATUS\`
- log: \`$LOG_DIR/a2_doc_sync.stdout\` / \`.stderr\`
- pid: \`$(cat $LOG_DIR/a2_doc_sync.pid 2>/dev/null || echo "?")\`

## 2. B1: 备份 + manifest
- 旧 yaml 已备份到: \`$BACKUP_DIR\`
- manifest: \`$LOG_DIR/minimal-units.txt\` ($MD_COUNT 文件)

## 3. B2: extract_docs（全量 Markdown → YAML，workers=4）
- exit code: $B2_RC
- batch state: \`$B2_STATUS\`
- success: $B2_SUCCESS  failed: $B2_FAILED  skipped: $B2_SKIPPED
- 最终 yaml 文件数: $YAML_COUNT
- log: \`$LOG_DIR/b2_extract_docs.stdout\` / \`.stderr\`

## 4. C: fix_signatures（兜底补全空 signature）
- exit code: $C_RC
- scanned: $C_SCANNED
- fixed: $C_FIXED
- remaining_empty: $C_REMAINING_EMPTY
- report: \`$LOG_DIR/c_report.txt\`

## 当前快照
- API参考/ 下 md 文件数: $MD_COUNT_NOW
- yaml/ 下 yaml 文件数: $YAML_COUNT
- 备份: \`$BACKUP_DIR/\`

## 关键路径
- pipeline 主 log: \`$PIPELINE_LOG\`
- 最终报告: \`$REPORT\`
- run-sync 旧 log（参考）: \`data/doc_sync/logs/\`
- zvec 灌库（待跑）: \`uv run python -m src.script.ingest --help\`
EOF

log "============================================================"
log "  PIPELINE DONE  $(date)"
log "  FINAL_REPORT: $REPORT"
log "============================================================"
