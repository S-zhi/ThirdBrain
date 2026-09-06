#!/usr/bin/env bash
# run-sync.sh — 启动 doc_sync 增量同步任务（apply + 后台 + 1h timeout）
#
# 用法:
#   ./run-sync.sh                       # 默认: apply + 后台 + 1h timeout
#   ./run-sync.sh --dry-run             # 只生成 manifest，不写入
#   ./run-sync.sh --foreground          # 前台运行（阻塞当前 shell）
#   ./run-sync.sh --timeout 1800        # 自定义超时（秒）
#   ./run-sync.sh --trigger manual      # 写入 manifest 的触发来源（manual / scheduled）
#   ./run-sync.sh --batch-size 100      # 分批执行的每批最大页面数
#   ./run-sync.sh --resume-from doc_id  # 从指定 document_id (含) 开始增量同步/自举
#   ./run-sync.sh -h | --help
#
# 产物:
#   data/doc_sync/logs/run-<时间戳>.log   完整 stdout+stderr
#   data/doc_sync/logs/run-<时间戳>.pid   后台主进程 PID
#   data/doc_sync/logs/run-<时间戳>.meta  JSON: 启动参数、启动时间、命令
#
# 锁:
#   若 data/doc_sync/sync.lock 存在且对应 PID 还活着，拒绝启动；
#   若锁残留但 PID 已死，给出 WARN，但仍允许新进程重新获取 flock。
#
# 停止后台任务:  kill $(cat data/doc_sync/logs/run-<时间戳>.pid)

set -eo pipefail

# === 切到脚本所在目录（脚本可从任意 cwd 调用） ============================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# === 默认参数 ==============================================================
APPLY=true
FOREGROUND=false
TIMEOUT=3600
TRIGGER=scheduled
BATCH_SIZE=""
RESUME_FROM=""
CONFIG=configs/document_sync.yaml
LOG_DIR=data/doc_sync/logs
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/run-${TIMESTAMP}.log"
PID_FILE="$LOG_DIR/run-${TIMESTAMP}.pid"
META_FILE="$LOG_DIR/run-${TIMESTAMP}.meta"

