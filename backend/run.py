"""PyCharm 开发启动脚本。

用法（PyCharm 运行配置）：
  - Script path:  D:\\code\\AI_stock\\backend\\run.py
  - Working directory: D:\\code\\AI_stock\\backend
  - Interpreter:  backend\\.venv\\Scripts\\python.exe

说明：reload 固定为 False，因为 uvicorn 的 --reload 会 fork 出 WatchFiles
子进程，在 Windows + PyCharm 调试器下会把 pydevd 的路径拼坏，报
"SyntaxError: unexpected character after line continuation character"。
"""
from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
