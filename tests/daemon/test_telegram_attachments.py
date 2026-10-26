"""
Attachments from the owner over Telegram, with a fake Bot API and a fake transcription API (httpx.MockTransport):
a photo reaches the run as image input (or is saved when the model can't see), a voice note's transcript is the
message, a PDF is saved to workspace/files/inbox/ and the run is told where. Strangers' attachments are never
downloaded, and downloads are capped.
"""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from local_agent import config
from local_agent.channels import telegram
from local_agent.core import gate as gate_module
from tests.daemon.test_telegram import BOT_TOKEN, OWNER, STRANGER, FakeTelegram, paired
from tests.scripted import ScriptedModel, say

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
TODAY = f'{NOW.astimezone():%Y-%m-%d}'
PDF = b'%PDF-1.4\n% a tiny test document\n%%EOF\n'
JPEG = b'\xff\xd8\xff\xe0 a small jpeg'


class AttachmentTelegram(FakeTelegram):
    """FakeTelegram plus files: getFile and the file download URL."""

    def __init__(self):
        super().__init__()
        self.files, self.downloads = {}, []

    def attach(self, chat_id, username='shahar', **fields):
        """A message with media fields (photo, voice, document...) and maybe a caption."""
        self.updates.append({'update_id': self.next_update, 'message': {
            'message_id': self.next_message, 'chat': {'id': chat_id, 'type': 'private', 'username': username},
            **fields}})
        self.next_update += 1
        self.next_message += 1

    def file(self, file_id, data, declared=None):
        self.files[file_id] = (data, len(data) if declared is None else declared)
        return {'file_id': file_id, 'file_unique_id': file_id, 'file_size': len(data) if declared is None
                else declared}

    def method_calls(self, method):
        return [params for name, params in self.calls if name == method]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f'/file/bot{BOT_TOKEN}/'):
            file_id = request.url.path.rsplit('/', 1)[1]
            self.downloads.append(file_id)
            return httpx.Response(200, content=self.files[file_id][0])
        if request.url.path.endswith('/getFile'):
            file_id = json.loads(request.content)['file_id']
            self.calls.append(('getFile', {'file_id': file_id}))
            return httpx.Response(200, json={'ok': True, 'result': {
                'file_id': file_id, 'file_size': self.files[file_id][1], 'file_path': f'documents/{file_id}'}})
        return super().handler(request)


@pytest.fixture
def fake(isolated):
    config.set_key('telegram', BOT_TOKEN)
    return AttachmentTelegram()


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


@pytest.fixture
def sees(monkeypatch):
    """sees(True/False): whether the configured model takes images."""
    def set_(value):
        monkeypatch.setattr(telegram.providers, 'can_see_images', lambda config: value)
    return set_


async def started(make_daemon, fake, model):
    daemon, _ = await make_daemon(model, telegram_transport=httpx.MockTransport(fake.handler), clock=lambda: NOW)
    await paired(daemon, fake)
    return daemon


async def deliver(daemon):
    await daemon.telegram.poll_once(timeout=0)
    await daemon.telegram.settle()


def first_user_message(model):
    return next(item for item in model.inputs[0] if isinstance(item, dict) and item.get('role') == 'user')


# --- photos ------------------------------------------------------------------------------------------------

async def test_a_photo_reaches_the_run_as_image_input_with_its_caption(make_daemon, fake, sees):
    sees(True)
    model = ScriptedModel([[say('A receipt from the bakery.')]])
    daemon = await started(make_daemon, fake, model)
    fake.attach(OWNER, caption='what is this?', photo=[
        {**fake.file('small', b'tiny'), 'width': 90, 'height': 90},
        {**fake.file('medium', JPEG), 'width': 800, 'height': 600},
        {**fake.file('huge', b'x', declared=6_000_000), 'width': 2560, 'height': 1920}])  # over the photo cap
    await deliver(daemon)

    assert fake.downloads == ['medium']  # the largest size under the cap, and only that one
    content = first_user_message(model)['content']
    assert content[0] == {'type': 'input_text', 'text': '[photo] what is this?'}
    assert content[1]['type'] == 'input_image'
    assert content[1]['image_url'] == 'data:image/jpeg;base64,' + base64.b64encode(JPEG).decode()
    assert fake.sent(OWNER)[-1]['text'] == 'A receipt from the bakery.'
    # the owner's own input: not tainted; the log and the conversation keep the text, not the image
    [run] = daemon.rt.store.recent_runs(1)
    start = daemon.rt.store.run_events(run.run_id)[0].payload
    assert start['message'] == '[photo] what is this?' and start['images'] == 1 and start['untrusted_inputs'] == []
    conversation = daemon.rt.store.find_conversation('telegram', str(OWNER))
    assert [m.content for m in daemon.rt.store.messages(conversation.id)][0] == '[photo] what is this?'


