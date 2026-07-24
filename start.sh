#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "[错误] 未找到 .venv，请先运行: python3 -m venv .venv && .venv/bin/pip install -r <(echo 'gallery-dl websocket-client onnxruntime numpy Pillow')"
    exit 1
fi

PY=".venv/bin/python"
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

# Load CUDA libraries for onnxruntime-gpu (from MonadForge venv)
NVIDIA_LIBS=$(find /home/buxinzi/Projects/toolbox/model-train/MonadForge/.venv/lib/python*/site-packages/nvidia -name "lib" -type d 2>/dev/null | tr '\n' ':')
export LD_LIBRARY_PATH="${NVIDIA_LIBS}${LD_LIBRARY_PATH}"

"$PY" app.py --no-browser
