"""Pick real or fake providers from SINAN_MODE.

Demo mode always uses fakes, so the demo needs no Google account or API key.
"""

from pydantic import SecretStr

from app.config import Settings
from app.crypto import TokenCipher
from app.providers.anthropic_llm import AnthropicExtractor, AnthropicLinkPicker, AnthropicSuggester
from app.providers.demo_data import DEMO_EMAIL, DEMO_MESSAGES, DEMO_RESPONSES
from app.providers.fakes import FakeExtractor, FakeLinkPicker, FakeMailReader, FakeSuggester
from app.providers.gmail import GmailReader
from app.providers.google_oauth import GoogleOAuth
from app.providers.llm import Extractor, LinkPicker, Suggester
from app.providers.mail import MailReader


def mail_reader(settings: Settings, access_token: SecretStr | None = None) -> MailReader:
    """The demo mailbox, or a read-only Gmail client for one access token."""
    if settings.sinan_mode == "demo":
        return FakeMailReader(DEMO_EMAIL, DEMO_MESSAGES)
    if access_token is None:
        raise ValueError("a live mail reader needs an access token")
    return GmailReader(access_token)


def google_oauth(settings: Settings) -> GoogleOAuth:
    """The real Google OAuth client. Raises ValueError when it is not configured."""
    client_id = settings.google_client_id
    secret = settings.google_client_secret
    if client_id is None or secret is None:
        raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required in live mode")
    return GoogleOAuth(client_id, secret, settings.sinan_oauth_redirect_uri)


def token_cipher(settings: Settings) -> TokenCipher:
    key = settings.sinan_encryption_key
    if key is None:
        raise ValueError("SINAN_ENCRYPTION_KEY is required in live mode")
    return TokenCipher(key.get_secret_value())


def extractor(settings: Settings) -> Extractor | None:
    """The fake model in demo mode; Claude in live mode, or None without a key."""
    if settings.sinan_mode == "demo":
        return FakeExtractor(DEMO_RESPONSES)
    key = settings.anthropic_api_key
    if key is None:
        return None
    return AnthropicExtractor(key, settings.sinan_llm_model)


def link_picker(settings: Settings) -> LinkPicker | None:
    """The link step (ADR 0019): a fake that never links in demo mode; Claude
    with the extraction model in live mode, or None without a key."""
    if settings.sinan_mode == "demo":
        return FakeLinkPicker()
    key = settings.anthropic_api_key
    if key is None:
        return None
    return AnthropicLinkPicker(key, settings.sinan_llm_model)


def suggester(settings: Settings) -> Suggester | None:
    """Suggested due times (ADR 0022, decision 4): a fake that suggests
    nothing in demo mode; Claude in live mode, or None without a key."""
    if settings.sinan_mode == "demo":
        return FakeSuggester()
    key = settings.anthropic_api_key
    if key is None:
        return None
    return AnthropicSuggester(key, settings.sinan_llm_model)
