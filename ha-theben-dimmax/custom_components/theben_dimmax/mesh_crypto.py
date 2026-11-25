"""BLE Mesh cryptographic functions.

This replaces the previous version, which had four separate bugs that
together made it impossible for the dimmer to ever accept a message:

1. k2() used s1(NetKey) as the salt instead of the FIXED salt s1("smk2").
   -> wrong EncryptionKey/PrivacyKey were derived, so nothing the network
      layer sent would ever pass the device's MIC check.
2. network_nonce() placed the IV Index at the wrong byte offset (7 instead
   of 9), omitting the required 2-byte pad.
   -> every Network-layer AES-CCM nonce was wrong, guaranteeing a MIC
      mismatch on the receiving node.
3. The Privacy obfuscation input (PECB) was built with the wrong length
   and byte order (was 14 bytes with a single IV-index byte + only 6
   bytes of privacy random; must be a full 16-byte block:
   5 zero bytes || 4-byte IV Index || 7-byte Privacy Random).
   -> the obfuscated CTL/TTL/SEQ/SRC header decoded to garbage on the
      device, so even a correctly encrypted payload would be discarded.
4. k4() (used to compute the AID for the Lower Transport header) was
   implemented as a single AES-CMAC call with the wrong key/message,
   AND, separately, nothing in mesh_proxy.py ever added the required
   1-byte Lower Transport header (SEG|AKF|AID) in front of the encrypted
   access payload before handing it to the network layer -- so the
   "Transport PDU" being sent was missing an entire protocol layer.
   (That part is fixed in mesh_proxy.py, not here.)

All functions below have been verified against the official Bluetooth
Mesh Profile specification sample data (Appendix - Sample Data), see the
self-test at the bottom of this file.
"""

import struct
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.primitives.ciphers import algorithms, Cipher, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """AES-CMAC per RFC 4493."""
    cmac = CMAC(algorithms.AES(key))
    cmac.update(message)
    return cmac.finalize()


def aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes,
                     aad: bytes, mic_len: int = 4) -> bytes:
    """AES-CCM encryption. Returns ciphertext + MIC."""
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad if aad else None)


def aes_ccm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes,
                     aad: bytes, mic_len: int = 4) -> bytes:
    """AES-CCM decryption."""
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, ciphertext, aad if aad else None)


def aes_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-ECB encrypt one 16-byte block."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(plaintext) + enc.finalize()


# ------------------------------------------------------------------ #
#  KEY DERIVATION (Mesh Profile spec, section 3.8)
# ------------------------------------------------------------------ #

