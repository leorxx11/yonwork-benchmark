from __future__ import annotations

import unittest

from runner.catalog import CatalogError, ModelChoice, resolve_model_choice
from runner.client import ChatClient
from runner.discovery import HostEndpoint


DEFAULT = ModelChoice(
    provider_account_id="yonyou-default-auto",
    model_id="deepseek-v4-flash",
    display_name="默认模型",
    choice_id="yonyou-default-auto::deepseek-v4-flash",
    is_default=True,
)
NEWAPI = ModelChoice(
    provider_account_id="custom-66270c13-bd22-4ae8-a96f-8b9e3deb3d66",
    model_id="deepseek-flash",
    display_name="newapi",
    choice_id="custom-66270c13-bd22-4ae8-a96f-8b9e3deb3d66::deepseek-flash",
)


class SelectionShapeTests(unittest.TestCase):
    def test_selection_uses_the_field_names_the_app_actually_reads(self) -> None:
        """写成 provider/model 或 modelRef 不会报错，只会静默回落到默认模型。"""
        self.assertEqual(
            {
                "providerAccountId": "custom-66270c13-bd22-4ae8-a96f-8b9e3deb3d66",
                "modelId": "deepseek-flash",
            },
            NEWAPI.selection,
        )

    def test_payload_carries_model_selection(self) -> None:
        client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"), model_choice=NEWAPI)
        payload = client._payload("bench-1", "hi", "agent:main:bench-1")
        self.assertEqual(NEWAPI.selection, payload["modelSelection"])

    def test_payload_omits_model_selection_when_unset(self) -> None:
        client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"))
        self.assertNotIn("modelSelection", client._payload("bench-1", "hi", "agent:main:bench-1"))


class ResolveTests(unittest.TestCase):
    def test_matches_display_name_model_id_and_choice_id(self) -> None:
        choices = [DEFAULT, NEWAPI]
        self.assertEqual(NEWAPI, resolve_model_choice(choices, "newapi"))
        self.assertEqual(NEWAPI, resolve_model_choice(choices, "deepseek-flash"))
        self.assertEqual(NEWAPI, resolve_model_choice(choices, NEWAPI.choice_id))
        self.assertEqual(DEFAULT, resolve_model_choice(choices, "默认模型"))

    def test_model_id_wins_over_partial_confusion(self) -> None:
        """deepseek-flash 和 deepseek-v4-flash 是两个模型，别匹配串了。"""
        self.assertEqual(DEFAULT, resolve_model_choice([DEFAULT, NEWAPI], "deepseek-v4-flash"))

    def test_unknown_model_lists_the_candidates(self) -> None:
        with self.assertRaises(CatalogError) as caught:
            resolve_model_choice([DEFAULT, NEWAPI], "gpt-9")
        self.assertIn("newapi", str(caught.exception))

    def test_ambiguous_match_is_rejected(self) -> None:
        twin = ModelChoice(provider_account_id="other", model_id="deepseek-flash", display_name="备用")
        with self.assertRaisesRegex(CatalogError, "多个"):
            resolve_model_choice([NEWAPI, twin], "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
