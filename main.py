"""起動用スクリプト: python main.py（ブラウザで開く）。デスクトップ版は python main.py --desktop"""

import sys

if __name__ == "__main__":
    if "--desktop" in sys.argv:
        from storage_organizer.app import main
        sys.exit(main())
    from storage_organizer.webapp import main
    sys.exit(main())
