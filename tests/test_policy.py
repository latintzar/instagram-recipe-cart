import pytest
from social_cart.policy import reel_url

@pytest.mark.parametrize("url", [
    "http://instagram.com/reel/ABC123",
    "https://instagram.com.evil.example/reel/ABC123",
    "https://instagram.com@evil.example/reel/ABC123",
    "https://www.instagram.com:8443/reel/ABC123",
    "https://127.0.0.1/reel/ABC123",
    "https://lookaside.fbsbx.com.evil.example/ig_messaging_cdn?asset_id=1",
    "https://lookaside.fbsbx.com/ig_messaging_cdn?asset_id=1&asset_id=2",
])
def test_reel_url_rejects_untrusted_targets(url):
    with pytest.raises(ValueError):
        reel_url(url)

def test_valid_meta_reel_urls():
    assert reel_url("https://instagram.com/reel/ABC123/?tracking=1") == "https://www.instagram.com/reel/ABC123/"
    assert reel_url("https://lookaside.fbsbx.com/ig_messaging_cdn?asset_id=123").endswith("asset_id=123")
