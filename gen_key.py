"""
Run once: python gen_key.py
Writes the private key directly to kalshi-keys/test2.txt (no copy-paste needed).
Prints only the public key — paste that into Kalshi when creating your API key.
"""
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from pathlib import Path

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

public_pem = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

# Write private key directly — no copy-paste risk
key_file = Path(__file__).resolve().parent.parent / "kalshi-keys" / "test2.txt"
key_file.write_bytes(private_pem)
print(f"Private key written to: {key_file}")

# Write public key to a text file so you can copy from Notepad (not CMD)
pub_file = Path(__file__).resolve().parent / "public_key_for_kalshi.txt"
pub_file.write_text(public_pem, encoding="utf-8")
print(f"Public key written to:  {pub_file}")
print()
print("Next steps:")
print("1. Open public_key_for_kalshi.txt in Notepad, Ctrl+A, Ctrl+C")
print("2. Paste into Kalshi Create key form -> Read/Write -> Create")
print("3. Copy the new UUID from Kalshi -> save to kalshi-keys/kalshi_api_key")
