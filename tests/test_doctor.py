import sys
from social_cart import worker

def test_doctor_uses_names_only_and_makes_no_network_calls(monkeypatch, capsys, tmp_path):
    env = {
      "SOCIAL_CART_DB":str(tmp_path / "private.db"), "INSTAGRAM_APP_SECRET":"test-secret",
      "INSTAGRAM_VERIFY_TOKEN":"test-verify", "INSTAGRAM_PAGE_ACCESS_TOKEN":"IGAA-test",
      "OPENROUTER_API_KEY":"test-model-key", "GROCERY_ORIGIN":"https://grocery.example",
      "CART_IDENTITY_SECRET":"x"*40, "VITRINA_MCP_URL":"https://grocery.example/mcp/",
      "VITRINA_MCP_BEARER_TOKEN":"test-bearer", "CHECKOUT_ORIGIN":"https://grocery.example"
    }
    for k,v in env.items(): monkeypatch.setenv(k,v)
    monkeypatch.setattr(sys, "argv", ["worker","--doctor"])
    worker.main()
    output = capsys.readouterr().out
    assert "no provider calls" in output
    assert all(v not in output for v in env.values())
    assert not (tmp_path / "private.db").exists()
