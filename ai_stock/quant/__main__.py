"""进程入口: ``python -m ai_stock.quant`` 启动量化子系统 (阻塞运行)."""

from __future__ import annotations

import logging


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    from .service import run_service

    run_service(with_scheduler=True, block=True)


if __name__ == "__main__":
    main()