async def test_a_photo_without_a_caption_gets_a_short_text(make_daemon, fake, sees):
    sees(True)
    model = ScriptedModel([[say('A cat.')]])
    daemon = await started(make_daemon, fake, model)
    fake.attach(OWNER, photo=[{**fake.file('p', JPEG), 'width': 10, 'height': 10}])
    await deliver(daemon)
    assert first_user_message(model)['content'][0]['text'] == '[photo, no caption]'


async def test_a_model_that_cant_see_gets_no_run_and_the_photo_is_saved(make_daemon, fake, sees, home):
    sees(False)
    model = ScriptedModel([])  # any model call would fail
    daemon = await started(make_daemon, fake, model)
    fake.attach(OWNER, caption='what is this?', photo=[{**fake.file('p', JPEG), 'width': 10, 'height': 10}])
    await deliver(daemon)

    assert model.inputs == []
    reply = fake.sent(OWNER)[-1]['text']
    assert "can't see images" in reply and f'inbox/{TODAY}-photo.jpg' in reply
    assert (home.files_dir / 'inbox' / f'{TODAY}-photo.jpg').read_bytes() == JPEG


# --- voice notes -------------------------------------------------------------------------------------------

def transcription_api(requests, text='Remind me to call Dana at five.'):
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'text': text})
    return httpx.MockTransport(handle)


async def test_a_voice_note_is_transcribed_and_the_transcript_is_the_message(make_daemon, fake):
    config.set_key('openai', 'sk-test-voice-0123456789')  # auto: openai, on the owner's key
    model = ScriptedModel([[say('I will remind you.')]])
    daemon = await started(make_daemon, fake, model)
    requests = []
    daemon.rt.voice_transport = transcription_api(requests)
    fake.attach(OWNER, voice={**fake.file('v1', b'OggS fake opus'), 'duration': 3, 'mime_type': 'audio/ogg'})
    await deliver(daemon)

    [request] = requests
    assert str(request.url) == 'https://api.openai.com/v1/audio/transcriptions'
    assert b'gpt-4o-mini-transcribe' in request.content and b'OggS fake opus' in request.content
    assert first_user_message(model)['content'] == '[voice note, transcribed] Remind me to call Dana at five.'
    assert fake.sent(OWNER)[-1]['text'] == 'I will remind you.'


async def test_a_voice_note_without_a_transcriber_says_how_to_set_one(make_daemon, fake, monkeypatch):
    monkeypatch.setattr(telegram.voice, 'whisper_installed', lambda: False)
    model = ScriptedModel([])
    daemon = await started(make_daemon, fake, model)
    fake.attach(OWNER, voice=fake.file('v1', b'OggS'))
    await deliver(daemon)

    assert model.inputs == [] and fake.downloads == []  # nothing downloaded for nothing
    reply = fake.sent(OWNER)[-1]['text']
    assert "can't listen to voice notes" in reply and 'local-agent key set openai' in reply
    assert 'local-agent[whisper]' in reply


async def test_a_failed_or_empty_transcription_is_said_plainly(make_daemon, fake):
    config.set_key('openai', 'sk-test-voice-0123456789')
    daemon = await started(make_daemon, fake, ScriptedModel([]))
    daemon.rt.voice_transport = httpx.MockTransport(lambda request: httpx.Response(429))
    fake.attach(OWNER, voice=fake.file('v1', b'OggS'))
    await deliver(daemon)
    assert fake.sent(OWNER)[-1]['text'] == ("I couldn't transcribe that voice note: OpenAI is rate limited or out of "
                                            "credit right now (HTTP 429).")
    daemon.rt.voice_transport = transcription_api([], text='  ')
    fake.attach(OWNER, voice=fake.file('v2', b'OggS'))
    await deliver(daemon)
    assert fake.sent(OWNER)[-1]['text'] == "I couldn't hear any words in that voice note."


# --- files -------------------------------------------------------------------------------------------------

