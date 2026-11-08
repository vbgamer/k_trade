#!/usr/bin/env python
"""
AlgoTrade Pro - Management Command Utility
Usage:
    python manage.py runserver [--host 127.0.0.1] [--port 8000] [--no-reload]
    python manage.py worker
    python manage.py seed
    python manage.py test
    python manage.py routes
    python manage.py genkeys [--force]
    python manage.py migrate
    python manage.py makemigrations -m "message"
"""
import sys
import os
import argparse
import subprocess

# Ensure backend root is on sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def runserver(args):
    """Run FastAPI development server with Uvicorn."""
    import socket
    import uvicorn

    # Test if target port is available
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((args.host, args.port))
        sock.close()
    except OSError as err:
        sock.close()
        print(f"\n[ERROR] Port {args.port} on {args.host} is already in use or restricted ({err}).")
        print(f"[TIP] Either stop the existing process or choose another port:")
        print(f"      python manage.py runserver --port 8001\n")
        sys.exit(1)

    print(f"\n=======================================================")
    print(f"  Starting AlgoTrade Pro Backend Server")
    print(f"  URL: http://{args.host}:{args.port}")
    print(f"  API Docs: http://{args.host}:{args.port}/docs")
    print(f"  Frontend Dashboard: http://{args.host}:{args.port}/")
    print(f"=======================================================\n")
    try:
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=not args.no_reload,
            log_level="info"
        )
    except Exception as e:
        print(f"\n[ERROR] Failed to start server: {e}")
        print(f"[TIP] Try specifying another port, e.g.: python manage.py runserver --port 8001\n")


def worker(args):
    """Run Celery background worker for strategy processing."""
    print("Starting Celery worker for AlgoTrade Pro strategies...")
    cmd = ["celery", "-A", "app.workers.tasks", "worker", "--loglevel=info", "-P", "solo"]
    subprocess.run(cmd)


def seed(args):
    """Seed initial demo trades, virtual margin, and test user."""
    from scripts.seed_demo_data import main as run_seed
    run_seed()


def test(args):
    """Run backend test suite with pytest."""
    print("Running backend tests...")
    cmd = [sys.executable, "-m", "pytest", os.path.join(BASE_DIR, "tests")]
    if args.verbose:
        cmd.append("-v")
    result = subprocess.run(cmd, cwd=BASE_DIR)
    sys.exit(result.returncode)


def genkeys(args):
    """Create backend/.env with freshly generated SECRET_KEY and ENCRYPTION_KEY."""
    import secrets
    from cryptography.fernet import Fernet

    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path) and not args.force:
        print(f"[SKIP] {env_path} already exists. Use --force to overwrite (existing encrypted broker credentials will become unreadable).")
        return

    example_path = os.path.join(BASE_DIR, ".env.example")
    with open(example_path, encoding="utf-8") as f:
        content = f.read()
    content = content.replace("SECRET_KEY=", f"SECRET_KEY={secrets.token_hex(32)}", 1)
    content = content.replace("ENCRYPTION_KEY=", f"ENCRYPTION_KEY={Fernet.generate_key().decode()}", 1)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] Wrote {env_path} with new secrets. Keep this file private; never commit it.")


def migrate(args):
    """Apply all pending database migrations (alembic upgrade head)."""
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BASE_DIR)
    sys.exit(result.returncode)


def makemigrations(args):
    """Autogenerate a new migration from model changes."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "revision", "--autogenerate", "-m", args.message],
        cwd=BASE_DIR,
    )
    sys.exit(result.returncode)


def routes(args):
    """List all registered FastAPI routes."""
    from app.main import app
    print("\n--- Registered API Gateway Routes ---")
    print(f"{'METHOD':<8} {'PATH':<45} {'NAME'}")
    print("-" * 75)
    for route in sorted(app.routes, key=lambda r: getattr(r, 'path', '')):
        methods = ",".join(getattr(route, "methods", ["WS"]))
        path = getattr(route, "path", "")
        name = getattr(route, "name", "")
        print(f"{methods:<8} {path:<45} {name}")
    print("-" * 75 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AlgoTrade Pro CLI Manager")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # runserver
    server_parser = subparsers.add_parser("runserver", help="Start the FastAPI backend server")
    server_parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    server_parser.add_argument("--port", type=int, default=8000, help="Port number (default: 8000)")
    server_parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload")

    # worker
    subparsers.add_parser("worker", help="Start Celery background worker")

    # seed
    subparsers.add_parser("seed", help="Seed demo trades, users, and broker data")

    # test
    test_parser = subparsers.add_parser("test", help="Run pytest suite")
    test_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose test output")

    # routes
    subparsers.add_parser("routes", help="Display all registered API routes")

    # genkeys
    genkeys_parser = subparsers.add_parser("genkeys", help="Generate backend/.env with new secret keys")
    genkeys_parser.add_argument("--force", action="store_true", help="Overwrite an existing .env")

    # migrate / makemigrations
    subparsers.add_parser("migrate", help="Apply database migrations")
    mm_parser = subparsers.add_parser("makemigrations", help="Autogenerate a migration from model changes")
    mm_parser.add_argument("-m", "--message", required=True, help="Migration message")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "runserver": runserver,
        "worker": worker,
        "seed": seed,
        "test": test,
        "routes": routes,
        "genkeys": genkeys,
        "migrate": migrate,
        "makemigrations": makemigrations,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
