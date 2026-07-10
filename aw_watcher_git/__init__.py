def main() -> None:
    """entry point - imported lazily so pure modules stay importable without aw deps."""
    from .watcher import main as run

    run()
