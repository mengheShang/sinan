"""Pick real or fake providers from SINAN_MODE.

Demo mode always uses fakes, so the demo needs no Google account or API key.
"""

from app.config import Settings
from app.providers.demo_data import DEMO_EMAIL, DEMO_MESSAGES, DEMO_RESPONSES
from app.providers.fakes import FakeExtractor, FakeMailReader
from app.providers.llm import Extractor
from app.providers.mail import MailReader


def mail_reader(settings: Settings) -> MailReader:
    if settings.sinan_mode == "demo":
        return FakeMailReader(DEMO_EMAIL, DEMO_MESSAGES)
    # The real read-only Gmail client comes in #10
    raise NotImplementedError("live mail reader is not implemented yet")


def extractor(settings: Settings) -> Extractor:
    if settings.sinan_mode == "demo":
        return FakeExtractor(DEMO_RESPONSES)
    # The real Anthropic client comes in #13
    raise NotImplementedError("live extractor is not implemented yet")