async def test_a_pdf_is_saved_to_the_inbox_and_the_run_is_told_where(make_daemon, fake, home):
    model = ScriptedModel([[say('Got it.')], [say('Got the second one.')]])
    daemon = await started(make_daemon, fake, model)
    document = {**fake.file('d1', PDF), 'file_name': 'Q3 report.pdf', 'mime_type': 'application/pdf'}
    fake.attach(OWNER, caption='summarize this', document=document)
    await deliver(daemon)
    fake.attach(OWNER, document=document)  # the same name again: saved next to it, never over it
    await deliver(daemon)

    inbox = home.files_dir / 'inbox'
    assert (inbox / f'{TODAY}-Q3 report.pdf').read_bytes() == PDF
    assert (inbox / f'{TODAY}-Q3 report-2.pdf').read_bytes() == PDF
    assert first_user_message(model)['content'] == (
        f'summarize this\n[The user sent a file, saved in the workspace files as inbox/{TODAY}-Q3 report.pdf. '
        'Read it with files_read if needed.]')
    second = [item for item in model.inputs[1] if isinstance(item, dict) and item.get('role') == 'user'][-1]
    assert f'inbox/{TODAY}-Q3 report-2.pdf' in second['content']


async def test_a_hostile_file_name_stays_in_the_inbox(make_daemon, fake, home):
    daemon = await started(make_daemon, fake, ScriptedModel([[say('ok')], [say('ok')]]))
    fake.attach(OWNER, document={**fake.file('d1', b'#!/bin/sh'), 'file_name': '../../../.ssh/authorized_keys'})
    fake.attach(OWNER, audio=fake.file('a1', b'ID3 audio'))  # no name: a default one
    await deliver(daemon)
    saved = sorted(p.name for p in (home.files_dir / 'inbox').iterdir())
    assert saved == [f'{TODAY}-audio', f'{TODAY}-authorized_keys']
    assert not (home.root / '.ssh').exists() and not (home.files_dir.parent / '.ssh').exists()


async def test_files_over_the_cap_are_refused_before_and_during_the_download(make_daemon, fake, monkeypatch, home):
    model = ScriptedModel([])
    daemon = await started(make_daemon, fake, model)
    fake.attach(OWNER, document={**fake.file('big', b'x', declared=25_000_000), 'file_name': 'movie.mkv'})
    await deliver(daemon)
    assert fake.method_calls('getFile') == []  # refused from the size Telegram declared
    assert 'too large' in fake.sent(OWNER)[-1]['text'] and '20 MB' in fake.sent(OWNER)[-1]['text']

    monkeypatch.setattr(telegram, 'MAX_FILE_BYTES', 100)
    fake.files['liar'] = (b'x' * 1000, 10)  # declares 10 bytes, sends 1000
    fake.attach(OWNER, document={'file_id': 'liar', 'file_size': 10, 'file_name': 'small.txt'})
    await deliver(daemon)
    assert 'too large' in fake.sent(OWNER)[-1]['text'] and model.inputs == []
    assert not (home.files_dir / 'inbox').exists() or list((home.files_dir / 'inbox').iterdir()) == []


async def test_a_failed_download_never_shows_the_token(make_daemon, fake):
    daemon = await started(make_daemon, fake, ScriptedModel([]))
    fake.files['gone'] = (b'', 1)
    original = fake.handler

    def broken(request):
        if request.url.path.startswith('/file/'):
            return httpx.Response(404)
        return original(request)
    daemon.telegram.bot._client._transport = httpx.MockTransport(broken)
    fake.attach(OWNER, document={'file_id': 'gone', 'file_size': 1, 'file_name': 'x.pdf'})
    await deliver(daemon)
    assert fake.sent(OWNER)[-1]['text'] == "I couldn't download that from Telegram. Please send it again."
    assert BOT_TOKEN not in json.dumps(fake.calls)


# --- strangers ---------------------------------------------------------------------------------------------

async def test_a_strangers_attachments_are_never_downloaded(make_daemon, fake, sees):
    sees(True)
    model = ScriptedModel([])
    daemon = await started(make_daemon, fake, model)
    fake.attach(STRANGER, username='eve', photo=[{**fake.file('p', JPEG), 'width': 10, 'height': 10}])
    fake.attach(STRANGER, username='eve', voice=fake.file('v', b'OggS'))
    fake.attach(STRANGER, username='eve', document={**fake.file('d', PDF), 'file_name': 'invoice.pdf'})
    await deliver(daemon)
    assert fake.method_calls('getFile') == [] and fake.downloads == [] and fake.sent(STRANGER) == []
    assert model.inputs == [] and not (daemon.rt.home.files_dir / 'inbox').exists()
