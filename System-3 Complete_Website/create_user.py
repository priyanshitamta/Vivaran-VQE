"""
Vivaran-VQE - create a trainer or admin account from the command line.

    python create_user.py --role admin   --username admin1
    python create_user.py --role trainer --username trainer1 --email trainer1@gov.in

The password is asked for interactively (never passed on the command line or
stored in plain text). Learners register themselves on the website.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from werkzeug.security import generate_password_hash

import db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", choices=("admin", "trainer"), required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--email")
    args = parser.parse_args()

    db.init_db()
    if db.get_user_by_username(args.username):
        print(f"User '{args.username}' already exists.")
        return 1
    password = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8 or password != getpass.getpass("Repeat password: "):
        print("Passwords must match and be at least 8 characters.")
        return 1
    if args.role == "admin" and len(db.list_users("admin")) >= 3:
        print("The platform already has the maximum of 3 admins.")
        return 1
    uid = db.create_user(args.username, generate_password_hash(password),
                         email=(args.email or None), role=args.role)
    if args.role == "admin" and not any(a.get("is_primary") for a in db.list_users("admin")):
        db.set_user_fields(uid, is_primary=1)
    print(f"Created {args.role} '{args.username}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
