from ebms_server import accounts
from ebms_server import constant
from ebms_server.accounts import Profile

def test_is_admin_email(monkeypatch):
    # Setup test ADMIN_EMAILS
    monkeypatch.setattr(constant, "ADMIN_EMAILS", {"admin@example.com", "superadmin@example.com"})

    # True cases
    profile1 = Profile(oauth="google", oauth_user_id="1", email="admin@example.com", email_verified=True, name="Admin")
    assert accounts._is_admin_email(profile1) is True

    # Case insensitive
    profile2 = Profile(oauth="google", oauth_user_id="1", email="ADMIN@example.com", email_verified=True, name="Admin")
    assert accounts._is_admin_email(profile2) is True

    # False cases
    # Not in list
    profile3 = Profile(oauth="google", oauth_user_id="1", email="user@example.com", email_verified=True, name="User")
    assert accounts._is_admin_email(profile3) is False

    # In list but not verified
    profile4 = Profile(oauth="google", oauth_user_id="1", email="admin@example.com", email_verified=False, name="Admin")
    assert accounts._is_admin_email(profile4) is False

    # Email is None
    profile5 = Profile(oauth="google", oauth_user_id="1", email=None, email_verified=True, name="NoEmail")
    assert accounts._is_admin_email(profile5) is False

    # Email is empty string
    profile6 = Profile(oauth="google", oauth_user_id="1", email="", email_verified=True, name="EmptyEmail")
    assert accounts._is_admin_email(profile6) is False
