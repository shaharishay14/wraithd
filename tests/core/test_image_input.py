"""
Image input for a run (a Telegram photo): the message goes to the model as text plus input_image, which the SDK
turns into each provider's own image part (Chat Completions and LiteLLM too), and whether the model can see.
"""

from agents.models.chatcmpl_converter import Converter

from local_agent.config import Config, ModelConfig
from local_agent.providers import can_see_images
from local_agent.run import user_content

IMAGE = 'data:image/jpeg;base64,/9j/AAAA'


def test_a_message_with_images_becomes_text_and_image_parts():
    assert user_content('hello') == 'hello'
    content = user_content('[photo] what is this?', [IMAGE])
    assert content == [{'type': 'input_text', 'text': '[photo] what is this?'},
                       {'type': 'input_image', 'image_url': IMAGE, 'detail': 'auto'}]
    # what OpenAIChatCompletionsModel and LitellmModel (Anthropic) send
    [message] = Converter.items_to_messages([{'role': 'user', 'content': content}])
    assert message['content'][1] == {'type': 'image_url', 'image_url': {'url': IMAGE, 'detail': 'auto'}}


def test_which_models_can_see():
    def sees(provider, name):
        return can_see_images(Config(model=ModelConfig(provider=provider, default=name)))
    assert sees('openai', 'gpt-4o-mini') and sees('anthropic', 'claude-sonnet-4-5')
    assert not sees('openai', 'gpt-3.5-turbo')  # LiteLLM's list says no
    assert sees('anthropic', 'claude-from-the-future-9')  # unknown to the list: the cloud providers' models see
    assert not sees('ollama', 'llama3') and not sees('openai-compatible', 'my-model')
