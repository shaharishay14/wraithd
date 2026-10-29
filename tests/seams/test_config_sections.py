"""Config sections: each module's settings in its own [table], with defaults."""

import logging

import pytest

from local_agent import config as config_module
from local_agent.config import CORE_TABLES, SECTIONS, Config, ConfigError, Section, load_config, load_sections


def check_hours(values):
    if not 0 <= values['quiet_from'] < 24:
        raise ConfigError('[heartbeat] quiet_from must be an hour from 0 to 23.')


DEMO = {'heartbeat': Section({'enabled': False, 'every_minutes': 30, 'quiet_from': 22, 'ratio': 0.5,
                              'chat_id': None, 'tools': ['memory_search']}, check_hours)}


def test_defaults_apply_without_a_table():
    assert load_sections({}, DEMO)['heartbeat'] == DEMO['heartbeat'].defaults


def test_a_table_overrides_its_defaults():
    found = load_sections({'heartbeat': {'enabled': True, 'every_minutes': 5, 'ratio': 1}}, DEMO)['heartbeat']
    assert found['enabled'] is True and found['every_minutes'] == 5 and found['ratio'] == 1
    assert found['quiet_from'] == 22  # the rest keep their defaults


@pytest.mark.parametrize('values, message', [
    ({'enabled': 'yes'}, r'\[heartbeat\] enabled must be bool'),
    ({'every_minutes': True}, r'\[heartbeat\] every_minutes must be int'),
    ({'every_minutes': 1.5}, r'\[heartbeat\] every_minutes must be int'),
    ({'tools': 'memory_search'}, r'\[heartbeat\] tools must be list'),
    ({'quiet_from': 25}, r'quiet_from must be an hour'),  # the section's own check
])
def test_wrong_values_are_config_errors(values, message):
    with pytest.raises(ConfigError, match=message):
        load_sections({'heartbeat': values}, DEMO)


def test_a_default_of_none_takes_any_value():
    for value in (12345, 'abc', [1], None):
        assert load_sections({'heartbeat': {'chat_id': value}}, DEMO)['heartbeat']['chat_id'] == value


def test_an_unknown_key_is_ignored_with_one_warning(caplog):
    config_module._warned.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            found = load_sections({'heartbeat': {'evrey_minutes': 5}}, DEMO)['heartbeat']
    assert 'evrey_minutes' not in found and found['every_minutes'] == 30
    assert caplog.text.count('[heartbeat] evrey_minutes is not a setting') == 1


def test_a_section_cannot_take_a_core_table():
    with pytest.raises(ConfigError, match='core settings table'):
        load_sections({}, {'daemon': Section({'x': 1})})
    with pytest.raises(ConfigError, match='must be a table'):
        load_sections({'heartbeat': 5}, DEMO)
    assert not set(SECTIONS) & set(CORE_TABLES)


def test_config_section_reads_the_file(home, monkeypatch):
    monkeypatch.setattr(config_module, 'SECTIONS', DEMO)
    home.config_path.write_text(home.config_path.read_text() + '\n[heartbeat]\nenabled = true\n')
    config = load_config(home)
    assert config.section('heartbeat')['enabled'] is True and config.section('heartbeat')['every_minutes'] == 30
    assert Config().section('heartbeat') == DEMO['heartbeat'].defaults  # a Config built in code: the defaults
