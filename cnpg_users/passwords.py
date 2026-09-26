"""PostgreSQL password verifiers, computed and checked offline."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import string

SCRAM_ITERATIONS = 4096  # PostgreSQL's scram_iterations default
PASSWORD_LENGTH = 32


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """Letters and digits, at least one capital and one digit (as cnpg-user.py create)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isupper() for c in password) and any(c.isdigit() for c in password):
            return password


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL would store for this password.
    ALTER ROLE ... PASSWORD '<verifier>' stores it as-is, so the plaintext
    never has to appear in SQL."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    stored_key = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def password_matches(password: str, username: str, verifier: str) -> bool | None:
    """Whether a stored verifier (pg_authid.rolpassword) is for this password.
    '' (no password) never matches; None means an unknown format."""
    if not verifier:
        return False
    m = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier)
    if m:
        salted = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(m[2]), int(m[1]))
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        return hmac.compare_digest(hashlib.sha256(client_key).digest(), base64.b64decode(m[3]))
    if verifier.startswith("md5"):
        return verifier == "md5" + hashlib.md5((password + username).encode()).hexdigest()
    return None