# === 参数解析 ==============================================================
print_help() {
  sed -n '2,20p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        APPLY=false; shift ;;
    --apply)          APPLY=true; shift ;;
    --foreground|-F)  FOREGROUND=true; shift ;;
    --timeout)
      [[ $# -ge 2 ]] || { echo "--timeout 需要参数" >&2; exit 64; }
      TIMEOUT="$2"; shift 2 ;;
    --trigger)
      [[ $# -ge 2 ]] || { echo "--trigger 需要参数" >&2; exit 64; }
      TRIGGER="$2"; shift 2 ;;
    --batch-size)
      [[ $# -ge 2 ]] || { echo "--batch-size 需要参数" >&2; exit 64; }
      BATCH_SIZE="$2"; shift 2 ;;
    --resume-from)
      [[ $# -ge 2 ]] || { echo "--resume-from 需要参数" >&2; exit 64; }
      RESUME_FROM="$2"; shift 2 ;;
    --config)
      [[ $# -ge 2 ]] || { echo "--config 需要参数" >&2; exit 64; }
      CONFIG="$2"; shift 2 ;;
    -h|--help)        print_help; exit 0 ;;
    *) echo "未知参数: $1（用 -h 看帮助）" >&2; exit 64 ;;
  esac
done

# === 准备 ==================================================================
mkdir -p "$LOG_DIR"

# 锁冲突检测（只读，不动）
LOCK_FILE=data/doc_sync/sync.lock
if [[ -f "$LOCK_FILE" ]]; then
  if command -v jq >/dev/null 2>&1; then
    EXISTING_PID=$(jq -r '.pid // empty' "$LOCK_FILE" 2>/dev/null || true)
  else
    EXISTING_PID=$(python3 -c "import json,sys; print(json.load(open('$LOCK_FILE')).get('pid',''))" 2>/dev/null || true)
  fi
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "❌ sync 任务已在跑 (PID $EXISTING_PID)，拒绝启动新的任务。" >&2
    echo "   停止旧任务: kill $EXISTING_PID" >&2
    exit 1
  else
    echo "⚠️  检测到残留 lock 文件 (PID ${EXISTING_PID:-?} 已退出)，新进程会重新争用 flock。" >&2
  fi
fi

# 构造命令
CMD_ARGS=(sync --config "$CONFIG" --trigger "$TRIGGER")
[[ -n "$BATCH_SIZE" ]] && CMD_ARGS+=(--batch-size "$BATCH_SIZE")
[[ -n "$RESUME_FROM" ]] && CMD_ARGS+=(--resume-from "$RESUME_FROM")
$APPLY && CMD_ARGS+=(--apply)

# 写 meta
if command -v jq >/dev/null 2>&1; then
  CMD_JSON=$(printf '%s\n' "${CMD_ARGS[@]}" | jq -R . | jq -s .)
else
  CMD_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "${CMD_ARGS[@]}")
fi
cat > "$META_FILE" <<EOF
{"args": $CMD_JSON, "timeout_seconds": $TIMEOUT, "apply": $APPLY, "trigger": "$TRIGGER", "foreground": $FOREGROUND, "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "log": "$LOG_FILE", "pid_file": "$PID_FILE"}
EOF

CMD=(uv run --no-progress python -m src.script.sync_docs "${CMD_ARGS[@]}")

# === 前台模式 ==============================================================
if $FOREGROUND; then
  echo "▶  前台模式（timeout=${TIMEOUT}s），Ctrl-C 终止"
  "${CMD[@]}" 2>&1 | tee "$LOG_FILE"
  exit $?
fi

# === 后台模式 ==============================================================
echo "▶  后台启动"
echo "   命令:  ${CMD[*]}"
echo "   日志:  $LOG_FILE"
echo "   Meta:  $META_FILE"

# nohup 屏蔽 SIGHUP + 显式重定向 stdout/stderr，让进程脱离父 shell 生命周期
# 注意: macOS 没有 setsid，单 nohup 已够（Mavis / bash tool 退出时 SIGHUP 不到子进程）
nohup "${CMD[@]}" > "$LOG_FILE" 2>&1 &
BG_PID=$!
echo "$BG_PID" > "$PID_FILE"
disown $BG_PID 2>/dev/null || true
# 补写 BG_PID 到 meta（事后查方便）
if command -v jq >/dev/null 2>&1; then
  jq --argjson pid "$BG_PID" '. + {pid: $pid}' "$META_FILE" > "$META_FILE.tmp" && mv "$META_FILE.tmp" "$META_FILE"
else
  python3 -c "import json,sys; d=json.load(open('$META_FILE')); d['pid']=$BG_PID; json.dump(d, open('$META_FILE','w'))"
fi

# 2 秒快速健康检查（秒崩检测）
sleep 2
if ! kill -0 "$BG_PID" 2>/dev/null; then
  echo "❌ 进程 2 秒内退出，查看日志: $LOG_FILE" >&2
  exit 1
fi

# Watchdog：纯 bash 实现，跨平台（不依赖 timeout/gtimeout）。
# 注意: subshell 里 SECONDS 会重置为 0，所以用 date +%s 算 deadline；
#       set -u 下直接读父 shell 变量可能触发 unbound，subshell 内显式捕获。
(
  bg_pid="$BG_PID"
  timeout_s="$TIMEOUT"
  log_file="$LOG_FILE"
  deadline=$(( $(date +%s) + timeout_s ))
  while (( $(date +%s) < deadline )); do
    if ! kill -0 "$bg_pid" 2>/dev/null; then exit 0; fi
    sleep 5
  done
  echo "[watchdog] timeout ${timeout_s}s reached, SIGTERM → $bg_pid" >> "$log_file"
  kill -TERM "$bg_pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! kill -0 "$bg_pid" 2>/dev/null; then exit 0; fi
    sleep 1
  done
  echo "[watchdog] SIGTERM ignored, SIGKILL → $bg_pid" >> "$log_file"
  kill -KILL "$bg_pid" 2>/dev/null || true
) &
WATCHDOG_PID=$!
disown $WATCHDOG_PID 2>/dev/null || true

echo "✅ Started, pid=$BG_PID, watchdog=$WATCHDOG_PID, timeout=${TIMEOUT}s"
echo "   log:    $LOG_FILE"
echo "   meta:   $META_FILE"
echo "   tail:   tail -f $LOG_FILE"
echo "   stop:   kill $BG_PID"
