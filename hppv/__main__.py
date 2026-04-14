from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from hppv.config import default_config
from hppv.ui.main_window import MainWindow


def main() -> int:
    """App entrypoint for `python -m hppv`."""

    config = default_config()
    app = QApplication(sys.argv)
    w = MainWindow(config)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

