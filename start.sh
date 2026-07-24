#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "[错误] 未找到 .venv，请先运行: python3 -m venv .venv && .venv/bin/pip install -r requirements-all.txt"
    exit 1
fi

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[错误] .venv 不完整，请重新创建 Python 虚拟环境。"
    exit 1
fi
PORT=8710

if curl -s "http://127.0.0.1:${PORT}/api/sources" >/dev/null 2>&1; then
    echo "[提示] 服务已在 http://127.0.0.1:${PORT}/ 运行"
    if command -v xdg-open &>/dev/null; then
        xdg-open "http://127.0.0.1:${PORT}/" &
    fi
    exit 0
fi

echo "正在启动 Multi-source Artist Grabber ..."
echo "关闭本终端即可退出服务。"
echo ""

# If an optional CUDA-enabled ONNX runtime is installed in this venv, expose
# its bundled NVIDIA libraries without relying on a contributor-specific path.
NVIDIA_LIBS=""
if [ -d ".venv/lib" ]; then
    NVIDIA_LIBS=$(find .venv/lib -type d -path '*/site-packages/nvidia/*/lib' -print 2>/dev/null | paste -sd ':' -)
fi
if [ -n "$NVIDIA_LIBS" ]; then
    export LD_LIBRARY_PATH="${NVIDIA_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec "$PY" app.py --no-browser
