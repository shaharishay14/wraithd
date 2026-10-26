"""
Voice notes to text, through a small provider interface like web search's (tools/web.py):

- openai: gpt-4o-mini-transcribe through the OpenAI audio API, on the owner's OpenAI key.
- whisper: faster-whisper on this computer (the `local-agent[whisper]` extra), the model downloaded on first use.
- elevenlabs: ElevenLabs speech-to-text (Scribe), on its own key (`local-agent key set elevenlabs`).

`[voice] transcribe = "auto"` (the default) is openai if its key is set, else whisper if it's installed, else off.
Errors never include the request (the key is in its headers).
"""

import asyncio
import importlib.util
from dataclasses import dataclass
from typing import Protocol

import httpx

from local_agent.config import get_key

TIMEOUT_SECONDS = 120
WHISPER_MODEL = 'base'  # about 150 MB, fine on a laptop CPU; downloaded into the Hugging Face cache on first use
COST = {'openai': 'about $0.003 per minute on your OpenAI account',
        'whisper': 'free, runs on this computer',
        'elevenlabs': 'about $0.007 per minute on your ElevenLabs plan'}
INSTALL_WHISPER = 'uv tool install --reinstall "local-agent[whisper]"'
HOW_TO_TURN_ON = (f'set an OpenAI key (local-agent key set openai), install local whisper ({INSTALL_WHISPER}), or '
                  'set [voice] transcribe = "elevenlabs" and run local-agent key set elevenlabs')


class TranscriptionFailed(Exception):
    """Transcribing failed. The message is safe to show the owner (no key, no URL with one)."""


class Transcriber(Protocol):
    name: str

    async def transcribe(self, audio: bytes, filename: str, mime_type: str) -> str: ...


def _status_failed(name: str, status: int) -> TranscriptionFailed:
    if status in (401, 403):
        return TranscriptionFailed(f'{name} rejected the key (HTTP {status}).')
    if status == 429:
        return TranscriptionFailed(f'{name} is rate limited or out of credit right now (HTTP 429).')
    return TranscriptionFailed(f'{name} transcription failed (HTTP {status}).')


async def _post_audio(name: str, url: str, headers: dict, fields: dict, audio: bytes, filename: str,
                      mime_type: str, transport) -> str:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, data=fields,
                                         files={'file': (filename, audio, mime_type)})
            response.raise_for_status()
            text = response.json().get('text')
    except httpx.HTTPStatusError as e:
        raise _status_failed(name, e.response.status_code) from None
    except (httpx.HTTPError, ValueError, AttributeError):
        raise TranscriptionFailed(f'{name} transcription did not respond.') from None
    if not isinstance(text, str):
        raise TranscriptionFailed(f'{name} sent back no transcript.')
    return text.strip()


class OpenAITranscriber:
    name = 'openai'
    URL = 'https://api.openai.com/v1/audio/transcriptions'
    MODEL = 'gpt-4o-mini-transcribe'

    def __init__(self, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.key, self.transport = key, transport

    async def transcribe(self, audio: bytes, filename: str, mime_type: str) -> str:
        return await _post_audio('OpenAI', self.URL, {'Authorization': f'Bearer {self.key}'},
                                 {'model': self.MODEL, 'response_format': 'json'}, audio, filename, mime_type,
                                 self.transport)


class ElevenLabsTranscriber:
    name = 'elevenlabs'
    URL = 'https://api.elevenlabs.io/v1/speech-to-text'
    MODEL = 'scribe_v1'

    def __init__(self, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.key, self.transport = key, transport

    async def transcribe(self, audio: bytes, filename: str, mime_type: str) -> str:
        return await _post_audio('ElevenLabs', self.URL, {'xi-api-key': self.key}, {'model_id': self.MODEL}, audio,
                                 filename, mime_type, self.transport)


def whisper_installed() -> bool:
    return importlib.util.find_spec('faster_whisper') is not None


class WhisperTranscriber:
    """faster-whisper on the CPU. The model loads once per process, on the first voice note."""

    name = 'whisper'
    _model = None

    def _transcribe(self, audio: bytes) -> str:
        import io

        from faster_whisper import WhisperModel

        if WhisperTranscriber._model is None:
            WhisperTranscriber._model = WhisperModel(WHISPER_MODEL, device='cpu', compute_type='int8')
        segments, _ = WhisperTranscriber._model.transcribe(io.BytesIO(audio))
        return ' '.join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, audio: bytes, filename: str, mime_type: str) -> str:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._transcribe, audio), TIMEOUT_SECONDS)
        except TimeoutError:
            raise TranscriptionFailed('Local whisper took too long.') from None
        except Exception as e:  # a missing model download, audio it can't decode
            raise TranscriptionFailed(f'Local whisper failed ({type(e).__name__}).') from None


@dataclass(frozen=True)
class TranscriberChoice:
    name: str  # '' when voice notes are off
    why: str  # one line for doctor and for the owner
    key: str | None = None


def choose_transcriber(config) -> TranscriberChoice:
    setting = config.transcribe
    if not setting:
        return TranscriberChoice('', 'off in the settings ([voice] transcribe = "")')
    if setting == 'auto':
        if key := get_key('openai'):
            return TranscriberChoice('openai', f'openai (your OpenAI key is set), {COST["openai"]}', key)
        if whisper_installed():
            return TranscriberChoice('whisper', f'whisper (installed, no OpenAI key), {COST["whisper"]}')
        return TranscriberChoice('', f'off: no OpenAI key and local whisper isn\'t installed; to turn it on, '
                                     f'{HOW_TO_TURN_ON}')
    if setting == 'whisper':
        if whisper_installed():
            return TranscriberChoice('whisper', f'whisper, {COST["whisper"]}')
        return TranscriberChoice('', f'off: local whisper isn\'t installed ({INSTALL_WHISPER})')
    key = get_key(setting)
    if not key:
        return TranscriberChoice('', f'off: {setting} needs a key (local-agent key set {setting})')
    return TranscriberChoice(setting, f'{setting}, {COST[setting]}', key)


def transcriber(config, transport=None) -> Transcriber | None:
    """The transcriber voice notes get, or None when it's off."""
    choice = choose_transcriber(config)
    if choice.name == 'openai':
        return OpenAITranscriber(choice.key, transport)
    if choice.name == 'elevenlabs':
        return ElevenLabsTranscriber(choice.key, transport)
    return WhisperTranscriber() if choice.name == 'whisper' else None
