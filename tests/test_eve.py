import unittest

from app.services.eve import token_scopes


class EveScopeTests(unittest.TestCase):
    def test_prefers_token_scope_when_present(self):
        self.assertEqual(
            token_scopes(
                {"scope": "esi-skills.read_skills.v1 esi-assets.read_assets.v1"},
                {"Scopes": ["esi-wallet.read_character_wallet.v1"]},
            ),
            ["esi-skills.read_skills.v1", "esi-assets.read_assets.v1"],
        )

    def test_uses_verified_identity_scopes_when_token_omits_scope(self):
        self.assertEqual(
            token_scopes(
                {},
                {
                    "Scopes": [
                        "esi-skills.read_skills.v1",
                        "esi-skills.read_skillqueue.v1",
                    ]
                },
            ),
            ["esi-skills.read_skills.v1", "esi-skills.read_skillqueue.v1"],
        )

    def test_empty_scope_is_an_empty_list(self):
        self.assertEqual(token_scopes({}, {}), [])


if __name__ == "__main__":
    unittest.main()
