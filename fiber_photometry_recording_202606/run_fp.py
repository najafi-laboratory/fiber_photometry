import sys
from PyQt6 import QtWidgets
from gui import MainWindow

def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(use_hw=True)
    win.show()
    sys.exit(app.exec())
if __name__ == '__main__':
    main()
