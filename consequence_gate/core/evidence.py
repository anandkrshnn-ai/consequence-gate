"""Ed25519 notary for gate decisions.

Produces a self-contained envelope: payload hash + public key + signature.
Offline verification needs only the envelope — not the simulators.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

from .models import EvaluationResult, GateDecision


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> str:
    return base64.b64decode(text.encode("ascii"))
