"""Passkeys (Face ID / Touch ID) nach dem WebAuthn-Standard – selbst geprüft, ohne Zusatzbibliothek.

Geprüft wird alles, was der Standard verlangt: Typ, Challenge (einmalig, 5 Min.), Herkunft (origin),
RP-ID-Hash, „Benutzer anwesend“ + „Benutzer verifiziert“ (Face ID), Signatur und Signaturzähler.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa


class WebAuthnError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    if not isinstance(s, str) or len(s) > 20000:
        raise WebAuthnError("ungültige Daten")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise WebAuthnError("ungültige Daten") from None


# --- Minimaler, strenger CBOR-Leser (nur was WebAuthn braucht) -------------
def _cbor(data: bytes, pos: int = 0, depth: int = 0):
    if depth > 8:
        raise WebAuthnError("CBOR zu tief verschachtelt")
    if pos >= len(data):
        raise WebAuthnError("CBOR unvollständig")
    ib = data[pos]
    major, info = ib >> 5, ib & 0x1F
    pos += 1
    if info < 24:
        val = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        if pos + n > len(data):
            raise WebAuthnError("CBOR unvollständig")
        val = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    else:
        raise WebAuthnError("CBOR-Format nicht unterstützt")
    if major == 0:
        return val, pos
    if major == 1:
        return -1 - val, pos
    if major in (2, 3):
        if pos + val > len(data):
            raise WebAuthnError("CBOR unvollständig")
        raw = data[pos:pos + val]
        return (raw if major == 2 else raw.decode("utf-8", "strict")), pos + val
    if major == 4:
        if val > 64:
            raise WebAuthnError("CBOR-Liste zu lang")
        out = []
        for _ in range(val):
            item, pos = _cbor(data, pos, depth + 1)
            out.append(item)
        return out, pos
    if major == 5:
        if val > 64:
            raise WebAuthnError("CBOR-Map zu groß")
        out = {}
        for _ in range(val):
            k, pos = _cbor(data, pos, depth + 1)
            v, pos = _cbor(data, pos, depth + 1)
            if isinstance(k, (bytes, list, dict)):
                raise WebAuthnError("CBOR-Schlüssel ungültig")
            out[k] = v
        return out, pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise WebAuthnError("CBOR-Typ nicht unterstützt")


def cbor_decode(data: bytes, allow_trailing: bool = False):
    val, pos = _cbor(data)
    if pos != len(data) and not allow_trailing:
        raise WebAuthnError("CBOR mit überzähligen Daten")
    return val, pos


# --- Öffentlicher Schlüssel (COSE) -----------------------------------------
ALGS = (-7, -8, -257)   # ES256, EdDSA, RS256


def cose_to_spki(cose: dict) -> tuple[int, bytes]:
    alg, kty = cose.get(3), cose.get(1)
    if alg == -7 and kty == 2:
        if cose.get(-1) != 1:
            raise WebAuthnError("Nur P-256 wird unterstützt")
        x, y = cose.get(-2), cose.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
            raise WebAuthnError("EC-Schlüssel ungültig")
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + x + y)
    elif alg == -257 and kty == 3:
        n, e = cose.get(-1), cose.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes) and 256 <= len(n) <= 512):
            raise WebAuthnError("RSA-Schlüssel ungültig")
        key = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
    elif alg == -8 and kty == 1:
        x = cose.get(-2)
        if cose.get(-1) != 6 or not (isinstance(x, bytes) and len(x) == 32):
            raise WebAuthnError("Ed25519-Schlüssel ungültig")
        key = ed25519.Ed25519PublicKey.from_public_bytes(x)
    else:
        raise WebAuthnError("Schlüsselart nicht unterstützt")
    spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return alg, spki


def _verify_sig(alg: int, spki: bytes, sig: bytes, data: bytes) -> None:
    key = serialization.load_der_public_key(spki)
    try:
        if alg == -7 and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        elif alg == -257 and isinstance(key, rsa.RSAPublicKey):
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        elif alg == -8 and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(sig, data)
        else:
            raise WebAuthnError("Schlüsselart passt nicht")
    except InvalidSignature:
        raise WebAuthnError("Signatur ungültig") from None


# --- Gemeinsame Prüfungen ----------------------------------------------------
def _client_data(raw: bytes, typ: str, challenge: str, origin: str) -> None:
    try:
        cd = json.loads(raw)
    except ValueError:
        raise WebAuthnError("clientData ungültig") from None
    if not isinstance(cd, dict) or cd.get("type") != typ:
        raise WebAuthnError("falscher Typ")
    if not secrets.compare_digest(str(cd.get("challenge", "")), challenge):
        raise WebAuthnError("Challenge passt nicht (abgelaufen?)")
    if cd.get("origin") != origin:
        raise WebAuthnError("Falsche Herkunft")
    if cd.get("crossOrigin") is True:
        raise WebAuthnError("Fremde Einbettung nicht erlaubt")


def _auth_data(ad: bytes, rp_id: str) -> tuple[int, int]:
    if len(ad) < 37:
        raise WebAuthnError("authenticatorData zu kurz")
    if not secrets.compare_digest(ad[:32], hashlib.sha256(rp_id.encode()).digest()):
        raise WebAuthnError("RP-ID passt nicht")
    flags = ad[32]
    if not flags & 0x01:
        raise WebAuthnError("Benutzer nicht anwesend")
    if not flags & 0x04:
        raise WebAuthnError("Face ID / Gerätecode wurde nicht bestätigt")
    count = struct.unpack(">I", ad[33:37])[0]
    return flags, count


def new_challenge() -> str:
    return b64u(secrets.token_bytes(32))


def registration_options(user_id: int, username: str, rp_id: str, challenge: str, exclude: list[str]) -> dict:
    return {
        "challenge": challenge,
        "rp": {"name": "EarnWatch", "id": rp_id},
        "user": {"id": b64u(f"earnwatch-{user_id}".encode()), "name": username, "displayName": username},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in ALGS],
        "timeout": 120000,
        "attestation": "none",
        "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"},
        "excludeCredentials": [{"type": "public-key", "id": c} for c in exclude],
    }


def verify_registration(resp: dict, challenge: str, origin: str, rp_id: str) -> dict:
    client = unb64u(resp.get("clientDataJSON", ""))
    _client_data(client, "webauthn.create", challenge, origin)
    att, _ = cbor_decode(unb64u(resp.get("attestationObject", "")))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise WebAuthnError("attestationObject ungültig")
    ad = att["authData"]
    flags, count = _auth_data(ad, rp_id)
    if not flags & 0x40 or len(ad) < 55:
        raise WebAuthnError("Kein Schlüssel enthalten")
    clen = struct.unpack(">H", ad[53:55])[0]
    if not 16 <= clen <= 1023 or len(ad) < 55 + clen:
        raise WebAuthnError("Schlüssel-ID ungültig")
    cred_id = ad[55:55 + clen]
    cose, _ = cbor_decode(ad[55 + clen:], allow_trailing=bool(flags & 0x80))
    if not isinstance(cose, dict):
        raise WebAuthnError("Schlüssel ungültig")
    alg, spki = cose_to_spki(cose)
    return {"cred_id": b64u(cred_id), "public_key": b64u(spki), "alg": alg, "sign_count": count}


def verify_assertion(resp: dict, stored, challenge: str, origin: str) -> int:
    client = unb64u(resp.get("clientDataJSON", ""))
    _client_data(client, "webauthn.get", challenge, origin)
    ad = unb64u(resp.get("authenticatorData", ""))
    _, count = _auth_data(ad, stored["rp_id"])
    sig = unb64u(resp.get("signature", ""))
    _verify_sig(stored["alg"], unb64u(stored["public_key"]), sig, ad + hashlib.sha256(client).digest())
    # Zähler: 0 = Gerät zählt nicht (z. B. iCloud-Schlüsselbund). Sonst muss er steigen (Klon-Schutz).
    if (count or stored["sign_count"]) and count <= stored["sign_count"]:
        raise WebAuthnError("Signaturzähler zu klein – möglicher Klon")
    return count
