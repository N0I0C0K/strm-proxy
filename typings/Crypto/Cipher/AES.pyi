"""Types for the AES operations used by the upstream signature/URL adapters."""

MODE_ECB: int
MODE_CBC: int
block_size: int

class AESCipher:
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...

def new(key: bytes, mode: int, iv: bytes = ...) -> AESCipher: ...
