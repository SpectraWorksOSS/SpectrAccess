"""Manage provider logins in the operating system keyring."""
import argparse

from .core.credentials import PROVIDERS, login, logout, status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "logout"):
        commands.add_parser(name).add_argument("provider", choices=PROVIDERS)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            if login(args.provider):
                print(f"Stored {args.provider} credentials.")
        elif args.command == "logout":
            logout(args.provider)
            print(f"Removed {args.provider} credentials.")
        else:
            for row in status():
                print(f"{row['provider']}: {'stored' if row['stored'] else 'not stored'}; "
                      f"account={row['account'] or '-'}; backend={row['backend']}")
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0
