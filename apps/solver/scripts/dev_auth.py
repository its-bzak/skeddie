"""Local-development sign-in, for calling the API before an identity provider is chosen.

    python scripts/dev_auth.py keys
    python scripts/dev_auth.py token dan@dansorg.example.com

`keys` creates a signing key and its public JWKS in apps/solver/.dev-auth/
(gitignored) and prints the AUTH_* lines to put in .env. `token` prints a
one-hour access token for that email, signed with the dev key.

The API accepts these tokens only while AUTH_JWKS_URL points at the dev JWKS
file, so never point a deployed API at it.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv

SOLVER_DIR = Path(__file__).resolve().parents[1]
KEY_DIR = SOLVER_DIR / ".dev-auth"
PRIVATE_KEY = KEY_DIR / "private_key.pem"
JWKS = KEY_DIR / "jwks.json"
KEY_ID = "skeddie-dev"
DEV_ISSUER = "https://skeddie.dev.local"
DEV_AUDIENCE = "skeddie-api"


def create_keys() -> None:
    KEY_DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    PRIVATE_KEY.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update(kid=KEY_ID, use="sig", alg="RS256")
    JWKS.write_text(json.dumps({"keys": [public_jwk]}, indent=2))

    print(f"Dev signing key written to {KEY_DIR}. Add these lines to .env:")
    print(f"AUTH_ISSUER={DEV_ISSUER}")
    print(f"AUTH_AUDIENCE={DEV_AUDIENCE}")
    print(f"AUTH_JWKS_URL={JWKS.as_uri()}")


def issue_token(email: str, hours: float) -> None:
    if not PRIVATE_KEY.is_file():
        sys.exit("No dev signing key yet: run `python scripts/dev_auth.py keys` first.")
    load_dotenv(SOLVER_DIR.parents[1] / ".env")
    now = int(time.time())
    claims = {
        "iss": os.environ.get("AUTH_ISSUER", DEV_ISSUER),
        "aud": os.environ.get("AUTH_AUDIENCE", DEV_AUDIENCE),
        "sub": f"dev|{email.lower()}",
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + int(hours * 3600),
    }
    print(jwt.encode(claims, PRIVATE_KEY.read_bytes(), algorithm="RS256", headers={"kid": KEY_ID}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keys", help="create the dev signing key and JWKS")
    token = commands.add_parser("token", help="print an access token for an email")
    token.add_argument("email")
    token.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args()

    if args.command == "keys":
        create_keys()
    else:
        issue_token(args.email, args.hours)


if __name__ == "__main__":
    main()
