#!/usr/bin/env python3

import secrets
import base64


def generate_jwt_secret_key() -> str:
    # Generate a random 256-bit key
    key = secrets.token_bytes(32)

    # Encode the key in a URL-safe Base64 format (HS256 uses a 256-bit key)
    hs256_key = base64.urlsafe_b64encode(key).decode('utf-8')

    return hs256_key


if __name__ == "__main__":
    jwt_secret_key: str = generate_jwt_secret_key()
    print(jwt_secret_key)
