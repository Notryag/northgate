from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, credential: str) -> bytes:
        return self._fernet.encrypt(credential.encode())

    def decrypt(self, encrypted_credential: bytes) -> str:
        try:
            return self._fernet.decrypt(encrypted_credential).decode()
        except InvalidToken as exc:
            raise ValueError("Provider credential cannot be decrypted") from exc
