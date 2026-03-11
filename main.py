#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# minimal texteditor built by rinbal


import sys
from PySide6.QtWidgets import QApplication
from main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("minimal texteditor")
    app.setStyleSheet("""
        QWidget { color: #D4D4D4; }
        QToolTip { 
            background-color: #252526; 
            color: #D4D4D4; 
            border: 1px solid #3C3C3C; 
        }
    """)
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    win = MainWindow(initial_path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
