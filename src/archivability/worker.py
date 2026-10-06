"""Production assessment queue worker entrypoint."""

from archivability.application.worker import health_main, main


__all__ = ["health_main", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
