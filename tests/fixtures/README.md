# Test Fixtures

`tests/fixtures/bundles/feedback_case` contains a curated mini-bundle used by the feedback resolver tests. The normalized leads intentionally include a low-confidence record (`row_hotglue`) whose company_name is incorrect. The paired `youcom_verified.json` and `tavily_verified.json` files include deterministic article evidence mentioning "Hotglue" across multiple domains so the resolver can promote that entity. These fixtures remain fully offline/deterministic and should only be updated alongside matching golden expectations.