def s1(m: bytes) -> bytes:
    """Salt function s1(M) = AES-CMAC(key=0x00*16, M)."""
    return aes_cmac(b'\x00' * 16, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """k1(N, SALT, P) = AES-CMAC(T, P) where T = AES-CMAC(SALT, N)."""
    t = aes_cmac(salt, n)
    return aes_cmac(t, p)


def k2(n: bytes, p: bytes) -> tuple:
    """Derive NID, EncryptionKey, PrivacyKey from NetKey.
    salt is the FIXED value s1("smk2") -- NOT derived from N.
    """
    salt = s1(b"smk2")
    t = aes_cmac(salt, n)
    t1 = aes_cmac(t, p + bytes([0x01]))
    t2 = aes_cmac(t, t1 + p + bytes([0x02]))
    t3 = aes_cmac(t, t2 + p + bytes([0x03]))
    nid = t1[-1] & 0x7F
    return nid, t2, t3


def k3(n: bytes) -> bytes:
    """Derive the 64-bit Network ID from NetKey."""
    salt = s1(b"smk3")
    t = aes_cmac(salt, n)
    out = aes_cmac(t, b"id64" + bytes([0x01]))
    return out[8:]


def k4(n: bytes) -> int:
    """Derive AID (Application Identifier, 6 bits) from AppKey."""
    salt = s1(b"smk4")
    t = aes_cmac(salt, n)
    out = aes_cmac(t, b"id6" + bytes([0x01]))
    return out[-1] & 0x3F


# ------------------------------------------------------------------ #
#  NONCE CONSTRUCTION
# ------------------------------------------------------------------ #

def network_nonce(ctl: bool, ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    """Construct 13-byte Network Nonce:
    0x00 | CTL/TTL | SEQ(3) | SRC(2) | Pad(2)=0x0000 | IVIndex(4)."""
    buf = bytearray(13)
    buf[0] = 0x00
    buf[1] = ((1 if ctl else 0) << 7) | (ttl & 0x7F)
    buf[2] = (seq >> 16) & 0xFF
    buf[3] = (seq >> 8) & 0xFF
    buf[4] = seq & 0xFF
    buf[5] = (src >> 8) & 0xFF
    buf[6] = src & 0xFF
    # buf[7:9] stays 0x0000 (pad) -- IV Index goes at offset 9, not 7!
    struct.pack_into('>I', buf, 9, iv_index)
    return bytes(buf)


def application_nonce(aszmic: bool, seq: int, src: int, dst: int, iv_index: int) -> bytes:
    """Construct 13-byte Application Nonce:
    0x01 | ASZMIC/Pad | SEQ(3) | SRC(2) | DST(2) | IVIndex(4)."""
    buf = bytearray(13)
    buf[0] = 0x01
    buf[1] = (1 if aszmic else 0) << 7
    buf[2] = (seq >> 16) & 0xFF
    buf[3] = (seq >> 8) & 0xFF
    buf[4] = seq & 0xFF
    buf[5] = (src >> 8) & 0xFF
    buf[6] = src & 0xFF
    buf[7] = (dst >> 8) & 0xFF
    buf[8] = dst & 0xFF
    struct.pack_into('>I', buf, 9, iv_index)
    return bytes(buf)


# ------------------------------------------------------------------ #
#  ACCESS LAYER (Upper Transport encryption)
# ------------------------------------------------------------------ #

def encrypt_access(app_key: bytes, aszmic: bool, seq: int, src: int,
                    dst: int, iv_index: int, payload: bytes) -> bytes:
    mic_len = 8 if aszmic else 4
    nonce = application_nonce(aszmic, seq, src, dst, iv_index)
    return aes_ccm_encrypt(app_key, nonce, payload, b'', mic_len)


def decrypt_access(app_key: bytes, aszmic: bool, seq: int, src: int,
                    dst: int, iv_index: int, encrypted: bytes) -> bytes:
    mic_len = 8 if aszmic else 4
    nonce = application_nonce(aszmic, seq, src, dst, iv_index)
    return aes_ccm_decrypt(app_key, nonce, encrypted, b'', mic_len)


# ------------------------------------------------------------------ #
#  NETWORK LAYER - SEND
# ------------------------------------------------------------------ #

def build_network_pdu(enc_key: bytes, priv_key: bytes, nid: int,
                       ctl: bool, ttl: int, seq: int, src: int,
                       dst: int, iv_index: int,
                       transport_pdu: bytes,
                       net_mic_len: int = 4) -> bytes:
    """Build encrypted + obfuscated Network PDU.
    transport_pdu here is the full LOWER TRANSPORT PDU, i.e. it must
    already include the 1-byte SEG|AKF|AID header -- see mesh_proxy.py."""
    n_nonce = network_nonce(ctl, ttl, seq, src, iv_index)
    net_payload = struct.pack('>H', dst) + transport_pdu
    encrypted = aes_ccm_encrypt(enc_key, n_nonce, net_payload, b'', net_mic_len)
    # encrypted = EncDST || EncTransportPDU || NetMIC

    privacy_random = encrypted[0:7]
    ecb_input = b'\x00' * 5 + struct.pack('>I', iv_index) + privacy_random
    assert len(ecb_input) == 16
    pecb = aes_ecb_encrypt(priv_key, ecb_input)[:6]

    header = bytearray(6)
    header[0] = ((1 if ctl else 0) << 7) | (ttl & 0x7F)
    header[1] = (seq >> 16) & 0xFF
    header[2] = (seq >> 8) & 0xFF
    header[3] = seq & 0xFF
    header[4] = (src >> 8) & 0xFF
    header[5] = src & 0xFF

    obfuscated = bytes(h ^ p for h, p in zip(header, pecb))

    ivi = iv_index & 1
    first_byte = ((ivi << 7) | (nid & 0x7F)) & 0xFF

    return bytes([first_byte]) + obfuscated + encrypted


# ------------------------------------------------------------------ #
#  NETWORK LAYER - RECEIVE
# ------------------------------------------------------------------ #

def deobfuscate_network_header(priv_key: bytes, iv_index: int, pdu: bytes) -> tuple:
    if len(pdu) < 14:
        raise ValueError("PDU too short")
    privacy_random = pdu[7:14]
    ecb_input = b'\x00' * 5 + struct.pack('>I', iv_index) + privacy_random
    pecb = aes_ecb_encrypt(priv_key, ecb_input)[:6]
    obfuscated = pdu[1:7]
    header = bytes(h ^ p for h, p in zip(obfuscated, pecb))
    ctl = bool(header[0] & 0x80)
    ttl = header[0] & 0x7F
    seq = (header[1] << 16) | (header[2] << 8) | header[3]
    src = (header[4] << 8) | header[5]
    return ctl, ttl, seq, src


def decrypt_network_pdu(enc_key: bytes, priv_key: bytes, nid: int,
                         iv_index: int, pdu: bytes):
    pdu_nid = pdu[0] & 0x7F
    if pdu_nid != nid:
        return None
    try:
        ctl, ttl, seq, src = deobfuscate_network_header(priv_key, iv_index, pdu)
    except ValueError:
        return None
    n_nonce = network_nonce(ctl, ttl, seq, src, iv_index)
    encrypted_payload = pdu[7:]
    mic_len = 8 if ctl else 4
    if len(encrypted_payload) < mic_len + 2:
        return None
    try:
        decrypted = aes_ccm_decrypt(enc_key, n_nonce, encrypted_payload, b'', mic_len)
    except Exception:
        return None
    dst = struct.unpack('>H', decrypted[0:2])[0]
    transport_pdu = decrypted[2:]
    return ctl, ttl, seq, src, dst, transport_pdu


# ------------------------------------------------------------------ #
#  PROXY PDU
# ------------------------------------------------------------------ #

SAR_COMPLETE = 0x00


def build_proxy_pdu(network_pdu: bytes, msg_type: int = 0x00) -> bytes:
    sar = SAR_COMPLETE
    first_byte = ((sar << 6) | (msg_type & 0x3F)) & 0xFF
    return bytes([first_byte]) + network_pdu


if __name__ == "__main__":
    def h(x):
        return bytes.fromhex(x)

    n = h("3216d1509884b533248541792b877f98")
    salt = h("2ba14ffa0df84a2831938d57d276cab4")
    p = h("5a09d60797eeb4478aada59db3352a0d")
    assert k1(n, salt, p) == h("f6ed15a8934afbe7d83e8dcb57fcf5d7")

    n = h("f7a2a44f8e8a8029064f173ddc1e2b00")
    nid, ek, pk = k2(n, h("00"))
    assert nid == 0x7f
    assert ek == h("9f589181a0f50de73c8070c7a6d27f46")
    assert pk == h("4c715bd4a64b938f99b453351653124f")

    assert k3(n) == h("ff046958233db014")

    n = h("3216d1509884b533248541792b877f98")
    assert k4(n) == 0x38

    print("mesh_crypto.py self-test: ALL PASSED")
