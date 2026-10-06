"""Production assessment queue worker entrypoint."""

from archivability.application.worker import main


if __name__ == "__main__":
    raise SystemExit(main())
