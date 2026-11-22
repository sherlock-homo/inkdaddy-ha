import unittest

from inkdaddy_hub.services.matter_onboarding import (
    CommissioningFlow,
    DiscoveryCapability,
    MatterOnboardingPayload,
    default_matter_onboarding_codes,
    generate_manual_code,
    generate_qr_payload,
)


class MatterOnboardingTests(unittest.TestCase):
    def test_qr_payload_matches_sdk_vector(self):
        payload = MatterOnboardingPayload(
            setup_pin=20202021,
            discriminator=0xF00,
            vendor_id=0x235A,
            product_id=0x4E4B,
            commissioning_flow=CommissioningFlow.STANDARD,
            discovery_capabilities=(DiscoveryCapability.BLE,),
        )
        self.assertEqual(generate_qr_payload(payload), "MT:W0GU2OTB00KA0648G00")

    def test_manual_code_matches_sdk_vector(self):
        payload = MatterOnboardingPayload(
            setup_pin=20202021,
            discriminator=0xF00,
            vendor_id=0x235A,
            product_id=0x4E4B,
            commissioning_flow=CommissioningFlow.STANDARD,
            discovery_capabilities=(DiscoveryCapability.BLE,),
        )
        self.assertEqual(generate_manual_code(payload), "34970112332")

    def test_default_codes_align_with_sisdk_platform_template(self):
        codes = default_matter_onboarding_codes()
        self.assertTrue(codes.qr_payload.startswith("MT:"))
        self.assertEqual(codes.setup_pin, 20202021)
        self.assertEqual(codes.discriminator, 0xF00)
        self.assertEqual(codes.vendor_id, 0xFFF1)
        self.assertEqual(codes.product_id, 0x8014)
        self.assertEqual(codes.manual_code, "34970112332")


if __name__ == "__main__":
    unittest.main()
