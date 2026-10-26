"""
Voice transcription providers (voice.py): each one's request and error mapping against a fake API, local whisper
faked (never installed or run in tests), and which provider "auto" picks.
"""

import httpx
import pytest

from local_agent import voice
from local_agent.config import ConfigError, Home, load_config, set_key

OPENAI_KEY, ELEVEN_KEY = 'sk-test-voice-0123456789', 'eleven-test-key-0123456789'


def api(requests, status=200, body=None):
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'text': ' Hello there. '} if body is None else body)
    return httpx.MockTransport(handle)


def configured(home, transcribe):
    home.config_path.write_text(f'[voice]\ntranscribe = "{transcribe}"\n')
    return load_config(home)


async def test_openai_sends_the_audio_to_gpt_4o_mini_transcribe():
    requests = []
    text = await voice.OpenAITranscriber(OPENAI_KEY, api(requests)).transcribe(b'OggS audio', 'voice.ogg', 'audio/ogg')
    assert text == 'Hello there.'
    [request] = requests
    assert str(request.url) == 'https://api.openai.com/v1/audio/transcriptions' and request.method == 'POST'
    assert request.headers['authorization'] == f'Bearer {OPENAI_KEY}'
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in request.content
    assert b'filename="voice.ogg"' in request.content and b'OggS audio' in request.content


async def test_elevenlabs_sends_the_audio_to_scribe():
    requests = []
    text = await voice.ElevenLabsTranscriber(ELEVEN_KEY, api(requests)).transcribe(b'OggS', 'voice.ogg', 'audio/ogg')
    assert text == 'Hello there.'
    [request] = requests
    assert str(request.url) == 'https://api.elevenlabs.io/v1/speech-to-text'
    assert request.headers['xi-api-key'] == ELEVEN_KEY and 'authorization' not in request.headers
    assert b'name="model_id"\r\n\r\nscribe_v1' in request.content


@pytest.mark.parametrize('cls,key,name', [(voice.OpenAITranscriber, OPENAI_KEY, 'OpenAI'),
                                          (voice.ElevenLabsTranscriber, ELEVEN_KEY, 'ElevenLabs')])
@pytest.mark.parametrize('status,message', [(401, 'rejected the key (HTTP 401)'),
                                            (429, 'is rate limited or out of credit right now (HTTP 429)'),
                                            (500, 'transcription failed (HTTP 500)')])
async def test_errors_are_mapped_and_never_show_the_key(cls, key, name, status, message):
    with pytest.raises(voice.TranscriptionFailed) as failed:
        await cls(key, api([], status=status)).transcribe(b'x', 'voice.ogg', 'audio/ogg')
    assert str(failed.value) == f'{name} {message}.' and key not in str(failed.value)


async def test_no_answer_or_no_transcript_is_a_failure():
    def refuse(request):
        raise httpx.ConnectError('down')
    with pytest.raises(voice.TranscriptionFailed, match='did not respond'):
        await voice.OpenAITranscriber(OPENAI_KEY, httpx.MockTransport(refuse)).transcribe(b'x', 'v.ogg', 'audio/ogg')
    with pytest.raises(voice.TranscriptionFailed, match='no transcript'):
        await voice.OpenAITranscriber(OPENAI_KEY, api([], body={'error': None})).transcribe(b'x', 'v.ogg', 'audio/ogg')


async def test_local_whisper_runs_off_the_event_loop_and_maps_its_errors(monkeypatch):
    monkeypatch.setattr(voice.WhisperTranscriber, '_transcribe', lambda self, audio: f'heard {len(audio)} bytes')
    assert await voice.WhisperTranscriber().transcribe(b'1234', 'voice.ogg', 'audio/ogg') == 'heard 4 bytes'

    def broken(self, audio):
        raise RuntimeError('no model')
    monkeypatch.setattr(voice.WhisperTranscriber, '_transcribe', broken)
    with pytest.raises(voice.TranscriptionFailed, match=r'Local whisper failed \(RuntimeError\)'):
        await voice.WhisperTranscriber().transcribe(b'1234', 'voice.ogg', 'audio/ogg')


# --- which one runs ----------------------------------------------------------------------------------------

def test_auto_is_openai_with_its_key_then_whisper_then_off(home, monkeypatch):
    monkeypatch.setattr(voice, 'whisper_installed', lambda: False)
    config = load_config(home)  # the default config: auto
    off = voice.choose_transcriber(config)
    assert off.name == '' and 'local-agent key set openai' in off.why and 'local-agent[whisper]' in off.why
    assert voice.transcriber(config) is None

    monkeypatch.setattr(voice, 'whisper_installed', lambda: True)
    assert voice.choose_transcriber(config).why == 'whisper (installed, no OpenAI key), free, runs on this computer'
    assert isinstance(voice.transcriber(config), voice.WhisperTranscriber)

    set_key('openai', OPENAI_KEY)
    choice = voice.choose_transcriber(config)
    assert (choice.name, choice.why) == ('openai', 'openai (your OpenAI key is set), about $0.003 per minute on '
                                                   'your OpenAI account')
    assert isinstance(voice.transcriber(config), voice.OpenAITranscriber)


def test_a_chosen_provider_needs_its_key_or_install(home, monkeypatch):
    monkeypatch.setattr(voice, 'whisper_installed', lambda: False)
    set_key('openai', OPENAI_KEY)  # not used when another provider is chosen
    eleven = configured(home, 'elevenlabs')
    assert voice.choose_transcriber(eleven).why == 'off: elevenlabs needs a key (local-agent key set elevenlabs)'
    set_key('elevenlabs', ELEVEN_KEY)
    assert isinstance(voice.transcriber(eleven), voice.ElevenLabsTranscriber)
    assert 'about $0.007 per minute' in voice.choose_transcriber(eleven).why
    assert 'isn\'t installed' in voice.choose_transcriber(configured(home, 'whisper')).why
    off = configured(home, '')
    assert voice.transcriber(off) is None and voice.choose_transcriber(off).why.startswith('off in the settings')


def test_an_unknown_transcriber_is_a_config_error(home):
    home.config_path.write_text('[voice]\ntranscribe = "siri"\n')
    with pytest.raises(ConfigError, match=r'\[voice\] transcribe must be one of'):
        load_config(Home.locate())
