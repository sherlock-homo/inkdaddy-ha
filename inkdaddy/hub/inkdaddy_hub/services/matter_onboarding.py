from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class CommissioningFlow(IntEnum):
    STANDARD = 0
    USER_ACTION_REQUIRED = 1
    CUSTOM = 2


class DiscoveryCapability(IntEnum):
    SOFT_AP = 0
    BLE = 1
    ON_NETWORK = 2
    WIFI_PAF = 3
    NFC = 4


@dataclass(frozen=True)
class MatterOnboardingPayload:
    setup_pin: int = 20202021
    discriminator: int = 0xF00
    vendor_id: int = 0xFFF1
    product_id: int = 0x8014
    commissioning_flow: CommissioningFlow = CommissioningFlow.STANDARD
    discovery_capabilities: tuple[DiscoveryCapability, ...] = (DiscoveryCapability.BLE,)
    version: int = 0


@dataclass(frozen=True)
class MatterOnboardingCodes:
    qr_payload: str
    manual_code: str
    setup_pin: int
    discriminator: int
    vendor_id: int
    product_id: int


_BASE38_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."
_BASE38_CHARS_PER_CHUNK = {1: 2, 2: 4, 3: 5}

_INVALID_SETUP_PINS = {
    0,
    11111111,
    22222222,
    33333333,
    44444444,
    55555555,
    66666666,
    77777777,
    88888888,
    99999999,
    12345678,
    87654321,
}

_VERHOEFF_MULTIPLY_TABLE = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    1,
    2,
    3,
    4,
    0,
    6,
    7,
    8,
    9,
    5,
    2,
    3,
    4,
    0,
    1,
    7,
    8,
    9,
    5,
    6,
    3,
    4,
    0,
    1,
    2,
    8,
    9,
    5,
    6,
    7,
    4,
    0,
    1,
    2,
    3,
    9,
    5,
    6,
    7,
    8,
    5,
    9,
    8,
    7,
    6,
    0,
    4,
    3,
    2,
    1,
    6,
    5,
    9,
    8,
    7,
    1,
    0,
    4,
    3,
    2,
    7,
    6,
    5,
    9,
    8,
    2,
    1,
    0,
    4,
    3,
    8,
    7,
    6,
    5,
    9,
    3,
    2,
    1,
    0,
    4,
    9,
    8,
    7,
    6,
    5,
    4,
    3,
    2,
    1,
    0,
)
_VERHOEFF_PERM_TABLE = (1, 5, 7, 6, 2, 8, 3, 0, 9, 4)


def default_matter_onboarding_codes() -> MatterOnboardingCodes:
    return matter_onboarding_codes(MatterOnboardingPayload())


def matter_onboarding_codes(payload: MatterOnboardingPayload) -> MatterOnboardingCodes:
    _validate_payload(payload)
    return MatterOnboardingCodes(
        qr_payload=generate_qr_payload(payload),
        manual_code=generate_manual_code(payload),
        setup_pin=payload.setup_pin,
        discriminator=payload.discriminator,
        vendor_id=payload.vendor_id,
        product_id=payload.product_id,
    )


def generate_qr_payload(payload: MatterOnboardingPayload) -> str:
    _validate_payload(payload)
    bits = bytearray(11)
    offset = 0
    for value, width in (
        (payload.version, 3),
        (payload.vendor_id, 16),
        (payload.product_id, 16),
        (int(payload.commissioning_flow), 2),
        (_rendezvous_info(payload.discovery_capabilities), 8),
        (payload.discriminator, 12),
        (payload.setup_pin, 27),
        (0, 4),
    ):
        _populate_lsb_bits(bits, offset, value, width)
        offset += width
    return f"MT:{_base38_encode(bytes(bits))}"


def generate_manual_code(payload: MatterOnboardingPayload) -> str:
    _validate_payload(payload)
    short_discriminator = _short_discriminator(payload.discriminator)
    has_vendor_product = payload.commissioning_flow != CommissioningFlow.STANDARD
    chunk1 = ((short_discriminator >> 2) & 0x03) | (0x04 if has_vendor_product else 0)
    chunk2 = (payload.setup_pin & 0x3FFF) | ((short_discriminator & 0x03) << 14)
    chunk3 = (payload.setup_pin >> 14) & 0x1FFF

    manual = f"{chunk1:01d}{chunk2:05d}{chunk3:04d}"
    if has_vendor_product:
        manual += f"{payload.vendor_id:05d}{payload.product_id:05d}"
    return manual + _verhoeff_check_char(manual)


def _validate_payload(payload: MatterOnboardingPayload) -> None:
    if not 0 <= payload.version < 8:
        raise ValueError("Matter QR payload version must fit in 3 bits.")
    if not 1 <= payload.vendor_id <= 0xFFF4:
        raise ValueError("Matter vendor ID must be 1..0xFFF4.")
    if not 1 <= payload.product_id <= 0xFFFF:
        raise ValueError("Matter product ID must be 1..0xFFFF.")
    if payload.commissioning_flow not in (
        CommissioningFlow.STANDARD,
        CommissioningFlow.USER_ACTION_REQUIRED,
        CommissioningFlow.CUSTOM,
    ):
        raise ValueError("Matter commissioning flow must be standard, user-action, or custom.")
    if not 0 <= payload.discriminator <= 0xFFF:
        raise ValueError("Matter discriminator must fit in 12 bits.")
    if not 1 <= payload.setup_pin <= 99_999_998 or payload.setup_pin in _INVALID_SETUP_PINS:
        raise ValueError("Matter setup PIN is invalid or reserved.")
    if not payload.discovery_capabilities:
        raise ValueError("At least one Matter discovery capability is required.")


def _populate_lsb_bits(bits: bytearray, offset: int, value: int, width: int) -> None:
    if value >= (1 << width):
        raise ValueError("Matter QR field does not fit in its bit width.")
    for bit in range(width):
        if value & (1 << bit):
            index = offset + bit
            bits[index // 8] |= 1 << (index % 8)


def _rendezvous_info(capabilities: tuple[DiscoveryCapability, ...]) -> int:
    value = 0
    for capability in capabilities:
        value |= 1 << int(capability)
    return value


def _short_discriminator(discriminator: int) -> int:
    return (discriminator >> 8) & 0x0F


def _base38_encode(data: bytes) -> str:
    out: list[str] = []
    for offset in range(0, len(data), 3):
        chunk = data[offset : offset + 3]
        value = sum(byte << (8 * index) for index, byte in enumerate(chunk))
        for _ in range(_BASE38_CHARS_PER_CHUNK[len(chunk)]):
            out.append(_BASE38_ALPHABET[value % 38])
            value //= 38
    return "".join(out)


def _verhoeff_check_char(value: str) -> str:
    checksum = 0
    for iteration, char in enumerate(reversed(value), start=1):
        digit = int(char)
        permuted = _verhoeff_permute(digit, iteration)
        checksum = _VERHOEFF_MULTIPLY_TABLE[checksum * 10 + permuted]
    return str(_verhoeff_dihedral_invert(checksum))


def _verhoeff_permute(value: int, iterations: int) -> int:
    for _ in range(iterations):
        value = _VERHOEFF_PERM_TABLE[value]
    return value


def _verhoeff_dihedral_invert(value: int) -> int:
    if 0 < value < 5:
        return 5 - value
    return value
