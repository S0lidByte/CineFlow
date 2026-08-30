from unittest.mock import MagicMock

from program.program import Program
from program.services.scrapers import Scraping


def test_scraping_reinitialize_retries_only_enabled_unavailable_services():
    unavailable = MagicMock(enabled=True, initialized=False)
    unavailable._initialize.side_effect = lambda: setattr(unavailable, "initialized", True)
    disabled = MagicMock(enabled=False, initialized=False)
    healthy = MagicMock(enabled=True, initialized=True)

    scraping = Scraping.__new__(Scraping)
    scraping.services = {object: unavailable, str: disabled, int: healthy}
    scraping.initialized_services = [healthy]
    scraping.initialized = True

    assert scraping.reinitialize() is True
    unavailable._initialize.assert_called_once_with()
    disabled._initialize.assert_not_called()
    assert scraping.initialized_services == [unavailable, healthy]
    assert scraping.initialized is True


def test_scraping_reinitialize_recovers_from_no_initial_services():
    unavailable = MagicMock(enabled=True, initialized=False)
    unavailable._initialize.side_effect = lambda: setattr(unavailable, "initialized", True)

    scraping = Scraping.__new__(Scraping)
    scraping.services = {object: unavailable}
    scraping.initialized_services = []
    scraping.initialized = False

    assert scraping.reinitialize() is True
    assert scraping.initialized_services == [unavailable]
    assert scraping.initialized is True


def test_program_recovers_scraping_without_rebuilding_services():
    program = Program()
    scraping = MagicMock(initialized=False)
    scraping.reinitialize.return_value = True
    program.services = MagicMock(scraping=scraping)

    program._recover_core_services()

    scraping.reinitialize.assert_called_once_with()


def test_program_skips_recovery_when_scraping_is_healthy():
    program = Program()
    scraping = MagicMock(initialized=True)
    program.services = MagicMock(scraping=scraping)

    program._recover_core_services()

    scraping.reinitialize.assert_not_called()
