import os
import traceback
from dotenv import load_dotenv
load_dotenv()
from anthropic import Anthropic
try:
    client = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    client.messages.create(model='claude-3-haiku-20240307', max_tokens=10, messages=[{'role':'user', 'content':'hello'}])
    print('Success')
except Exception as e:
    print('Error:')
    traceback.print_exc()
