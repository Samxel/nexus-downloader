import sys
import traceback


def main():
    try:
        from downloader import run
    except ImportError as e:
        sys.exit(f"Missing dependency ({e.name}). Install with: pip install -r requirements.txt")
    run()


if __name__ == "__main__":
    frozen = getattr(sys, "frozen", False)
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception:
        if not frozen:
            raise
        traceback.print_exc()
    finally:
        # A double-clicked .exe closes its window right away; keep it open so the output can be read.
        if frozen and sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")
